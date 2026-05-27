"""Post-apply verify-and-self-correct primitives (ADR 025 D3).

After an apply, the driver runs:

1. **validate** — reuse :func:`movate.core.loader.load_agent` (the structural
   sensor; same path ``mdk validate`` uses). A failure raises ``AgentLoadError``
   carrying the friendly error (#119) — the driver reverts (D4) and returns it.
2. **run --mock** — reuse the executor's mock path (:class:`MockProvider` +
   :class:`InMemoryStorage`) so the smoke is hermetic: no API keys, no
   ``~/.movate`` writes. We deliberately build an in-memory runtime here rather
   than ``build_local_runtime`` (which defaults to a SQLite store under the home
   dir) so the verify loop never touches a developer's machine state.
3. **eval** — optional, deferred to the caller (cost-bearing); the driver
   exposes the hook but does not auto-run it.

All of this is wired at the edges (the driver), never imported into execution
logic — preserving the boundary rule.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from movate.core.config import AgentDefaults
from movate.core.loader import AgentLoadError, load_agent
from movate.core.models import AgentRuntime, RunRequest

if TYPE_CHECKING:
    from movate.core.loader import AgentBundle


def validate_agent(agent_dir: Path) -> AgentBundle:
    """Load + validate an agent directory. Raises ``AgentLoadError`` on failure.

    Uses an empty :class:`AgentDefaults` so validation is against the pristine
    agent.yaml (no project-policy merge) — the same escape hatch library
    callers use, keeping the verify deterministic regardless of cwd.
    """
    return load_agent(agent_dir, defaults=AgentDefaults())


def mock_run(bundle: AgentBundle) -> bool:
    """Run the agent once against the deterministic mock provider (hermetic).

    Returns ``True`` when the run returns ``status == "success"``. Builds an
    in-memory storage + MockProvider executor inline so nothing touches the
    home dir or the network. Mirrors ``mdk run --mock`` semantics, including
    feeding the bundle's eval dataset (if any) to the mock so the canned
    response satisfies a non-trivial output schema.
    """

    async def _run() -> bool:
        # Lazy imports keep the verify module free of heavy executor deps at
        # import time and avoid a cycle (executor -> models -> ...).
        from movate.core.executor import Executor  # noqa: PLC0415
        from movate.providers.mock import (  # noqa: PLC0415
            MockProvider,
            load_dataset_expecteds,
        )
        from movate.providers.pricing import load_pricing  # noqa: PLC0415
        from movate.providers.registry import ProviderRegistry  # noqa: PLC0415
        from movate.testing import InMemoryStorage, NullTracer  # noqa: PLC0415

        provider = MockProvider()
        # Feed the eval dataset's `expected` outputs to the mock so the canned
        # response conforms to the agent's output schema (mirrors run.py).
        dataset_decl = getattr(bundle.spec.evals, "dataset", None) if bundle.spec.evals else None
        if dataset_decl:
            expecteds = load_dataset_expecteds((bundle.agent_dir / dataset_decl).resolve())
            if expecteds:
                provider.configure_dataset(expecteds)

        registry = ProviderRegistry(default_litellm=provider)
        for rt in AgentRuntime:
            registry.register(rt, provider)

        storage = InMemoryStorage()
        await storage.init()
        try:
            executor = Executor(
                registry=registry,
                pricing=load_pricing(),
                storage=storage,
                tracer=NullTracer(),
                tenant_id="local",
            )
            request = RunRequest(agent=bundle.spec.name, input=_mock_input(bundle))
            response = await executor.execute(bundle, request)
            return response.status == "success"
        finally:
            await storage.close()

    return asyncio.run(_run())


def _mock_input(bundle: AgentBundle) -> dict[str, Any]:
    """Build a minimal input satisfying the agent's input schema for the smoke.

    Fills each required property with a type-appropriate placeholder. The
    MockProvider doesn't read the input meaningfully; this just needs to pass
    input-schema validation so the run reaches the provider.
    """
    schema = bundle.input_schema or {}
    props = schema.get("properties", {}) or {}
    required = schema.get("required", list(props.keys()))
    out: dict[str, Any] = {}
    for name in required:
        spec = props.get(name, {})
        out[name] = _placeholder(spec)
    return out


def _placeholder(spec: dict[str, Any]) -> Any:
    """Type-appropriate placeholder for one JSON-schema property."""
    t = spec.get("type", "string")
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return {}
    enum = spec.get("enum")
    if enum:
        return enum[0]
    return "smoke"


__all__ = ["AgentLoadError", "mock_run", "validate_agent"]
