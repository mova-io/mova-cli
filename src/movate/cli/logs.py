"""``mdk logs <run-id>`` — inspect a run record and its metrics.

Without ``--tail``, prints the stored record and exits.  With
``--tail``, polls until the run reaches a terminal status (completed,
failed, dead_letter) and streams each status transition live.

No ``run_id`` + ``--last`` prints the most-recent run across all agents.
``--agent <name>`` scopes to one agent.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from movate.cli._runtime import build_storage
from movate.core.models import JobStatus, RunRecord

console = Console()
err = Console(stderr=True)

_TERMINAL: frozenset[str] = frozenset(
    {JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.SAFETY_BLOCKED, JobStatus.DEAD_LETTER}
)
_POLL_INTERVAL_S = 1.5
_TAIL_TIMEOUT_S = 300


def logs(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID to inspect.  Omit with --last to show the most-recent run."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", "-a", help="Filter --last by agent name."),
    ] = None,
    last: Annotated[
        bool,
        typer.Option("--last", help="Show the most-recent run (ignores RUN_ID if both given)."),
    ] = False,
    tail: Annotated[
        bool,
        typer.Option("--tail", "-f", help="Poll until the run reaches a terminal status."),
    ] = False,
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Print the raw RunRecord JSON and exit."),
    ] = False,
) -> None:
    """Inspect a run record: input, output, cost, latency, errors.

    Examples::

        mdk logs abc123               # show run abc123
        mdk logs --last               # show the most-recent run
        mdk logs --last --agent faq   # most-recent faq run
        mdk logs abc123 --tail        # poll until terminal
        mdk logs abc123 --raw         # raw JSON record
    """
    asyncio.run(_cmd(run_id=run_id, agent=agent, last=last, tail=tail, raw=raw))


async def _cmd(
    *,
    run_id: str | None,
    agent: str | None,
    last: bool,
    tail: bool,
    raw: bool,
) -> None:
    storage = build_storage()
    await storage.init()

    record = await _resolve(storage, run_id=run_id, agent=agent, last=last)
    if record is None:
        err.print("[red]✗[/red] run not found")
        raise typer.Exit(code=1)

    if raw:
        console.print_json(record.model_dump_json())
        return

    _render(record)

    if tail and record.status not in _TERMINAL:
        await _poll(storage, run_id=record.run_id)


async def _resolve(
    storage,
    *,
    run_id: str | None,
    agent: str | None,
    last: bool,
) -> RunRecord | None:
    if last or run_id is None:
        runs = await storage.list_runs(agent=agent, limit=1)
        return runs[0] if runs else None
    return await storage.get_run(run_id, tenant_id="local")


async def _poll(storage, *, run_id: str) -> None:
    deadline = time.monotonic() + _TAIL_TIMEOUT_S
    last_status: str | None = None
    console.print("[dim]watching for terminal status…[/dim]")
    while time.monotonic() < deadline:
        record = await storage.get_run(run_id, tenant_id="local")
        if record is None:
            break
        if record.status != last_status:
            last_status = record.status
            _render_status_line(record)
        if record.status in _TERMINAL:
            _render(record)
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    err.print(f"[yellow]⚠[/yellow] timed out after {_TAIL_TIMEOUT_S}s — run still in progress")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _status_color(status: str) -> str:
    if status == JobStatus.SUCCESS:
        return "green"
    if status in {JobStatus.ERROR, JobStatus.SAFETY_BLOCKED, JobStatus.DEAD_LETTER}:
        return "red"
    return "yellow"


def _render_status_line(record: RunRecord) -> None:
    color = _status_color(record.status)
    console.print(f"  [{color}]{record.status}[/{color}]")


def _render(record: RunRecord) -> None:
    color = _status_color(record.status)

    # ---- header ----
    status_badge = Text(f" {record.status} ", style=f"bold white on {color}")
    header = Text()
    header.append(record.run_id, style="bold")
    header.append("  ")
    header.append_text(status_badge)
    console.print(Panel(header, expand=False, padding=(0, 1)))

    # ---- meta table ----
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", no_wrap=True)
    meta.add_column()
    meta.add_row("agent", f"[bold]{record.agent}[/bold]  [dim]v{record.agent_version}[/dim]")
    meta.add_row("provider", record.provider)
    meta.add_row("created", str(record.created_at.replace(microsecond=0)))
    if record.workflow_run_id:
        meta.add_row("workflow", record.workflow_run_id)
        if record.node_id:
            meta.add_row("node", record.node_id)
    console.print(meta)

    # ---- metrics ----
    m = record.metrics
    parts: list[str] = [f"[cyan]{m.latency_ms} ms[/cyan]"]
    if m.cost_usd:
        parts.append(f"[green]${m.cost_usd:.5f}[/green]")
    if m.tokens.input or m.tokens.output:
        cached = f" +{m.tokens.cached_input}c" if m.tokens.cached_input else ""
        parts.append(f"[dim]{m.tokens.input}{cached}→{m.tokens.output} tok[/dim]")
    console.print("  " + "  ".join(parts))

    # ---- input ----
    console.print()
    console.print("[bold]Input[/bold]")
    console.print_json(json.dumps(record.input))

    # ---- output ----
    if record.output is not None:
        console.print()
        console.print("[bold]Output[/bold]")
        console.print_json(json.dumps(record.output))

    # ---- error ----
    if record.error:
        console.print()
        console.print(
            f"[red bold]Error[/red bold]  [dim]{record.error.type}[/dim]\n{record.error.message}"
        )
