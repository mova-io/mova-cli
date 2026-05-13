"""Shared CLI helpers: build provider/storage/tracer/executor for a local run."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from movate.core.config import load_project_config
from movate.core.executor import Executor
from movate.core.models import AgentRuntime
from movate.providers.base import BaseLLMProvider
from movate.providers.litellm import LiteLLMProvider
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.providers.registry import ProviderRegistry
from movate.storage import StorageProvider, build_storage
from movate.tracing import Tracer, build_tracer


@dataclass
class LocalRuntime:
    """Bundle of long-lived collaborators for one CLI invocation."""

    executor: Executor
    provider: BaseLLMProvider
    storage: StorageProvider
    tracer: Tracer


def _try_register_native_adapters(registry: ProviderRegistry, *, mock: bool) -> None:
    """Opportunistically register optional native-SDK adapters.

    Each adapter is gated by an ``[optional-dependency]`` extra
    (``anthropic``, ``openai``, ``langchain``) — if the extra isn't
    installed, the import fails and we silently skip registration.
    The user's agent.yaml will then fail at ``movate validate`` with
    "runtime not registered" instead of a cryptic ImportError mid-run.

    ``mock=True`` short-circuits everything to the MockProvider so
    smoke tests / offline dev never hit real SDKs."""
    if mock:
        return  # MockProvider is wired for every runtime via the registry default
    try:
        from movate.providers.anthropic import AnthropicProvider  # noqa: PLC0415

        registry.register(AgentRuntime.NATIVE_ANTHROPIC, AnthropicProvider())
    except ImportError:
        pass
    try:
        from movate.providers.openai_native import OpenAIProvider  # noqa: PLC0415

        registry.register(AgentRuntime.NATIVE_OPENAI, OpenAIProvider())
    except ImportError:
        pass
    try:
        # LangChain provider needs no constructor args — the user's
        # Runnable entry-point is resolved per-request from the
        # agent's model.provider field.
        import langchain_core  # noqa: F401, PLC0415

        from movate.providers.langchain_native import LangChainProvider  # noqa: PLC0415

        registry.register(AgentRuntime.LANGCHAIN, LangChainProvider())
    except ImportError:
        pass
    # Lyzr adapter is HTTP-only (no SDK dep), so we always register it
    # — the constructor doesn't fail on a missing LYZR_API_KEY (we
    # surface the AuthError on first ``complete()`` call instead). This
    # lets ``movate validate`` of a ``runtime: lyzr`` agent succeed
    # even before the operator has set the env var.
    from movate.providers.lyzr import LyzrProvider  # noqa: PLC0415

    registry.register(AgentRuntime.LYZR, LyzrProvider())


async def build_local_runtime(*, mock: bool) -> LocalRuntime:
    """Construct the local runtime for a CLI invocation.

    Storage: SQLite at ``~/.movate/local.db``.
    Tracer: stdout-on-stderr (Langfuse + OTel land via env opt-in).
    Providers: a :class:`ProviderRegistry` with LiteLLM wired by default
    plus any native-SDK adapters whose extras are installed. ``mock=True``
    swaps every runtime for :class:`MockProvider` so offline dev / tests
    don't need real API keys.
    """
    storage = build_storage()
    await storage.init()
    tracer = build_tracer()
    pricing = load_pricing()
    # Load the project policy once per CLI invocation. Permissive default
    # if movate.yaml is absent or has no `policy:` block — local-only
    # devs never trip a policy check they didn't set up.
    project_cfg = load_project_config()

    provider: BaseLLMProvider = MockProvider() if mock else LiteLLMProvider()
    registry = ProviderRegistry(default_litellm=provider)
    if mock:
        # Under --mock, register the mock for every known runtime so
        # an agent with `runtime: native_anthropic` still smoke-tests
        # against the deterministic mock response.
        for rt in AgentRuntime:
            registry.register(rt, provider)
    else:
        _try_register_native_adapters(registry, mock=mock)

    executor = Executor(
        registry=registry,
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        tenant_id="local",
        policy=project_cfg.policy,
        runtime_policy=project_cfg.runtime,
    )
    return LocalRuntime(executor=executor, provider=provider, storage=storage, tracer=tracer)


async def shutdown_runtime(storage: StorageProvider, tracer: Tracer) -> None:
    """Flush the tracer if supported, then close storage."""
    flush = getattr(tracer, "flush", None)
    if callable(flush):
        with contextlib.suppress(Exception):
            flush()
    await storage.close()
