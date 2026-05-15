"""``mdk memory`` — agent memory management (Sprint T MVP).

Operator-facing access to the :class:`movate.memory.MemoryStore`.
The full architectural integration (Executor consults memory mid-run,
summarisation policy, vector recall) lands in Sprint T+; the CLI
surface ships today so operators can manage memory state explicitly.

Subcommands:

* ``mdk memory list <agent>``       — every stored entry for an agent
* ``mdk memory get <agent> <key>``  — one entry's value as JSON
* ``mdk memory set <agent> <key> <json>`` — insert/replace
* ``mdk memory evict <agent> --before <ISO>`` — bulk delete by age
* ``mdk memory summarise <agent>``  — operator-readable summary
* ``mdk memory query <agent> <text>`` — substring search (vector
  recall is a deferred backend)

Storage: defaults to a JSON file at ``~/.movate/memory.json`` (set
``MOVATE_MEMORY_FILE`` to override). Cross-invocation persistence
is the point — ``mdk memory set`` followed by ``mdk memory list``
returns the entry the prior call wrote.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.memory import MemoryStore, build_memory_store

console = Console()
err_console = Console(stderr=True)


# Preview length caps for table rendering. Operators want at-a-glance
# scanability — the JSON-dumped value column is collapsed past these.
_LIST_PREVIEW_CHARS = 60
_QUERY_PREVIEW_CHARS = 80


memory_app = typer.Typer(
    name="memory",
    help=(
        "Agent memory — list / get / set / evict / summarise / query stored entries. "
        "Default backend: JSON file at [bold]~/.movate/memory.json[/bold]. "
        "Set [bold]MOVATE_MEMORY_FILE[/bold] to change."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _store() -> MemoryStore:
    return build_memory_store()


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


@memory_app.command("list")
def list_(
    agent: str = typer.Argument(..., help="Agent name to list memory entries for."),
    since_days: int = typer.Option(
        0,
        "--since-days",
        help=(
            "Only show entries newer than N days. 0 (default) = no time filter. "
            "Mirrors [bold]mdk costs report --since-days[/bold]."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit entries as JSON instead of a Rich table."
    ),
) -> None:
    """List every memory entry for an agent."""
    entries = asyncio.run(_store().list(agent))
    if since_days > 0:
        entries = _filter_entries_by_age(entries, since_days)
    if json_output:
        console.print_json(
            json.dumps(
                [
                    {
                        "key": e.key,
                        "value": e.value,
                        "created_at": e.created_at,
                        "ttl_seconds": e.ttl_seconds,
                    }
                    for e in entries
                ]
            )
        )
        return
    if not entries:
        console.print(
            f"[yellow]⚠[/yellow] no memory entries for agent [cyan]{agent}[/cyan]. "
            "[dim]Run [bold]mdk memory set[/bold] to add one.[/dim]"
        )
        return
    table = Table(title=f"Memory ({len(entries)}) for [cyan]{agent}[/cyan]", title_style="bold")
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value preview", style="dim")
    table.add_column("Created", style="dim", no_wrap=True)
    table.add_column("TTL", justify="right", style="dim")
    for e in entries:
        preview = json.dumps(e.value)
        truncated = (
            preview
            if len(preview) <= _LIST_PREVIEW_CHARS
            else preview[: _LIST_PREVIEW_CHARS - 1] + "…"
        )
        ttl = f"{e.ttl_seconds}s" if e.ttl_seconds else "—"
        table.add_row(e.key, truncated, e.created_at, ttl)
    console.print(table)


# ---------------------------------------------------------------------------
# Subcommand: get
# ---------------------------------------------------------------------------


@memory_app.command("get")
def get(
    agent: str = typer.Argument(..., help="Agent name."),
    key: str = typer.Argument(..., help="Memory key."),
) -> None:
    """Print one memory entry's value as JSON."""
    entry = asyncio.run(_store().get(agent, key))
    if entry is None:
        err_console.print(f"[red]✗[/red] no entry [bold]{key}[/bold] for [cyan]{agent}[/cyan]")
        raise typer.Exit(code=1)
    # Plain stdout (no Rich) — operators substitute into shell scripts.
    print(json.dumps(entry.value, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: set
# ---------------------------------------------------------------------------


@memory_app.command("set")
def set_(
    agent: str = typer.Argument(..., help="Agent name."),
    key: str = typer.Argument(..., help="Memory key."),
    value_json: str = typer.Argument(..., help="Value as a JSON object."),
    ttl_seconds: int = typer.Option(
        0,
        "--ttl-seconds",
        help="Time-to-live in seconds. 0 (default) = no expiration.",
    ),
) -> None:
    """Insert or replace a memory entry."""
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]✗[/red] value is not valid JSON: {exc}")
        raise typer.Exit(code=2) from None
    if not isinstance(value, dict):
        err_console.print("[red]✗[/red] value must be a JSON object")
        raise typer.Exit(code=2)
    try:
        entry = asyncio.run(_store().set(agent, key, value, ttl_seconds=ttl_seconds))
    except NotImplementedError as exc:
        # SqliteStore scaffold — surface cleanly.
        err_console.print(f"[yellow]⚠[/yellow] backend not implemented: {exc}")
        raise typer.Exit(code=2) from None
    console.print(
        f"[green]✓[/green] stored [bold]{key}[/bold] for "
        f"[cyan]{agent}[/cyan] [dim]({entry.created_at})[/dim]"
    )


# ---------------------------------------------------------------------------
# Subcommand: delete
# ---------------------------------------------------------------------------


@memory_app.command("delete")
def delete(
    agent: str = typer.Argument(..., help="Agent name."),
    key: str = typer.Argument(..., help="Memory key."),
    force: bool = typer.Option(
        False, "--force", help="Required to actually delete (dry-run by default)."
    ),
) -> None:
    """Remove a single memory entry."""
    if not force:
        console.print(
            f"[yellow]⚠ dry-run:[/yellow] would delete [bold]{key}[/bold] "
            f"for [cyan]{agent}[/cyan]. Re-run with [bold]--force[/bold]."
        )
        raise typer.Exit(code=1)
    removed = asyncio.run(_store().delete(agent, key))
    if not removed:
        err_console.print(f"[red]✗[/red] no entry [bold]{key}[/bold] for [cyan]{agent}[/cyan]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] deleted [bold]{key}[/bold] from [cyan]{agent}[/cyan]")


# ---------------------------------------------------------------------------
# Subcommand: evict
# ---------------------------------------------------------------------------


@memory_app.command("evict")
def evict(
    agent: str = typer.Argument(..., help="Agent name."),
    before_days: int = typer.Option(
        0,
        "--before-days",
        help="Drop entries older than N days. Required (use 0 to keep all).",
    ),
    before_iso: str = typer.Option(
        "",
        "--before",
        help="Drop entries created before this ISO-8601 timestamp.",
    ),
    force: bool = typer.Option(False, "--force", help="Required to actually evict."),
) -> None:
    """Bulk-delete memory entries older than a threshold."""
    if not before_iso and before_days <= 0:
        err_console.print(
            "[red]✗[/red] specify [bold]--before-days N[/bold] or [bold]--before ISO[/bold]"
        )
        raise typer.Exit(code=2)
    cutoff = before_iso or (
        (datetime.now(UTC) - timedelta(days=before_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    )
    if not force:
        # Dry-run: count what WOULD be evicted without writing.
        entries = asyncio.run(_store().list(agent))
        stale = [e for e in entries if e.created_at < cutoff]
        console.print(
            f"[yellow]⚠ dry-run:[/yellow] would evict {len(stale)} entry(ies) "
            f"for [cyan]{agent}[/cyan] older than [dim]{cutoff}[/dim]. "
            "Re-run with [bold]--force[/bold]."
        )
        raise typer.Exit(code=1)
    n = asyncio.run(_store().evict_older_than(agent, cutoff))
    console.print(f"[green]✓[/green] evicted {n} entry(ies) from [cyan]{agent}[/cyan]")


# ---------------------------------------------------------------------------
# Subcommand: summarise
# ---------------------------------------------------------------------------


@memory_app.command("summarise")
def summarise(
    agent: str = typer.Argument(..., help="Agent name."),
) -> None:
    """Operator-readable summary of an agent's memory.

    MVP: counts + oldest/newest timestamps + total stored bytes.
    Sprint T+ adds LLM-driven semantic summarisation.
    """
    entries = asyncio.run(_store().list(agent))
    if not entries:
        console.print(f"[yellow]⚠[/yellow] no entries for agent [cyan]{agent}[/cyan]")
        return
    total_bytes = sum(len(json.dumps(e.value)) for e in entries)
    oldest = entries[0].created_at
    newest = entries[-1].created_at
    body = (
        f"[bold]Agent:[/bold]    [cyan]{agent}[/cyan]\n"
        f"[bold]Entries:[/bold]  {len(entries)}\n"
        f"[bold]Bytes:[/bold]    {total_bytes:,} (value payloads only)\n"
        f"[bold]Oldest:[/bold]   {oldest}\n"
        f"[bold]Newest:[/bold]   {newest}"
    )
    console.print(Panel(body, title="Memory summary", title_align="left", border_style="cyan"))


# ---------------------------------------------------------------------------
# Subcommand: query
# ---------------------------------------------------------------------------


@memory_app.command("query")
def query(
    agent: str = typer.Argument(..., help="Agent name."),
    text: str = typer.Argument(..., help="Substring to search for in entry values."),
) -> None:
    """Substring search across an agent's memory values.

    MVP: literal substring match on the JSON-serialized value. Vector
    semantic recall is a deferred backend (Sprint T+ once pgvector /
    Azure AI Search lands).
    """
    entries = asyncio.run(_store().list(agent))
    needle = text.lower()
    hits = [e for e in entries if needle in json.dumps(e.value).lower()]
    if not hits:
        console.print(
            f"[yellow]⚠[/yellow] no entries for [cyan]{agent}[/cyan] match [dim]{text!r}[/dim]"
        )
        return
    table = Table(title=f"{len(hits)} hit(s) for [dim]{text!r}[/dim]", title_style="bold")
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value preview", style="dim")
    table.add_column("Created", style="dim", no_wrap=True)
    for e in hits:
        preview = json.dumps(e.value)
        truncated = (
            preview
            if len(preview) <= _QUERY_PREVIEW_CHARS
            else preview[: _QUERY_PREVIEW_CHARS - 1] + "…"
        )
        table.add_row(e.key, truncated, e.created_at)
    console.print(table)


def _filter_entries_by_age(entries: list, days: int) -> list:
    """Keep entries whose ``created_at`` is within ``days`` of now.

    Permissive: entries with unparseable timestamps survive the filter
    (we don't drop data on a parse glitch). Mirrors the semantics of
    :func:`movate.cli.costs_cmd._filter_by_since`.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(days=days)
    out = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e.created_at.rstrip("Z")).replace(tzinfo=UTC)
        except (ValueError, AttributeError):
            # Keep the entry — unparseable timestamp is a data
            # quality issue, not a reason to drop the row.
            out.append(e)
            continue
        if ts >= cutoff:
            out.append(e)
    return out
