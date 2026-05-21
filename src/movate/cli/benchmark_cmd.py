"""``mdk benchmark live <agent>`` — shadow-traffic benchmark (Sprint S).

Replays recent RunRecords against the agent with a *candidate* model
override and compares the candidate's output / cost / latency vs the
recorded original. Operators use this to answer "would gpt-5-mini
have produced the same answers cheaper?" without exposing real users
to the candidate.

[bold]Shadow, not live.[/bold] We never route prod traffic to the
candidate. We replay PAST traffic (or a curated subset of recorded
runs) against the candidate and let the operator read the diff.
The "live" in the name refers to using REAL recorded inputs — not
synthetic — not to teeing live traffic.

Usage::

  mdk benchmark live triage --candidate-model openai/gpt-4o-mini-2024-07-18
  mdk benchmark live triage --candidate-model anthropic/claude-haiku-4-5-20251001 --limit 50
  mdk benchmark live triage --candidate-model X --since-days 7 --json
  mdk benchmark live triage --candidate-model X --mock          # tests / hermetic

[bold]MVP scope:[/bold] sequential replay (no parallel candidate
calls), single-candidate comparison (one model at a time), no
LLM-as-judge grading on the diff. Operators read the side-by-side
output. Sprint S+ can add parallel + multi-candidate + judge.

What we DON'T do:

* No live traffic tee — we never call the candidate on a real user
  request. Sprint S+ might add an opt-in tee if there's demand.
* No prompt change — only the model.provider gets overridden. Tune
  is for prompt sweeps.
* No persistence by default — the candidate's outputs are NOT saved
  to RunRecord. ``--persist`` opts in (same flag-reservation as
  ``mdk tune --persist``).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.cli.tune_cmd import _override_bundle
from movate.core.loader import AgentLoadError, load_agent
from movate.core.models import RunRecord, RunRequest

console = Console()
err_console = Console(stderr=True)


_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200


def _resolve_agent_path(name_or_path: str, project_root: Path) -> Path:
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "agent.yaml").is_file():
        return candidate.resolve()
    by_name = project_root / "agents" / name_or_path
    if by_name.is_dir() and (by_name / "agent.yaml").is_file():
        return by_name.resolve()
    err_console.print(f"[red]✗[/red] agent not found: [bold]{name_or_path}[/bold]")
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayRow:
    """One past-run vs candidate-output comparison."""

    run_id: str
    original_provider: str
    original_cost: float
    original_latency_ms: int
    candidate_cost: float
    candidate_latency_ms: int
    outputs_match: bool
    error: str = ""


def _outputs_equal(a: Any, b: Any) -> bool:
    """Whitespace-insensitive comparison of two run outputs."""
    return json.dumps(a or {}, sort_keys=True) == json.dumps(b or {}, sort_keys=True)


async def _replay_run(
    rt: Any,
    candidate_bundle: Any,
    run: RunRecord,
) -> ReplayRow:
    """Re-execute ``run.input`` under the candidate bundle."""
    request = RunRequest(agent=candidate_bundle.spec.name, input=run.input)
    try:
        response = await rt.executor.execute(candidate_bundle, request)
    except Exception as exc:
        return ReplayRow(
            run_id=run.run_id,
            original_provider=run.provider,
            original_cost=run.metrics.cost_usd,
            original_latency_ms=run.metrics.latency_ms,
            candidate_cost=0.0,
            candidate_latency_ms=0,
            outputs_match=False,
            error=str(exc),
        )
    return ReplayRow(
        run_id=run.run_id,
        original_provider=run.provider,
        original_cost=run.metrics.cost_usd,
        original_latency_ms=run.metrics.latency_ms,
        candidate_cost=response.metrics.cost_usd,
        candidate_latency_ms=response.metrics.latency_ms,
        outputs_match=_outputs_equal(run.output, response.data),
    )


# ---------------------------------------------------------------------------
# Rendering + aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkSummary:
    """Roll-up across all replayed rows."""

    rows: tuple[ReplayRow, ...]
    n: int
    match_rate: float
    mean_original_cost: float
    mean_candidate_cost: float
    mean_original_latency: float
    mean_candidate_latency: float
    errors: int


def summarize(rows: list[ReplayRow]) -> BenchmarkSummary:
    """Pure aggregation — used by both the Rich table + --json output."""
    if not rows:
        return BenchmarkSummary(
            rows=(),
            n=0,
            match_rate=0.0,
            mean_original_cost=0.0,
            mean_candidate_cost=0.0,
            mean_original_latency=0.0,
            mean_candidate_latency=0.0,
            errors=0,
        )
    error_count = sum(1 for r in rows if r.error)
    successful = [r for r in rows if not r.error]
    matches = sum(1 for r in successful if r.outputs_match)
    n_ok = len(successful) or 1  # avoid div-by-zero in pure-error sets
    return BenchmarkSummary(
        rows=tuple(rows),
        n=len(rows),
        match_rate=matches / n_ok if successful else 0.0,
        mean_original_cost=statistics.fmean(r.original_cost for r in successful)
        if successful
        else 0.0,
        mean_candidate_cost=statistics.fmean(r.candidate_cost for r in successful)
        if successful
        else 0.0,
        mean_original_latency=statistics.fmean(r.original_latency_ms for r in successful)
        if successful
        else 0.0,
        mean_candidate_latency=statistics.fmean(r.candidate_latency_ms for r in successful)
        if successful
        else 0.0,
        errors=error_count,
    )


def _render_summary(s: BenchmarkSummary, *, candidate: str) -> None:
    table = Table(title="Shadow benchmark", title_style="bold")
    table.add_column("Metric", style="dim", no_wrap=True)
    table.add_column("Original", justify="right", no_wrap=True)
    table.add_column("Candidate", justify="right", style="cyan", no_wrap=True)
    table.add_column("Δ", justify="right", no_wrap=True)

    def _delta(cur: float, new: float) -> str:
        if cur == 0:
            return "—"
        pct = (new - cur) / cur * 100
        color = "green" if pct < 0 else ("red" if pct > 0 else "")
        sign = "+" if pct > 0 else ""
        cell = f"{sign}{pct:.1f}%"
        return f"[{color}]{cell}[/{color}]" if color else cell

    table.add_row(
        "mean cost ($)",
        f"{s.mean_original_cost:.6f}",
        f"{s.mean_candidate_cost:.6f}",
        _delta(s.mean_original_cost, s.mean_candidate_cost),
    )
    table.add_row(
        "mean latency (ms)",
        f"{s.mean_original_latency:.0f}",
        f"{s.mean_candidate_latency:.0f}",
        _delta(s.mean_original_latency, s.mean_candidate_latency),
    )
    table.add_row(
        "output match rate",
        "—",
        f"{s.match_rate:.0%}",
        "",
    )
    table.add_row("errors", "—", str(s.errors), "")
    console.print(table)

    # Per-row table only when run count is small — at 50+ rows the
    # summary metrics are the actionable signal; the per-row table
    # becomes wall noise.
    detail_threshold = 15
    if s.n <= detail_threshold:
        detail = Table(title=f"Per-run vs {candidate}", show_lines=False)
        detail.add_column("Run", style="cyan", no_wrap=True)
        detail.add_column("Match", no_wrap=True)
        detail.add_column("Cost $ Δ", justify="right", no_wrap=True)
        detail.add_column("Latency Δ", justify="right", no_wrap=True)
        detail.add_column("Notes", style="dim")
        for r in s.rows:
            match_cell = "[green]✓[/green]" if r.outputs_match else "[red]✗[/red]"
            cost_delta = r.candidate_cost - r.original_cost
            lat_delta = r.candidate_latency_ms - r.original_latency_ms
            detail.add_row(
                r.run_id[:8],
                match_cell if not r.error else "[red]err[/red]",
                f"{cost_delta:+.6f}" if not r.error else "—",
                f"{lat_delta:+}" if not r.error else "—",
                r.error or "",
            )
        console.print(detail)


# ---------------------------------------------------------------------------
# Storage fetch
# ---------------------------------------------------------------------------


async def _fetch_runs(
    *,
    agent: str,
    tenant_id: str,
    limit: int,
    since_days: int,
) -> list[RunRecord]:
    """Pull recent successful runs from storage."""
    from movate.storage import build_storage  # noqa: PLC0415

    storage = build_storage()
    await storage.init()
    try:
        runs = await storage.list_runs(
            agent=agent,
            tenant_id=tenant_id or None,
            status="success",
            limit=limit,
        )
    finally:
        await storage.close()
    if since_days > 0:
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        runs = [
            r
            for r in runs
            if r.created_at
            and (r.created_at.replace(tzinfo=UTC) if r.created_at.tzinfo is None else r.created_at)
            >= cutoff
        ]
    return runs


# ---------------------------------------------------------------------------
# Command (registered as `mdk benchmark live` via sub-app)
# ---------------------------------------------------------------------------


benchmark_app = typer.Typer(
    name="benchmark",
    help=(
        "Shadow-replay past runs against a candidate model. "
        "[bold]mdk benchmark live[/bold] is the MVP."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@benchmark_app.command("live")
def benchmark_live(
    name: str = typer.Argument(..., help="Agent name or path.", metavar="AGENT"),
    candidate_model: str = typer.Option(
        ...,
        "--candidate-model",
        help=(
            "LiteLLM-style model string to test against the recorded runs "
            "(e.g. [dim]openai/gpt-4o-mini-2024-07-18[/dim])."
        ),
    ),
    limit: int = typer.Option(
        _DEFAULT_LIMIT,
        "--limit",
        help=f"How many recent successful runs to replay. Max {_MAX_LIMIT}.",
    ),
    since_days: int = typer.Option(
        0,
        "--since-days",
        help="Only replay runs from the last N days. 0 = no time filter.",
    ),
    tenant_id: str = typer.Option(
        "",
        "--tenant-id",
        help="Tenant scope for the storage lookup.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Use MockProvider for the candidate — hermetic CI / offline.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the summary + per-row results as JSON.",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        hidden=True,
    ),
) -> None:
    """Shadow-replay past runs against a candidate model.

    Pulls recent successful runs from storage, re-executes each with
    [bold]--candidate-model[/bold] overriding the agent's
    ``model.provider``, and renders a side-by-side comparison.

    [bold]No production impact:[/bold] we never call the candidate on
    live traffic — only on RunRecords already persisted.

    [bold]Examples:[/bold]

      [dim]$ mdk benchmark live triage \\
          --candidate-model anthropic/claude-haiku-4-5-20251001[/dim]
      [dim]$ mdk benchmark live triage --candidate-model X --limit 50 \\
          --since-days 7[/dim]
      [dim]$ mdk benchmark live triage --candidate-model X --mock[/dim]
    """
    if limit < 1 or limit > _MAX_LIMIT:
        err_console.print(f"[red]✗[/red] --limit must be in [1, {_MAX_LIMIT}]; got {limit}")
        raise typer.Exit(code=2)

    root = Path(project_root).resolve()
    agent_path = _resolve_agent_path(name, root)
    try:
        bundle = load_agent(agent_path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Override the model on a copy — same machinery as mdk tune.
    candidate_bundle = _override_bundle(bundle, "model", candidate_model)

    runs = asyncio.run(
        _fetch_runs(
            agent=bundle.spec.name,
            tenant_id=tenant_id,
            limit=limit,
            since_days=since_days,
        )
    )
    if not runs:
        console.print(
            f"[yellow]⚠[/yellow] no recorded successful runs for agent "
            f"[bold]{bundle.spec.name}[/bold]. "
            "[dim]Run the agent a few times first; benchmark needs RunRecords to replay.[/dim]"
        )
        return

    # Suppress the "Replaying…" header in JSON mode — it would
    # corrupt the parseable output.
    if not json_output:
        console.print(
            f"[dim]Replaying {len(runs)} run(s) against "
            f"[bold]{candidate_model}[/bold]{' (mock)' if mock else ''}…[/dim]"
        )

    rows = asyncio.run(
        _replay_all(
            candidate_bundle=candidate_bundle,
            runs=runs,
            mock=mock,
            show_progress=not json_output and sys.stderr.isatty(),
        )
    )
    summary = summarize(rows)

    if json_output:
        payload = {
            "candidate_model": candidate_model,
            "n": summary.n,
            "match_rate": summary.match_rate,
            "mean_original_cost": summary.mean_original_cost,
            "mean_candidate_cost": summary.mean_candidate_cost,
            "mean_original_latency": summary.mean_original_latency,
            "mean_candidate_latency": summary.mean_candidate_latency,
            "errors": summary.errors,
            "rows": [
                {
                    "run_id": r.run_id,
                    "original_provider": r.original_provider,
                    "original_cost": r.original_cost,
                    "candidate_cost": r.candidate_cost,
                    "original_latency_ms": r.original_latency_ms,
                    "candidate_latency_ms": r.candidate_latency_ms,
                    "outputs_match": r.outputs_match,
                    "error": r.error,
                }
                for r in summary.rows
            ],
        }
        console.print_json(json.dumps(payload, default=str))
        return

    _render_summary(summary, candidate=candidate_model)

    # Surface notable provider-distribution hint when original runs
    # weren't all from one provider — helps operators interpret the
    # cost-delta in mixed-provider deployments.
    original_providers = Counter(r.original_provider for r in summary.rows)
    if len(original_providers) > 1:
        console.print(
            f"\n[dim]original runs span multiple providers: {dict(original_providers)}[/dim]"
        )


async def _replay_all(
    *,
    candidate_bundle: Any,
    runs: list[RunRecord],
    mock: bool,
    show_progress: bool = False,
) -> list[ReplayRow]:
    """Drive sequential replay under one runtime.

    ``show_progress`` renders a Rich Progress bar to stderr — suppressed
    in JSON mode and non-TTY environments so CI log captures stay clean.
    """
    rt = await build_local_runtime(mock=mock)
    rows: list[ReplayRow] = []
    try:
        if show_progress:
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=err_console,
                transient=True,
            ) as progress:
                task = progress.add_task("[dim]Replaying…[/dim]", total=len(runs))
                for run in runs:
                    rows.append(await _replay_run(rt, candidate_bundle, run))
                    progress.advance(task)
        else:
            rows = [await _replay_run(rt, candidate_bundle, r) for r in runs]
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    return rows
