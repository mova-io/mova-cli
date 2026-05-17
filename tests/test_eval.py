"""Eval engine: dataset loading, scoring, family enforcement, aggregation, end-to-end."""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.eval import (
    EvalConfigError,
    EvalEngine,
    aggregate_scores,
    assert_cross_family,
    load_dataset,
    load_judge_config,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import JudgeConfig, JudgeMethod, ModelConfig
from movate.providers import provider_family
from movate.providers.base import BaseLLMProvider
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


def _executor(provider: BaseLLMProvider, pricing: PricingTable, storage, tracer) -> Executor:
    return Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)


# ---------------------------------------------------------------------------
# Family helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider,family",
    [
        ("openai/gpt-4o-mini", "openai"),
        ("azure/gpt-4o", "openai"),
        ("azure_openai/gpt-4o", "openai"),
        ("anthropic/claude-sonnet-4-6", "anthropic"),
        ("gemini/gemini-1.5-pro", "google"),
        ("vertex_ai/gemini-1.5-pro", "google"),
        ("ollama/llama3", "ollama"),
        ("unknown/x", "unknown"),
    ],
)
def test_provider_family(provider: str, family: str) -> None:
    assert provider_family(provider) == family


@pytest.mark.unit
def test_assert_cross_family_rejects_same() -> None:
    with pytest.raises(EvalConfigError, match="same-family"):
        assert_cross_family("openai/gpt-4o", "openai/gpt-4o-mini")


@pytest.mark.unit
def test_assert_cross_family_rejects_azure_vs_openai() -> None:
    """Azure OpenAI shares model family with OpenAI."""
    with pytest.raises(EvalConfigError):
        assert_cross_family("openai/gpt-4o", "azure/gpt-4o")


@pytest.mark.unit
def test_assert_cross_family_accepts_distinct() -> None:
    assert_cross_family("openai/gpt-4o-mini", "anthropic/claude-sonnet-4-6")
    assert_cross_family("anthropic/claude-haiku-4-5-20251001", "gemini/gemini-1.5-pro")


# ---------------------------------------------------------------------------
# Aggregation modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_mean() -> None:
    assert aggregate_scores([1.0, 0.5, 0.0], "mean") == pytest.approx(0.5)


@pytest.mark.unit
def test_aggregate_min() -> None:
    assert aggregate_scores([1.0, 0.5, 0.0], "min") == 0.0


@pytest.mark.unit
def test_aggregate_p10() -> None:
    """p10 is near-worst-case; tolerates one outlier across many samples."""
    assert aggregate_scores([1.0], "p10") == 1.0
    # 10 scores, idx = floor(10 * 0.1) = 1 → second-lowest
    assert aggregate_scores([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9], "p10") == 0.1


@pytest.mark.unit
def test_aggregate_unknown_mode_raises() -> None:
    with pytest.raises(EvalConfigError, match="unknown gate_mode"):
        aggregate_scores([0.5], "weighted")


# ---------------------------------------------------------------------------
# Dataset loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_dataset_template(tmp_path: Path) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    cases, digest = load_dataset(bundle)
    assert len(cases) == 2
    assert cases[0].input == {"text": "hello"}
    assert cases[0].expected == {"message": "Hello!"}
    assert len(digest) == 64  # sha256 hex


@pytest.mark.unit
def test_load_dataset_invalid_json(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text("{not json")
    bundle = load_agent(agent_dir)
    with pytest.raises(EvalConfigError, match="invalid JSON"):
        load_dataset(bundle)


@pytest.mark.unit
def test_load_dataset_skips_blank_lines(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    ds = agent_dir / "evals" / "dataset.jsonl"
    ds.write_text(
        '\n{"input": {"text": "a"}, "expected": {"message": "A"}}\n\n'
        '{"input": {"text": "b"}, "expected": {"message": "B"}}\n'
    )
    bundle = load_agent(agent_dir)
    cases, _ = load_dataset(bundle)
    assert len(cases) == 2


# ---------------------------------------------------------------------------
# Judge config loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_judge_default_is_exact(tmp_path: Path) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    judge = load_judge_config(bundle)
    assert judge.method is JudgeMethod.EXACT
    assert judge.model is None


@pytest.mark.unit
def test_load_judge_from_yaml(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n"
        "  provider: anthropic/claude-sonnet-4-6\n"
        "rubric: 'be strict'\n"
        "threshold: 0.8\n"
    )
    bundle = load_agent(agent_dir)
    judge = load_judge_config(bundle)
    assert judge.method is JudgeMethod.LLM_JUDGE
    assert judge.model is not None
    assert judge.model.provider == "anthropic/claude-sonnet-4-6"
    assert judge.threshold == 0.8


@pytest.mark.unit
def test_load_judge_invalid_yaml_raises(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text("method: not-a-method")
    bundle = load_agent(agent_dir)
    with pytest.raises(EvalConfigError, match="invalid judge config"):
        load_judge_config(bundle)


# ---------------------------------------------------------------------------
# Engine — exact-match scoring
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_exact_match_pass(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    # MockProvider returns exactly what the dataset's first case expects.
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    assert summary.sample_count == 2
    # First case matches → 1.0; second ("4") doesn't → 0.0.
    assert summary.cases[0].aggregated_score == 1.0
    assert summary.cases[0].passed
    assert summary.cases[1].aggregated_score == 0.0
    assert not summary.cases[1].passed
    assert summary.mean_score == 0.5
    assert summary.pass_rate == 0.5
    # Default per-case threshold is 0.7 → second case fails → overall fail.
    assert not summary.overall_pass


@pytest.mark.unit
async def test_engine_exact_match_all_pass_with_perfect_provider(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Provider returning per-case expected output → 100% pass."""
    agent_dir = _scaffold(tmp_path / "demo")
    # Single-case dataset for determinism.
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)

    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    assert summary.overall_pass
    assert summary.pass_rate == 1.0
    assert summary.mean_score == 1.0


# ---------------------------------------------------------------------------
# Engine — N runs + aggregation modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_runs_per_case_aggregates_correctly(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """N runs through MockProvider give N identical 1.0 scores → mean stays 1.0."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, runs_per_case=3)

    summary = await engine.run(bundle)
    assert summary.runs_per_case == 3
    assert len(summary.cases[0].runs) == 3
    assert summary.cases[0].aggregated_score == 1.0


@pytest.mark.unit
def test_engine_rejects_zero_runs() -> None:
    with pytest.raises(EvalConfigError, match="runs_per_case must be"):
        EvalEngine(executor=None, provider=MockProvider(), runs_per_case=0)  # type: ignore[arg-type]


@pytest.mark.unit
def test_engine_rejects_unknown_gate_mode() -> None:
    with pytest.raises(EvalConfigError, match="gate_mode"):
        EvalEngine(executor=None, provider=MockProvider(), gate_mode="weighted")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Engine — LLM-as-judge path with cross-family enforcement
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_llm_judge_happy_path(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n  provider: anthropic/claude-sonnet-4-6\n"
        "rubric: 'be strict'\n"
        "threshold: 0.8\n"
    )
    bundle = load_agent(agent_dir)

    provider = JudgeStubProvider(agent_response='{"message": "good"}', judge_score=0.95)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, runs_per_case=2)

    summary = await engine.run(bundle)
    assert summary.judge_provider == "anthropic/claude-sonnet-4-6"
    assert summary.cases[0].aggregated_score == pytest.approx(0.95)
    assert summary.cases[0].passed
    # Engine called both the agent provider (openai) and judge provider (anthropic).
    assert any(c.startswith("openai/") for c in provider.calls)
    assert any(c.startswith("anthropic/") for c in provider.calls)


@pytest.mark.unit
async def test_engine_rejects_same_family_judge(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\nmodel:\n  provider: openai/gpt-4o-2024-08-06\nrubric: 'x'\n"
    )
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "x"}', judge_score=1.0)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    with pytest.raises(EvalConfigError, match="same-family"):
        await engine.run(bundle)


@pytest.mark.unit
async def test_engine_llm_judge_requires_rubric(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\nmodel:\n  provider: anthropic/claude-sonnet-4-6\n"
    )
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "x"}', judge_score=1.0)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    with pytest.raises(EvalConfigError, match="requires both 'model' and 'rubric'"):
        await engine.run(bundle)


# ---------------------------------------------------------------------------
# EvalSummary → EvalRecord
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_summary_to_record_round_trips(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    record = summary.to_record()
    assert record.agent == "demo"
    assert record.judge_method is JudgeMethod.EXACT
    assert record.judge_provider is None
    assert record.runs_per_case == 1
    assert record.gate_mode == "mean"
    assert record.sample_count == 2


# ---------------------------------------------------------------------------
# judge_override — inline JudgeConfig bypasses judge.yaml
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_judge_override_bypasses_judge_yaml(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """judge_override on EvalEngine skips judge.yaml (exact-match default) and
    applies the supplied LLM judge config instead."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    # No judge.yaml — engine would default to EXACT without the override.
    bundle = load_agent(agent_dir)

    override = JudgeConfig(
        method=JudgeMethod.LLM_JUDGE,
        model=ModelConfig(provider="anthropic/claude-opus-4-7"),
        rubric="Score 0-1: 1=correct, 0=wrong",
    )
    provider = JudgeStubProvider(agent_response='{"message": "ok"}', judge_score=0.9)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(
        executor=executor,
        provider=provider,
        judge_override=override,
    )

    summary = await engine.run(bundle)
    # LLM judge fired (score comes from stub, not exact-match).
    assert summary.judge.method is JudgeMethod.LLM_JUDGE
    assert summary.judge_provider == "anthropic/claude-opus-4-7"
    assert summary.cases[0].aggregated_score == pytest.approx(0.9)


@pytest.mark.unit
async def test_judge_override_takes_precedence_over_judge_yaml(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """When both judge.yaml and judge_override are present, override wins."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    # judge.yaml says exact-match.
    (agent_dir / "evals" / "judge.yaml").write_text("method: exact\n")
    bundle = load_agent(agent_dir)

    override = JudgeConfig(
        method=JudgeMethod.LLM_JUDGE,
        model=ModelConfig(provider="anthropic/claude-opus-4-7"),
        rubric="Score 0-1",
    )
    provider = JudgeStubProvider(agent_response='{"message": "ok"}', judge_score=0.75)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(
        executor=executor,
        provider=provider,
        judge_override=override,
    )

    summary = await engine.run(bundle)
    assert summary.judge.method is JudgeMethod.LLM_JUDGE
    assert summary.cases[0].aggregated_score == pytest.approx(0.75)
