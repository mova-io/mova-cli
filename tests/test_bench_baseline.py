"""BenchBaselineDiff math — per-model deltas, regression detection, drift signals.

Sister to ``tests/test_baseline.py`` (eval baselines). Bench baselines
differ because they're per-model — the interesting coverage is:

* Match-by-provider: same provider name in baseline + current → matched
  row with deltas. Provider in only one side → added/removed list.
* Score delta is ``None`` when either side had no judge — we don't
  flag those as regressions.
* Regression detection: per-model score drop > tolerance flips the
  ``is_regression`` flag.
* Input drift signal (``input_changed``): when the baseline ran against
  a different input, the comparison is weaker; the operator gets a
  visible warning but the diff still computes.
* Cross-agent diff: must raise — comparing across agents is nonsense.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from movate.core.bench_baseline import (
    BenchModelDelta,
    compute_bench_baseline_diff,
)
from movate.core.models import BenchModelRow, BenchRecord


def _model_row(
    provider: str,
    *,
    score: float | None = 0.8,
    cost_mean: float = 0.0005,
    p50: int = 400,
    p95: int = 500,
) -> BenchModelRow:
    return BenchModelRow(
        provider=provider,
        successful_runs=3,
        error_count=0,
        cost_total_usd=cost_mean * 3,
        cost_mean_usd=cost_mean,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        score=score,
    )


def _record(
    *,
    bench_id: str | None = None,
    agent: str = "alpha",
    input_hash: str = "h" * 16,
    models: list[BenchModelRow] | None = None,
    created_at: datetime | None = None,
) -> BenchRecord:
    return BenchRecord(
        bench_id=bench_id or str(uuid4()),
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        input_hash=input_hash,
        judge_method=None,
        judge_provider=None,
        rubric=None,
        runs_per_model=3,
        gate_mode="mean",
        total_cost_usd=sum(m.cost_total_usd for m in (models or [])),
        models=models or [],
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Match + delta math
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_matched_models_compute_deltas() -> None:
    """Same provider in baseline + current → matched delta with
    score/cost/latency deltas computed as ``current - baseline``."""
    baseline = _record(
        models=[_model_row("openai/gpt-4o-mini", score=0.80, cost_mean=0.0005, p50=400, p95=500)]
    )
    current = _record(
        models=[_model_row("openai/gpt-4o-mini", score=0.85, cost_mean=0.0007, p50=420, p95=540)]
    )

    diff = compute_bench_baseline_diff(baseline, current)
    assert len(diff.matched) == 1
    delta = diff.matched[0]
    assert isinstance(delta, BenchModelDelta)
    assert delta.score_delta == pytest.approx(0.05)
    assert delta.cost_mean_delta == pytest.approx(0.0002)
    assert delta.latency_p50_delta == 20
    assert delta.latency_p95_delta == 40


@pytest.mark.unit
def test_score_delta_none_when_either_side_has_no_judge() -> None:
    """Score is ``None`` when no judge was configured. We treat
    "no opinion" symmetrically: either side missing → score_delta is
    None, and the row is never flagged as a regression."""
    baseline = _record(models=[_model_row("openai/gpt-4o-mini", score=None)])
    current = _record(models=[_model_row("openai/gpt-4o-mini", score=0.80)])

    diff = compute_bench_baseline_diff(baseline, current)
    assert diff.matched[0].score_delta is None
    assert not diff.matched[0].is_regression(tolerance=0.0)


# ---------------------------------------------------------------------------
# Added / removed model sets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_added_and_removed_providers_listed_separately() -> None:
    """Providers in only one side surface in added/removed (sorted),
    not in matched."""
    baseline = _record(
        models=[
            _model_row("openai/gpt-4o-mini"),
            _model_row("anthropic/claude-haiku"),
        ]
    )
    current = _record(
        models=[
            _model_row("openai/gpt-4o-mini"),
            _model_row("google/gemini-2-flash"),
        ]
    )

    diff = compute_bench_baseline_diff(baseline, current)
    assert [m.provider for m in diff.matched] == ["openai/gpt-4o-mini"]
    assert diff.added == ["google/gemini-2-flash"]
    assert diff.removed == ["anthropic/claude-haiku"]


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_regression_flagged_when_score_drops_past_tolerance() -> None:
    """Default tolerance 0.0: any score drop is a regression."""
    baseline = _record(models=[_model_row("p1", score=0.85)])
    current = _record(models=[_model_row("p1", score=0.80)])

    diff = compute_bench_baseline_diff(baseline, current)
    assert diff.matched[0].score_delta == pytest.approx(-0.05)
    assert diff.is_regression(tolerance=0.0)
    assert diff.matched[0].is_regression(tolerance=0.0)
    # With wider tolerance, the same drop isn't a regression.
    assert not diff.is_regression(tolerance=0.10)
    assert not diff.matched[0].is_regression(tolerance=0.10)


@pytest.mark.unit
def test_no_regression_on_score_improvement() -> None:
    """Positive score delta = improvement. Never a regression
    regardless of tolerance."""
    baseline = _record(models=[_model_row("p1", score=0.70)])
    current = _record(models=[_model_row("p1", score=0.85)])

    diff = compute_bench_baseline_diff(baseline, current)
    assert not diff.is_regression(tolerance=0.0)


@pytest.mark.unit
def test_regressing_models_filter_returns_only_regressions() -> None:
    """Multiple matched models: some regress, some don't. Filter
    returns just the regressing ones — useful for CI summaries
    showing exactly which models flagged."""
    baseline = _record(
        models=[
            _model_row("good", score=0.80),
            _model_row("bad", score=0.85),
        ]
    )
    current = _record(
        models=[
            _model_row("good", score=0.85),  # improvement
            _model_row("bad", score=0.70),  # regression
        ]
    )

    diff = compute_bench_baseline_diff(baseline, current)
    regs = diff.regressing_models(tolerance=0.0)
    assert [m.provider for m in regs] == ["bad"]


# ---------------------------------------------------------------------------
# Drift signals + cross-agent guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_input_changed_surfaces_when_hashes_differ() -> None:
    """Different input_hash between baseline + current → input_changed
    is True. Diff still computes; operator sees the warning."""
    baseline = _record(input_hash="aaaa" * 4)
    current = _record(input_hash="bbbb" * 4)
    diff = compute_bench_baseline_diff(baseline, current)
    assert diff.input_changed is True


@pytest.mark.unit
def test_baseline_age_seconds_positive_for_older_baseline() -> None:
    """Sanity: baselines are typically older than the current run."""
    baseline = _record(created_at=datetime.now(UTC) - timedelta(hours=24))
    current = _record(created_at=datetime.now(UTC))
    diff = compute_bench_baseline_diff(baseline, current)
    assert diff.baseline_age_seconds > 0


@pytest.mark.unit
def test_cross_agent_diff_raises() -> None:
    """Comparing two records from different agents is nonsense — we
    raise rather than silently emit a confusing diff."""
    baseline = _record(agent="alpha")
    current = _record(agent="beta")
    with pytest.raises(ValueError, match="differs from current"):
        compute_bench_baseline_diff(baseline, current)


# ---------------------------------------------------------------------------
# Aggregate roll-up
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_total_cost_delta_is_current_minus_baseline() -> None:
    """Aggregate cost delta = current.total_cost_usd - baseline.total_cost_usd.
    Positive = bench got more expensive."""
    baseline = _record(
        models=[_model_row("p1", cost_mean=0.001), _model_row("p2", cost_mean=0.002)]
    )
    current = _record(models=[_model_row("p1", cost_mean=0.002), _model_row("p2", cost_mean=0.003)])
    diff = compute_bench_baseline_diff(baseline, current)
    # baseline total = (0.001 + 0.002) * 3 = 0.009; current = (0.002 + 0.003) * 3 = 0.015
    assert diff.total_cost_delta == pytest.approx(0.006)
