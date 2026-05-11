"""Tests for the provider registry that maps :class:`AgentRuntime`
to a :class:`BaseLLMProvider`.

The registry is the seam through which native-SDK and LangChain
adapters (Tier-2 #6/#7/#8) plug in. v0.5 only registers LITELLM —
this suite locks the contract so future adapters drop in without
breaking the dispatch shape.
"""

from __future__ import annotations

import pytest

from movate.core.models import AgentRuntime
from movate.providers.registry import ProviderRegistry, UnregisteredRuntimeError
from movate.providers.mock import MockProvider


@pytest.fixture
def stub_provider() -> MockProvider:
    return MockProvider()


@pytest.mark.unit
def test_litellm_registered_by_default(stub_provider: MockProvider) -> None:
    """Every registry has LITELLM wired from construction — every
    movate install ships a LiteLLM provider as the baseline."""
    registry = ProviderRegistry(default_litellm=stub_provider)
    assert registry.is_registered(AgentRuntime.LITELLM)
    assert registry.get(AgentRuntime.LITELLM) is stub_provider


@pytest.mark.unit
def test_other_runtimes_unregistered_by_default(stub_provider: MockProvider) -> None:
    """Native + LangChain runtimes are NOT auto-registered. Calling
    .get() for them raises UnregisteredRuntimeError so ``movate validate``
    can fail loud at parse time."""
    registry = ProviderRegistry(default_litellm=stub_provider)
    for runtime in (
        AgentRuntime.NATIVE_ANTHROPIC,
        AgentRuntime.NATIVE_OPENAI,
        AgentRuntime.LANGCHAIN,
    ):
        assert not registry.is_registered(runtime)
        with pytest.raises(UnregisteredRuntimeError) as exc_info:
            registry.get(runtime)
        # Error message names the requested runtime and the available set.
        assert runtime.value in str(exc_info.value)
        assert "litellm" in str(exc_info.value)


@pytest.mark.unit
def test_register_adds_runtime(stub_provider: MockProvider) -> None:
    """Registering a runtime makes it lookup-able + listable."""

    class FakeAnthropic(MockProvider):
        name = "fake_anthropic"

    registry = ProviderRegistry(default_litellm=stub_provider)
    anthropic_provider = FakeAnthropic()
    registry.register(AgentRuntime.NATIVE_ANTHROPIC, anthropic_provider)

    assert registry.is_registered(AgentRuntime.NATIVE_ANTHROPIC)
    assert registry.get(AgentRuntime.NATIVE_ANTHROPIC) is anthropic_provider
    # Registered set now includes both.
    assert set(registry.registered_runtimes()) == {
        AgentRuntime.LITELLM,
        AgentRuntime.NATIVE_ANTHROPIC,
    }


@pytest.mark.unit
def test_register_replaces_previous(stub_provider: MockProvider) -> None:
    """Last-writer-wins. Tests use this to swap in mocks per-runtime
    without having to construct a fresh registry."""
    registry = ProviderRegistry(default_litellm=stub_provider)
    new_provider = MockProvider()
    registry.register(AgentRuntime.LITELLM, new_provider)
    assert registry.get(AgentRuntime.LITELLM) is new_provider
    assert registry.get(AgentRuntime.LITELLM) is not stub_provider


@pytest.mark.unit
def test_unregistered_error_is_lookup_subclass(stub_provider: MockProvider) -> None:
    """``UnregisteredRuntimeError`` is a ``LookupError`` so callers
    that handle dict misses generically still catch it. But the
    subclass lets ``movate validate`` produce a runtime-specific
    error message."""
    registry = ProviderRegistry(default_litellm=stub_provider)
    with pytest.raises(LookupError):
        registry.get(AgentRuntime.NATIVE_OPENAI)
