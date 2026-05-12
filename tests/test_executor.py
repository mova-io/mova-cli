"""Executor end-to-end with MockProvider + InMemoryStorage."""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.executor import Executor
from movate.core.failures import (
    AuthError,
    ContentFilterError,
    ModelUnavailableError,
    MovateError,
    RateLimitError,
    SchemaError,
)
from movate.core.loader import load_agent
from movate.core.models import (
    JobStatus,
    ModelConfig,
    RunRequest,
    TokenUsage,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

# ---------------------------------------------------------------------------
# Test-local provider double (specific to this file's fallback-chain tests)
# ---------------------------------------------------------------------------


class FlakyProvider(BaseLLMProvider):
    """Raises a configured exception on the first N calls, then delegates to inner."""

    name = "flaky"
    version = "0.0.1"

    def __init__(self, raise_n: int, exc: Exception, then: BaseLLMProvider) -> None:
        self._remaining = raise_n
        self._exc = exc
        self._inner = then
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return await self._inner.complete(request)

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold(dst: Path, name: str = "demo") -> Path:
    """Back-compat shim for existing test bodies."""
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_happy_path(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    executor = Executor(
        provider=MockProvider(response='{"message": "hello"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))

    assert response.status == "success"
    assert response.data == {"message": "hello"}
    assert response.metrics.provider == "openai/gpt-4o-mini-2024-07-18"
    assert response.metrics.cost_usd > 0  # mock reports tokens; price > 0
    assert response.error is None
    # Persisted
    assert len(storage.runs) == 1
    assert storage.runs[0].status == JobStatus.SUCCESS
    assert storage.runs[0].provider == "openai/gpt-4o-mini-2024-07-18"


# ---------------------------------------------------------------------------
# Schema failures
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_input_schema_failure(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"wrong_key": "x"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "schema_error"
    assert len(storage.failures) == 1


@pytest.mark.unit
async def test_executor_output_schema_failure(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    # MockProvider returns JSON missing the required "message" field
    bad = MockProvider(response='{"oops": "wrong-shape"}')
    executor = Executor(provider=bad, pricing=pricing, storage=storage, tracer=tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "schema_error"


@pytest.mark.unit
async def test_executor_non_json_output(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    bad = MockProvider(response='"a json string but not an object"')
    executor = Executor(provider=bad, pricing=pricing, storage=storage, tracer=tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "schema_error"


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_budget_breach(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    yaml = agent_dir / "agent.yaml"
    yaml.write_text(yaml.read_text().replace("0.50", "0.0000001"))
    bundle = load_agent(agent_dir)
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "cost_budget_exceeded"


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_fallback_after_model_unavailable(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """ModelUnavailable on primary → exec walks the fallback chain."""
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    inner = MockProvider(response='{"message": "fallback worked"}')
    flaky = FlakyProvider(raise_n=10, exc=ModelUnavailableError("boom"), then=inner)
    executor = Executor(provider=flaky, pricing=pricing, storage=storage, tracer=tracer)

    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    # The default fallback in the template is anthropic; provider gets one shot
    # at the primary (and exhausts its retries), then walks to fallback.
    # Our flaky provider raises on every call, so the eventual fallback also
    # fails — but the executor records *that* as the final outcome. Verify
    # observability instead: a fallback_triggered event was logged.
    assert any(e.get("fallback_triggered") for e in tracer.events)
    assert response.status == "error"


@pytest.mark.unit
async def test_executor_fallback_recovers_on_second_provider(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """First provider exhausts; second provider succeeds on the same call sequence."""
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    inner = MockProvider(response='{"message": "yay"}')
    # 3 unavail attempts on primary, then chain falls through to fallback
    # (provider attempt 4 = first attempt against fallback, succeeds).
    flaky = FlakyProvider(raise_n=3, exc=ModelUnavailableError("boom"), then=inner)
    executor = Executor(provider=flaky, pricing=pricing, storage=storage, tracer=tracer)

    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    assert response.data == {"message": "yay"}
    # The chosen provider should be the *fallback* one, not the primary.
    assert response.metrics.provider == "anthropic/claude-haiku-4-5-20251001"
    assert any(e.get("fallback_triggered") for e in tracer.events)


# ---------------------------------------------------------------------------
# Auth error / non-retryable taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_auth_error_non_retryable(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    inner = MockProvider()
    flaky = FlakyProvider(raise_n=1, exc=AuthError("nope"), then=inner)
    executor = Executor(provider=flaky, pricing=pricing, storage=storage, tracer=tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "auth_error"
    # No retry happened — the flaky provider was called exactly once.
    assert flaky.calls == 1


@pytest.mark.unit
async def test_executor_content_filter_marks_safety_blocked(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    inner = MockProvider()
    flaky = FlakyProvider(raise_n=1, exc=ContentFilterError("blocked"), then=inner)
    executor = Executor(provider=flaky, pricing=pricing, storage=storage, tracer=tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "safety_blocked"
    assert response.error is not None
    assert response.error.type == "content_filter"


# ---------------------------------------------------------------------------
# model_override (used by bench in v0.2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_model_override_skips_fallback(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    inner = MockProvider(response='{"message": "ok"}')
    executor = Executor(provider=inner, pricing=pricing, storage=storage, tracer=tracer)

    response = await executor.execute(
        bundle,
        RunRequest(agent="demo", input={"text": "hi"}),
        model_override=ModelConfig(provider="anthropic/claude-haiku-4-5-20251001"),
    )
    assert response.status == "success"
    assert response.metrics.provider == "anthropic/claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Cost-drift warning
# ---------------------------------------------------------------------------


class _DriftProvider(BaseLLMProvider):
    name = "drift"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        # Report a cost wildly different from the pricing table to trip drift.
        return CompletionResponse(
            text='{"message": "ok"}',
            tokens=TokenUsage(input=100, output=50),
            raw={"litellm_cost_usd": 9999.0},
        )

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.unit
async def test_cost_drift_logs_event(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Bump the budget so we don't trip BudgetExceeded before drift is checked.
    agent_dir = _scaffold(tmp_path / "demo")
    yaml = agent_dir / "agent.yaml"
    yaml.write_text(yaml.read_text().replace("0.50", "1000000.0"))
    bundle = load_agent(agent_dir)

    executor = Executor(provider=_DriftProvider(), pricing=pricing, storage=storage, tracer=tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    assert any("cost_drift" in e for e in tracer.events)


# ---------------------------------------------------------------------------
# Smoke: error types are imported and stable
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_mirrors_cost_and_token_attrs_onto_span(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Every successful run sets ``cost_usd``, ``pricing_version``,
    ``chosen_provider``, and per-token ``tokens.input`` / ``tokens.output``
    / ``tokens.cached_input`` on the agent.execute span — so Langfuse
    / OTel consumers can build cost dashboards by filtering on those
    attrs without joining back to RunRecord.

    The values match what's persisted on the RunRecord; the storage
    row is canonical if anything drifts."""
    agent_dir = _scaffold(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    executor = Executor(
        provider=MockProvider(response='{"message": "hi"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )

    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"

    # Filter attribute calls down to the agent.execute span only —
    # provider-level spans might add their own attrs later.
    attrs = {k: v for (span_name, k, v) in tracer.attribute_calls if span_name == "agent.execute"}

    # All six required attrs are present.
    assert "cost_usd" in attrs
    assert "pricing_version" in attrs
    assert "chosen_provider" in attrs
    assert "tokens.input" in attrs
    assert "tokens.output" in attrs
    assert "tokens.cached_input" in attrs

    # Values match the persisted RunRecord (storage is canonical).
    assert attrs["cost_usd"] == response.metrics.cost_usd
    assert attrs["pricing_version"] == response.metrics.pricing_version
    assert attrs["chosen_provider"] == response.metrics.provider
    assert attrs["tokens.input"] == response.metrics.tokens.input
    assert attrs["tokens.output"] == response.metrics.tokens.output
    assert attrs["tokens.cached_input"] == response.metrics.tokens.cached_input


@pytest.mark.unit
def test_typed_failure_types_distinct() -> None:
    assert SchemaError("x").__class__ is not RateLimitError("y").__class__
    assert isinstance(SchemaError("x"), MovateError)
