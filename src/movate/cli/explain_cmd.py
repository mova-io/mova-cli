"""``mdk explain <run-id>`` — operator-facing run summarizer (Phase J-2).

Closes the demo arc that started with ``mdk list`` (PR #9, run-id
discovery). Operator picks a run from ``mdk list``, then ``mdk explain
<run-id>`` renders the full RunRecord as a Rich panel:

  $ mdk list                                 # see what's available
  $ mdk explain cccccccc                     # zoom in on a specific run

What renders:
* Header — run_id (short/full), agent + version, status (color-coded), when
* Metrics — latency, tokens, cost, provider, pricing version
* Input — pretty-printed JSON the agent was called with
* Output — pretty-printed JSON the agent produced (success path)
* Error — typed error category + message + optional hint (failure path)
* Workflow context — workflow_run_id + node_id if part of a workflow
* Footer — pointer at ``mdk trace replay`` for Langfuse-level depth
  (guardrail / reflection / retry trace events live there, not in RunRecord)

Lookup-only: this command never re-runs the agent or calls a provider.
Pure local-storage read + render.

Future (J-2 v2): one-paragraph LLM-generated "why" summary behind a
``--summary`` flag. Skipped in v1 to avoid the API-key dependency in
the operator-debugging path — the operator already has the run's data;
the LLM is a convenience, not a requirement.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime

if TYPE_CHECKING:
    from movate.core.models import RunRecord

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Status → Rich color (mirrors mdk list)
# ---------------------------------------------------------------------------

_STATUS_STYLE: dict[str, str] = {
    "success": "green",
    "running": "cyan",
    "queued": "yellow",
    "error": "red",
    "safety_blocked": "magenta",
    "dead_letter": "bright_red",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def explain(
    run_id: str = typer.Argument(
        ...,
        help=(
            "Run id (full UUID or unambiguous 8-char prefix as printed "
            "by ``mdk list``). Use ``mdk list --full-id`` for the full UUID."
        ),
    ),
    full_id: bool = typer.Option(
        False,
        "--full-id",
        help="Show the full UUID in the header instead of the short prefix.",
    ),
    show_raw: bool = typer.Option(
        False,
        "--raw",
        help=("Skip Rich rendering and emit the RunRecord as JSON. Pipe-friendly for scripting."),
    ),
) -> None:
    """Explain a run — render the persisted RunRecord as an operator-friendly panel.

    [bold]Examples:[/bold]

      [dim]# Common flow: list → pick → explain[/dim]
      $ mdk list
      $ mdk explain cccccccc

      [dim]# Full UUID in case of ambiguity[/dim]
      $ mdk explain cccccccc-3333-4444-5555-666666666666 --full-id

      [dim]# Raw JSON for scripting / piping[/dim]
      $ mdk explain cccccccc --raw | jq '.metrics.cost_usd'
    """
    asyncio.run(_run_explain(run_id=run_id, full_id=full_id, show_raw=show_raw))


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _run_explain(*, run_id: str, full_id: bool, show_raw: bool) -> None:
    """Resolve run_id (possibly a prefix) → RunRecord → render panel."""
    runtime = await build_local_runtime(mock=True)
    try:
        record = await _resolve_run(runtime.storage, run_id_or_prefix=run_id)
        if record is None:
            err_console.print(
                f"[red]✗[/red] no run found for {run_id!r}. "
                f"Try [bold]mdk list[/bold] to see available run ids."
            )
            raise typer.Exit(code=1)

        if show_raw:
            console.print_json(json.dumps(record.model_dump(mode="json")))
            return

        _render_panel(record, full_id=full_id)
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


async def _resolve_run(storage: object, *, run_id_or_prefix: str) -> RunRecord | None:
    """Look up a run by full UUID; fall back to prefix-match over recent runs.

    Two paths:

    1. **Exact match** — try ``get_run`` first. If the supplied value
       is a full UUID, this is the cheap O(1) path.
    2. **Prefix match** — if exact fails AND the input is < 36 chars
       (full UUID length), scan the most recent 200 RunRecords for
       a prefix match. Errors out if the prefix is ambiguous (more
       than one match) so the operator can disambiguate by adding
       characters or running with ``--full-id``.

    The 200-record window is a deliberate cap — operators who need to
    look up older runs can pass the full UUID. Scanning the whole
    table on every explain would scale poorly.
    """
    # Path 1 — exact match.
    record = await storage.get_run(  # type: ignore[attr-defined]
        run_id_or_prefix, tenant_id="local"
    )
    if record is not None:
        return record  # type: ignore[no-any-return]

    # Path 2 — prefix match over recent runs (no API key needed).
    # We accept any prefix length down to 4 chars to disambiguate
    # against UUID-shaped IDs without false-matching the first byte.
    min_prefix_for_match = 4
    if len(run_id_or_prefix) < min_prefix_for_match:
        return None

    recents = await storage.list_runs(  # type: ignore[attr-defined]
        tenant_id="local",
        limit=200,
    )
    candidates = [r for r in recents if r.run_id.startswith(run_id_or_prefix)]
    if len(candidates) == 0:
        return None
    if len(candidates) > 1:
        err_console.print(
            f"[red]✗[/red] prefix {run_id_or_prefix!r} is ambiguous — "
            f"matched {len(candidates)} runs. Add more characters or use "
            f"[bold]--full-id[/bold] with the value from [bold]mdk list "
            f"--full-id[/bold]."
        )
        raise typer.Exit(code=2)
    return candidates[0]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_panel(record: RunRecord, *, full_id: bool) -> None:
    """Render the run as a Rich panel with sections per category.

    Sections render in this order so the operator reads them top-down
    in the order they'd think:
      1. **Header** — what / who / when / status (the 60-second answer)
      2. **Metrics** — how much / how long (always interesting)
      3. **Error** (failure path only) — typed category + hint
      4. **Input / Output** — the actual data (most volume; goes last)
      5. **Footer** — pointers to deeper trace surfaces

    Output is one Rich Panel for visual grouping; sub-sections use
    Rich Tables and Syntax-highlighted JSON blocks.
    """
    _render_header(record, full_id=full_id)
    console.print()
    _render_metrics(record)
    if record.error is not None:
        console.print()
        _render_error(record)
    console.print()
    _render_io(record)
    console.print()
    _render_footer(record)


def _render_header(record: RunRecord, *, full_id: bool) -> None:
    status_color = _STATUS_STYLE.get(record.status.value, "white")
    rid = record.run_id if full_id else record.run_id[:8]
    title = (
        f"[bold]run[/bold] [cyan]{rid}[/cyan] · "
        f"[bold]{record.agent}[/bold] v{record.agent_version} · "
        f"[{status_color}]{record.status.value}[/{status_color}] · "
        f"[dim]{_format_when(record.created_at)}[/dim]"
    )
    console.print(Panel(title, title="Run details", title_align="left", border_style="cyan"))


def _render_metrics(record: RunRecord) -> None:
    metrics = record.metrics
    table = Table(title="Metrics", title_style="bold", show_header=False, expand=False)
    table.add_column("field", style="dim")
    table.add_column("value", style="white")
    table.add_row("latency", _fmt_latency(metrics.latency_ms))
    table.add_row(
        "tokens",
        f"in={metrics.tokens.input}, out={metrics.tokens.output}"
        + (f", cached={metrics.tokens.cached_input}" if metrics.tokens.cached_input else ""),
    )
    table.add_row("cost", _fmt_cost(metrics.cost_usd))
    table.add_row("provider", record.provider or "—")
    table.add_row("pricing version", metrics.pricing_version or record.pricing_version or "—")
    if record.workflow_run_id:
        table.add_row("workflow run", record.workflow_run_id)
    if record.node_id:
        table.add_row("workflow node", record.node_id)
    console.print(table)


def _render_error(record: RunRecord) -> None:
    """Render the typed error block. Only fired when ``record.error`` is set.

    The hint field is rendered separately + dimmed so an operator
    sees the actionable remediation pointer at a glance.
    """
    err = record.error
    if err is None:
        return  # mypy convenience; caller already guards.
    panel_body = f"[bold red]{err.type}[/bold red]\n{err.message}"
    if err.hint:
        panel_body += f"\n\n[dim]hint: {err.hint}[/dim]"
    if err.retryable:
        panel_body += "\n\n[dim](this error is in the retryable category)[/dim]"
    console.print(
        Panel(
            panel_body,
            title="[red]Error[/red]",
            title_align="left",
            border_style="red",
        )
    )


def _render_io(record: RunRecord) -> None:
    """Render input + (if success) output as syntax-highlighted JSON."""
    console.print("[bold]Input:[/bold]")
    console.print(
        Syntax(
            json.dumps(record.input, indent=2),
            "json",
            theme="monokai",
            line_numbers=False,
            background_color="default",
        )
    )
    if record.output is not None:
        console.print()
        console.print("[bold]Output:[/bold]")
        console.print(
            Syntax(
                json.dumps(record.output, indent=2),
                "json",
                theme="monokai",
                line_numbers=False,
                background_color="default",
            )
        )


def _render_footer(record: RunRecord) -> None:
    """Pointers to deeper trace surfaces (Langfuse / mdk trace replay).

    RunRecord doesn't carry the full tracer event stream (guardrail
    verdicts, reflection iterations, retry/fallback timeline) — those
    live in the OTel/Langfuse trace. We point the operator there
    instead of duplicating the data.
    """
    console.print("[dim]For full trace (guardrails, reflection, retries):[/dim]")
    console.print(
        f"  [cyan]mdk trace replay {record.run_id[:8]}[/cyan]   [dim]# rich timeline view[/dim]"
    )
    console.print(
        f"  [dim]Langfuse:[/dim] [cyan]{record.run_id}[/cyan] "
        f"[dim](if MDK_LANGFUSE_HOST configured)[/dim]"
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def _format_when(when: datetime) -> str:
    """Human-friendly timestamp — relative for recent, absolute for older.

    Mirrors :func:`movate.cli.list_cmd._format_when` so ``mdk list`` and
    ``mdk explain`` agree on time display. Different module to avoid a
    cross-CLI import.
    """
    now = datetime.now(UTC)
    delta = (now - when).total_seconds()
    if delta < _SECONDS_PER_MINUTE:
        return f"{int(delta)}s ago"
    if delta < _SECONDS_PER_HOUR:
        return f"{int(delta // _SECONDS_PER_MINUTE)}m ago"
    if delta < _SECONDS_PER_DAY:
        return f"{int(delta // _SECONDS_PER_HOUR)}h ago"
    return when.strftime("%Y-%m-%d %H:%M")


def _fmt_latency(latency_ms: int | None) -> str:
    if latency_ms is None:
        return "—"
    seconds_threshold_ms = 1000
    if latency_ms < seconds_threshold_ms:
        return f"{latency_ms} ms"
    return f"{latency_ms / 1000:.2f} s ({latency_ms} ms)"


# Sub-cent threshold — under 1¢ we render six-digit precision so a
# run that cost $0.000234 doesn't display as ``$0.0000`` after
# rounding. Above the threshold, four digits is plenty.
_SUB_CENT_COST_THRESHOLD = 0.01


def _fmt_cost(cost_usd: float | None) -> str:
    if cost_usd is None:
        return "—"
    if cost_usd < _SUB_CENT_COST_THRESHOLD:
        return f"${cost_usd:.6f}"
    return f"${cost_usd:.4f}"
