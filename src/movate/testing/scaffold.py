"""Scaffolding helpers for agent tests.

* :func:`scaffold_agent` — clone the packaged ``agent_init`` template into a
  directory and substitute the agent name.
* :func:`build_test_executor` — wire :class:`InMemoryStorage` +
  :class:`NullTracer` + a provider into a ready-to-use :class:`Executor`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from movate.core.executor import Executor
from movate.providers.base import BaseLLMProvider
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.templates import get_template_path
from movate.testing.doubles import InMemoryStorage, NullTracer


def scaffold_agent(dst: Path, *, name: str = "demo", template: str = "default") -> Path:
    """Clone a packaged agent template into ``dst`` and stamp ``name``.

    ``template`` is one of the names in :mod:`movate.templates.TEMPLATES`
    (``default``, ``faq``, ``summarizer``, ``classifier``). Returns ``dst``
    for chaining. The destination must not already exist.
    """
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    src = get_template_path(template)
    shutil.copytree(src, dst)
    yaml = dst / "agent.yaml"
    yaml.write_text(yaml.read_text().replace("__AGENT_NAME__", name))
    return dst


def build_test_executor(
    *,
    provider: BaseLLMProvider | None = None,
    response: str | None = None,
    pricing: PricingTable | None = None,
    tenant_id: str = "test",
) -> tuple[Executor, BaseLLMProvider, InMemoryStorage, NullTracer]:
    """Construct (executor, provider, storage, tracer) for an agent test.

    * If ``provider`` is given, it's used as-is. Otherwise a ``MockProvider``
      is constructed; ``response`` (when given) configures the agent reply.
    * Storage and tracer are always test doubles.
    * Pricing defaults to the packaged production table so cost calculations
      match real runs.
    """
    chosen: BaseLLMProvider = provider or MockProvider(response=response)
    storage = InMemoryStorage()
    tracer = NullTracer()
    executor = Executor(
        provider=chosen,
        pricing=pricing or load_pricing(),
        storage=storage,
        tracer=tracer,
        tenant_id=tenant_id,
    )
    return executor, chosen, storage, tracer
