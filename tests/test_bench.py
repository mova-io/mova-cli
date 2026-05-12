"""BenchEngine: per-model runs, judge skipping on family conflict, latency stats, errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.bench import BenchEngine, BenchSummary
from movate.core.eval import EvalConfigError
from movate.core.executor import Executor
from movate.core.failures import AuthError
from movate.core.loader import load_agent
from movate.core.models import JudgeConfig, JudgeMethod, ModelConfig
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import (
    InMemoryStorage,
    JudgeStubProvider,
    NullTracer,
    scaffold_agent,
)


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
# Test-local provider double
# ---------------------------------------------------------------------------


class FailForProvider(BaseLLMProvider):
    """Always raises a non-retryable AuthError for one provider; delegates the rest."""

    name = "fail_for"
    version = "0.0.1"

    def __init__(self, fail_provider: str, inner: BaseLLMProvider) -> None:
        self._fail_provider = fail_provider
        self._inner = inner

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if request.provider == self._fail_provider:
            raise AuthError("no key for this provider")
        return await self._inner.complete(request)

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Engine — happy path, no judge
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_bench_no_judge_two_models(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = BenchEngine(executor=executor, provider=provider, runs_per_model=2)

    summary = await engine.run(
        bundle,
        input_payload={"text": "hi"},
        providers=[
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",
        ],
    )

    assert isinstance(summary, BenchSummary)
    assert len(summary.models) == 2
    for row in summary.models:
        assert len(row.runs) == 2
        assert row.error_count == 0
        assert row.cost_mean_usd > 0
        assert row.aggregated_score(summary.gate_mode) is None  # no judge configured


@pytest.mark.unit
async def test_bench_rejects_empty_provider_list(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    engine = BenchEngine(
        executor=_executor(MockProvider(), pricing, storage, tracer),
        provider=MockProvider(),
    )
    with pytest.raises(EvalConfigError, match="at least one --model"):
        await engine.run(bundle, input_payload={"text": "x"}, providers=[])


# ---------------------------------------------------------------------------
# Engine — with LLM judge
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_bench_with_judge_scores_each_model(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    provider = JudgeStubProvider(agent_response='{"message": "good"}', judge_score=0.9)
    executor = _executor(provider, pricing, storage, tracer)
    judge = JudgeConfig(
        method=JudgeMethod.LLM_JUDGE,
        model=ModelConfig(provider="anthropic/claude-sonnet-4-6"),
        rubric="be strict",
    )
    engine = BenchEngine(executor=executor, provider=provider, runs_per_model=1, judge=judge)
    summary = await engine.run(
        bundle,
        input_payload={"text": "hi"},
        providers=[
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",
        ],
    )
    # Anthropic model shares family with judge → judge skipped on that row.
    o = next(m for m in summary.models if m.provider.startswith("openai/"))
    a = next(m for m in summary.models if m.provider.startswith("anthropic/"))
    assert o.aggregated_score(summary.gate_mode) == pytest.approx(0.9)
    assert not o.skipped_score
    assert a.skipped_score
    assert a.aggregated_score(summary.gate_mode) is None


@pytest.mark.unit
async def test_bench_judge_requires_rubric(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    judge = JudgeConfig(
        method=JudgeMethod.LLM_JUDGE,
        model=ModelConfig(provider="anthropic/claude-sonnet-4-6"),
        # no rubric
    )
    with pytest.raises(EvalConfigError, match="requires a rubric"):
        BenchEngine(
            executor=_executor(MockProvider(), pricing, storage, tracer),
            provider=MockProvider(),
            judge=judge,
        )


@pytest.mark.unit
async def test_bench_judge_inline_rubric_overrides_judge_rubric(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    judge = JudgeConfig(
        method=JudgeMethod.LLM_JUDGE,
        model=ModelConfig(provider="anthropic/claude-sonnet-4-6"),
        rubric="be lenient",
    )
    provider = JudgeStubProvider(agent_response='{"message": "ok"}', judge_score=0.5)
    engine = BenchEngine(
        executor=_executor(provider, pricing, storage, tracer),
        provider=provider,
        judge=judge,
        rubric="be strict",
    )
    summary = await engine.run(
        bundle,
        input_payload={"text": "x"},
        providers=["openai/gpt-4o-mini-2024-07-18"],
    )
    # Inline rubric "be strict" wins over the JudgeConfig's "be lenient".
    assert provider.judge_prompts, "judge was not called"
    assert "be strict" in provider.judge_prompts[0]
    assert "be lenient" not in provider.judge_prompts[0]
    assert summary.models[0].aggregated_score(summary.gate_mode) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Engine — error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_bench_records_failed_runs_per_model(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    inner = MockProvider(response='{"message": "ok"}')
    provider = FailForProvider(fail_provider="openai/gpt-4o-mini-2024-07-18", inner=inner)
    executor = _executor(provider, pricing, storage, tracer)
    engine = BenchEngine(executor=executor, provider=provider, runs_per_model=2)
    summary = await engine.run(
        bundle,
        input_payload={"text": "hi"},
        providers=[
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",
        ],
    )
    o = next(m for m in summary.models if m.provider.startswith("openai/"))
    a = next(m for m in summary.models if m.provider.startswith("anthropic/"))
    # OpenAI is non-retryable AuthError → both runs fail. Anthropic is fine.
    assert o.error_count == 2
    assert a.error_count == 0
    assert a.cost_mean_usd > 0
    # The failed model still appears in the summary so the user sees what's broken.
    assert o.aggregated_score(summary.gate_mode) is None
    assert o.cost_mean_usd == 0.0  # no successful runs to average


@pytest.mark.unit
async def test_bench_rejects_zero_runs(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    with pytest.raises(EvalConfigError, match="runs_per_model"):
        BenchEngine(
            executor=_executor(MockProvider(), pricing, storage, tracer),
            provider=MockProvider(),
            runs_per_model=0,
        )


# ---------------------------------------------------------------------------
# Latency stats
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_latency_stats_reasonable(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    provider = MockProvider(response='{"message": "ok"}')
    engine = BenchEngine(
        executor=_executor(provider, pricing, storage, tracer),
        provider=provider,
        runs_per_model=3,
    )
    summary = await engine.run(
        bundle,
        input_payload={"text": "hi"},
        providers=["openai/gpt-4o-mini-2024-07-18"],
    )
    m = summary.models[0]
    assert m.latency_p50_ms >= 0
    assert m.latency_p95_ms >= m.latency_p50_ms


# ---------------------------------------------------------------------------
# BenchSummary.to_record() — Pydantic conversion for persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_to_record_collapses_summary_to_per_model_rows(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """``BenchSummary.to_record()`` returns a BenchRecord whose
    ``models`` list mirrors the live ``ModelBenchResult`` aggregates
    (successful_runs, cost_total/mean, latency p50/p95, score),
    plus a populated ``total_cost_usd`` rolled up across models."""
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = BenchEngine(executor=executor, provider=provider, runs_per_model=2)
    summary = await engine.run(
        bundle,
        input_payload={"text": "hi"},
        providers=[
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",
        ],
    )

    record = summary.to_record(tenant_id="tenant-a")
    assert record.agent == bundle.spec.name
    assert record.tenant_id == "tenant-a"
    assert record.judge_method is None  # no judge configured
    assert record.judge_provider is None
    assert record.runs_per_model == 2
    # input_hash is deterministic for stable inputs
    assert len(record.input_hash) == 16
    # Per-model rows mirror live aggregates
    assert len(record.models) == 2
    for live, persisted in zip(summary.models, record.models, strict=True):
        assert persisted.provider == live.provider
        assert persisted.successful_runs == len(live.successful_runs)
        assert persisted.error_count == live.error_count
        assert persisted.cost_total_usd == live.cost_total_usd
        assert persisted.cost_mean_usd == live.cost_mean_usd
        assert persisted.latency_p50_ms == live.latency_p50_ms
        assert persisted.latency_p95_ms == live.latency_p95_ms
    # total cost rolls up across all models
    assert record.total_cost_usd == pytest.approx(sum(m.cost_total_usd for m in summary.models))


@pytest.mark.unit
async def test_to_record_input_hash_stable_across_runs(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Two benches over the same input dict → same input_hash; different
    inputs → different hashes. Lets ``--baseline`` callers detect when
    they're comparing against a different input."""
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = BenchEngine(executor=executor, provider=provider, runs_per_model=1)

    s1 = await engine.run(
        bundle, input_payload={"text": "abc"}, providers=["openai/gpt-4o-mini-2024-07-18"]
    )
    s2 = await engine.run(
        bundle, input_payload={"text": "abc"}, providers=["openai/gpt-4o-mini-2024-07-18"]
    )
    s3 = await engine.run(
        bundle, input_payload={"text": "xyz"}, providers=["openai/gpt-4o-mini-2024-07-18"]
    )

    h1 = s1.to_record().input_hash
    h2 = s2.to_record().input_hash
    h3 = s3.to_record().input_hash

    assert h1 == h2  # same input → same hash
    assert h1 != h3  # different input → different hash
