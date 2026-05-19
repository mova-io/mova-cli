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
import contextlib
import os
from typing import Literal

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
        help="Filter to one tenant. Omit for all. Ignored when --target is used.",
    ),
    include_revoked: bool = typer.Option(False, "--include-revoked", help="Show revoked keys too."),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Query a deployed runtime via HTTP instead of local storage. "
            "Reads the bearer from the target's key_env. "
            "Returns keys belonging to the calling tenant's identity."
        ),
    ),
) -> None:
    """List API keys, newest first.

    Without [bold]--target[/bold], reads local storage (operator tool).
    With [bold]--target[/bold], calls [bold]GET /api/v1/auth/keys[/bold]
    on the deployed runtime and shows keys for the calling tenant.

    [bold]Examples:[/bold]

      [dim]$ mdk auth list-keys                     # local storage[/dim]
      [dim]$ mdk auth list-keys --target dev        # deployed runtime[/dim]
      [dim]$ mdk auth list-keys --target dev --include-revoked[/dim]
    """
    if target is not None:
        _list_keys_remote(target=target, include_revoked=include_revoked)
        return

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


def _list_keys_remote(*, target: str, include_revoked: bool) -> None:
    """Call GET /api/v1/auth/keys on a deployed runtime and render the result."""
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from movate.config import resolve_target  # noqa: PLC0415
    from movate.core.user_config import UserConfigError  # noqa: PLC0415

    try:
        _, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    api_key = os.environ.get(target_cfg.key_env, "").strip()
    base_url = target_cfg.url.rstrip("/")
    if not api_key:
        error(f"env var ${target_cfg.key_env} is empty. Run mdk auth refresh-runtime-key {target}.")
        raise typer.Exit(code=2)

    params: dict[str, str] = {}
    if include_revoked:
        params["include_revoked"] = "true"

    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(
                f"{base_url}/api/v1/auth/keys",
                headers={"Authorization": f"Bearer {api_key}"},
                params=params,
            )
    except httpx.HTTPError as exc:
        error(f"could not reach {base_url}: {exc}")
        raise typer.Exit(code=2) from None

    if resp.status_code == httpx.codes.UNAUTHORIZED:
        error("401 Unauthorized — key is invalid or expired.")
        raise typer.Exit(code=2)
    if resp.status_code != httpx.codes.OK:
        error(f"HTTP {resp.status_code}: {resp.text[:200]!r}")
        raise typer.Exit(code=2)

    data = resp.json()
    keys = data.get("keys", [])

    if not keys:
        hint("[dim]no keys found[/dim]")
        return

    table = Table(title=f"api keys on {target}")
    table.add_column("key_id", style="bold")
    table.add_column("tenant_id", style="dim")
    table.add_column("env")
    table.add_column("label")
    table.add_column("created")
    table.add_column("last_used")
    table.add_column("expires")
    table.add_column("status")

    _status_style = {
        "active": "[green]active[/green]",
        "revoked": "[red]revoked[/red]",
        "expired": "[yellow]expired[/yellow]",
    }
    for k in keys:
        raw_status = k.get("status", "active")
        status_cell = _status_style.get(raw_status, raw_status)
        created = (k.get("created_at") or "")[:10]
        last_used = (k.get("last_used_at") or "")[:10] or "—"
        expires = (k.get("expires_at") or "")[:10] or "—"
        table.add_row(
            k.get("key_id", "?"),
            k.get("tenant_id", "?"),
            k.get("env", "?"),
            k.get("label") or "",
            created,
            last_used,
            expires,
            status_cell,
        )
    stdout.print(table)
    stdout.print(f"[dim]{data.get('count', len(keys))} key(s)[/dim]")


@auth_app.command("whoami")
def whoami(
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target to query. Reads the bearer from the target's key_env. "
            "Omit to use the [bold]MDK_API_KEY[/bold] env var and [bold]MDK_RUNTIME_URL[/bold]."
        ),
    ),
) -> None:
    """Show the identity of the current API key on a deployed runtime.

    Calls [bold]GET /api/v1/auth/me[/bold] with the bearer token from
    the configured target (or MDK_API_KEY) and prints key_id, tenant,
    env, label, and expiry.

    [bold]Examples:[/bold]

      [dim]$ mdk auth whoami --target dev[/dim]
      [dim]$ MDK_API_KEY=mvt_live_... mdk auth whoami[/dim]
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    if target is not None:
        from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

        try:
            _, target_cfg = resolve_target(target)
        except UserConfigError as exc:
            error(str(exc))
            raise typer.Exit(code=2) from None
        api_key = os.environ.get(target_cfg.key_env, "").strip()
        base_url = target_cfg.url.rstrip("/")
        if not api_key:
            error(
                f"env var ${target_cfg.key_env} is empty. "
                f"Run mdk auth refresh-runtime-key {target}."
            )
            raise typer.Exit(code=2)
    else:
        api_key = os.environ.get("MDK_API_KEY", os.environ.get("MOVATE_API_KEY", "")).strip()
        base_url = os.environ.get("MDK_RUNTIME_URL", "").rstrip("/")
        if not api_key:
            error("no API key found. Pass --target or set MDK_API_KEY.")
            raise typer.Exit(code=2)
        if not base_url:
            error("no runtime URL found. Pass --target or set MDK_RUNTIME_URL.")
            raise typer.Exit(code=2)

    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(
                f"{base_url}/api/v1/auth/me",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        error(f"could not reach {base_url}: {exc}")
        raise typer.Exit(code=2) from None

    if resp.status_code == httpx.codes.UNAUTHORIZED:
        error("401 Unauthorized — key is invalid or expired.")
        raise typer.Exit(code=2)
    if resp.status_code != httpx.codes.OK:
        error(f"HTTP {resp.status_code}: {resp.text[:200]!r}")
        raise typer.Exit(code=2)

    data = resp.json()
    stdout.print(f"[bold]key_id:[/bold]    {data.get('key_id', '?')}")
    stdout.print(f"[bold]tenant_id:[/bold] {data.get('tenant_id', '?')}")
    stdout.print(f"[bold]env:[/bold]       {data.get('env', '?')}")
    if data.get("label"):
        stdout.print(f"[bold]label:[/bold]     {data['label']}")
    if data.get("scope"):
        stdout.print(f"[bold]scope:[/bold]     {data['scope']}")
    expires = data.get("expires_at")
    stdout.print(f"[bold]expires_at:[/bold] {expires if expires else 'never'}")


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


@auth_app.command("rotate-key")
def rotate_key(
    key_id: str = typer.Argument(..., help="Key id of the key to rotate."),
    ttl_days: int = typer.Option(
        90,
        "--ttl-days",
        help="Validity of the new key in days. 0 = no expiry.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirm prompt.",
    ),
) -> None:
    """Rotate an API key — mint a fresh one, revoke the old one.

    Prints the new key to stdout once (pipe into your vault).
    The old key is revoked immediately after the new one is saved, so
    there is a brief window where both are valid. Use ``--yes`` in
    automated rotation scripts.

    [bold]Example:[/bold]

      [dim]$ NEW=$(mdk auth rotate-key <key_id> --yes)[/dim]
    """
    confirm_destructive(
        f"Rotate API key {key_id}? The old key will be revoked immediately.",
        yes=yes,
    )

    async def _rotate(old_key_id: str) -> str:
        storage = build_storage()
        await storage.init()
        try:
            old_record = await storage.get_api_key(old_key_id)
            if old_record is None:
                error(f"key {old_key_id!r} not found")
                raise typer.Exit(code=2)
            if old_record.revoked_at is not None:
                error(f"key {old_key_id!r} is already revoked")
                raise typer.Exit(code=2)
            minted = mint_api_key(
                tenant_id=old_record.tenant_id,
                env=old_record.env,
                label=old_record.label,
                ttl_days=ttl_days,
            )
            await storage.save_api_key(minted.record)
            await storage.revoke_api_key(old_key_id, tenant_id=old_record.tenant_id)
            return minted.full_key
        finally:
            await storage.close()

    new_key = asyncio.run(_rotate(key_id))
    stdout.print(new_key, soft_wrap=True, highlight=False)
    err.print("[yellow]save this now — never shown again[/yellow]")
    err.print(f"[dim]old key {key_id} revoked[/dim]")
    success("rotated → new key minted")


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
    # Telegram is the deploy-notification channel, not an LLM provider.
    # Same UX surface as the LLM providers because operators expect
    # "set up the integration once" to look the same regardless.
    "telegram": "Telegram bot (deploy notifications)",
}

_PROVIDER_TO_ENV_VAR = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "lyzr": "LYZR_API_KEY",
}

# Telegram needs TWO values (bot token + chat ID), not one — handled
# via a dedicated code path in `login()` rather than the single-key
# provider table above. Same auth-login UX surface either way.
_TELEGRAM_PROVIDERS = frozenset({"telegram"})


@auth_app.command("login")
def login(  # noqa: PLR0912 — branch count inherent to the multi-mode flow
    provider: str = typer.Argument(
        None,
        help=(
            "Provider to set the API key for: "
            "[bold]openai[/bold], [bold]anthropic[/bold], "
            "[bold]azure[/bold], [bold]gemini[/bold], [bold]lyzr[/bold], "
            "or [bold]telegram[/bold]. Omit to pick interactively."
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

    # When no provider is passed, render an interactive picker. Mirrors
    # the `mdk menu` UX shape: numbered options, type the digit, hit
    # enter. Operators who don't know which providers MDK supports get
    # a discoverable list instead of an error.
    if provider is None:
        provider = _prompt_for_provider()

    provider = provider.lower().strip()

    # Telegram is a separate flow — needs token + chat_id, not a single
    # key. Dispatched here so the single-key code path below stays clean.
    if provider in _TELEGRAM_PROVIDERS:
        _login_telegram(key=key, no_verify=no_verify, save_to=save_to)
        return

    if provider not in _PROVIDER_TO_ENV_VAR:
        valid = ", ".join(sorted(set(_PROVIDER_TO_ENV_VAR) | _TELEGRAM_PROVIDERS))
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
        success(f"saved [bold]{env_var}[/bold] to [cyan]{store.path}[/cyan] (mode 0600).")
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

    counts = {"ok": 0, "unset": 0, "rejected": 0}
    # Live-verify every set LLM provider in parallel so the table
    # below distinguishes ``✓ verified`` from ``✗ set but rejected``
    # (key persisted but provider returns 401). Bounded ~5s.
    _verify_all_configured_providers_in_parallel()
    for env_var in PROVIDER_KEY_ENV_VARS:
        src = key_source(env_var)
        provider = env_var.lower().removesuffix("_api_key").split("_")[0]
        if src == "unset":
            counts["unset"] += 1
            table.add_row(
                env_var,
                "[yellow]⊘ not set[/yellow]",
                "—",
                f"run [bold]mdk auth login {provider}[/bold]",
            )
        else:
            # Translate the verify state into a per-row marker + hint.
            # ``rejected`` is counted separately from ``ok`` so the
            # summary line at the bottom tells the operator they have
            # an actionable problem (set-but-broken) distinct from a
            # missing-key problem.
            state = _provider_status(provider)
            if state == "verified":
                counts["ok"] += 1
                table.add_row(
                    env_var,
                    "[green]✓ verified[/green]",
                    src.replace("_", " "),
                    "",
                )
            elif state == "rejected":
                counts["rejected"] += 1
                table.add_row(
                    env_var,
                    "[red]✗ set but rejected[/red]",
                    src.replace("_", " "),
                    f"key set but provider 401'd — "
                    f"run [bold]mdk auth login {provider}[/bold] to rotate",
                )
            else:  # unverifiable
                counts["ok"] += 1
                table.add_row(
                    env_var,
                    "[yellow]⚠ set, couldn't verify[/yellow]",
                    src.replace("_", " "),
                    "network error reaching provider — try again later",
                )

    # Separator + notifications group. `mdk deploy --notify` reads
    # these env vars; surface them in the status table so operators
    # know whether notifications will fire BEFORE attempting a deploy.
    table.add_row("", "", "", "")
    table.add_row("[bold]Notifications[/bold]", "", "[dim]for mdk deploy --notify[/dim]", "")
    for env_var, hint_text in (
        ("TELEGRAM_BOT_TOKEN", "run [bold]mdk auth login telegram[/bold]"),
        ("TELEGRAM_CHAT_ID", "run [bold]mdk auth login telegram[/bold]"),
        ("MOVATE_DEPLOY_WEBHOOK", "set in your shell for Slack/Teams/Discord/etc."),
    ):
        src = key_source(env_var)
        if src == "unset":
            counts["unset"] += 1
            table.add_row(
                env_var,
                "[yellow]⊘ not set[/yellow]",
                "—",
                hint_text,
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

    # ------------------------------------------------------------------
    # Runtime targets section (PR #112)
    #
    # The provider-keys table above covers LLM creds. Operators also
    # juggle (a) one or more deployed-runtime bearer keys minted via
    # `mdk auth save-runtime-key` / `mdk auth refresh-runtime-key`, and
    # (b) the Azure subscription each target is pinned to. Both live
    # on disk (credentials file + ~/.movate/config.yaml respectively),
    # but operators switching between VS Code windows / terminals
    # constantly grep for them. Show them HERE so `mdk auth status`
    # is the one command that answers "am I wired up?".
    # ------------------------------------------------------------------
    _render_runtime_targets_section(counts)

    stdout.print()
    stdout.print(f"[dim]credentials file: [cyan]{CredentialsStore().path}[/cyan][/dim]")
    # Summary line tail keeps the ``set=`` and ``unset=`` keys for
    # backwards-compat with downstream scrapers; adds ``rejected=``
    # when any keys came back 401 so CI scripts can gate on it
    # (``mdk_auth_status_summary: ... rejected=1`` → flagged).
    rejected_part = f" rejected={counts['rejected']}" if counts["rejected"] else ""
    stdout.print(
        f"[dim]mdk_auth_status_summary: set={counts['ok']} "
        f"unset={counts['unset']}{rejected_part}[/dim]"
    )


def _render_runtime_targets_section(counts: dict[str, int]) -> None:  # noqa: PLR0912 — per-target state-machine reads clearer flat than refactored
    """Append the Runtime Targets table to `mdk auth status` output.

    For each target in ~/.movate/config.yaml shows:
      - URL (the deployed runtime endpoint)
      - key_env name + whether the bearer is currently resolved
      - Azure subscription (or `—` for non-Azure targets)
      - Drift flag if `az account show` returns a different subscription

    The `az` check is best-effort: if `az` isn't installed or the
    operator isn't logged in, we skip the drift detection and don't
    fail the whole `status` command — the LLM-keys part still works
    offline.
    """
    from movate.core.user_config import (  # noqa: PLC0415
        UserConfigError,
        load_user_config,
    )
    from movate.credentials import key_source  # noqa: PLC0415

    try:
        cfg = load_user_config()
    except UserConfigError as exc:
        stdout.print(f"[yellow]⚠[/yellow] could not read user config: {exc}")
        return

    if not cfg.targets:
        stdout.print(
            "[dim]No deployment targets configured. "
            "Add one with [bold]mdk config add-target <name> --url <url> "
            "--key-env <ENV_VAR> --azure-subscription <id> ...[/bold][/dim]"
        )
        return

    # Best-effort: ask az for the currently-active subscription so we
    # can flag drift between the operator's shell + each target's
    # pinned subscription. Catches the cross-window footgun where you
    # `az account set` in one terminal and forget about it in another.
    current_az_sub = _current_az_subscription()

    table = Table(
        title="Runtime Targets",
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Target", style="cyan", no_wrap=True)
    table.add_column("Bearer", no_wrap=True)
    table.add_column("URL", style="dim", no_wrap=False)
    table.add_column("Azure subscription", style="dim", no_wrap=False)

    for name in sorted(cfg.targets):
        target = cfg.targets[name]
        is_active = name == cfg.active
        name_cell = f"{name}{' [green](active)[/green]' if is_active else ''}"

        # Bearer key resolution — same `key_source` the LLM-keys path
        # uses, so the diagnostic comes from the same machinery.
        if target.key_env:
            src = key_source(target.key_env)
            if src == "unset":
                bearer_cell = f"[yellow]⊘ {target.key_env} not set[/yellow]"
                counts["unset"] += 1
            else:
                bearer_cell = (
                    f"[green]✓ {target.key_env}[/green] [dim]({src.replace('_', ' ')})[/dim]"
                )
                counts["ok"] += 1
        else:
            bearer_cell = "[dim]—[/dim]"

        # Azure subscription cell — flag drift when current az account
        # doesn't match this target's pinned subscription.
        if target.azure_subscription:
            sub_short = target.azure_subscription[:8] + "…"
            if current_az_sub and current_az_sub != target.azure_subscription:
                drift_short = current_az_sub[:8] + "…"
                azure_cell = (
                    f"[yellow]⚠[/yellow] {sub_short} "
                    f"[dim](az is on {drift_short} — run "
                    f"[bold]az account set --subscription {target.azure_subscription}[/bold])[/dim]"
                )
            elif current_az_sub:
                azure_cell = f"[green]✓[/green] {sub_short}"
            else:
                # az unavailable or not logged in — show the pinned id but
                # don't claim it matches.
                azure_cell = f"{sub_short} [dim](az not detected)[/dim]"
        else:
            azure_cell = "[dim]—[/dim]"

        table.add_row(name_cell, bearer_cell, target.url, azure_cell)

    stdout.print(table)

    # If we couldn't reach az at all, give one hint at the bottom.
    if current_az_sub is None:
        stdout.print(
            "[dim]→ Azure drift detection skipped (`az` not installed or "
            "not logged in). Run [bold]az login[/bold] to enable.[/dim]"
        )


def _current_az_subscription() -> str | None:
    """Return the current `az account show --query id` value, or None.

    Best-effort. Returns None silently when:
      - `az` isn't on PATH (Azure CLI not installed)
      - operator isn't logged in (`az login` not run)
      - az returns non-JSON or non-zero
    Keeps `mdk auth status` working offline + on non-Azure dev boxes.
    """
    import json as _json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if shutil.which("az") is None:
        return None
    try:
        result = subprocess.run(
            ["az", "account", "show", "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return None
    sub_id = payload.get("id")
    return sub_id if isinstance(sub_id, str) else None


@auth_app.command("save-runtime-key")
def save_runtime_key(
    target: str = typer.Argument(
        ...,
        help="Deployment target name (from `mdk config list-targets`).",
    ),
    key: str = typer.Argument(
        ...,
        help=(
            "The full `mvt_<env>_<tenant>_<keyid>_<secret>` value printed "
            "by `mdk auth create-key`. Quote it if your shell would otherwise "
            "interpret special characters. Pass `-` to read the key from "
            "stdin instead — recommended for piped recovery flows so the "
            "secret never lands in shell history or `ps` output."
        ),
    ),
) -> None:
    """Save a minted runtime bearer key to the credentials store.

    The bearer-key flow today is two-step: (1) run ``mdk auth
    create-key`` inside the deployed Container App via ``az
    containerapp exec``, copy the printed secret; (2) ``export
    MDK_DEV_KEY=...`` locally. Step 2 has to run in every new shell.

    This command replaces step 2 with a single write to
    ``~/.movate/credentials``. Future shells autoload the variable
    automatically (the loader pattern-matches ``MDK_*_KEY`` entries —
    see :mod:`movate.credentials.loader`).

    Examples:

      [dim]# Inline (works but leaks the secret to shell history):[/dim]
      $ mdk auth save-runtime-key dev mvt_live_demotena_…_…

      [dim]# Piped — preferred for the recovery flow (no shell history,[/dim]
      [dim]# no `ps` exposure):[/dim]
      $ az containerapp exec -g movate-dev-rg -n movate-dev-api \\\\
          --command "mdk auth create-key --tenant-id demotenant \\\\
                       --env live --quiet" \\\\
        | mdk auth save-runtime-key dev -
    """
    # Local imports keep CLI cold-start cheap (config + credentials
    # are only needed when this command actually runs).
    import sys  # noqa: PLC0415

    from movate.core.user_config import (  # noqa: PLC0415
        UserConfigError,
        load_user_config,
    )
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    # `-` is the conventional stdin sentinel. Read the entire pipe
    # (callers typically pipe a single line) and scrape the first
    # `mvt_*` token out — robust against trailing newlines, ANSI
    # color codes, and az-exec's "save this now" preamble.
    if key == "-":
        if sys.stdin.isatty():
            error(
                "key is `-` but stdin is a tty — nothing to read. "
                "Pipe the output of `mdk auth create-key` or pass the "
                "key directly as the second argument."
            )
            raise typer.Exit(code=2)
        piped = sys.stdin.read()
        scraped = _extract_mvt_key(piped)
        if scraped is None:
            error(
                "no `mvt_<env>_<tenant>_<keyid>_<secret>` token found "
                "on stdin. Pipe the output of `mdk auth create-key` "
                "(with or without --quiet) or paste the value directly."
            )
            raise typer.Exit(code=2)
        key = scraped

    try:
        cfg = load_user_config()
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    if target not in cfg.targets:
        registered = sorted(cfg.targets) or ["<none>"]
        error(
            f"unknown target {target!r}. Registered: "
            f"{', '.join(registered)}. Add one with `mdk config add-target`."
        )
        raise typer.Exit(code=2)
    target_cfg = cfg.targets[target]
    env_var = target_cfg.key_env
    if not env_var:
        error(
            f"target {target!r} has no `key_env` configured. Re-register "
            f"the target with `mdk config add-target {target} --key-env "
            f"MDK_{target.upper()}_KEY ...`"
        )
        raise typer.Exit(code=2)

    # Light sanity-check the key shape so we catch obvious paste errors
    # (e.g. operator copied half the line) before the runtime would.
    # The canonical format is `mvt_<env>_<tenant>_<keyid>_<secret>` —
    # five underscore-separated parts.
    expected_parts = 5
    if len(key.split("_")) < expected_parts or not key.startswith("mvt_"):
        err.print(
            "[yellow]⚠[/yellow] key doesn't look like a movate bearer "
            "(expected `mvt_<env>_<tenant>_<keyid>_<secret>`). "
            "Saving anyway — if auth fails later, double-check the value."
        )

    store = CredentialsStore()
    store.set(env_var, key)
    success(f"saved as [cyan]{env_var}[/cyan] in [cyan]{store.path}[/cyan].")
    hint(
        f"[dim]Future shells autoload {env_var} automatically. For the "
        f"current shell, run: [bold]export {env_var}=$(grep '^{env_var}=' "
        f"{store.path} | cut -d= -f2-)[/bold] or open a new terminal.[/dim]"
    )


class RefreshRuntimeKeyError(Exception):
    """Raised by :func:`refresh_runtime_key_inline` on any failure.

    Carries a single operator-actionable message in ``args[0]`` plus
    optional ``stdout``/``stderr`` strings captured from the underlying
    ``az containerapp exec`` call. Callers that want to treat refresh
    failures as a soft fall-through (e.g. ``mdk deploy``'s 401 auto-
    recovery) catch this rather than ``typer.Exit``; interactive
    callers can surface the captured outputs for triage.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def refresh_runtime_key_inline(  # noqa: PLR0912 — orchestrator; az shell-out + parse + save reads clearer flat
    target: str,
    *,
    tenant: str = "demotenant",
    env: str = "live",
    label: str | None = None,
    container_app: str | None = None,
) -> tuple[str, str]:
    """Mint + save a fresh runtime bearer programmatically.

    Same mechanics as the ``mdk auth refresh-runtime-key`` typer command,
    but returns ``(minted_key, env_var)`` instead of printing and exits
    nothing. Callers in non-interactive contexts (auto-recovery from
    ``mdk deploy``'s 401 handler) use this to avoid shelling out to
    another ``mdk`` process.

    Returns:
        ``(minted_key, env_var)`` — the new ``mvt_*`` bearer and the
        credentials-store env var name where it was saved.

    Raises:
        RefreshRuntimeKeyError: on any failure, with an
            operator-actionable message in ``args[0]``.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    from movate.core.user_config import (  # noqa: PLC0415
        UserConfigError,
        load_user_config,
    )
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    try:
        cfg = load_user_config()
    except UserConfigError as exc:
        raise RefreshRuntimeKeyError(str(exc)) from None
    if target not in cfg.targets:
        registered = sorted(cfg.targets) or ["<none>"]
        raise RefreshRuntimeKeyError(
            f"unknown target {target!r}. Registered: "
            f"{', '.join(registered)}. Add one with `mdk config add-target`."
        )
    target_cfg = cfg.targets[target]
    if not target_cfg.azure_resource_group:
        raise RefreshRuntimeKeyError(
            f"target {target!r} has no `azure_resource_group` configured. "
            f"Re-register with `mdk config add-target` + the Azure fields, "
            f"OR mint the key manually inside the Container App + use "
            f"`mdk auth save-runtime-key`."
        )

    if container_app is None:
        if not target_cfg.azure_env:
            raise RefreshRuntimeKeyError(
                f"target {target!r} has no `azure_env` configured. "
                f"Pass --container-app <name> explicitly, or re-register "
                f"the target with `--azure-env dev|staging|prod`."
            )
        container_app = f"movate-{target_cfg.azure_env}-api"

    if shutil.which("az") is None:
        raise RefreshRuntimeKeyError(
            "`az` (Azure CLI) not found on PATH. Install it from "
            "https://learn.microsoft.com/cli/azure/install-azure-cli, "
            "OR mint the key manually inside the Container App + use "
            "`mdk auth save-runtime-key` to persist it."
        )

    if target_cfg.azure_subscription:
        try:
            subprocess.run(
                ["az", "account", "set", "--subscription", target_cfg.azure_subscription],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RefreshRuntimeKeyError(
                f"az account set failed: {exc.stderr.decode(errors='replace').strip()[:200]}"
            ) from None

    inner_cmd_parts = [
        "mdk",
        "auth",
        "create-key",
        "--tenant-id",
        tenant,
        "--env",
        env,
    ]
    if label:
        inner_cmd_parts.extend(["--label", label])
    inner_cmd_parts.append("--quiet")
    inner_cmd = " ".join(inner_cmd_parts)

    try:
        result = subprocess.run(
            [
                "az",
                "containerapp",
                "exec",
                "-g",
                target_cfg.azure_resource_group,
                "-n",
                container_app,
                "--command",
                inner_cmd,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RefreshRuntimeKeyError(f"command not found: az ({exc})") from None

    if result.returncode != 0:
        raise RefreshRuntimeKeyError(
            f"az containerapp exec failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:400]} — confirm the Container App "
            f"{container_app!r} exists in resource group "
            f"{target_cfg.azure_resource_group!r}, and that you have "
            f"`Container Apps Contributor` or higher on it."
        )

    minted_key = _extract_mvt_key(result.stdout + "\n" + result.stderr)
    if minted_key is None:
        raise RefreshRuntimeKeyError(
            "could not find a `mvt_*` key in the az containerapp exec output. "
            "Run the command manually to debug, then use "
            f"`mdk auth save-runtime-key {target} <key>` directly: "
            f"az containerapp exec -g {target_cfg.azure_resource_group} "
            f"-n {container_app} --command {inner_cmd!r}",
            stdout=result.stdout,
            stderr=result.stderr,
        )

    env_var = target_cfg.key_env
    if not env_var:
        raise RefreshRuntimeKeyError(
            f"target {target!r} has no `key_env` configured. Re-register "
            f"the target with `--key-env MDK_{target.upper()}_KEY`."
        )
    store = CredentialsStore()
    store.set(env_var, minted_key)
    return minted_key, env_var


@auth_app.command("refresh-runtime-key")
def refresh_runtime_key(
    target: str = typer.Argument(
        ...,
        help="Deployment target name (from `mdk config list-targets`).",
    ),
    tenant: str = typer.Option(
        "demotenant",
        "--tenant",
        help=(
            "Tenant id to mint the key for. Defaults to `demotenant` "
            "(matches the default used by `mdk auth bootstrap-seed`). "
            "Must be at least TENANT_PREFIX_LEN (8) chars — the wire "
            "format embeds the first 8 characters as the tenant_prefix "
            "segment, so anything shorter fails validation."
        ),
    ),
    env: str = typer.Option(
        "live",
        "--env",
        help="Key env class: `live` for production deploys, `test` for staging.",
    ),
    label: str = typer.Option(
        None,
        "--label",
        help="Optional human-readable note attached to the key.",
    ),
    container_app: str = typer.Option(
        None,
        "--container-app",
        help=(
            "Override the auto-derived Container App name. Default: "
            "`movate-{azure_env}-api` (e.g. `movate-dev-api`)."
        ),
    ),
) -> None:
    """Mint + save a fresh runtime bearer in one step.

    Recovery path for the "my saved key returns 401 after a deploy"
    failure mode. The auth design is opaque token + per-key
    `secret_hash + salt` lookup against the runtime's storage (NOT
    JWT signing — there is no shared signing secret to rotate). When
    a Container App revision recycles and the runtime falls back to
    a non-durable storage backend (SQLite-in-pod when MOVATE_DB_URL
    isn't honored), the ApiKeyRecord table vanishes with the pod's
    filesystem and every previously-minted key returns 401.

    Without this command, the operator has to manually:

    1. Find the right subscription / resource group / Container App.
    2. Run `az containerapp exec ... mdk auth create-key`.
    3. Copy the printed key.
    4. Run `mdk auth save-runtime-key <target> <key>`.

    This command does all four steps as one verb. Reads the target
    config to derive the Azure addressing, shells out to
    `az containerapp exec` to mint a fresh key inside the live pod,
    parses the printed `mvt_...` value, and writes it to
    `~/.movate/credentials` for autoload across shells.

    Long-term fix is at the storage layer: `mdk doctor target <name>`
    surfaces whether the runtime is on durable Postgres or
    ephemeral SQLite. If it's the latter, recoveries like this one
    will be needed on every revision recycle.

    [bold]Examples:[/bold]

      [dim]# Recover from 401 after a deploy:[/dim]
      $ mdk auth refresh-runtime-key dev

      [dim]# Mint for a non-default tenant:[/dim]
      $ mdk auth refresh-runtime-key prod --tenant customer-acme --env live
    """
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    try:
        _minted_key, env_var = refresh_runtime_key_inline(
            target,
            tenant=tenant,
            env=env,
            label=label,
            container_app=container_app,
        )
    except RefreshRuntimeKeyError as exc:
        error(str(exc))
        # Dump the captured subprocess output when available — only
        # populated for the "az exec returned 0 but no mvt_ key in
        # output" path, where the raw streams are essential for
        # operator triage.
        if exc.stdout.strip():
            err.print(f"[dim]stdout (truncated):[/dim]\n{exc.stdout[:500]}")
        if exc.stderr.strip():
            err.print(f"[dim]stderr (truncated):[/dim]\n{exc.stderr[:500]}")
        raise typer.Exit(code=2) from None

    store_path = CredentialsStore().path
    success(
        f"minted + saved fresh runtime key for [bold]{target}[/bold] "
        f"(tenant={tenant}, env={env}) → [cyan]{env_var}[/cyan] in "
        f"[cyan]{store_path}[/cyan]."
    )
    hint(
        f"[dim]Future shells autoload {env_var} automatically. For the "
        f"current shell, run: [bold]export {env_var}=$(grep '^{env_var}=' "
        f"{store_path} | cut -d= -f2-)[/bold] or open a new terminal. "
        f"Then retry [bold]mdk deploy --target {target}[/bold].[/dim]"
    )


def _extract_mvt_key(text: str) -> str | None:
    """Find the first `mvt_<env>_<tenant>_<keyid>_<secret>` token in
    arbitrary text. Used to scrape the freshly-minted key out of
    `az containerapp exec` output (which mixes the inner command's
    stdout + stderr with Azure CLI's own noise).

    Strategy: tokenize on whitespace + common quote/punctuation
    boundaries, then keep tokens that start with `mvt_` and have at
    least 4 underscore-separated segments after the prefix (the
    canonical shape — env + tenant + keyid + secret). The secret
    segment legitimately contains underscores, so we accept ≥ 4
    rather than exactly 4 — the secret eats the remainder.

    Returns the first matching token, or None.
    """
    import re  # noqa: PLC0415

    # Token boundary: whitespace, quotes, parens, commas. Lets us scrape
    # the key out of "secret: mvt_..." just as easily as a JSON-quoted
    # "key": "mvt_..." line.
    for token in re.split(r"[\s\"'(),]+", text):
        if not token.startswith("mvt_"):
            continue
        # env + tenant + keyid + secret = 4 segments after `mvt`.
        if token.count("_") < 4:  # noqa: PLR2004
            continue
        # Light shape check — env segment must be `live` or `test`.
        parts = token.split("_", 2)
        if len(parts) < 3 or parts[1] not in ("live", "test"):  # noqa: PLR2004
            continue
        return token
    return None


def _provider_is_configured(provider: str) -> bool:
    """Return True if the provider's API key(s) are already set.

    Powers the green-check marker in the interactive picker so
    operators can see at a glance which providers they've already
    configured and which still need setup.

    LLM providers each have one env var (mapped via
    :data:`_PROVIDER_TO_ENV_VAR`). Telegram is special: needs BOTH
    ``TELEGRAM_BOT_TOKEN`` AND ``TELEGRAM_CHAT_ID`` — show "configured"
    only if both are set.

    This is a CHEAP boolean — just "is something there?" — without
    contacting the provider. For the UI markers in ``mdk auth login``
    and ``mdk auth status``, see :func:`_provider_status` which adds
    a live-verify probe so the marker can distinguish "set + works"
    from "set + rejected by provider".
    """
    from movate.credentials import key_source  # noqa: PLC0415

    if provider == "telegram":
        return (
            key_source("TELEGRAM_BOT_TOKEN") != "unset"
            and key_source("TELEGRAM_CHAT_ID") != "unset"
        )
    env_var = _PROVIDER_TO_ENV_VAR.get(provider)
    if env_var is None:
        return False
    return key_source(env_var) != "unset"


# State returned by :func:`_provider_status` — drives the marker
# rendered in the auth-login picker and the auth-status table.
#
# ``unset`` — no key configured for this provider.
# ``verified`` — key is set AND the provider's metadata endpoint
#   accepted it. Safe to use for real work.
# ``rejected`` — key is set but the provider returned 401 / "invalid
#   key". Operator needs to rotate. THIS is the state that used to be
#   silently shown as ``✓ configured`` even though the key was bogus
#   (the bug the 2026-05-19 reproduction surfaced: stub keys lingering
#   in shells / credentials file).
# ``unverifiable`` — key is set, but we couldn't reach the provider
#   to verify (network error, DNS failure, hard timeout). Don't lie
#   to the operator either way — show "set, not verified".
ProviderState = Literal["unset", "verified", "rejected", "unverifiable"]


# Tiny per-process cache. ``mdk auth login``'s picker calls
# ``_provider_status`` once per provider; ``mdk auth status`` also
# iterates over them. Without a cache we'd double-probe every
# provider. Scoped per-process so multi-shot CLI commands re-verify
# (a key rotated mid-session would otherwise still appear stale).
_verify_cache: dict[str, ProviderState] = {}


def _provider_status(provider: str) -> ProviderState:
    """Return the verified state of a provider's API key.

    Goes beyond :func:`_provider_is_configured` by actually calling
    the provider's metadata endpoint when a key is set, so the
    operator-facing marker can show "set but rejected" (key is wrong)
    distinctly from "set + verified" (key works). The probe is the
    same one ``mdk auth login`` uses post-save; cost is ~$0 per call.

    Telegram is not live-verifiable through this path (its API is
    different + isn't an LLM provider); returns ``verified`` when
    both env vars are set, ``unset`` otherwise.

    Cached per-process — repeated calls in one CLI invocation share
    the result so the picker + status table don't double-probe.
    """
    # Telegram check stays cheap — no HTTP call.
    if provider == "telegram":
        return "verified" if _provider_is_configured("telegram") else "unset"

    if not _provider_is_configured(provider):
        return "unset"

    if provider in _verify_cache:
        return _verify_cache[provider]

    env_var = _PROVIDER_TO_ENV_VAR.get(provider)
    if env_var is None:
        return "unset"
    key_value = os.environ.get(env_var, "").strip()
    if not key_value:
        # Defensive: ``_provider_is_configured`` said True but the
        # env value is empty. Treat as unset.
        return "unset"

    # Live probe. Network errors → ``unverifiable`` (don't claim
    # rejection when we can't actually reach the provider).
    try:
        from movate.credentials import verify_provider_key  # noqa: PLC0415

        result = verify_provider_key(provider, key_value)
    except Exception:
        # ``verify_provider_key`` already catches httpx errors and
        # returns a ``VerifyResult(network_error=True)`` — this except
        # is only for truly unexpected exceptions (e.g. a verify
        # implementation for a new provider that's not wired yet).
        state: ProviderState = "unverifiable"
    else:
        if result.ok:
            state = "verified"
        elif result.network_error:
            state = "unverifiable"
        else:
            state = "rejected"

    _verify_cache[provider] = state
    return state


def _verify_all_configured_providers_in_parallel() -> None:
    """Pre-warm :data:`_verify_cache` for every configured LLM provider
    in parallel — so the auth-status table / login picker render in
    ~1 round-trip latency instead of N sequential probes.

    ``mdk auth status`` was visibly slow when 3+ providers needed a
    5-second-per-probe HTTP roundtrip. Threading them out keeps the
    total wait bounded by the slowest single provider.

    Safe to call multiple times — each call is a no-op for providers
    already in the cache.
    """
    import concurrent.futures  # noqa: PLC0415

    # Only probe providers that have keys AND aren't already cached.
    todo = [
        provider
        for provider in _PROVIDER_TO_ENV_VAR
        if provider not in _verify_cache and _provider_is_configured(provider)
    ]
    if not todo:
        return

    # Each verify call has its own 5-second timeout; threading 4 of
    # them caps the picker wait at ~5s in the worst case (vs ~20s
    # serially).
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(todo)) as executor:
        futures = {executor.submit(_provider_status, p): p for p in todo}
        for future in concurrent.futures.as_completed(futures):
            # The function caches its own result; we just need to wait.
            with contextlib.suppress(Exception):
                future.result()


def _provider_status_marker(provider: str) -> str:
    """Render the Rich-marked-up marker text for a provider's state.

    Returns the empty string for the ``unset`` case (no marker —
    matches the legacy "blank means unconfigured" pattern). The other
    three states each get a distinct color + glyph so an operator
    glancing at the picker can tell at a glance which row needs
    attention.
    """
    state = _provider_status(provider)
    if state == "verified":
        return " [green]✓ verified[/green]"
    if state == "rejected":
        return " [red]✗ set but rejected[/red]"
    if state == "unverifiable":
        return " [yellow]⚠ set, couldn't verify[/yellow]"
    return ""


def _prompt_for_provider() -> str:
    """Numbered-picker fallback for ``mdk auth login`` with no arg.

    Renders the supported providers as a numbered list and prompts for
    a digit. Each option is decorated with a green ``✓ configured``
    marker when the provider's API key(s) are already in the operator's
    environment — visual confirmation of project state without a
    separate ``mdk auth status`` call.

    Mirrors the discoverability pattern in ``mdk menu`` — no prior
    knowledge of provider keys required. Returns the canonical
    lowercase provider key (e.g. ``"openai"``, ``"telegram"``) ready
    to pass to the existing dispatch logic.

    Falls back to a typed name if the input isn't a digit — operators
    who already know the provider name can skip the picker.
    """
    options: list[tuple[str, str]] = [
        ("openai", _PROVIDERS_PROMPT_NAME["openai"]),
        ("anthropic", _PROVIDERS_PROMPT_NAME["anthropic"]),
        ("azure", _PROVIDERS_PROMPT_NAME["azure"]),
        ("gemini", _PROVIDERS_PROMPT_NAME["gemini"]),
        ("lyzr", _PROVIDERS_PROMPT_NAME["lyzr"]),
        ("telegram", _PROVIDERS_PROMPT_NAME["telegram"]),
    ]
    stdout.print("[bold]Which provider would you like to set up?[/bold]")
    # Live-verify every configured provider in parallel BEFORE rendering
    # the picker so the markers reflect reality, not "is the env var
    # populated with anything" (which silently green-checked stub keys
    # like ``sk-test-*****2345`` before this change). Threading caps
    # total latency at ~5s worst case (one probe roundtrip) regardless
    # of how many providers are configured.
    _verify_all_configured_providers_in_parallel()
    for i, (key, name) in enumerate(options, start=1):
        # Marker now distinguishes ``✓ verified`` (key set + provider
        # accepted) from ``✗ set but rejected`` (key set + provider
        # 401'd) so an operator picking option N knows whether they're
        # rotating a working key or replacing a broken one.
        marker = _provider_status_marker(key)
        stdout.print(f"  [cyan]{i}[/cyan]) {name} [dim]({key})[/dim]{marker}")
    raw_input = typer.prompt(f"Choice [1-{len(options)} or provider name]")
    raw = str(raw_input).strip()

    # Numeric pick takes precedence.
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(options):
            return options[idx - 1][0]
        error(f"choice {idx} is out of range (1-{len(options)})")
        raise typer.Exit(code=2)

    # Allow the operator to skip the picker by typing a provider name
    # at the prompt. Falls through to the existing unknown-provider
    # error path below if the typed name isn't valid.
    return raw


def _login_telegram(*, key: str | None, no_verify: bool, save_to: str) -> None:
    """Guided Telegram-bot setup for ``mdk deploy --notify``.

    Telegram needs TWO values:
      * ``TELEGRAM_BOT_TOKEN`` — from @BotFather (create a bot, get the token)
      * ``TELEGRAM_CHAT_ID`` — the chat to post to (DM with bot OR group ID)

    Verification: send a test message to the chat. Success means the
    token works AND the chat is reachable from the bot. Failure could
    mean bad token, bad chat_id, or the bot isn't a member of the
    target chat — we surface the HTTP status to disambiguate.

    Saves to ``~/.movate/credentials`` (default) or project ``.env``
    via the same dispatch as the LLM-provider login flow.
    """
    from movate.credentials import CredentialsStore  # noqa: PLC0415

    # The --key flag isn't meaningful for telegram (we need TWO values).
    # If passed, we'd need a parsing convention; rather than invent one,
    # require interactive input.
    if key is not None:
        error(
            "[bold]--key[/bold] doesn't apply to telegram (we need both a "
            "bot token AND a chat ID). Re-run without [bold]--key[/bold] "
            "for the interactive flow, or set "
            "[bold]TELEGRAM_BOT_TOKEN[/bold] + [bold]TELEGRAM_CHAT_ID[/bold] "
            "directly via [bold]mdk secrets set[/bold] / your shell."
        )
        raise typer.Exit(code=2)

    hint(
        "[dim]Telegram bot setup:\n"
        "  1. Open Telegram, message [bold]@BotFather[/bold], run [bold]/newbot[/bold]\n"
        "  2. Copy the [bold]HTTP API token[/bold] BotFather gives you\n"
        "  3. /start a chat with your bot, then visit\n"
        "     [bold]https://api.telegram.org/bot<token>/getUpdates[/bold]\n"
        "     to find the [bold]chat_id[/bold] for the chat you want notifications in[/dim]"
    )
    token = typer.prompt("Telegram bot token", hide_input=True, confirmation_prompt=False).strip()
    if not token:
        error("empty token — aborted.")
        raise typer.Exit(code=2)
    chat_id = typer.prompt("Chat ID (numeric)").strip()
    if not chat_id:
        error("empty chat_id — aborted.")
        raise typer.Exit(code=2)

    # Verify by sending a test message. Skipping is the operator's
    # choice (offline setup, etc.).
    if not no_verify:
        import httpx  # noqa: PLC0415

        with stdout.status("sending test message..."):
            try:
                resp = httpx.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (
                            "✓ mdk auth login telegram — test message. "
                            "If you see this, deploy notifications are wired."
                        ),
                    },
                    timeout=5.0,
                )
            except httpx.HTTPError as exc:
                err.print(
                    f"[yellow]⚠[/yellow] verify call failed (network): "
                    f"{exc}. Saving credentials anyway."
                )
                resp = None
        if resp is not None:
            if resp.status_code == 200:  # noqa: PLR2004
                success("test message delivered.")
            else:
                error(
                    f"test message rejected ({resp.status_code}): "
                    f"{resp.text[:160]}. Double-check the bot token + chat_id, "
                    "or pass [bold]--no-verify[/bold] to save anyway."
                )
                raise typer.Exit(code=2)

    # Persist. Same save-to logic as the LLM login path.
    save_to = save_to.lower().strip()
    if save_to == "global":
        store = CredentialsStore()
        store.set("TELEGRAM_BOT_TOKEN", token)
        store.set("TELEGRAM_CHAT_ID", chat_id)
        success(
            f"saved [bold]TELEGRAM_BOT_TOKEN[/bold] + "
            f"[bold]TELEGRAM_CHAT_ID[/bold] to [cyan]{store.path}[/cyan] "
            f"(mode 0600)."
        )
        hint(
            "[dim]Every [bold]mdk deploy --notify[/bold] on this machine "
            "will now fire a Telegram message on success.[/dim]"
        )
    elif save_to == "project":
        from pathlib import Path  # noqa: PLC0415

        dotenv = Path(".env")
        with dotenv.open("a") as fh:
            fh.write(f"TELEGRAM_BOT_TOKEN={token}\n")
            fh.write(f"TELEGRAM_CHAT_ID={chat_id}\n")
        success(f"appended to [cyan]{dotenv.resolve()}[/cyan].")
    else:
        error(f"--save-to must be 'global' or 'project'; got {save_to!r}")
        raise typer.Exit(code=2)


class BootstrapSeedError(Exception):
    """Raised by :func:`bootstrap_seed_inline` on any failure.

    Carries an operator-actionable message in ``args[0]``. Callers
    that want to chain bootstrap-seed into a larger flow (e.g. ``mdk
    infra apply --target dev`` running it after a successful Bicep
    deploy) catch this instead of ``typer.Exit``.
    """


def bootstrap_seed_inline(
    target: str,
    *,
    keyvault: str,
    tenant_id: str = "demotenant",
    env: str = "live",
    force: bool = False,
) -> tuple[str, str]:
    """Mint + upload bootstrap key to Key Vault + save locally; return ``(key, env_var)``.

    Same mechanics as the ``mdk auth bootstrap-seed`` typer command,
    but returns ``(minted_key, env_var)`` instead of printing-and-exiting.
    Used by ``mdk infra apply`` to auto-chain after a successful
    Bicep deployment so first-deploy is one command.

    Raises :class:`BootstrapSeedError` on any failure.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    from movate.core.user_config import (  # noqa: PLC0415
        UserConfigError,
        load_user_config,
    )
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    try:
        cfg = load_user_config()
    except UserConfigError as exc:
        raise BootstrapSeedError(str(exc)) from None
    if target not in cfg.targets:
        registered = sorted(cfg.targets) or ["<none>"]
        raise BootstrapSeedError(
            f"unknown target {target!r}. Registered: "
            f"{', '.join(registered)}. Add one with `mdk config add-target`."
        )
    target_cfg = cfg.targets[target]
    env_var = target_cfg.key_env
    if not env_var:
        raise BootstrapSeedError(
            f"target {target!r} has no `key_env` configured. Re-register "
            f"with `--key-env MDK_{target.upper()}_KEY`."
        )

    if shutil.which("az") is None:
        raise BootstrapSeedError(
            "`az` (Azure CLI) not found on PATH. Install it from "
            "https://learn.microsoft.com/cli/azure/install-azure-cli."
        )

    if not force:
        probe = subprocess.run(
            [
                "az",
                "keyvault",
                "secret",
                "show",
                "--vault-name",
                keyvault,
                "--name",
                "bootstrap-api-key",
                "--query",
                "id",
                "-o",
                "tsv",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            raise BootstrapSeedError(
                f"`bootstrap-api-key` already exists in {keyvault!r}. To "
                f"rotate, re-run with --force. To recover its value "
                f"locally without rotating, run: "
                f"`mdk auth pull-runtime-key {target} --keyvault {keyvault}`"
            )

    try:
        env_enum = ApiKeyEnv(env)
    except ValueError as exc:
        raise BootstrapSeedError(
            f"--env must be one of {[e.value for e in ApiKeyEnv]}; got {env!r}"
        ) from exc

    try:
        minted = mint_api_key(
            tenant_id=tenant_id,
            env=env_enum,
            label="bootstrap-seed",
            ttl_days=0,
        )
    except ValueError as exc:
        raise BootstrapSeedError(str(exc)) from None
    seed_key = minted.full_key

    result = subprocess.run(
        [
            "az",
            "keyvault",
            "secret",
            "set",
            "--vault-name",
            keyvault,
            "--name",
            "bootstrap-api-key",
            "--value",
            seed_key,
            "--output",
            "none",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BootstrapSeedError(
            f"az keyvault secret set failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:400]}. Confirm {keyvault!r} exists "
            f"and that you have `Key Vault Secrets Officer` on it."
        )

    store = CredentialsStore()
    store.set(env_var, seed_key)
    return seed_key, env_var


@auth_app.command("bootstrap-seed")
def bootstrap_seed(
    target: str = typer.Argument(
        ...,
        help="Deployment target name (from `mdk config list-targets`).",
    ),
    keyvault: str = typer.Option(
        ...,
        "--keyvault",
        help=(
            "Azure Key Vault name (no FQDN — e.g. `movate-dev-kv-mvt`). "
            "The mint result is uploaded as the secret `bootstrap-api-key` "
            "which the runtime's Bicep wires as `MOVATE_SEED_API_KEY`."
        ),
    ),
    tenant_id: str = typer.Option(
        "demotenant",
        "--tenant-id",
        help=(
            "Tenant id baked into the key (≥ 8 chars per "
            "TENANT_PREFIX_LEN). The seed key gets `scope=fleet-admin` "
            "so it can mint per-tenant keys later."
        ),
    ),
    env: str = typer.Option(
        "live",
        "--env",
        help="Key env class: `live` for production, `test` for staging.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite the Key Vault secret if it already exists. Without "
            "this flag, the command exits with a hint if the secret is "
            "already present (so operators don't accidentally rotate "
            "the bootstrap key in shared environments)."
        ),
    ),
) -> None:
    """Mint a fresh bootstrap API key + upload to Key Vault + save locally.

    Solves the chicken-and-egg of fresh deployments: the runtime
    needs a known bearer in its ``api_keys`` table to authenticate
    deploys, but minting that bearer normally requires an existing
    bearer. This command does the one-time setup that breaks the
    cycle:

      1. Mint a fresh `mvt_<env>_<tenant>_<keyid>_<secret>` key
         using the same `mint_api_key()` the runtime would use.
      2. Upload it to Key Vault as the secret `bootstrap-api-key`.
         The Container App's Bicep references this secret as
         `MOVATE_SEED_API_KEY`; on every pod restart the runtime's
         `_seed_bootstrap_key()` inserts the matching ApiKeyRecord
         in the database (idempotent — exists-check skips reseeding).
      3. Save the same value to the local credentials store so
         `mdk deploy --target <name>` works immediately.

    Run this ONCE per environment after `mdk infra apply` (which
    chains into bootstrap-seed automatically by default, so most
    operators never invoke this directly) and before the first
    `mdk deploy`. Subsequent deploys (and revision recycles) keep
    working because the Bicep's secretRef + the runtime's seed code
    together ensure the key is reseeded on every cold start.

    [bold]Examples:[/bold]

      [dim]# Fresh dev environment bootstrap:[/dim]
      $ mdk auth bootstrap-seed dev --keyvault movate-dev-kv-mvt

      [dim]# Rotate the bootstrap key (security event, etc.):[/dim]
      $ mdk auth bootstrap-seed dev --keyvault movate-dev-kv-mvt --force
    """
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    hint(
        f"[dim]→ az keyvault secret set --vault-name {keyvault} "
        f"--name bootstrap-api-key --value <minted-key>[/dim]"
    )
    try:
        _seed_key, env_var = bootstrap_seed_inline(
            target,
            keyvault=keyvault,
            tenant_id=tenant_id,
            env=env,
            force=force,
        )
    except BootstrapSeedError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    store_path = CredentialsStore().path
    success(
        f"bootstrap key minted + uploaded to [cyan]{keyvault}/bootstrap-api-key[/cyan] "
        f"+ saved locally as [cyan]{env_var}[/cyan]."
    )
    hint(
        f"[dim]Next: run [bold]mdk deploy --target {target} --mode runtime[/bold] "
        f"(or restart the Container App) so the runtime picks up the "
        f"new MOVATE_SEED_API_KEY value and seeds the ApiKeyRecord row.[/dim]"
    )
    hint(
        f"[dim]For the current shell, run: [bold]export {env_var}=$(grep "
        f"'^{env_var}=' {store_path} | cut -d= -f2-)[/bold] or open a new "
        f"terminal.[/dim]"
    )


@auth_app.command("pull-runtime-key")
def pull_runtime_key(
    target: str = typer.Argument(
        ...,
        help="Deployment target name (from `mdk config list-targets`).",
    ),
    keyvault: str = typer.Option(
        ...,
        "--keyvault",
        help=(
            "Azure Key Vault name (no FQDN — e.g. `movate-dev-kv-mvt`). "
            "The same vault where `bootstrap-seed` uploaded the secret."
        ),
    ),
    secret_name: str = typer.Option(
        "bootstrap-api-key",
        "--secret-name",
        help=(
            "Key Vault secret name to read. Defaults to `bootstrap-api-key` "
            "(the value `mdk auth bootstrap-seed` uploads)."
        ),
    ),
) -> None:
    """Pull the bootstrap key from Key Vault into the local credentials store.

    Recovery path when ``~/.movate/credentials`` has been cleared (new
    laptop, new shell user, deliberate rotation of local state) and
    the deployed Container App's `bootstrap-api-key` secret is the
    canonical source of truth. One `az keyvault secret show` + one
    `CredentialsStore.set` — no minting, no `az containerapp exec`,
    no chicken-and-egg.

    Use this AFTER `mdk auth bootstrap-seed <target>` has been run
    once for the environment. To rotate the bootstrap key itself,
    use `mdk auth bootstrap-seed <target> --force` instead.

    [bold]Examples:[/bold]

      [dim]# New laptop, dev already provisioned:[/dim]
      $ mdk auth pull-runtime-key dev --keyvault movate-dev-kv-mvt
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    from movate.core.user_config import (  # noqa: PLC0415
        UserConfigError,
        load_user_config,
    )
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    try:
        cfg = load_user_config()
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    if target not in cfg.targets:
        registered = sorted(cfg.targets) or ["<none>"]
        error(
            f"unknown target {target!r}. Registered: "
            f"{', '.join(registered)}. Add one with `mdk config add-target`."
        )
        raise typer.Exit(code=2)
    target_cfg = cfg.targets[target]
    env_var = target_cfg.key_env
    if not env_var:
        error(
            f"target {target!r} has no `key_env` configured. Re-register "
            f"with `--key-env MDK_{target.upper()}_KEY`."
        )
        raise typer.Exit(code=2)

    if shutil.which("az") is None:
        error(
            "`az` (Azure CLI) not found on PATH. Install it from "
            "https://learn.microsoft.com/cli/azure/install-azure-cli."
        )
        raise typer.Exit(code=2)

    # `--query value -o tsv` returns the bare secret on stdout. Capture
    # to a Python str rather than letting it land in shell scope.
    result = subprocess.run(
        [
            "az",
            "keyvault",
            "secret",
            "show",
            "--vault-name",
            keyvault,
            "--name",
            secret_name,
            "--query",
            "value",
            "-o",
            "tsv",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # `SecretNotFound` from KV vs other failures — surface the
        # underlying stderr so the operator can tell whether the
        # secret name is wrong, the vault is wrong, or RBAC is wrong.
        error(
            f"az keyvault secret show failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:400]}. Confirm `{secret_name}` exists "
            f"in {keyvault!r} (run `mdk auth bootstrap-seed {target} "
            f"--keyvault {keyvault}` first) and that you have `Key Vault "
            f"Secrets User` on the vault."
        )
        raise typer.Exit(code=2)

    secret_value = result.stdout.strip()
    if not secret_value.startswith("mvt_"):
        error(
            f"value pulled from {keyvault}/{secret_name} doesn't look like "
            f"a movate bearer (expected `mvt_<env>_<tenant>_<keyid>_<secret>`). "
            f"Did `bootstrap-seed` actually populate this secret? Read the "
            f"value manually with `az keyvault secret show --vault-name "
            f"{keyvault} --name {secret_name} --query value -o tsv` to debug."
        )
        raise typer.Exit(code=2)

    store = CredentialsStore()
    store.set(env_var, secret_value)
    success(
        f"pulled [cyan]{keyvault}/{secret_name}[/cyan] → saved as "
        f"[cyan]{env_var}[/cyan] in [cyan]{store.path}[/cyan]."
    )
    hint(
        f"[dim]For the current shell, run: [bold]export {env_var}=$(grep "
        f"'^{env_var}=' {store.path} | cut -d= -f2-)[/bold] or open a new "
        f"terminal.[/dim]"
    )
