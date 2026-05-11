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
        err.print(f"[red]✗[/red] env must be 'live' or 'test'; got {env!r}")
        raise typer.Exit(code=2) from exc

    try:
        minted = mint_api_key(tenant_id=tenant_id, env=env_enum, label=label)
    except ValueError as exc:
        err.print(f"[red]✗[/red] {exc}")
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
        err.print("[dim]no keys found[/dim]")
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
) -> None:
    """Revoke an API key. Idempotent — re-revoking is a silent no-op."""
    asyncio.run(_revoke(key_id))
    err.print(f"[green]✓[/green] revoked {key_id}")


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
