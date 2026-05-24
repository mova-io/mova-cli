"""Executor wiring for per-tenant BYOK provider keys (ADR 018).

Two things matter most here:

1. **No-config path is byte-for-byte unchanged.** With no tenant key (and the
   shared-key fallback on, the default), the executor passes NO ``api_key`` to
   the provider — exactly as before BYOK. This is the back-compat guard.
2. When the calling tenant HAS a key, the executor resolves it and threads it
   into the provider call via the existing ``api_key`` param (LiteLLM runtime).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import AgentRuntime, RunRequest
from movate.core.provider_keys import ENV_ALLOW_SHARED, mint_tenant_provider_key
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import load_pricing
from movate.providers.registry import ProviderRegistry
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

_FERNET_KEY = Fernet.generate_key()


class _RecordingProvider(BaseLLMProvider):
    """Captures the params of every complete() call so tests can assert
    whether (and which) ``api_key`` was threaded through."""

    name = "litellm"
    version = "0.0.1"

    def __init__(self, response: str = '{"message": "ok"}') -> None:
        self._response = response
        self.seen_params: list[dict] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.seen_params.append(dict(request.params))
        from movate.core.models import TokenUsage  # noqa: PLC0415

        return CompletionResponse(
            text=self._response,
            tokens=TokenUsage(input=5, output=5),
            raw={"mock": True},
        )

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _executor(storage: InMemoryStorage, provider: _RecordingProvider) -> Executor:
    registry = ProviderRegistry(default_litellm=provider)
    return Executor(
        registry=registry,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )


@pytest.mark.unit
async def test_no_tenant_key_passes_no_api_key_back_compat(
    tmp_path: Path, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BACK-COMPAT GUARD: no tenant key + fallback on (default) → the provider
    request carries NO api_key, byte-for-byte the pre-BYOK behavior."""
    monkeypatch.delenv(ENV_ALLOW_SHARED, raising=False)  # default on
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    provider = _RecordingProvider()
    executor = _executor(storage, provider)

    resp = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert resp.status == "success"
    assert provider.seen_params, "provider should have been called"
    # The crux: no api_key was injected → the SDK uses its env default exactly
    # as before BYOK.
    assert all("api_key" not in p for p in provider.seen_params)


@pytest.mark.unit
async def test_tenant_key_is_injected_into_params(
    tmp_path: Path, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the calling tenant has its own key, it's threaded into api_key."""
    monkeypatch.setenv("MOVATE_PROVIDER_KEY_SECRET", _FERNET_KEY.decode())
    # The scaffolded agent runs runtime: litellm with provider openai/... →
    # BYOK key namespace "openai". Store a key for the run's tenant ("local").
    await storage.save_tenant_provider_key(
        mint_tenant_provider_key(
            tenant_id="local",
            provider="openai",
            plaintext="sk-tenant-byok",
            fernet=Fernet(_FERNET_KEY),
        )
    )
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    provider = _RecordingProvider()
    executor = _executor(storage, provider)

    resp = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert resp.status == "success"
    assert provider.seen_params[0].get("api_key") == "sk-tenant-byok"


@pytest.mark.unit
async def test_other_tenant_key_not_used(
    tmp_path: Path, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key stored for a DIFFERENT tenant is never used for this run."""
    monkeypatch.delenv(ENV_ALLOW_SHARED, raising=False)
    monkeypatch.setenv("MOVATE_PROVIDER_KEY_SECRET", _FERNET_KEY.decode())
    await storage.save_tenant_provider_key(
        mint_tenant_provider_key(
            tenant_id="someone-else",
            provider="openai",
            plaintext="sk-not-yours",
            fernet=Fernet(_FERNET_KEY),
        )
    )
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    provider = _RecordingProvider()
    executor = _executor(storage, provider)  # runs as tenant "local"

    await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    # tenant "local" has no key → no injection; never sees "someone-else"'s key.
    assert all(p.get("api_key") != "sk-not-yours" for p in provider.seen_params)
    assert all("api_key" not in p for p in provider.seen_params)


@pytest.mark.unit
async def test_native_runtime_does_not_inject(
    tmp_path: Path, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native runtimes can't take a per-call api_key — injection is skipped
    (they fall through to the env default, unchanged)."""
    monkeypatch.setenv("MOVATE_PROVIDER_KEY_SECRET", _FERNET_KEY.decode())
    await storage.save_tenant_provider_key(
        mint_tenant_provider_key(
            tenant_id="local",
            provider="openai",
            plaintext="sk-tenant-byok",
            fernet=Fernet(_FERNET_KEY),
        )
    )
    # Build a native-runtime agent + register the recording provider for it.
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    bundle.spec.runtime = AgentRuntime.NATIVE_OPENAI
    bundle.spec.model.provider = "gpt-4o-mini"  # native bare id
    provider = _RecordingProvider()
    provider.name = "native_openai"
    registry = ProviderRegistry(default_litellm=_RecordingProvider())
    registry.register(AgentRuntime.NATIVE_OPENAI, provider)
    executor = Executor(
        registry=registry,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )

    await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert all("api_key" not in p for p in provider.seen_params)
