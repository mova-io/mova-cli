"""``mdk explain <run-id>`` — decision chain visualization for a completed run.

Renders the reasoning chain behind a run: input, each LLM call's metrics
(tokens, latency, cost), output, and any error. When step-level tracing is
available (``MOVATE_TRACER=langfuse``) this will reflect richer per-step
data; in the current v0.8 storage schema (no ``trace_steps`` field on
``RunRecord``) it renders the complete picture from what IS persisted.

Exit codes:
    0 — record found and rendered.
    1 — run not found (unknown id / empty storage).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from movate.cli._runtime import build_storage
from movate.core.models import JobStatus, RunRecord

console = Console()
err = Console(stderr=True)

_STEP_TRACER_HINT = (
    "Step-level tracing not available for this run.\n"
    "To capture per-step decisions, configure a tracer: "
    "[bold]MOVATE_TRACER=langfuse[/bold]"
)


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------


def explain(
    run_id: Annotated[
        str | None,
        typer.Argument(
            help="Run ID to explain.  Omit with --last to explain the most-recent run."
        ),
    ] = None,
    last: Annotated[
        bool,
        typer.Option("--last", help="Explain the most-recent run (ignores RUN_ID if both given)."),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of the human view."),
    ] = False,
) -> None:
    """Render the decision chain behind a completed run.

    Shows the input, LLM call metrics (model, tokens, cost, latency), and
    the final output in the order the executor processed them. When the run
    failed, the error is shown instead of an output section.

    Step-level skill traces (which tool was called, what it returned) require
    ``MOVATE_TRACER=langfuse`` at run time; without it a summary note is
    printed.

    Examples::

        mdk explain abc123               # explain run abc123
        mdk explain --last               # explain the most-recent run
        mdk explain abc123 --json        # machine-readable JSON
    """
    import asyncio  # noqa: PLC0415

    asyncio.run(_cmd(run_id=run_id, last=last, as_json=as_json))


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _cmd(*, run_id: str | None, last: bool, as_json: bool) -> None:
    storage = build_storage()
    await storage.init()

    record = await _resolve(storage, run_id=run_id, last=last)
    if record is None:
        err.print("[red]✗[/red] run not found")
        raise typer.Exit(code=1)

    if as_json:
        console.print_json(_to_json(record))
        return

    _render_chain(record)


async def _resolve(storage: Any, *, run_id: str | None, last: bool) -> RunRecord | None:
    if last or run_id is None:
        runs = await storage.list_runs(limit=1)
        return runs[0] if runs else None
    return await storage.get_run(run_id, tenant_id="local")


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------


def _to_json(record: RunRecord) -> str:
    """Machine-readable representation of the decision chain."""
    m = record.metrics
    chain: dict[str, Any] = {
        "run_id": record.run_id,
        "agent": record.agent,
        "agent_version": record.agent_version,
        "status": record.status,
        "input": record.input,
        "llm_call": {
            "model": m.provider,
            "tokens_in": m.tokens.input,
            "tokens_out": m.tokens.output,
            "tokens_cached": m.tokens.cached_input,
            "latency_ms": m.latency_ms,
            "cost_usd": m.cost_usd,
        },
        "output": record.output,
        "error": record.error.model_dump() if record.error else None,
        "step_tracing": "unavailable — configure MOVATE_TRACER=langfuse for per-step traces",
    }
    return json.dumps(chain, indent=2, default=str)


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _status_icon(status: str) -> str:
    if status == JobStatus.SUCCESS:
        return "[green]✓ success[/green]"
    if status == JobStatus.ERROR:
        return "[red]✗ error[/red]"
    if status == JobStatus.SAFETY_BLOCKED:
        return "[red]✗ safety_blocked[/red]"
    if status == JobStatus.DEAD_LETTER:
        return "[red]✗ dead_letter[/red]"
    return f"[yellow]{status}[/yellow]"


def _render_chain(record: RunRecord) -> None:
    """Render the full decision chain for *record* to stdout."""
    m = record.metrics

    # ---- header ----
    header = Text()
    header.append("Run  ", style="dim")
    header.append(record.run_id, style="bold cyan")
    header.append("  ")
    header.append_text(Text.from_markup(_status_icon(record.status)))
    header.append("  ")
    header.append(f"{record.agent} v{record.agent_version}", style="bold")
    console.print(header)
    console.print(Rule(style="dim"))

    # ---- Input ----
    console.print("[bold]Input[/bold]")
    _print_indented_json(record.input)

    # ---- Step 1 — LLM call ----
    console.print()
    console.print("[bold]Step 1 — LLM call[/bold]")
    console.print(f"  [dim]Model:[/dim]   {m.provider or record.provider}")

    if m.tokens.input or m.tokens.output:
        cached_note = f" (cached: {m.tokens.cached_input})" if m.tokens.cached_input else ""
        console.print(
            f"  [dim]Tokens:[/dim]  {m.tokens.input} in "
            f"→ {m.tokens.output} out{cached_note}"
        )

    if m.cost_usd:
        console.print(f"  [dim]Cost:[/dim]    [green]${m.cost_usd:.6f}[/green]")

    if m.latency_ms:
        console.print(f"  [dim]Latency:[/dim] [cyan]{m.latency_ms} ms[/cyan]")

    # ---- Output or Error ----
    if record.output is not None:
        console.print()
        console.print("[bold]Output[/bold]")
        _print_indented_json(record.output)
    elif record.error:
        console.print()
        error = record.error
        console.print(
            f"[red bold]Error[/red bold]  "
            f"[dim]{error.type}[/dim]\n  {error.message}"
        )
        if error.hint:
            console.print(f"  [dim]Hint:[/dim] {error.hint}")

    # ---- Tracer hint ----
    console.print()
    console.print(f"[dim]{_STEP_TRACER_HINT}[/dim]")


def _print_indented_json(data: dict[str, Any]) -> None:
    """Print *data* as pretty JSON with a two-space left indent."""
    raw = json.dumps(data, indent=2, default=str)
    for line in raw.splitlines():
        console.print("  " + line)
