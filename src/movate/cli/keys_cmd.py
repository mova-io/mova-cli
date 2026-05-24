"""``mdk keys`` — per-tenant BYOK provider keys (ADR 018).

Each tenant can bring its own OpenAI/Anthropic/etc. provider API key,
**encrypted at rest** (Fernet, keyed by ``MOVATE_PROVIDER_KEY_SECRET``). At
run time the runtime resolves the calling tenant's key first, falling back to
the shared fleet key (the provider's env-default) when the tenant has none —
so this is additive + back-compatible.

Subcommands:

* ``set <provider>`` — store (or rotate) the key for a provider. The key is
  prompted **hidden** (never echoed), encrypted, and only a masked fingerprint
  is printed back. The plaintext is never stored or shown.
* ``list`` — show which providers have a key configured + a masked
  fingerprint. Never the value.
* ``delete <provider>`` — remove a provider's key.
* ``test <provider>`` — make a cheap provider call with the stored key to
  validate it works before relying on it.

Keys are stored under the local ``"local"`` tenant (matching the rest of the
local CLI, e.g. ``mdk trigger`` / ``mdk schedule``). In a deployed runtime the
same store is reachable per-tenant via the ``/api/v1/provider-keys`` endpoints.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from movate.core.models import TenantProviderKey
from movate.core.provider_keys import (
    ENV_PROVIDER_KEY_SECRET,
    ProviderKeyError,
    decrypt_provider_key,
    mint_tenant_provider_key,
    normalize_provider,
)
from movate.storage.base import StorageProvider

console = Console()
err_console = Console(stderr=True)

# Local CLI storage scopes records under the "local" tenant — matches
# trigger_cmd / schedule_cmd / build_local_runtime's Executor tenant_id.
_LOCAL_TENANT = "local"

keys_app = typer.Typer(
    name="keys",
    help="Manage this tenant's own provider API keys, encrypted at rest (BYOK, ADR 018).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@keys_app.command("set")
def set_key(
    provider: str = typer.Argument(
        ...,
        help="Provider namespace, e.g. openai | anthropic (the part before '/' in model.provider).",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="The provider key. Omit to be prompted HIDDEN (recommended — keeps it off your "
        "shell history).",
    ),
) -> None:
    """Store (or rotate) this tenant's key for [bold]provider[/bold].

    The key is encrypted at rest with ``MOVATE_PROVIDER_KEY_SECRET`` (Fernet);
    only a masked fingerprint is printed back — the value is [bold]never[/bold]
    stored in plaintext or echoed.

    [bold]Examples:[/bold]

      [dim]# Prompt for the key, hidden[/dim]
      $ mdk keys set openai

      [dim]# Rotate the Anthropic key (overwrites in place)[/dim]
      $ mdk keys set anthropic
    """
    norm = normalize_provider(provider)
    # Hidden prompt when not passed inline — never echoed to the terminal.
    plaintext = api_key if api_key is not None else typer.prompt(f"{norm} API key", hide_input=True)
    plaintext = plaintext.strip()
    if not plaintext:
        err_console.print("[red]✗[/red] empty key — nothing stored")
        raise typer.Exit(code=2)

    try:
        record = mint_tenant_provider_key(
            tenant_id=_LOCAL_TENANT,
            provider=norm,
            plaintext=plaintext,
        )
    except ProviderKeyError as exc:
        _fail_misconfigured(exc)

    asyncio.run(_save(record))
    console.print(
        f"[green]✓[/green] stored [bold]{norm}[/bold] key "
        f"(fingerprint [bold]{record.fingerprint}[/bold]) — encrypted at rest, "
        "value never shown again"
    )


@keys_app.command("list")
def list_keys() -> None:
    """List configured providers + masked fingerprints (never the values)."""
    rows = asyncio.run(_list())
    if not rows:
        console.print(
            "[dim]no provider keys — add one with[/dim] mdk keys set <provider> "
            "[dim](else runs use the shared fleet key)[/dim]"
        )
        return
    table = Table(title="Per-tenant provider keys (BYOK)")
    table.add_column("provider", style="bold")
    table.add_column("fingerprint")
    table.add_column("updated")
    for k in rows:
        table.add_row(
            k.provider,
            k.fingerprint,
            k.updated_at.isoformat(timespec="seconds"),
        )
    console.print(table)


@keys_app.command("delete")
def delete_key(
    provider: str = typer.Argument(..., help="Provider namespace to remove (e.g. openai)."),
) -> None:
    """Remove this tenant's key for a provider (falls back to the shared key)."""
    norm = normalize_provider(provider)
    deleted = asyncio.run(_delete(norm))
    if deleted:
        console.print(f"[green]✓[/green] deleted [bold]{norm}[/bold] key")
    else:
        console.print(f"[dim]no[/dim] {norm} [dim]key — nothing to delete[/dim]")


@keys_app.command("test")
def test_key(
    provider: str = typer.Argument(..., help="Provider namespace to validate (e.g. openai)."),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Model to probe (default: a cheap per-provider default).",
    ),
) -> None:
    """Make a cheap provider call with the stored key to validate it.

    Decrypts the stored key and runs a 1-token completion through LiteLLM so
    you can confirm the key works before relying on it. The key is used only
    in-memory for this call — never printed.
    """
    norm = normalize_provider(provider)
    probe_model = model or _default_probe_model(norm)
    if probe_model is None:
        err_console.print(
            f"[red]✗[/red] no default probe model for {norm!r} — pass --model <model>"
        )
        raise typer.Exit(code=2)
    ok, detail = asyncio.run(_test(norm, probe_model))
    if ok:
        console.print(f"[green]✓[/green] {norm} key works ([dim]{probe_model}[/dim])")
    else:
        err_console.print(f"[red]✗[/red] {norm} key check failed: {detail}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail_misconfigured(exc: ProviderKeyError) -> NoReturn:
    """Render a clean operator error when the encryption key is missing/bad."""
    err_console.print(
        f"[red]✗[/red] {exc}\n"
        f"[dim]Set {ENV_PROVIDER_KEY_SECRET} (a Fernet key) before storing provider keys.[/dim]"
    )
    raise typer.Exit(code=2)


def _default_probe_model(provider: str) -> str | None:
    """A cheap LiteLLM-style model string for `mdk keys test`, per provider."""
    return {
        "openai": "openai/gpt-4o-mini",
        "anthropic": "anthropic/claude-haiku-4-5",
    }.get(provider)


@asynccontextmanager
async def _local_storage() -> AsyncIterator[StorageProvider]:
    """Build the local runtime, yield its storage, tear down cleanly."""
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415

    runtime = await build_local_runtime(mock=True)
    try:
        yield runtime.storage
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


async def _save(record: TenantProviderKey) -> None:
    async with _local_storage() as storage:
        await storage.save_tenant_provider_key(record)


async def _list() -> list[TenantProviderKey]:
    async with _local_storage() as storage:
        return await storage.list_tenant_provider_keys(tenant_id=_LOCAL_TENANT)


async def _delete(provider: str) -> bool:
    async with _local_storage() as storage:
        return await storage.delete_tenant_provider_key(provider, tenant_id=_LOCAL_TENANT)


async def _test(provider: str, model: str) -> tuple[bool, str]:
    """Decrypt the stored key and run a 1-token probe via LiteLLM."""
    async with _local_storage() as storage:
        row = await storage.get_tenant_provider_key(provider, tenant_id=_LOCAL_TENANT)
        if row is None:
            return (False, f"no {provider!r} key set — add one with `mdk keys set {provider}`")
        try:
            plaintext = decrypt_provider_key(row.ciphertext)
        except ProviderKeyError as exc:
            return (False, str(exc))

    # Probe via the same LiteLLM path runs use, with the decrypted key passed
    # as a per-call api_key (never logged). Imported locally to keep litellm
    # off the hot CLI-import path.
    from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415
    from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

    provider_impl = LiteLLMProvider()
    req = CompletionRequest(
        provider=model,
        messages=[Message(role="user", content="ping")],
        params={"api_key": plaintext, "max_tokens": 1, "temperature": 0},
    )
    try:
        await provider_impl.complete(req)
    except Exception as exc:
        # Never include the key; the provider's message doesn't carry it.
        return (False, type(exc).__name__ + ": " + str(exc))
    return (True, "ok")


__all__ = ["keys_app"]
