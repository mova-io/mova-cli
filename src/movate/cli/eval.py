"""``movate eval <agent>`` — score an agent against its dataset and gate on a threshold.

``--baseline <eval-id>`` opts into the regression-detection loop: the
current eval is diffed against the persisted baseline and the CLI exits
non-zero if mean_score or pass_rate dropped past ``--regression-tolerance``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._output import Report
from movate.cli._progress import progress_bar
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.baseline import BaselineDiff, compute_baseline_diff, format_delta
from movate.core.eval import CaseSummary, EvalConfigError, EvalEngine, EvalSummary
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import EvalRecord
from movate.core.reporters import render_eval_markdown
from movate.storage.base import StorageProvider

console = Console()
err_console = Console(stderr=True)


def eval_(
    path: Path = typer.Argument(..., help="Path to agent directory."),
    gate: float = typer.Option(0.7, "--gate", help="Per-case score required to pass (0.0-1.0)."),
    gate_mode: str = typer.Option(
        "mean",
        "--gate-mode",
        help="How to aggregate N runs per case into a single score: mean | min | p10.",
    ),
    runs: int = typer.Option(
        1,
        "--runs",
        "-r",
        help="Runs per case. Use 3+ for LLM-as-judge to defeat sampling variance.",
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic MockProvider (no API keys)."
    ),
    baseline: str = typer.Option(
        None,
        "--baseline",
        help="Eval id of a stored EvalRecord to diff against. CLI exits 1 on regression.",
    ),
    baseline_file: Path = typer.Option(
        None,
        "--baseline-file",
        help=(
            "Path to a JSON-serialized EvalRecord. CI-friendly alternative to --baseline "
            "since CI runners have ephemeral sqlite. Mutually exclusive with --baseline."
        ),
    ),
    output_baseline: Path = typer.Option(
        None,
        "--output-baseline",
        help=(
            "Write the current EvalRecord to this path as JSON. Use on main-branch "
            "merges to refresh the committed baseline; CI then diffs PRs against it."
        ),
    ),
    regression_tolerance: float = typer.Option(
        0.0,
        "--regression-tolerance",
        help="Allowable score drop vs baseline before flagging a regression (0.0-1.0).",
    ),
    output_format: Report = typer.Option(Report.TABLE, "--output", "-o", case_sensitive=False),
) -> None:
    """Run the eval suite for an agent and gate on a threshold.

    [bold]Examples:[/bold]

      [dim]# Exact-match scoring against the dataset[/dim]
      $ movate eval ./faq-agent --gate 0.7

      [dim]# Compare against a stored (sqlite) baseline by id[/dim]
      $ movate eval ./faq-agent --baseline 4f8a-... --regression-tolerance 0.05

      [dim]# CI flow: gate against a git-tracked baseline file[/dim]
      $ movate eval ./faq-agent --mock --baseline-file .movate/baseline.json

      [dim]# Refresh the committed baseline on main-branch merge[/dim]
      $ movate eval ./faq-agent --mock --output-baseline .movate/baseline.json

      [dim]# Hermetic CI run (no API keys needed)[/dim]
      $ movate eval ./faq-agent --mock
    """
    if baseline is not None and baseline_file is not None:
        err_console.print("[red]✗[/red] --baseline and --baseline-file are mutually exclusive")
        raise typer.Exit(code=2)

    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    asyncio.run(
        _run_eval(
            bundle,
            gate=gate,
            gate_mode=gate_mode,
            runs=runs,
            mock=mock,
            baseline_id=baseline,
            baseline_file=baseline_file,
            output_baseline=output_baseline,
            regression_tolerance=regression_tolerance,
            output_format=output_format,
        )
    )


async def _run_eval(
    bundle: AgentBundle,
    *,
    gate: float,
    gate_mode: str,
    runs: int,
    mock: bool,
    baseline_id: str | None = None,
    baseline_file: Path | None = None,
    output_baseline: Path | None = None,
    regression_tolerance: float = 0.0,
    output_format: Report = Report.TABLE,
) -> None:
    rt = await build_local_runtime(mock=mock)
    baseline_record: EvalRecord | None = None
    try:
        # Progress UI is on for human-facing output (table); off for
        # machine-readable formats so JSON / Markdown stay clean if a
        # user accidentally redirects stderr too. Mock mode is fast
        # enough that progress just adds noise — also off.
        show_progress = output_format == Report.TABLE and not mock

        with _maybe_eval_progress(show_progress) as on_case:
            engine = EvalEngine(
                executor=rt.executor,
                provider=rt.provider,
                runs_per_case=runs,
                gate_mode=gate_mode,
                on_case_complete=on_case,
            )
            try:
                summary = await engine.run(bundle)
            except EvalConfigError as exc:
                err_console.print(f"[red]✗ eval config error:[/red] {exc}")
                raise typer.Exit(code=2) from None

        record = summary.to_record()
        await rt.storage.save_eval(record)
        baseline_record = await _resolve_storage_baseline(rt.storage, baseline_id)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    # Resolve a file-based baseline outside the storage block — pure I/O,
    # no runtime needed. (`baseline_id` and `baseline_file` are mutually
    # exclusive at the CLI entry point so only one branch fires.)
    if baseline_file is not None:
        baseline_record = _resolve_file_baseline(baseline_file)

    # Write the current run's EvalRecord to disk if requested. Done after
    # storage is closed so a write failure can't corrupt the DB.
    if output_baseline is not None:
        _write_baseline_file(output_baseline, record)

    # Apply CLI gate (overrides judge.threshold for "case passes").
    cases_passing = sum(1 for c in summary.cases if c.aggregated_score >= gate)
    overall_pass = summary.sample_count > 0 and cases_passing == summary.sample_count

    diff: BaselineDiff | None = None
    if baseline_record is not None:
        diff = compute_baseline_diff(baseline_record, record)

    if output_format == Report.JSON:
        _emit_json(
            summary,
            record=record,
            gate=gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
            diff=diff,
            regression_tolerance=regression_tolerance,
        )
    elif output_format == Report.MARKDOWN:
        print(render_eval_markdown(summary, gate=gate))
    else:
        _emit_table(
            summary,
            record=record,
            gate=gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
        )
        if diff is not None:
            _emit_diff_table(diff, regression_tolerance=regression_tolerance)

    # Exit codes: gate failure OR baseline regression both fail the CLI.
    failed_gate = not overall_pass
    failed_regression = diff is not None and diff.is_regression(tolerance=regression_tolerance)
    if failed_gate or failed_regression:
        raise typer.Exit(code=1)


def _emit_table(
    summary: EvalSummary,
    *,
    record: EvalRecord,
    gate: float,
    cases_passing: int,
    overall_pass: bool,
) -> None:
    head = Table(
        title=f"{summary.agent} v{summary.agent_version} — eval results",
        show_header=False,
    )
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("eval_id", record.eval_id)
    head.add_row("judge", f"{summary.judge.method.value}")
    if summary.judge_provider:
        head.add_row("judge.provider", summary.judge_provider)
    head.add_row("dataset.hash", summary.dataset_hash[:12] + "…")
    head.add_row("runs/case", str(summary.runs_per_case))
    head.add_row("gate_mode", summary.gate_mode)
    head.add_row("gate", f"{gate:.2f}")
    head.add_row("cases", str(summary.sample_count))
    head.add_row("mean score", f"{summary.mean_score:.3f}")
    head.add_row(
        "pass rate",
        f"{cases_passing}/{summary.sample_count} "
        f"({(cases_passing / summary.sample_count if summary.sample_count else 0):.0%})",
    )
    head.add_row("total cost", f"${summary.total_cost_usd:.6f}")
    verdict = "[green]PASS[/green]" if overall_pass else "[red]FAIL[/red]"
    head.add_row("verdict", verdict)
    console.print(head)

    cases = Table(title="Cases", show_header=True, header_style="bold")
    cases.add_column("#", style="dim", width=3)
    cases.add_column("score")
    cases.add_column("runs")
    cases.add_column("input", overflow="fold")
    cases.add_column("first rationale", overflow="fold")
    for i, c in enumerate(summary.cases, start=1):
        score_text = f"{c.aggregated_score:.2f}"
        score_styled = f"[green]{score_text}[/green]" if c.passed else f"[red]{score_text}[/red]"
        first_rat = c.runs[0].rationale if c.runs else ""
        scores_per_run = ", ".join(f"{r.score:.2f}" for r in c.runs)
        cases.add_row(
            str(i),
            score_styled,
            scores_per_run,
            _truncate(str(c.case.input), 40),
            _truncate(first_rat, 60),
        )
    console.print(cases)


def _emit_diff_table(diff: BaselineDiff, *, regression_tolerance: float) -> None:
    """Render the baseline diff as a Rich table after the main eval output."""
    table = Table(
        title=f"Baseline diff vs {diff.baseline.eval_id[:8]}…",
        show_header=False,
    )
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("baseline_eval_id", diff.baseline.eval_id)
    table.add_row("baseline_age", _humanize_age(diff.baseline_age_seconds))
    table.add_row(
        "mean_score",
        f"{diff.baseline.mean_score:.3f} → {diff.current.mean_score:.3f}  "
        f"({_signed(diff.mean_score_delta)})",
    )
    table.add_row(
        "pass_rate",
        f"{diff.baseline.pass_rate:.3f} → {diff.current.pass_rate:.3f}  "
        f"({_signed(diff.pass_rate_delta)})",
    )
    table.add_row(
        "sample_count",
        f"{diff.baseline.sample_count} → {diff.current.sample_count}  "
        f"({diff.sample_count_delta:+d})",
    )
    table.add_row(
        "total_cost",
        f"${diff.baseline.total_cost_usd:.6f} → ${diff.current.total_cost_usd:.6f}  "
        f"({_signed(diff.cost_delta)})",
    )
    if diff.dataset_changed:
        table.add_row(
            "dataset",
            "[yellow]changed[/yellow] [dim](hash differs from baseline)[/dim]",
        )

    if diff.is_regression(tolerance=regression_tolerance):
        verdict = f"[red]REGRESSION[/red] [dim](tolerance ±{regression_tolerance:.2f})[/dim]"
    else:
        verdict = "[green]OK[/green]"
    table.add_row("verdict", verdict)
    console.print(table)


def _signed(value: float) -> str:
    delta = format_delta(value)
    if value > 0:
        return f"[green]{delta}[/green]"
    if value < 0:
        return f"[red]{delta}[/red]"
    return f"[dim]{delta}[/dim]"


_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def _humanize_age(seconds: float) -> str:
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.0f}s ago"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds / _SECONDS_PER_MINUTE:.1f} min ago"
    if seconds < _SECONDS_PER_DAY:
        return f"{seconds / _SECONDS_PER_HOUR:.1f} hr ago"
    return f"{seconds / _SECONDS_PER_DAY:.1f} days ago"


def _emit_json(
    summary: EvalSummary,
    *,
    record: EvalRecord,
    gate: float,
    cases_passing: int,
    overall_pass: bool,
    diff: BaselineDiff | None,
    regression_tolerance: float,
) -> None:
    payload: dict[str, object] = {
        "eval_id": record.eval_id,
        "agent": summary.agent,
        "agent_version": summary.agent_version,
        "dataset_hash": summary.dataset_hash,
        "judge_method": summary.judge.method.value,
        "judge_provider": summary.judge_provider,
        "runs_per_case": summary.runs_per_case,
        "gate_mode": summary.gate_mode,
        "gate": gate,
        "sample_count": summary.sample_count,
        "mean_score": round(summary.mean_score, 6),
        "cases_passing": cases_passing,
        "pass_rate": round(cases_passing / summary.sample_count, 6)
        if summary.sample_count
        else 0.0,
        "total_cost_usd": summary.total_cost_usd,
        "overall_pass": overall_pass,
        "cases": [
            {
                "input": c.case.input,
                "expected": c.case.expected,
                "score": round(c.aggregated_score, 6),
                "passed": c.passed,
                "scores_per_run": [round(r.score, 6) for r in c.runs],
                "rationales": [r.rationale for r in c.runs],
            }
            for c in summary.cases
        ],
    }
    if diff is not None:
        payload["baseline"] = {
            "eval_id": diff.baseline.eval_id,
            "mean_score": diff.baseline.mean_score,
            "pass_rate": diff.baseline.pass_rate,
            "sample_count": diff.baseline.sample_count,
            "dataset_hash": diff.baseline.dataset_hash,
            "created_at": diff.baseline.created_at.isoformat(),
            "mean_score_delta": diff.mean_score_delta,
            "pass_rate_delta": diff.pass_rate_delta,
            "sample_count_delta": diff.sample_count_delta,
            "cost_delta": diff.cost_delta,
            "dataset_changed": diff.dataset_changed,
            "regression": diff.is_regression(tolerance=regression_tolerance),
            "regression_tolerance": regression_tolerance,
        }
    print(json.dumps(payload, indent=2))


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


async def _resolve_storage_baseline(
    storage: StorageProvider, baseline_id: str | None
) -> EvalRecord | None:
    """Look up a sqlite-stored baseline by id; ``None`` if no id passed.

    Lives as a helper because the lookup must happen before the storage
    layer closes — otherwise ``finally`` would shut storage down first.
    """
    if baseline_id is None:
        return None
    # Local CLI flow — Executor stamps tenant_id="local" on every run.
    # Server-side use (when this lands behind HTTP) will pass the
    # authenticated tenant.
    record = await storage.get_eval(baseline_id, tenant_id="local")
    if record is None:
        err_console.print(f"[red]✗[/red] baseline eval id {baseline_id!r} not found in storage")
        raise typer.Exit(code=2) from None
    return record


def _resolve_file_baseline(baseline_file: Path) -> EvalRecord:
    """Read a JSON-serialized EvalRecord from disk, with a friendly error path."""
    try:
        return _load_baseline_file(baseline_file)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        err_console.print(f"[red]✗[/red] baseline file load failed: {exc}")
        raise typer.Exit(code=2) from None


def _load_baseline_file(path: Path) -> EvalRecord:
    """Read an :class:`EvalRecord` JSON dump back into a model instance.

    Raises :class:`FileNotFoundError`, :class:`json.JSONDecodeError`, or
    :class:`ValueError` (from Pydantic) — the caller turns these into
    ``Exit(2)``.
    """
    raw = path.read_text()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"baseline file must be a JSON object, got {type(payload).__name__}")
    return EvalRecord.model_validate(payload)


def _write_baseline_file(path: Path, record: EvalRecord) -> None:
    """Persist ``record`` as pretty-printed JSON.

    Creates parent directories so users can drop the baseline at
    ``.movate/baseline.json`` without pre-creating the dir.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n")
    err_console.print(f"[green]✓[/green] wrote baseline → {path}")


@contextmanager
def _maybe_eval_progress(
    enabled: bool,
) -> Iterator[Callable[[int, int, CaseSummary], None] | None]:
    """Yield a callback suitable for ``EvalEngine.on_case_complete``.

    When ``enabled``, drives a stderr progress bar that updates after
    each case with running mean score. When disabled, yields ``None``
    so the engine sees no progress hook (clean JSON / Markdown output).

    The bar's total isn't known until the engine starts iterating
    (``load_dataset`` runs inside ``EvalEngine.run``), so we set total
    on the first callback via ``advance(total=...)``.
    """
    if not enabled:
        yield None
        return

    running_total = 0.0

    with progress_bar(description="cases", total=None) as advance:

        def on_case(done: int, total: int, summary: CaseSummary) -> None:
            nonlocal running_total
            running_total += summary.aggregated_score
            mean = running_total / done if done else 0.0
            advance(total=total, suffix=f" (mean={mean:.2f})")

        yield on_case
