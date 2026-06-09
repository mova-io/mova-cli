"""Temporal ``start-dev`` integration test — ADR 054 Phase 1 item 1.13.

Starts the dev server (via the session-scoped ``temporal_server`` fixture),
connects, submits a trivial workflow that calls one ``call_agent_activity``
with a mocked agent, waits for completion, and asserts the result came back.

Gated behind ``@pytest.mark.temporal`` — skipped by default; run with::

    pytest -m temporal

The dev-server fixture skips automatically when the ``temporal`` CLI binary
is not on ``$PATH`` (see ``conftest.py``).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

# temporalio is an opt-in extra. Import it at module scope (guarded) so the
# workflow classes below can be defined at MODULE level — temporalio's
# ``@workflow.run`` rejects classes defined inside a function (``<locals>`` in
# ``__qualname__``) as of temporalio 1.27. The ``temporal_client`` fixture
# still skips the individual tests when the ``temporal`` CLI binary is absent
# (e.g. on CI), so this only adds a clean collection-time skip when the Python
# SDK itself isn't installed.
pytest.importorskip("temporalio")

from temporalio import activity, workflow
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

pytestmark = pytest.mark.temporal


# -- Scaffold a real text → step1 → step2 two-agent workflow bundle on disk --


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> None:
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
                "description": f"reads {input_key}, writes {output_key}",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo {{ input." + input_key + " }} as " + output_key)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [input_key],
                "properties": {input_key: {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [output_key],
                "properties": {output_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )


def _scaffold_linear_workflow(root: Path, *, name: str) -> Path:
    """Write a ``text → step1 → step2`` Temporal workflow bundle under ``root/<name>``."""
    workflow_dir = root / name
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        workflow_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "text": {"type": "string"},
                    "step1": {"type": "string"},
                    "step2": {"type": "string"},
                },
            }
        )
    )
    (workflow_dir / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": name,
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "first",
                "runtime": "temporal",
                "nodes": [
                    {"id": "first", "type": "agent", "ref": "./agents/first"},
                    {"id": "second", "type": "agent", "ref": "./agents/second"},
                ],
                "edges": [{"from": "first", "to": "second"}],
            }
        )
    )
    return workflow_dir


# -- A trivial activity that stands in for call_agent_activity --------------


@activity.defn(name="test_agent_activity")
async def _trivial_agent_activity(
    node_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """A trivial activity that returns a fixed result."""
    return {"agent_output": f"hello from {node_id}", "input_echo": state.get("user_input", "")}


# -- A trivial single-node workflow that calls the activity -----------------


@workflow.defn
class TrivialTestWorkflow:
    """A single-node workflow: call the activity, merge result, return."""

    @workflow.run
    async def run(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        state = dict(initial_state)
        result = await workflow.execute_activity(
            _trivial_agent_activity,
            args=["agent-node-1", state],
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        state.update(result)
        return state


# -- A workflow that drives the real mdk ``call_agent_activity`` ------------


@workflow.defn
class MdkActivityTestWorkflow:
    @workflow.run
    async def run(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        with workflow.unsafe.imports_passed_through():
            from movate.core.workflow.temporal_activities import (  # noqa: PLC0415
                call_agent_activity as _activity,
            )
        state = dict(initial_state)
        result = await workflow.execute_activity(
            _activity,
            args=["test-node", "fake-agent-ref", state, "test-run-id"],
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        state.update(result)
        return state


@pytest.mark.timeout(60)
async def test_trivial_workflow_via_startdev(temporal_client: Any) -> None:
    """Submit a trivial workflow, wait for completion, assert result.

    The workflow is a single ``@workflow.defn`` class whose ``run`` method
    calls the activity once and returns the result merged into state. The
    activity returns a fixed dict (no real LLM call, no real agent bundle).
    """
    # -- Run the workflow on the dev server ---------------------------------

    task_queue = "test-temporal-startdev"

    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[TrivialTestWorkflow],
        activities=[_trivial_agent_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        result = await temporal_client.execute_workflow(
            TrivialTestWorkflow.run,
            {"user_input": "test-input"},
            id="test-trivial-workflow-1",
            task_queue=task_queue,
        )

    # -- Assert the result came back correctly -----------------------------

    assert isinstance(result, dict)
    assert result["agent_output"] == "hello from agent-node-1"
    assert result["input_echo"] == "test-input"
    assert result["user_input"] == "test-input"


@pytest.mark.timeout(60)
async def test_mdk_activity_wiring_via_startdev(temporal_client: Any) -> None:
    """Submit a workflow that calls the real ``call_agent_activity`` (mocked executor).

    This verifies that mdk's Track-C activity wrappers
    (``temporal_activities.py``) integrate correctly with a real Temporal
    server. The Executor and agent loader are mocked so no LLM call or
    real agent bundle is needed.
    """
    from movate.core.workflow.temporal_activities import (  # noqa: PLC0415
        call_agent_activity,
        configure_activities,
    )

    # -- Mock the activity context's dependencies --------------------------

    mock_storage = AsyncMock()
    mock_storage.init = AsyncMock()
    mock_tracer = AsyncMock()
    mock_provider = AsyncMock()
    mock_pricing = {}

    configure_activities(
        storage=mock_storage,
        pricing=mock_pricing,
        tracer=mock_tracer,
        provider=mock_provider,
        tenant_id="test-tenant",
    )

    # -- Mock the agent loader and executor --------------------------------

    mock_bundle = AsyncMock()
    mock_bundle.spec.name = "test-agent"
    mock_bundle.input_schema = {"properties": {"user_input": {"type": "string"}}}

    mock_response = AsyncMock()
    mock_response.status = "success"
    mock_response.data = {"answer": "mocked-answer"}
    mock_response.error = None

    mock_executor_instance = AsyncMock()
    mock_executor_instance.execute = AsyncMock(return_value=mock_response)

    task_queue = "test-mdk-activity-startdev"

    with (
        # ``load_agent`` is imported lazily inside ``call_agent_activity``
        # (``from movate.core.loader import load_agent``), so patch it at its
        # source module, not on ``temporal_activities``.
        patch(
            "movate.core.loader.load_agent",
            return_value=mock_bundle,
        ),
        patch(
            "movate.core.workflow.temporal_activities._executor_for",
            return_value=mock_executor_instance,
        ),
    ):
        async with Worker(
            temporal_client,
            task_queue=task_queue,
            workflows=[MdkActivityTestWorkflow],
            activities=[call_agent_activity],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            result = await temporal_client.execute_workflow(
                MdkActivityTestWorkflow.run,
                {"user_input": "hello"},
                id="test-mdk-activity-workflow-1",
                task_queue=task_queue,
            )

    assert isinstance(result, dict)
    assert result["answer"] == "mocked-answer"
    assert result["user_input"] == "hello"


# -- A REAL compiled workflow run end-to-end on the live server -------------


from movate.providers.base import BaseLLMProvider  # noqa: E402


class _StateAwareProvider(BaseLLMProvider):
    """Deterministic offline provider (mirrors test_workflow_replay_cli.py) so
    the live run needs no LLM key / network — just the durable machinery."""

    name = "state_aware"
    version = "0.0.1"

    async def complete(self, request: Any) -> Any:
        from movate.providers.base import CompletionResponse  # noqa: PLC0415

        body = request.messages[0].content
        if "step1" in body and "step2" not in body:
            return CompletionResponse(text='{"step1": "alpha"}')
        return CompletionResponse(text='{"step2": "beta"}')

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.timeout(120)
async def test_compiled_workflow_end_to_end_via_startdev(
    temporal_client: Any, tmp_path: Any
) -> None:
    """The load-bearing live smoke: a REAL ``workflow.yaml`` → ``TemporalCompiler``
    → loaded ``@workflow.defn`` class, run end-to-end on a live dev server through
    the **full** activity set + the terminal persist activity — the closest test
    to production short of a real LLM.

    The hand-written workflow classes above prove connectivity + activity wiring;
    this proves the *compiler's emitted control flow* (dispatch loop, state merge,
    terminal persist) executes durably on a real server, against the same
    activity registration ``run_temporal_worker`` uses.
    """
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: PLC0415

    from movate.core.workflow import compile_workflow, load_workflow_spec  # noqa: PLC0415
    from movate.core.workflow.compilers.temporal import TemporalCompiler  # noqa: PLC0415
    from movate.core.workflow.temporal_activities import (  # noqa: PLC0415
        call_agent_activity,
        call_gate_activity,
        call_human_activity,
        call_judge_activity,
        call_skill_activity,
        configure_activities,
        persist_workflow_result_activity,
    )
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.runtime.workflow_backend import load_compiled_workflow_class  # noqa: PLC0415
    from movate.testing import InMemoryStorage, NullTracer  # noqa: PLC0415

    # Scaffold a real text → step1 → step2 two-agent workflow on disk.
    _scaffold_linear_workflow(tmp_path, name="live-wf")
    spec, parent = load_workflow_spec(tmp_path / "live-wf" / "workflow.yaml")
    graph = compile_workflow(spec, parent)

    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=load_pricing(),
        tracer=NullTracer(),
        provider=_StateAwareProvider(),
        tenant_id="local",
    )

    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )

    task_queue = "test-compiled-live"
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[workflow_cls],
        # The SAME activity set run_temporal_worker registers (workflow_backend.py).
        activities=[
            call_agent_activity,
            call_skill_activity,
            call_gate_activity,
            call_judge_activity,
            call_human_activity,
            persist_workflow_result_activity,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        result = await temporal_client.execute_workflow(
            workflow_cls.run,
            {"text": "hello", "tenant_id": "local"},
            id="test-compiled-live-1",
            task_queue=task_queue,
        )

    # The deterministic provider drives step1=alpha → step2=beta; the compiled
    # control flow must thread both through state to completion.
    assert result["step1"] == "alpha"
    assert result["step2"] == "beta"
