"""``movate auth`` — manage tenant API keys.

Three subcommands:

* ``movate auth create-key`` — mint a key for a tenant; prints the
  full key once to stdout (pipe into a vault) with a "save this now"
  warning to stderr.
* ``movate auth list-keys`` — show keys for a tenant; defaults to
  active only, ``--include-revoked`` for the full history.
* ``movate auth revoke-key`` — flip ``revoked_at`` on a key; idempotent.

Local-only in v0.5 stage 2 — talks straight to the configured
``StorageProvider``. The HTTP runtime (stage 3) consumes the same
storage methods through middleware.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import confirm_destructive, error, hint, success
from movate.core.auth import mint_api_key
from movate.core.models import ApiKeyEnv, ApiKeyRecord
from movate.storage import build_storage

stdout = Console()
err = Console(stderr=True)

auth_app = typer.Typer(
    name="auth",
    help="Manage API keys for the movate runtime.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@auth_app.command("create-key")
def create_key(
    tenant_id: str = typer.Option(..., "--tenant-id", help="Tenant id (≥8 chars)."),
    env: str = typer.Option("live", "--env", help="`live` or `test`."),
    label: str = typer.Option(None, "--label", help="Optional human-readable note."),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Print only the key_id (for scripting). Disables warnings.",
    ),
) -> None:
    """Mint a new API key.

    Full key is printed to **stdout once** — it is irrecoverable after
    that. The CLI prints a "save this now" warning to **stderr** so
    the warning doesn't pollute scripted ``> /vault.txt`` redirection.

    [bold]Examples:[/bold]

      [dim]# Interactive use; copy the key into your password manager.[/dim]
      $ movate auth create-key --tenant-id <uuid> --env live --label ci-bot

      [dim]# Scripting: the bare key on stdout, warnings on stderr.[/dim]
      $ KEY=$(movate auth create-key --tenant-id <uuid> --env live --quiet)
    """
    try:
        env_enum = ApiKeyEnv(env)
    except ValueError as exc:
        error(f"env must be 'live' or 'test'; got {env!r}")
        raise typer.Exit(code=2) from exc

    try:
        minted = mint_api_key(tenant_id=tenant_id, env=env_enum, label=label)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc

    asyncio.run(_persist(minted.record))

    if quiet:
        # Scripting mode — bare key_id on stdout for easy capture into a var.
        # The full key still prints to stderr so an interactive operator
        # piping `$(... --quiet)` doesn't lose the secret.
        stdout.print(minted.record.key_id, soft_wrap=True, highlight=False)
        err.print(f"[yellow]secret:[/yellow] {minted.full_key}")
        err.print("[yellow]save this now — never shown again[/yellow]")
    else:
        # Interactive mode — full key on stdout, warning on stderr.
        stdout.print(minted.full_key, soft_wrap=True, highlight=False)
        err.print()  # blank line for readability
        err.print(
            f"[bold yellow]save this now — never shown again[/bold yellow]\n"
            f"  key_id:    {minted.record.key_id}\n"
            f"  tenant_id: {minted.record.tenant_id}\n"
            f"  env:       {minted.record.env.value}" + (f"\n  label:     {label}" if label else "")
        )


@auth_app.command("list-keys")
def list_keys(
    tenant_id: str = typer.Option(
        None,
        "--tenant-id",
        help="Filter to one tenant. Omit for all-tenants (operator-only).",
    ),
    include_revoked: bool = typer.Option(False, "--include-revoked", help="Show revoked keys too."),
) -> None:
    """List API keys, newest first."""
    keys = asyncio.run(_load_keys(tenant_id=tenant_id, include_revoked=include_revoked))

    if not keys:
        hint("[dim]no keys found[/dim]")
        return

    table = Table(title=f"api keys{f' for tenant {tenant_id[:8]}…' if tenant_id else ''}")
    table.add_column("key_id", style="bold")
    table.add_column("tenant_id", style="dim")
    table.add_column("env")
    table.add_column("label")
    table.add_column("created")
    table.add_column("last_used")
    table.add_column("status")

    for k in keys:
        status = "[red]revoked[/red]" if k.revoked_at else "[green]active[/green]"
        table.add_row(
            k.key_id,
            k.tenant_id,
            k.env.value,
            k.label or "",
            k.created_at.date().isoformat(),
            k.last_used_at.date().isoformat() if k.last_used_at else "—",
            status,
        )
    stdout.print(table)


@auth_app.command("revoke-key")
def revoke_key(
    key_id: str = typer.Argument(..., help="Key id to revoke."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirm prompt (use in scripts / CI).",
    ),
) -> None:
    """Revoke an API key. Idempotent — re-revoking is a silent no-op.

    Prompts ``Revoke key <id>? Y/N`` before doing anything; pass
    ``-y`` to bypass for scripts. In a non-TTY context without
    ``-y`` we abort rather than block — so CI pipelines fail loud
    when they forget the flag instead of hanging."""
    confirm_destructive(
        f"Revoke API key {key_id}? This cannot be undone.",
        yes=yes,
    )
    asyncio.run(_revoke(key_id))
    success(f"revoked {key_id}")


# ---------------------------------------------------------------------------
# Async glue — these live as helpers so the Typer commands stay
# synchronous (typer's idiomatic shape). They go straight at storage
# (no tracer, no executor) since auth-table edits are pure state moves.
# ---------------------------------------------------------------------------


async def _persist(record: ApiKeyRecord) -> None:
    storage = build_storage()
    await storage.init()
    try:
        await storage.save_api_key(record)
    finally:
        await storage.close()


async def _load_keys(*, tenant_id: str | None, include_revoked: bool) -> list[ApiKeyRecord]:
    storage = build_storage()
    await storage.init()
    try:
        return await storage.list_api_keys(tenant_id=tenant_id, include_revoked=include_revoked)
    finally:
        await storage.close()


async def _revoke(key_id: str) -> None:
    storage = build_storage()
    await storage.init()
    try:
        # `revoke_api_key` is tenant-scoped at the storage layer (v1.0
        # stage 4). The CLI is operator-only, so we look up the key
        # first to derive its tenant_id, then revoke. This keeps the
        # operator UX (just paste the key_id) while preserving the
        # SQL-layer filter that blocks an HTTP-level cross-tenant
        # revoke attack.
        record = await storage.get_api_key(key_id)
        if record is None:
            return  # idempotent — silent no-op on missing
        await storage.revoke_api_key(key_id, tenant_id=record.tenant_id)
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Provider-key credentials (PR B)
#
# `mdk auth login <provider>` and `mdk auth status` manage the
# machine-global ~/.movate/credentials file — distinct from the tenant
# API keys above (which authenticate clients against the movate runtime).
# Same `auth` command surface because both flows are about credentials.
# ---------------------------------------------------------------------------


_PROVIDERS_PROMPT_NAME = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "azure": "Azure OpenAI",
    "gemini": "Gemini",
    "lyzr": "Lyzr Studio",
}

_PROVIDER_TO_ENV_VAR = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "lyzr": "LYZR_API_KEY",
}


@auth_app.command("login")
def login(
    provider: str = typer.Argument(
        ...,
        help=(
            "Provider to set the API key for: "
            "[bold]openai[/bold], [bold]anthropic[/bold], "
            "[bold]azure[/bold], [bold]gemini[/bold], or [bold]lyzr[/bold]."
        ),
    ),
    key: str = typer.Option(
        None,
        "--key",
        help=(
            "Pass the key non-interactively (CI scripts). Without "
            "[bold]--key[/bold], the CLI prompts interactively with "
            "hidden input."
        ),
    ),
    no_verify: bool = typer.Option(
        False,
        "--no-verify",
        help=(
            "Skip the live verification call. Useful when the provider "
            "is unreachable from the operator's network at setup time."
        ),
    ),
    save_to: str = typer.Option(
        "global",
        "--save-to",
        help=(
            "Where to write the key: [bold]global[/bold] "
            "([bold]~/.movate/credentials[/bold], machine-global) or "
            "[bold]project[/bold] ([bold]./.env[/bold], project-only)."
        ),
    ),
) -> None:
    """Set a provider API key, verify it, persist it for every project.

    [bold]Examples:[/bold]

      [dim]# Interactive — prompts for the key, hides input[/dim]
      $ mdk auth login openai

      [dim]# Save to project .env instead of machine-global[/dim]
      $ mdk auth login anthropic --save-to project

      [dim]# CI-style: pass key non-interactively, skip verify[/dim]
      $ mdk auth login openai --key "$OPENAI_API_KEY" --no-verify
    """
    from movate.credentials import (  # noqa: PLC0415
        CredentialsStore,
        verify_provider_key,
    )

    provider = provider.lower().strip()
    if provider not in _PROVIDER_TO_ENV_VAR:
        valid = ", ".join(sorted(_PROVIDER_TO_ENV_VAR))
        error(f"unknown provider {provider!r}. Valid: {valid}")
        raise typer.Exit(code=2)

    env_var = _PROVIDER_TO_ENV_VAR[provider]
    friendly_name = _PROVIDERS_PROMPT_NAME[provider]

    # Resolve the key — flag, env var, or interactive prompt.
    if key is None:
        key = typer.prompt(
            f"{friendly_name} API key",
            hide_input=True,
            confirmation_prompt=False,
        )
    key = key.strip()
    if not key:
        error("empty key — aborted.")
        raise typer.Exit(code=2)

    # Verify (unless opted out).
    if not no_verify:
        with stdout.status(f"verifying {friendly_name} key..."):
            result = verify_provider_key(provider, key)
        if result.ok:
            success(result.detail)
        elif result.network_error:
            err.print(
                f"[yellow]⚠[/yellow] verify call failed (network): "
                f"{result.detail}. Saving key anyway — offline scenarios "
                "can verify later."
            )
        else:
            error(f"verification failed: {result.detail}")
            raise typer.Exit(code=2)

    # Persist.
    save_to = save_to.lower().strip()
    if save_to == "global":
        store = CredentialsStore()
        store.set(env_var, key)
        success(
            f"saved [bold]{env_var}[/bold] to "
            f"[cyan]{store.path}[/cyan] (mode 0600)."
        )
        hint(
            "[dim]Every `mdk` invocation on this machine now picks up "
            "this key automatically. Run [bold]mdk auth status[/bold] "
            "to confirm.[/dim]"
        )
    elif save_to == "project":
        # Append to project .env. We don't try to in-place edit; the
        # operator's editor handles dedup later if they want.
        from pathlib import Path  # noqa: PLC0415

        dotenv = Path(".env")
        if dotenv.is_file() and env_var in dotenv.read_text():
            error(
                f"{env_var} is already in {dotenv.resolve()}. "
                "Edit the file or use --save-to global instead."
            )
            raise typer.Exit(code=2)
        with dotenv.open("a") as fh:
            fh.write(f"{env_var}={key}\n")
        success(f"appended to [cyan]{dotenv.resolve()}[/cyan].")
    else:
        error(f"--save-to must be 'global' or 'project'; got {save_to!r}")
        raise typer.Exit(code=2)


@auth_app.command("status")
def status() -> None:
    """Show which provider keys are configured and where they came from.

    Renders a table per provider showing the resolved value's source
    (shell / dotenv / credentials_file / unset). Operators see at a
    glance which keys are wired and where to fix gaps.
    """
    from movate.credentials import (  # noqa: PLC0415
        PROVIDER_KEY_ENV_VARS,
        CredentialsStore,
        key_source,
    )

    table = Table(
        title="movate auth status",
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Env var", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Source", style="dim")
    table.add_column("Hint", style="dim")

    counts = {"ok": 0, "unset": 0}
    for env_var in PROVIDER_KEY_ENV_VARS:
        src = key_source(env_var)
        if src == "unset":
            counts["unset"] += 1
            provider = env_var.lower().removesuffix("_api_key").split("_")[0]
            table.add_row(
                env_var,
                "[yellow]⊘ not set[/yellow]",
                "—",
                f"run [bold]mdk auth login {provider}[/bold]",
            )
        else:
            counts["ok"] += 1
            table.add_row(
                env_var,
                "[green]✓ set[/green]",
                src.replace("_", " "),
                "",
            )

    stdout.print(table)
    stdout.print()
    stdout.print(
        f"[dim]credentials file: [cyan]{CredentialsStore().path}[/cyan][/dim]"
    )
    stdout.print(
        f"[dim]mdk_auth_status_summary: "
        f"set={counts['ok']} unset={counts['unset']}[/dim]"
    )
