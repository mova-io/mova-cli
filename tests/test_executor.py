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
    AgentRuntime,
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
from movate.providers.registry import ProviderRegistry
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
# Streaming (executor.execute with on_token callback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_streaming_invokes_callback_with_chunks(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """When ``on_token`` is set, the executor uses ``provider.stream()``
    and surfaces every text delta via the callback. The accumulated
    response is still schema-validated and persisted normally —
    streaming is purely an observation channel."""
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    executor = Executor(
        provider=MockProvider(response='{"message": "hello world"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )

    chunks: list[str] = []

    response = await executor.execute(
        bundle,
        RunRequest(agent="demo", input={"text": "hi"}),
        on_token=chunks.append,
    )

    # Callback fired at least once with content (mock yields 10-char
    # slices, so for "{\"message\": \"hello world\"}" we'd expect ≥ 2).
    assert len(chunks) >= 1
    # Concatenated chunks form the final response text.
    assert "".join(chunks) == '{"message": "hello world"}'
    # Same success path as non-streaming.
    assert response.status == "success"
    assert response.data == {"message": "hello world"}
    # Same persistence (RunRecord saved).
    assert len(storage.runs) == 1
    # Cost still accounted (tokens come from the final usage-only
    # stream chunk; if that path broke, cost would be 0).
    assert response.metrics.cost_usd > 0


@pytest.mark.unit
async def test_executor_streaming_off_by_default_uses_complete(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Without ``on_token``, the executor uses ``provider.complete()``
    — proves we didn't accidentally tip the default path into the
    streaming branch."""

    class CountingMock(MockProvider):
        complete_calls = 0
        stream_calls = 0

        async def complete(self, request):  # type: ignore[no-untyped-def]
            CountingMock.complete_calls += 1
            return await super().complete(request)

        async def stream(self, request):  # type: ignore[no-untyped-def]
            CountingMock.stream_calls += 1
            async for chunk in super().stream(request):
                yield chunk

    bundle = load_agent(_scaffold(tmp_path / "demo"))
    executor = Executor(
        provider=CountingMock(response='{"message": "hi"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )

    await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert CountingMock.complete_calls == 1
    assert CountingMock.stream_calls == 0


# ---------------------------------------------------------------------------
# Provider registry dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_dispatches_via_registry_when_runtime_registered(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """An agent declaring ``runtime: native_anthropic`` should be
    dispatched to the registered provider for that runtime — proves
    the registry seam works end-to-end when an adapter is wired."""
    import yaml  # noqa: PLC0415

    bundle_dir = _scaffold(tmp_path / "anthropic-demo")
    # Promote the agent to runtime: native_anthropic.
    yaml_path = bundle_dir / "agent.yaml"
    spec_dict = yaml.safe_load(yaml_path.read_text())
    spec_dict["runtime"] = AgentRuntime.NATIVE_ANTHROPIC.value
    yaml_path.write_text(yaml.safe_dump(spec_dict))

    bundle = load_agent(bundle_dir)
    assert bundle.spec.runtime == AgentRuntime.NATIVE_ANTHROPIC

    # Build a registry with a distinct stub for the anthropic runtime
    # so we can verify dispatch picked the right one.
    litellm_stub = MockProvider(response='{"message": "from litellm"}')
    anthropic_stub = MockProvider(response='{"message": "from anthropic"}')
    registry = ProviderRegistry(default_litellm=litellm_stub)
    registry.register(AgentRuntime.NATIVE_ANTHROPIC, anthropic_stub)

    executor = Executor(
        registry=registry,
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    # The anthropic stub answered — not the litellm one.
    assert response.data == {"message": "from anthropic"}


@pytest.mark.unit
async def test_executor_uses_pricing_key_translation_for_native_runtimes(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Native-runtime agents declare bare model ids; the executor must
    ask the adapter for its pricing-table key and use that, not the
    bare id (which would KeyError).

    Regression test for a real bug: before this fix, a working
    ``runtime: native_anthropic`` agent crashed on the first run with
    ``KeyError: 'claude-haiku-4-5-20251001'`` because pricing.yaml uses
    the ``anthropic/`` prefix."""
    import yaml  # noqa: PLC0415

    bundle_dir = _scaffold(tmp_path / "native-cost-demo")
    # Bare-id native_anthropic agent.
    yaml_path = bundle_dir / "agent.yaml"
    spec_dict = yaml.safe_load(yaml_path.read_text())
    spec_dict["runtime"] = AgentRuntime.NATIVE_ANTHROPIC.value
    spec_dict["model"]["provider"] = "claude-haiku-4-5-20251001"
    yaml_path.write_text(yaml.safe_dump(spec_dict))

    bundle = load_agent(bundle_dir)

    class FakeAnthropic(MockProvider):
        """Mock that reports its pricing key the way AnthropicProvider does."""

        def pricing_key(self, provider: str) -> str:
            return f"anthropic/{provider}" if not provider.startswith("anthropic/") else provider

    registry = ProviderRegistry(default_litellm=MockProvider())
    registry.register(AgentRuntime.NATIVE_ANTHROPIC, FakeAnthropic())
    executor = Executor(
        registry=registry,
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    # Run succeeded — no KeyError mid-flight.
    assert response.status == "success"
    # Cost > 0 — pricing lookup found the prefixed key. (Mock's tokens
    # are non-zero; if the pricing_key translation had been skipped,
    # this would have been 0.0 from the KeyError fallback.)
    assert response.metrics.cost_usd > 0


@pytest.mark.unit
async def test_executor_records_zero_cost_when_pricing_key_is_none(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """LangChain adapter returns ``pricing_key=None`` because the model
    is hidden inside the Runnable. The executor should record cost=0
    rather than crash with KeyError."""
    import yaml  # noqa: PLC0415

    bundle_dir = _scaffold(tmp_path / "langchain-demo")
    yaml_path = bundle_dir / "agent.yaml"
    spec_dict = yaml.safe_load(yaml_path.read_text())
    spec_dict["runtime"] = AgentRuntime.LANGCHAIN.value
    spec_dict["model"]["provider"] = "fake.module:fake_factory"
    yaml_path.write_text(yaml.safe_dump(spec_dict))
    bundle = load_agent(bundle_dir)

    class OpaqueModelProvider(MockProvider):
        def pricing_key(self, provider: str) -> str | None:
            return None

    registry = ProviderRegistry(default_litellm=MockProvider())
    registry.register(AgentRuntime.LANGCHAIN, OpaqueModelProvider())
    executor = Executor(registry=registry, pricing=pricing, storage=storage, tracer=tracer)
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    assert response.metrics.cost_usd == 0.0


@pytest.mark.unit
async def test_executor_rejects_unregistered_runtime_at_execute_time(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """If an agent's ``runtime:`` field doesn't have a registered
    provider (the v0.5 baseline for native_anthropic / native_openai /
    langchain), the executor surfaces a schema_error — same exit shape
    as a bad input schema. Retries don't help here, so failing fast is
    the right call."""
    import yaml  # noqa: PLC0415

    bundle_dir = _scaffold(tmp_path / "unwired")
    yaml_path = bundle_dir / "agent.yaml"
    spec_dict = yaml.safe_load(yaml_path.read_text())
    spec_dict["runtime"] = AgentRuntime.LANGCHAIN.value
    # LangChain agents use entry-point specs (must contain a colon) —
    # the AgentSpec cross-field validator rejects bare strings.
    spec_dict["model"] = {"provider": "fake.module:fake_factory"}
    yaml_path.write_text(yaml.safe_dump(spec_dict))

    bundle = load_agent(bundle_dir)
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "schema_error"
    # The error message names the missing runtime so the operator can
    # tell what's not wired.
    assert "langchain" in response.error.message


@pytest.mark.unit
def test_executor_requires_provider_or_registry(
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """At least one of ``provider=`` or ``registry=`` must be passed —
    construct-time validation prevents the "no provider wired" footgun."""
    with pytest.raises(ValueError, match="provider= or registry="):
        Executor(pricing=pricing, storage=storage, tracer=tracer)


@pytest.mark.unit
def test_executor_rejects_both_provider_and_registry(
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Passing BOTH is ambiguous — which one wins? Reject construction
    so the caller picks one explicitly."""
    with pytest.raises(ValueError, match="not both"):
        Executor(
            provider=MockProvider(),
            registry=ProviderRegistry(default_litellm=MockProvider()),
            pricing=pricing,
            storage=storage,
            tracer=tracer,
        )


@pytest.mark.unit
async def test_executor_enforces_runtime_policy_at_execute_time(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """A bundle that opts into a banned runtime should fail at execute
    time with a policy_violation error — even if validate was skipped.
    Defense-in-depth: a worker that loaded an agent over HTTP can't
    bypass the project's 'A by default' stance."""
    import yaml  # noqa: PLC0415

    from movate.core.config import RuntimePolicy  # noqa: PLC0415

    bundle_dir = _scaffold(tmp_path / "demo")
    yaml_path = bundle_dir / "agent.yaml"
    spec_dict = yaml.safe_load(yaml_path.read_text())
    spec_dict["runtime"] = AgentRuntime.NATIVE_ANTHROPIC.value
    yaml_path.write_text(yaml.safe_dump(spec_dict))
    bundle = load_agent(bundle_dir)

    # Build a registry that DOES have native_anthropic wired — so the
    # rejection is from the policy, not from "runtime missing".
    registry = ProviderRegistry(default_litellm=MockProvider())
    registry.register(AgentRuntime.NATIVE_ANTHROPIC, MockProvider())

    executor = Executor(
        registry=registry,
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        runtime_policy=RuntimePolicy(allowed=[AgentRuntime.LITELLM]),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "policy_violation"
    assert "native_anthropic" in response.error.message
    assert "litellm" in response.error.message


@pytest.mark.unit
async def test_executor_prepends_history_to_provider_messages(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """``execute(history=[...])`` should prepend the history to the
    Messages list passed to the provider. The CURRENT turn's rendered
    prompt comes after history as the final user message.

    This is the seam ``movate chat`` uses for conversation memory."""
    from movate.providers.base import (  # noqa: PLC0415
        BaseLLMProvider,
        CompletionResponse,
    )
    from movate.providers.base import (
        Message as ProviderMessage,
    )

    captured_messages: list[ProviderMessage] = []

    class CapturingProvider(BaseLLMProvider):
        name = "capture"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            captured_messages.extend(request.messages)
            return CompletionResponse(
                text='{"message": "ok"}',
                tokens=TokenUsage(input=5, output=3),
            )

        async def stream(self, request):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def embed(self, text, *, model):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    bundle = load_agent(_scaffold(tmp_path / "demo"))
    executor = Executor(
        provider=CapturingProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )

    history = [
        ProviderMessage(role="user", content="first turn"),
        ProviderMessage(role="assistant", content="first reply"),
    ]
    await executor.execute(
        bundle,
        RunRequest(agent="demo", input={"text": "second turn"}),
        history=history,
    )

    # 3 messages reached the provider: 2 history + 1 current rendered prompt.
    assert len(captured_messages) == 3
    assert captured_messages[0].role == "user"
    assert captured_messages[0].content == "first turn"
    assert captured_messages[1].role == "assistant"
    assert captured_messages[1].content == "first reply"
    # The last message is the current turn's rendered prompt. The
    # default template renders `echo {{ input.text }}` → its content
    # includes "second turn" from the request.input.
    assert captured_messages[2].role == "user"
    assert "second turn" in captured_messages[2].content


@pytest.mark.unit
async def test_executor_runtime_policy_permissive_by_default(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Without an explicit ``runtime_policy=``, the executor lets every
    registered runtime through — matches the v0.5 baseline behavior so
    existing tests / code paths aren't affected."""
    import yaml  # noqa: PLC0415

    bundle_dir = _scaffold(tmp_path / "demo")
    yaml_path = bundle_dir / "agent.yaml"
    spec_dict = yaml.safe_load(yaml_path.read_text())
    spec_dict["runtime"] = AgentRuntime.NATIVE_ANTHROPIC.value
    yaml_path.write_text(yaml.safe_dump(spec_dict))
    bundle = load_agent(bundle_dir)

    registry = ProviderRegistry(default_litellm=MockProvider())
    registry.register(AgentRuntime.NATIVE_ANTHROPIC, MockProvider())

    executor = Executor(
        registry=registry,
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        # No runtime_policy — permissive default.
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"


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
def test_typed_failure_types_distinct() -> None:
    assert SchemaError("x").__class__ is not RateLimitError("y").__class__
    assert isinstance(SchemaError("x"), MovateError)
