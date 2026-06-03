"""Phase 1 Track C unit tests for ``movate.core.workflow.temporal_activities``.

These tests verify the **load-bearing** property of ADR 054 D3: the two
activities reuse the existing mdk execution path. They assert:

* ``call_agent_activity`` calls :meth:`Executor.execute` with the right args
  (no second execution model — D3).
* ``session_id`` flows from the activity input onto the
  :class:`RunRequest.session_id` (D10 — sessions hold conversation state).
* A serialized parent-span dict is reconstructed into a SpanCtx and forwarded
  as ``parent_span=`` (D11 — tracing).
* ``workflow_id`` is forwarded as ``workflow_run_id=`` (D6 — one identity).
* The returned dict is the JSON-shape of a :class:`RunResponse`.
* The tracer is invoked through the existing executor path (not bypassed).
* ``call_skill_activity`` calls :func:`dispatch_skill` with the right args
  for both the python backend and an HTTP-style mocked backend, and the
  :class:`SkillExecutionContext` fields it builds carry the activity inputs.
* The module imports cleanly even with ``temporalio`` absent (lazy contract).

The tests stub ``temporalio.activity`` either via ``sys.modules`` (lazy-import
case) or by patching :class:`Executor` and :func:`dispatch_skill` (the
exercise-the-shim cases). No real Temporal install is required.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import (
    Metrics,
    RunRequest,
    RunResponse,
    SkillImplementationKind,
    TokenUsage,
)
from movate.core.skill_backend.base import (
    _BACKENDS,
    SkillExecutionContext,
    register_backend,
)
from movate.core.skill_loader import SkillBundle, load_skill
from movate.core.workflow.temporal_activities import (
    call_agent_activity,
    call_skill_activity,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer
from movate.tracing.base import SpanCtx

# ---------------------------------------------------------------------------
# Helpers — agent fixture builder (mirrors tests/test_workflow_runner.py)
# ---------------------------------------------------------------------------


def _scaffold_agent(agent_dir: Path, *, name: str = "echo-agent") -> Path:
    """Build a minimal valid agent directory the loader can parse.

    Same shape as the workflow-runner test fixtures so this file stays
    consistent with the rest of the suite.
    """
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)

    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": "echo agent for temporal activity tests",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo {{ input.text }}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"text": "x"}, "expected": {"text": "x"}}) + "\n"
    )
    return agent_dir


def _success_response(run_id: str = "fixed-run") -> RunResponse:
    """A canonical successful :class:`RunResponse` used by the executor mocks."""
    return RunResponse(
        status="success",
        run_id=run_id,
        data={"text": "ok"},
        human_readable="ok",
        trace_id="trace-from-executor",
        metrics=Metrics(
            latency_ms=12,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=0.0001,
            provider="mock",
            pricing_version="test",
            trace_id="trace-from-executor",
        ),
    )


# ---------------------------------------------------------------------------
# call_agent_activity tests — D3 / D6 / D10 / D11
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_agent_activity_invokes_executor(tmp_path: Path) -> None:
    """D3 — the activity calls ``Executor.execute(bundle, request, ...)``
    on the existing executor, not a Temporal-specific second path."""
    agent_dir = _scaffold_agent(tmp_path / "agent")
    mock_executor = MagicMock()
    mock_executor.execute = AsyncMock(return_value=_success_response())

    with patch(
        "movate.core.workflow.temporal_activities._build_executor",
        return_value=mock_executor,
    ):
        await call_agent_activity(
            agent_ref=str(agent_dir),
            request_json={"agent": "echo-agent", "input": {"text": "hello"}},
        )

    assert mock_executor.execute.await_count == 1, (
        "Executor.execute should be the single load-bearing call (ADR 054 D3)"
    )
    # Positional args: (bundle, request)
    pos_args, _kw_args = mock_executor.execute.await_args
    bundle, request = pos_args
    assert bundle.spec.name == "echo-agent"
    assert isinstance(request, RunRequest)
    assert request.input == {"text": "hello"}


@pytest.mark.asyncio
async def test_call_agent_activity_threads_session_id(tmp_path: Path) -> None:
    """D10 — ``session_id`` flows onto the :class:`RunRequest` so
    conversation state lives in the session store, not Temporal history."""
    agent_dir = _scaffold_agent(tmp_path / "agent")
    mock_executor = MagicMock()
    mock_executor.execute = AsyncMock(return_value=_success_response())

    with patch(
        "movate.core.workflow.temporal_activities._build_executor",
        return_value=mock_executor,
    ):
        await call_agent_activity(
            agent_ref=str(agent_dir),
            request_json={"agent": "echo-agent", "input": {"text": "hi"}},
            session_id="session-abc",
        )

    _, request = mock_executor.execute.await_args.args
    assert request.session_id == "session-abc"


@pytest.mark.asyncio
async def test_call_agent_activity_threads_parent_span(tmp_path: Path) -> None:
    """D11 — a serialized parent-span dict is reconstructed as a
    :class:`SpanCtx` and passed to ``Executor.execute(parent_span=...)``."""
    agent_dir = _scaffold_agent(tmp_path / "agent")
    mock_executor = MagicMock()
    mock_executor.execute = AsyncMock(return_value=_success_response())

    parent_span_context = {
        "span_id": "span-1",
        "trace_id": "trace-from-workflow",
        "parent_id": None,
        "name": "workflow.execute",
        "attributes": {"workflow_run_id": "wf-123"},
    }

    with patch(
        "movate.core.workflow.temporal_activities._build_executor",
        return_value=mock_executor,
    ):
        await call_agent_activity(
            agent_ref=str(agent_dir),
            request_json={"agent": "echo-agent", "input": {"text": "hi"}},
            parent_span_context=parent_span_context,
        )

    _, kw_args = mock_executor.execute.await_args
    parent_span = kw_args["parent_span"]
    assert isinstance(parent_span, SpanCtx)
    assert parent_span.trace_id == "trace-from-workflow"
    assert parent_span.span_id == "span-1"
    assert parent_span.name == "workflow.execute"


@pytest.mark.asyncio
async def test_call_agent_activity_uses_workflow_id_as_run_id(tmp_path: Path) -> None:
    """D6 — the Temporal ``workflow_id`` IS the mdk ``workflow_run_id``."""
    agent_dir = _scaffold_agent(tmp_path / "agent")
    mock_executor = MagicMock()
    mock_executor.execute = AsyncMock(return_value=_success_response())

    with patch(
        "movate.core.workflow.temporal_activities._build_executor",
        return_value=mock_executor,
    ):
        await call_agent_activity(
            agent_ref=str(agent_dir),
            request_json={"agent": "echo-agent", "input": {"text": "hi"}},
            workflow_id="wf-id-abc",
            node_id="node-1",
            tenant_id="tenant-xyz",
        )

    _, kw_args = mock_executor.execute.await_args
    assert kw_args["workflow_run_id"] == "wf-id-abc"
    assert kw_args["node_id"] == "node-1"
    assert kw_args["tenant_id_override"] == "tenant-xyz"


@pytest.mark.asyncio
async def test_call_agent_activity_returns_run_response_dict(tmp_path: Path) -> None:
    """The activity returns a JSON-safe dict shaped like a :class:`RunResponse`,
    so Temporal can marshal it back to the workflow."""
    agent_dir = _scaffold_agent(tmp_path / "agent")
    response = _success_response(run_id="run-xyz")
    mock_executor = MagicMock()
    mock_executor.execute = AsyncMock(return_value=response)

    with patch(
        "movate.core.workflow.temporal_activities._build_executor",
        return_value=mock_executor,
    ):
        result = await call_agent_activity(
            agent_ref=str(agent_dir),
            request_json={"agent": "echo-agent", "input": {"text": "hi"}},
        )

    assert isinstance(result, dict)
    assert result["status"] == "success"
    assert result["run_id"] == "run-xyz"
    assert result["data"] == {"text": "ok"}
    # The full Pydantic shape must be preserved so the workflow body can
    # reconstruct a RunResponse on the other side without a custom converter.
    rebuilt = RunResponse.model_validate(result)
    assert rebuilt.metrics.cost_usd == response.metrics.cost_usd


@pytest.mark.asyncio
async def test_call_agent_activity_does_not_bypass_tracing(tmp_path: Path) -> None:
    """D11 — tracing wires through the existing :class:`Executor` path; the
    activity never builds its own tracer/span. Concretely: when the real
    executor runs, its tracer's ``start_span`` IS invoked (we don't smuggle
    in a NullTracer or skip tracing). We assert this via a real executor with
    a tracer spy.
    """
    agent_dir = _scaffold_agent(tmp_path / "agent")

    # Build a real executor whose tracer is a NullTracer wrapped with a spy
    # on start_span — proves the executor's tracing path fires.
    storage = InMemoryStorage()
    await storage.init()
    tracer = NullTracer()
    original_start_span = tracer.start_span
    start_span_calls: list[str] = []

    def _spy_start_span(name: str, attrs: Any = None, parent: Any = None) -> Any:
        start_span_calls.append(name)
        return original_start_span(name, attrs, parent)

    tracer.start_span = _spy_start_span  # type: ignore[method-assign]

    executor = Executor(
        provider=MockProvider(response=json.dumps({"text": "ok"})),
        pricing=load_pricing(),
        storage=storage,
        tracer=tracer,
    )

    with patch(
        "movate.core.workflow.temporal_activities._build_executor",
        return_value=executor,
    ):
        result = await call_agent_activity(
            agent_ref=str(agent_dir),
            request_json={"agent": "echo-agent", "input": {"text": "hi"}},
        )

    assert result["status"] == "success"
    assert any(name == "agent.execute" for name in start_span_calls), (
        "Executor.execute must open its agent.execute span — tracing is not bypassed."
    )


# ---------------------------------------------------------------------------
# call_skill_activity tests — dispatch_skill reuse
# ---------------------------------------------------------------------------


def _scaffold_python_skill(
    skill_dir: Path,
    *,
    name: str = "echo-skill",
    entry: str = "tests.test_temporal_activities:_echo_skill",
) -> SkillBundle:
    """Build a minimal Python-backend skill bundle in memory.

    Mirrors the pattern in ``tests/test_skill_backend_tracing.py`` — uses
    the inline shorthand schema (``input: {text: string}``) the loader
    compiles into a JSON Schema at load time.
    """
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input:\n"
        "    text: string\n"
        "  output:\n"
        "    result: string\n"
        "implementation:\n"
        "  kind: python\n"
        f"  entry: {entry}\n"
    )
    return load_skill(skill_dir)


def _echo_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Python skill entrypoint used by the Python-backend tests."""
    return {"result": input.get("text", "")}


@pytest.mark.asyncio
async def test_call_skill_activity_invokes_dispatch_skill(tmp_path: Path) -> None:
    """The activity is a thin shim — it calls :func:`dispatch_skill` with the
    loaded :class:`SkillBundle`, the input dict, and a built
    :class:`SkillExecutionContext`."""
    skill_bundle = _scaffold_python_skill(tmp_path / "echo-skill")

    mock_dispatch = AsyncMock(return_value={"result": "hello"})

    with patch(
        "movate.core.skill_backend.dispatch_skill",
        new=mock_dispatch,
    ):
        out = await call_skill_activity(
            skill_ref=str(skill_bundle.skill_dir),
            input_json={"text": "hello"},
            workflow_id="wf-abc",
            tenant_id="t-1",
        )

    assert out == {"result": "hello"}
    assert mock_dispatch.await_count == 1
    skill_arg, input_arg, ctx_arg = mock_dispatch.await_args.args
    assert isinstance(skill_arg, SkillBundle)
    assert skill_arg.spec.name == "echo-skill"
    assert input_arg == {"text": "hello"}
    assert isinstance(ctx_arg, SkillExecutionContext)


@pytest.mark.asyncio
async def test_call_skill_activity_works_with_python_backend(tmp_path: Path) -> None:
    """Integration: the activity dispatches to the real Python backend without
    any Temporal-specific second path."""
    skill_bundle = _scaffold_python_skill(tmp_path / "echo-skill")
    output = await call_skill_activity(
        skill_ref=str(skill_bundle.skill_dir),
        input_json={"text": "hello"},
    )
    assert output == {"result": "hello"}


@pytest.mark.asyncio
async def test_call_skill_activity_works_with_http_backend_mocked(
    tmp_path: Path,
) -> None:
    """The activity wraps any registered backend — verified here by registering
    a fake HTTP-style backend (network mocked) and dispatching through it.

    We re-use the existing :data:`_BACKENDS` registry, register a fake HTTP
    backend keyed on :attr:`SkillImplementationKind.HTTP`, scaffold an
    http-kind skill bundle, then call the activity. The activity loads the
    bundle from disk, calls :func:`dispatch_skill`, which routes by kind to
    the fake backend — same code path the real production HTTP backend takes.
    """
    skill_dir = tmp_path / "http-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: http-skill\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input:\n"
        "    q: string\n"
        "  output:\n"
        "    answer: string\n"
        "implementation:\n"
        "  kind: http\n"
        "  entry: https://example.invalid/skill\n"
    )

    class _FakeHTTPBackend:
        kind = SkillImplementationKind.HTTP

        async def execute(
            self,
            skill: SkillBundle,
            input: dict[str, Any],
            ctx: SkillExecutionContext,
        ) -> dict[str, Any]:
            # Network mocked — same shape a real backend would return.
            return {"answer": f"mocked:{input['q']}"}

    # Snapshot the registry and restore after — register_backend() mutates a
    # module-level dict, so we have to put the real http backend back.
    original = _BACKENDS.get(SkillImplementationKind.HTTP)
    register_backend(_FakeHTTPBackend())
    try:
        out = await call_skill_activity(
            skill_ref=str(skill_dir),
            input_json={"q": "ping"},
        )
    finally:
        if original is not None:
            _BACKENDS[SkillImplementationKind.HTTP] = original
        else:
            _BACKENDS.pop(SkillImplementationKind.HTTP, None)
    assert out == {"answer": "mocked:ping"}


@pytest.mark.asyncio
async def test_call_skill_activity_threads_context(tmp_path: Path) -> None:
    """The activity inputs (parent_span, workflow_id, tenant_id, agent_ref)
    appear on the :class:`SkillExecutionContext` passed to
    :func:`dispatch_skill`."""
    skill_bundle = _scaffold_python_skill(tmp_path / "echo-skill")

    captured: dict[str, Any] = {}

    async def _fake_dispatch(
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        captured["ctx"] = ctx
        return {"result": "ok"}

    parent_span_context = {
        "span_id": "skill-span-1",
        "trace_id": "trace-xyz",
        "parent_id": None,
        "name": "workflow.execute",
        "attributes": {},
    }
    with patch(
        "movate.core.skill_backend.dispatch_skill",
        new=_fake_dispatch,
    ):
        await call_skill_activity(
            skill_ref=str(skill_bundle.skill_dir),
            input_json={"text": "hi"},
            parent_span_context=parent_span_context,
            workflow_id="wf-42",
            tenant_id="acme",
            agent_ref="some-agent",
            call_ms_budget=12_345,
        )

    ctx = captured["ctx"]
    assert ctx.run_id == "wf-42"  # D6 alignment
    assert ctx.tenant_id == "acme"
    assert ctx.agent_name == "some-agent"
    assert ctx.call_ms_budget == 12_345
    assert ctx.trace_id == "trace-xyz"
    assert isinstance(ctx.parent_span, SpanCtx)
    assert ctx.parent_span.span_id == "skill-span-1"


# ---------------------------------------------------------------------------
# Lazy-import contract
# ---------------------------------------------------------------------------


def test_lazy_temporalio_import() -> None:
    """Re-importing the activities module with ``temporalio`` hidden must NOT
    raise — the module ships an internal no-op decorator so the lazy-import
    contract (the same ADR 030 D1 established for LangGraph) holds.
    """
    # Hide temporalio from import so the module re-import falls through to
    # the internal _NoopActivityModule.
    saved: dict[str, Any] = {
        key: sys.modules[key]
        for key in list(sys.modules)
        if key == "temporalio" or key.startswith("temporalio.")
    }
    for key in list(saved):
        del sys.modules[key]

    # Make temporalio unimportable.
    class _Blocker:
        def find_module(self, name: str, path: Any = None) -> Any:
            if name == "temporalio" or name.startswith("temporalio."):
                return self
            return None

        def load_module(self, name: str) -> Any:
            raise ImportError(f"blocked: {name}")

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)  # type: ignore[arg-type]
    try:
        # Drop the activities module so the next import re-binds against
        # the blocked temporalio.
        sys.modules.pop("movate.core.workflow.temporal_activities", None)
        mod = importlib.import_module("movate.core.workflow.temporal_activities")
        assert hasattr(mod, "call_agent_activity")
        assert hasattr(mod, "call_skill_activity")
        # The internal helper must report the no-op shape.
        assert mod._activity.__class__.__name__ == "_NoopActivityModule"
        # _require_temporalio must raise with the documented remediation.
        with pytest.raises(RuntimeError, match=r"\[temporal\] extra is not installed"):
            mod._require_temporalio()
    finally:
        sys.meta_path.remove(blocker)  # type: ignore[arg-type]
        sys.modules.update(saved)
        # Re-import with temporalio available so subsequent tests get the
        # real-SDK path.
        sys.modules.pop("movate.core.workflow.temporal_activities", None)
        importlib.import_module("movate.core.workflow.temporal_activities")
