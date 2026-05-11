"""``movate trace`` — inspect and replay traces from local storage.

Subcommands:

* ``movate trace replay <id>`` — given a run_id or workflow_run_id, render
  the full timeline (input, output, metrics, error, per-node summary).
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._output import TableJson
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.models import RunRecord, WorkflowRunRecord
from movate.core.replay import (
    Replay,
    ReplayNotFoundError,
    load_replay,
    render_replay_json,
    truncate,
)

trace_app = typer.Typer(
    name="trace",
    help="Inspect and replay traces.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()
err_console = Console(stderr=True)


@trace_app.command("replay")
def replay(
    run_id: str = typer.Argument(..., help="Run id or workflow_run_id."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show full input/output bodies (no truncation)."
    ),
) -> None:
    """Replay a run or workflow_run from local storage.

    [bold]Examples:[/bold]

      [dim]# Replay an agent run[/dim]
      $ movate trace replay 09c20552-d4d6-4a7b-b5e7-729e6324e0aa

      [dim]# Replay a workflow + see full per-node JSON bodies[/dim]
      $ movate trace replay <workflow_run_id> -v

      [dim]# Pipe-friendly[/dim]
      $ movate trace replay <id> -o json | jq .
    """
    asyncio.run(_run_replay(run_id, output_format=output_format, verbose=verbose))


async def _run_replay(run_id: str, *, output_format: TableJson, verbose: bool) -> None:
    rt = await build_local_runtime(mock=True)  # mock=True: no API keys needed
    try:
        try:
            replay_data = await load_replay(rt.storage, run_id)
        except ReplayNotFoundError as exc:
            err_console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=1) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == TableJson.JSON:
        print(render_replay_json(replay_data))
        return

    if replay_data.kind == "agent":
        _render_agent(replay_data, verbose=verbose)
    else:
        _render_workflow(replay_data, verbose=verbose)


# ---------------------------------------------------------------------------
# Renderers (Rich)
# ---------------------------------------------------------------------------


def _render_agent(replay_data: Replay, *, verbose: bool) -> None:
    assert replay_data.run is not None
    r = replay_data.run

    head = Table(title=f"trace replay — run {r.run_id[:8]}…", show_header=False)
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("kind", "agent")
    head.add_row("agent", f"{r.agent} v{r.agent_version}")
    head.add_row("status", _status_badge(r.status.value))
    head.add_row("provider", r.provider)
    head.add_row("latency", f"{r.metrics.latency_ms} ms")
    head.add_row("cost", f"${r.metrics.cost_usd:.6f}")
    head.add_row(
        "tokens",
        f"in={r.metrics.tokens.input} out={r.metrics.tokens.output} "
        f"cached={r.metrics.tokens.cached_input}",
    )
    head.add_row("prompt_hash", r.prompt_hash[:16] + "…" if r.prompt_hash else "—")
    head.add_row("created_at", r.created_at.isoformat())
    head.add_row("run_id", r.run_id)
    if r.workflow_run_id:
        head.add_row("workflow_run_id", r.workflow_run_id)
        head.add_row("node_id", r.node_id or "—")
    console.print(head)

    if r.error:
        err = Table(title="Error", show_header=False)
        err.add_column("field", style="dim")
        err.add_column("value")
        err.add_row("type", r.error.type)
        err.add_row("message", r.error.message)
        err.add_row("retryable", str(r.error.retryable))
        console.print(err)

    _print_body("input", r.input, verbose=verbose)
    _print_body("output", r.output, verbose=verbose)


def _render_workflow(replay_data: Replay, *, verbose: bool) -> None:
    assert replay_data.workflow is not None
    w = replay_data.workflow
    children = replay_data.children or []

    head = Table(
        title=f"trace replay — workflow {w.workflow_run_id[:8]}…",
        show_header=False,
    )
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("kind", "workflow")
    head.add_row("workflow", f"{w.workflow} v{w.workflow_version}")
    head.add_row("status", _status_badge(w.status.value))
    head.add_row("nodes recorded", str(len(children)))
    head.add_row("total cost", f"${replay_data.total_cost_usd:.6f}")
    head.add_row("total latency (sum)", f"{replay_data.total_latency_ms} ms")
    if w.error_node_id:
        head.add_row("error_node_id", w.error_node_id)
    head.add_row("created_at", w.created_at.isoformat())
    head.add_row("workflow_run_id", w.workflow_run_id)
    console.print(head)

    if w.error:
        err = Table(title="Error", show_header=False)
        err.add_column("field", style="dim")
        err.add_column("value")
        err.add_row("type", w.error.type)
        err.add_row("message", w.error.message)
        console.print(err)

    if children:
        nodes = Table(title="Nodes (chronological)", show_header=True, header_style="bold")
        nodes.add_column("#", style="dim", width=3)
        nodes.add_column("node")
        nodes.add_column("agent")
        nodes.add_column("status")
        nodes.add_column("ms")
        nodes.add_column("cost")
        nodes.add_column("output (head)", overflow="fold")
        for i, r in enumerate(children, start=1):
            nodes.add_row(
                str(i),
                r.node_id or "?",
                r.agent,
                r.status.value,
                str(r.metrics.latency_ms),
                f"${r.metrics.cost_usd:.6f}",
                truncate(r.output, max_chars=80) if r.output else "—",
            )
        console.print(nodes)

    # State diff: simple side-by-side; users get the JSON when they want a
    # real diff via `-o json | jq` or similar.
    _print_body("initial_state", w.initial_state, verbose=verbose)
    _print_body("final_state", w.final_state, verbose=verbose)

    if verbose and children:
        for i, r in enumerate(children, start=1):
            console.print(f"\n[dim]── node {i}: {r.node_id} ──[/dim]")
            _print_body("input", r.input, verbose=True)
            _print_body("output", r.output, verbose=True)
            if r.error:
                console.print(f"[red]error:[/red] {r.error.type}: {r.error.message}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _status_badge(status: str) -> str:
    if status == "success":
        return "[green]✓ SUCCESS[/green]"
    if status == "safety_blocked":
        return "[yellow]⚠ SAFETY_BLOCKED[/yellow]"
    return f"[red]✗ {status.upper()}[/red]"


def _print_body(label: str, value: object, *, verbose: bool) -> None:
    """Pretty-print a dict body either truncated or full."""
    console.print(f"\n[bold]{label}[/bold]")
    if value is None:
        console.print("  [dim]—[/dim]")
        return
    if verbose:
        console.print(json.dumps(value, indent=2, default=str))
    else:
        console.print(f"  {truncate(value, max_chars=300)}")


# References kept for static analysis — these are imported types we don't use
# inline but expose to help readers grok the function shape.
_TYPES = (RunRecord, WorkflowRunRecord)
