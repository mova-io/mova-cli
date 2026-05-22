"""``movate ci eval`` — gate every agent in the project on its committed baseline.

CI-friendly wrapper around ``movate eval``: discovers all agents in
``agents_dir`` (from ``movate.yaml``, default ``./agents``), runs each
through the eval engine, compares against the per-agent committed
baseline at ``<baselines_dir>/<agent_name>/baseline.json``, and emits
a structured + markdown summary suitable for ``$GITHUB_STEP_SUMMARY``.

Designed so the workflow YAML stays a one-liner:

    - run: uv run movate ci eval --mock

…instead of a per-agent matrix with shell-glue. Same command runs
locally before a push, so devs can pre-flight the gate the way CI will.

Exit code policy:

* ``0`` — every agent passed (no regressions past
  ``--regression-tolerance``; agents without baselines are skipped
  with a notice, not a failure).
* ``1`` — at least one agent regressed.
* ``2`` — eval engine error (bad config, missing dataset, etc.) on
  any agent; matches the existing ``movate eval`` convention.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from movate.cli._console import error, hint, success
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.baseline import BaselineDiff, compute_baseline_diff
from movate.core.config import load_project_config
from movate.core.eval import EvalConfigError, EvalEngine, EvalSummary
from movate.core.loader import AgentBundle
from movate.core.models import EvalRecord
from movate.core.paths import project_state_dir
from movate.runtime.registry import scan_agents

ci_app = typer.Typer(
    name="ci",
    help="CI helpers — pre-flight gates that wrap multi-agent invocations.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@ci_app.command("eval")
def eval_all(
    baselines_dir: Path | None = typer.Option(
        None,
        "--baselines-dir",
        help=(
            "Root dir under which each agent's baseline is "
            "``<dir>/<agent_name>/baseline.json``. "
            "Default: the project's .mdk/ (or legacy .movate/)."
        ),
    ),
    regression_tolerance: float = typer.Option(
        0.0,
        "--regression-tolerance",
        help=(
            "Allowable score drop vs baseline before flagging a regression. "
            "0.0 means any drop fails; raise to ~0.05 for noisy LLM-as-judge."
        ),
    ),
    runs: int = typer.Option(
        1, "--runs", "-r", help="Runs per case (3+ for LLM-as-judge variance)."
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic MockProvider (no API keys)."
    ),
    summary_file: Path = typer.Option(
        None,
        "--summary-file",
        help=(
            "Append a markdown summary to this file. Designed for "
            "``$GITHUB_STEP_SUMMARY`` — set ``--summary-file $GITHUB_STEP_SUMMARY`` "
            "in CI to get a rendered diff on every PR."
        ),
    ),
) -> None:
    """Eval every agent in the project; fail on any regression.

    [bold]Examples:[/bold]

      [dim]# Local pre-flight (same gate CI runs)[/dim]
      $ movate ci eval --mock

      [dim]# In CI — append the diff summary to GitHub's PR view[/dim]
      $ movate ci eval --mock --summary-file "$GITHUB_STEP_SUMMARY"

      [dim]# Real-LLM eval with provider keys in env[/dim]
      $ movate ci eval --runs 3 --regression-tolerance 0.05
    """
    if baselines_dir is None:
        baselines_dir = project_state_dir(Path.cwd())
    project = load_project_config()
    agents_root = Path(project.agents_dir)
    bundles = scan_agents(agents_root)
    if not bundles:
        error(f"no agents found under {agents_root}")
        raise typer.Exit(code=2)

    results = asyncio.run(
        _run_all(
            bundles=bundles,
            baselines_dir=baselines_dir,
            regression_tolerance=regression_tolerance,
            runs=runs,
            mock=mock,
        )
    )

    _print_summary(results, regression_tolerance=regression_tolerance)
    if summary_file is not None:
        _append_markdown_summary(summary_file, results, regression_tolerance=regression_tolerance)

    # Exit policy: 2 if any engine error, 1 if any regression, 0 otherwise.
    if any(r.engine_error for r in results):
        raise typer.Exit(code=2)
    if any(r.regressed for r in results):
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Async core + per-agent result type
# ---------------------------------------------------------------------------


class _AgentResult:
    """Per-agent CI result. Plain class instead of a dataclass so we can
    populate it incrementally as the run progresses."""

    __slots__ = (
        "agent_name",
        "baseline_path",
        "baseline_present",
        "diff",
        "engine_error",
        "regressed",
        "summary",
    )

    def __init__(self, agent_name: str, baseline_path: Path) -> None:
        self.agent_name = agent_name
        self.baseline_path = baseline_path
        self.baseline_present = baseline_path.is_file()
        self.summary: EvalSummary | None = None
        self.diff: BaselineDiff | None = None
        self.engine_error: str | None = None
        self.regressed = False


async def _run_all(
    *,
    bundles: list[AgentBundle],
    baselines_dir: Path,
    regression_tolerance: float,
    runs: int,
    mock: bool,
) -> list[_AgentResult]:
    """Run each agent's eval sequentially, sharing one local runtime
    (one storage open, one tracer open) across the whole batch.

    Sequential rather than asyncio.gather because the eval engine
    writes runs to local sqlite — concurrent writes would deadlock
    or serialize anyway, with no speedup."""
    rt = await build_local_runtime(mock=mock)
    results: list[_AgentResult] = []
    try:
        for bundle in bundles:
            result = _AgentResult(
                agent_name=bundle.spec.name,
                baseline_path=baselines_dir / bundle.spec.name / "baseline.json",
            )
            engine = EvalEngine(
                executor=rt.executor,
                provider=rt.provider,
                runs_per_case=runs,
                gate_mode="mean",
            )
            try:
                summary = await engine.run(bundle)
            except EvalConfigError as exc:
                result.engine_error = str(exc)
                results.append(result)
                continue

            result.summary = summary
            current_record = summary.to_record()
            await rt.storage.save_eval(current_record)

            if result.baseline_present:
                baseline_record = _load_baseline_record(result.baseline_path)
                if baseline_record is None:
                    # File exists but is corrupted — treat as engine error.
                    result.engine_error = f"baseline at {result.baseline_path} is unreadable"
                else:
                    result.diff = compute_baseline_diff(baseline_record, current_record)
                    # ``BaselineDiff.is_regression`` already encodes the
                    # mean_score-OR-pass_rate drop policy with tolerance
                    # — reuse it so the gate semantics match
                    # ``movate eval --regression-tolerance``.
                    result.regressed = result.diff.is_regression(tolerance=regression_tolerance)

            results.append(result)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    return results


def _load_baseline_record(path: Path) -> EvalRecord | None:
    """Read a baseline JSON file. Returns ``None`` on any read /
    parse failure so the caller can surface it as an engine error
    rather than an uncaught exception."""
    try:
        payload = json.loads(path.read_text())
        return EvalRecord.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_summary(results: list[_AgentResult], *, regression_tolerance: float) -> None:
    """Human-readable summary on stderr."""
    n_total = len(results)
    n_baselined = sum(1 for r in results if r.baseline_present)
    n_regressed = sum(1 for r in results if r.regressed)
    n_errored = sum(1 for r in results if r.engine_error)

    for r in results:
        if r.engine_error:
            error(f"{r.agent_name}: {r.engine_error}")
            continue
        if not r.baseline_present:
            hint(f"[dim]{r.agent_name}: no baseline at {r.baseline_path} — gate skipped[/dim]")
            continue
        # We have a summary + diff for this agent.
        assert r.summary is not None and r.diff is not None
        if r.regressed:
            error(
                f"{r.agent_name}: REGRESSED "
                f"(mean_score Δ={r.diff.mean_score_delta:+.4f}, "
                f"pass_rate Δ={r.diff.pass_rate_delta:+.4f}, "
                f"tolerance {regression_tolerance})"
            )
        else:
            success(
                f"{r.agent_name}: mean={r.summary.mean_score:.4f} "
                f"pass={r.summary.pass_rate:.4f} "
                f"(Δ mean={r.diff.mean_score_delta:+.4f})"
            )

    # Bottom-line verdict.
    if n_errored > 0:
        error(f"{n_errored}/{n_total} agent(s) errored — gate exits 2")
    elif n_regressed > 0:
        error(f"{n_regressed}/{n_total} agent(s) regressed — gate exits 1")
    else:
        success(f"{n_total} agent(s) checked, {n_baselined} gated against baseline — all good")


def _append_markdown_summary(
    path: Path,
    results: list[_AgentResult],
    *,
    regression_tolerance: float,
) -> None:
    """Append a GitHub-Step-Summary-compatible markdown table to ``path``."""
    lines: list[str] = [
        "## movate ci eval",
        "",
        "| agent | mean_score | pass_rate | Δ mean | Δ pass | status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        if r.engine_error:
            lines.append(f"| {r.agent_name} | — | — | — | — | ❌ {r.engine_error} |")
            continue
        if not r.baseline_present:
            assert r.summary is not None  # engine succeeded
            lines.append(
                f"| {r.agent_name} | {r.summary.mean_score:.4f} | "
                f"{r.summary.pass_rate:.4f} | — | — | ⚠️ no baseline |"
            )
            continue
        assert r.summary is not None and r.diff is not None
        status = "❌ regressed" if r.regressed else "✅"
        lines.append(
            f"| {r.agent_name} | {r.summary.mean_score:.4f} | "
            f"{r.summary.pass_rate:.4f} | "
            f"{r.diff.mean_score_delta:+.4f} | "
            f"{r.diff.pass_rate_delta:+.4f} | {status} |"
        )
    lines.append("")
    lines.append(f"_regression tolerance: {regression_tolerance}_")
    lines.append("")
    # Append (not overwrite) — multiple workflow steps can each contribute.
    with path.open("a") as f:
        f.write("\n".join(lines))
        f.write("\n")


__all__ = ["ci_app"]
