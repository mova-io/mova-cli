"""``mdk agent`` — durable-registry version management (ADR 014 D3).

Surfaces the agent registry's version history + rollback for a team
authoring collaboratively:

* ``mdk agent history <name>`` — the version history of an agent,
  newest-first, marking the current (latest) version and showing who
  published each version when (the ``created_by`` audit, ADR 013).
* ``mdk agent revert <name> --to <version>`` — roll an agent back to a
  prior version by **re-publishing that version forward** as a new
  latest registry row. Non-destructive: no version is ever deleted, so
  the full history (including the one you reverted away from) stays
  intact. Prompts before mutating; ``--yes`` skips the prompt.

Local-only — talks straight to the configured ``StorageProvider`` (the
same pattern as ``mdk tenants``). The HTTP runtime exposes the matching
``GET /api/v1/agents/{name}/versions`` + ``POST .../revert`` endpoints
for the deployed multi-pod path; this CLI is the operator-side door over
the local store. Tenant scope defaults to ``local`` (CLI convention);
override with ``--tenant-id`` where the tenant comes from elsewhere.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import confirm_destructive, error, hint, success
from movate.core.models import AgentBundleRecord
from movate.storage import build_storage

_DEFAULT_TENANT = "local"

stdout = Console()
err = Console(stderr=True)

agent_app = typer.Typer(
    name="agent",
    help="Inspect + roll back published agent versions (durable registry).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@dataclass(frozen=True)
class _RevertResult:
    """Outcome of a revert, pre-formatted for the CLI to print without
    re-touching storage."""

    name: str
    new_version: str
    reverted_from: str
    previous_version: str


@agent_app.command("history")
def history(
    name: str = typer.Argument(..., help="Agent name (the agent.yaml ``name``)."),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help=(
            "Tenant scope. Defaults to 'local' for CLI use. Override in "
            "production where the tenant comes from the auth context."
        ),
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Max number of versions to show (newest-first).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the version history as JSON instead of a table.",
    ),
) -> None:
    """Show the version history for ``name``, newest-first.

    The most-recently-published version is marked as current — that's
    what a versionless run/resolve serves. Each row shows who published
    it (``created_by``) and when, so a team can audit "who changed what."

    [bold]Examples:[/bold]

      [dim]# Version history of the faq-bot agent[/dim]
      $ mdk agent history faq-bot

      [dim]# As JSON, for scripting[/dim]
      $ mdk agent history faq-bot --json
    """
    versions = asyncio.run(_load_history(name, tenant_id=tenant_id, limit=limit))

    if as_json:
        stdout.print_json(
            data={
                "name": name,
                "versions": [
                    {
                        "version": r.version,
                        "created_by": r.created_by,
                        "created_at": r.created_at.isoformat(),
                        "content_hash": r.content_hash,
                        "is_current": i == 0,
                    }
                    for i, r in enumerate(versions)
                ],
                "count": len(versions),
            }
        )
        return

    if not versions:
        hint(f"[dim]no published versions for agent '{name}' (tenant={tenant_id})[/dim]")
        return

    table = Table(title=f"agent '{name}' — version history (tenant {tenant_id})")
    table.add_column("", style="green", no_wrap=True)  # current marker
    table.add_column("version", style="bold")
    table.add_column("published by")
    table.add_column("published at", style="dim")
    table.add_column("content hash", style="dim")

    for i, r in enumerate(versions):
        marker = "→" if i == 0 else ""
        table.add_row(
            marker,
            r.version,
            r.created_by or "[dim]<system>[/dim]",
            r.created_at.isoformat(),
            r.content_hash[:12],
        )
    stdout.print(table)
    hint("[dim]→ marks the current (latest) version[/dim]")


@agent_app.command("revert")
def revert(
    name: str = typer.Argument(..., help="Agent name."),
    to_version: str = typer.Option(
        ...,
        "--to",
        help="The prior version to roll back to (must exist in the history).",
    ),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help="Tenant scope. Defaults to 'local' for CLI use.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirm prompt (use in scripts / CI).",
    ),
) -> None:
    """Roll ``name`` back to ``--to <version>`` (non-destructive).

    Re-publishes the target version's bundle forward as a NEW latest
    version — the prior history is left untouched, so you can revert
    again (even back to where you were). The new version is the same
    bundle as ``--to`` with a ``+revert.N`` provenance suffix so it
    doesn't collide with the immutable original row.

    [bold]Examples:[/bold]

      [dim]# Roll faq-bot back to the 0.2.0 bundle[/dim]
      $ mdk agent revert faq-bot --to 0.2.0

      [dim]# Non-interactive (CI)[/dim]
      $ mdk agent revert faq-bot --to 0.2.0 --yes
    """
    confirm_destructive(
        f"Revert agent '{name}' to version {to_version}? "
        "(re-publishes it as a new latest version; history is preserved)",
        yes=yes,
    )
    try:
        result = asyncio.run(_revert(name, to_version=to_version, tenant_id=tenant_id))
    except _UnknownVersionError:
        error(
            f"no version {to_version!r} for agent {name!r} (tenant={tenant_id}); "
            "run 'mdk agent history' to see available versions",
            context="agent revert",
        )
        raise typer.Exit(code=1) from None

    success(
        f"reverted '{result.name}' to {result.reverted_from} "
        f"(new latest version: {result.new_version})"
    )


# ---------------------------------------------------------------------------
# Async helpers — keep the Typer commands synchronous (mirrors tenants.py)
# ---------------------------------------------------------------------------


class _UnknownVersionError(Exception):
    """The requested ``to_version`` doesn't exist for this agent/tenant."""


async def _load_history(name: str, *, tenant_id: str, limit: int) -> list[AgentBundleRecord]:
    storage = build_storage()
    await storage.init()
    try:
        return await storage.list_agent_versions(name, tenant_id=tenant_id, limit=limit)
    finally:
        await storage.close()


async def _revert(name: str, *, to_version: str, tenant_id: str) -> _RevertResult:
    """Re-publish ``to_version`` forward as a new latest registry row.

    Non-destructive: appends a new ``(name, version)`` row whose bundle
    is byte-identical to ``to_version`` (same ``files`` + ``content_hash``)
    under a collision-free ``+revert.N`` version. Mirrors the runtime's
    ``POST /api/v1/agents/{name}/revert`` so local + deployed behave the
    same.
    """
    storage = build_storage()
    await storage.init()
    try:
        target = await storage.get_agent_bundle(name, tenant_id=tenant_id, version=to_version)
        if target is None:
            raise _UnknownVersionError(to_version)

        history = await storage.list_agent_versions(name, tenant_id=tenant_id, limit=1000)
        previous_version = history[0].version if history else to_version
        existing = {r.version for r in history}
        new_version = _mint_revert_version(to_version, existing)

        reverted = AgentBundleRecord(
            name=target.name,
            tenant_id=target.tenant_id,
            version=new_version,
            created_by="cli",
            content_hash=target.content_hash,
            files=target.files,
        )
        await storage.save_agent_bundle(reverted)
        return _RevertResult(
            name=name,
            new_version=new_version,
            reverted_from=to_version,
            previous_version=previous_version,
        )
    finally:
        await storage.close()


def _mint_revert_version(to_version: str, existing: set[str]) -> str:
    """Collision-free ``<base>+revert.N`` version for a revert publish.

    Kept in sync with the runtime helper of the same name: the registry
    PK is ``(tenant, name, version)``, so a revert can't re-use the
    target version string verbatim. ``N`` bumps until unique.
    """
    base = to_version.split("+revert.", 1)[0]
    n = 1
    candidate = f"{base}+revert.{n}"
    while candidate in existing:
        n += 1
        candidate = f"{base}+revert.{n}"
    return candidate


__all__ = ["agent_app"]
