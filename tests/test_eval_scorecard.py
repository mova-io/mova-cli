"""Tests for the movate-evals 10-category weighted scorecard.

Covers: specialist scoring, consistency computation, safety hard gate,
weighted composite, ProductionReadiness verdict, _build_weighted_scorecard.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pytest

from movate.core.eval import (
    DIMENSION_WEIGHTS,
    DimensionalMeans,
    EvalEngine,
    _build_weighted_scorecard,
    _compute_consistency,
    _score_required_fields,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import ProductionReadiness
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import (
    InMemoryStorage,
    JudgeStubProvider,
    NullTracer,
    scaffold_agent,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold(dst: Path, name: str = "demo") -> Path:
    return scaffold_agent(dst, name=name)


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


def _executor(provider, pricing, storage, tracer) -> Executor:
    return Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)


# ---------------------------------------------------------------------------
# _score_required_fields (deterministic, no LLM)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_required_fields_all_present() -> None:
    result = _score_required_fields(
        ["category", "priority", "routing_queue"],
        {"category": "bug", "priority": "p1_high", "routing_queue": "engineering"},
    )
    assert result.value == pytest.approx(1.0)
    assert "all required fields" in result.rationale


@pytest.mark.unit
def test_required_fields_partial() -> None:
    result = _score_required_fields(
        ["category", "priority", "routing_queue"],
        {"category": "bug"},
    )
    assert result.value == pytest.approx(1 / 3)
    assert "missing" in result.rationale


@pytest.mark.unit
def test_required_fields_empty_values_count_as_missing() -> None:
    result = _score_required_fields(["title"], {"title": ""})
    assert result.value == pytest.approx(0.0)


@pytest.mark.unit
def test_required_fields_none_list_is_unscored() -> None:
    result = _score_required_fields([], {"category": "bug"})
    assert result.value is None


# ---------------------------------------------------------------------------
# _compute_consistency
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consistency_single_run_unscored() -> None:
    result = _compute_consistency([0.9])
    assert result.value is None


@pytest.mark.unit
def test_consistency_identical_runs_perfect() -> None:
    result = _compute_consistency([0.8, 0.8, 0.8])
    assert result.value == pytest.approx(1.0)
    assert "std_dev=0" in result.rationale


@pytest.mark.unit
def test_consistency_high_variance_low_score() -> None:
    scores = [1.0, 0.0, 1.0, 0.0]
    std_dev = statistics.stdev(scores)
    expected = max(0.0, 1.0 - std_dev)
    result = _compute_consistency(scores)
    assert result.value == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _build_weighted_scorecard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scorecard_none_when_no_v08_dims() -> None:
    means = DimensionalMeans(accuracy=0.9, faithfulness=0.8, latency=0.7)
    assert _build_weighted_scorecard(means) is None


@pytest.mark.unit
def test_scorecard_production_ready() -> None:
    means = DimensionalMeans(
        accuracy=0.95,
        faithfulness=0.95,
        safety=0.98,
        completeness=0.92,
        tool_usage=0.90,
        latency=0.85,
        ux_tone=0.88,
        task_success=0.93,
    )
    scorecard = _build_weighted_scorecard(means)
    assert scorecard is not None
    assert scorecard.verdict == ProductionReadiness.PRODUCTION_READY
    assert scorecard.composite >= 90.0
    assert scorecard.safety_gate_passed is True


@pytest.mark.unit
def test_scorecard_safety_hard_gate_fails() -> None:
    means = DimensionalMeans(
        accuracy=0.95,
        safety=0.80,  # below 0.95 threshold
        completeness=0.90,
        task_success=0.92,
    )
    scorecard = _build_weighted_scorecard(means)
    assert scorecard is not None
    assert scorecard.safety_gate_passed is False
    assert scorecard.verdict != ProductionReadiness.PRODUCTION_READY
    assert scorecard.task_success == 0.0  # forced to 0


@pytest.mark.unit
def test_scorecard_not_ready_low_scores() -> None:
    means = DimensionalMeans(
        accuracy=0.40,
        safety=0.95,
        completeness=0.50,
        task_success=0.45,
        ux_tone=0.55,
    )
    scorecard = _build_weighted_scorecard(means)
    assert scorecard is not None
    assert scorecard.verdict == ProductionReadiness.NOT_READY
    assert scorecard.composite < 70.0


@pytest.mark.unit
def test_scorecard_pilot_ready_band() -> None:
    means = DimensionalMeans(
        accuracy=0.83,
        safety=0.96,
        completeness=0.80,
        task_success=0.82,
        ux_tone=0.79,
    )
    scorecard = _build_weighted_scorecard(means)
    assert scorecard is not None
    assert scorecard.verdict == ProductionReadiness.PILOT_READY


@pytest.mark.unit
def test_scorecard_confidence_from_consistency() -> None:
    means = DimensionalMeans(
        safety=0.97,
        completeness=0.90,
        task_success=0.91,
        consistency=0.85,
    )
    scorecard = _build_weighted_scorecard(means)
    assert scorecard is not None
    assert scorecard.confidence == pytest.approx(0.85)


@pytest.mark.unit
def test_dimension_weights_sum_reasonable() -> None:
    total = sum(DIMENSION_WEIGHTS.values())
    assert 10.0 <= total <= 15.0  # 13.1 currently


# ---------------------------------------------------------------------------
# EvalEngine end-to-end with v0.8 dims
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_produces_scorecard_with_llm_judge(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """When judge.model is set, v0.8 specialist dims are scored and a scorecard is built."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}, "required_fields": ["message"]}\n'
    )
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n  provider: anthropic/claude-sonnet-4-6\n"
        "rubric: 'be strict'\n"
        "threshold: 0.8\n"
    )
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "ok"}', judge_score=0.9)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, runs_per_case=1)
    summary = await engine.run(bundle)

    # v0.8 dims scored
    run = summary.cases[0].runs[0]
    assert run.dimensions.safety.value is not None
    assert run.dimensions.completeness.value is not None
    assert run.dimensions.ux_tone.value is not None
    assert run.dimensions.task_success.value is not None

    # Scorecard built
    assert summary.scorecard is not None
    assert isinstance(summary.scorecard.verdict, ProductionReadiness)


@pytest.mark.unit
async def test_engine_no_scorecard_exact_match(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Exact-match judge (no model) → no specialist dims → no scorecard."""
    agent_dir = _scaffold(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    assert summary.scorecard is None
    assert summary.dimensional_means.safety is None
    assert summary.dimensional_means.ux_tone is None


@pytest.mark.unit
async def test_consistency_computed_across_runs(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Consistency dimension is set after N runs and reflects score variance."""
    agent_dir = _scaffold(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, runs_per_case=3)
    summary = await engine.run(bundle)

    # Consistency stored on first run's dimensions
    cons = summary.cases[0].runs[0].dimensions.consistency
    assert cons.value is not None  # 3 runs → scoreable
    assert summary.dimensional_means.consistency is not None
