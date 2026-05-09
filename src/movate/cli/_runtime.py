"""Shared CLI helpers: build provider/storage/tracer/executor for a local run."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from movate.core.executor import Executor
from movate.providers.base import BaseLLMProvider
from movate.providers.litellm import LiteLLMProvider
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.storage import StorageProvider, build_storage
from movate.tracing import Tracer, build_tracer


@dataclass
class LocalRuntime:
    """Bundle of long-lived collaborators for one CLI invocation."""

    executor: Executor
    provider: BaseLLMProvider
    storage: StorageProvider
    tracer: Tracer


async def build_local_runtime(*, mock: bool) -> LocalRuntime:
    """Construct the local runtime for a CLI invocation.

    Storage: SQLite at ``~/.movate/local.db`` (Postgres lands v0.5).
    Tracer: stdout-on-stderr (Langfuse + OTel land v0.4).
    Provider: ``MockProvider`` if ``mock=True``, else ``LiteLLMProvider``.
    """
    storage = build_storage()
    await storage.init()
    tracer = build_tracer()
    pricing = load_pricing()

    provider: BaseLLMProvider = MockProvider() if mock else LiteLLMProvider()

    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        tenant_id="local",
    )
    return LocalRuntime(executor=executor, provider=provider, storage=storage, tracer=tracer)


async def shutdown_runtime(storage: StorageProvider, tracer: Tracer) -> None:
    """Flush the tracer if supported, then close storage."""
    flush = getattr(tracer, "flush", None)
    if callable(flush):
        with contextlib.suppress(Exception):
            flush()
    await storage.close()
