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

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.temporal


@pytest.mark.timeout(60)
async def test_trivial_workflow_via_startdev(temporal_client: Any) -> None:
    """Submit a trivial workflow, wait for completion, assert result.

    The workflow is a single ``@workflow.defn`` class whose ``run`` method
    calls ``call_agent_activity`` once and returns the result merged into
    state. The agent activity is patched to return a fixed dict (no real
    LLM call, no real agent bundle).
    """
    from temporalio import activity, workflow  # noqa: PLC0415
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: PLC0415

    # -- Define a trivial activity that stands in for call_agent_activity ---

    @activity.defn(name="test_agent_activity")
    async def test_agent_activity(
        node_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """A trivial activity that returns a fixed result."""
        return {"agent_output": f"hello from {node_id}", "input_echo": state.get("user_input", "")}

    # -- Define a trivial workflow that calls the activity ------------------

    @workflow.defn
    class TrivialTestWorkflow:
        """A single-node workflow: call the activity, merge result, return."""

        @workflow.run
        async def run(self, initial_state: dict[str, Any]) -> dict[str, Any]:
            state = dict(initial_state)
            result = await workflow.execute_activity(
                test_agent_activity,
                args=["agent-node-1", state],
                schedule_to_close_timeout=workflow.unsafe.timedelta(seconds=30),
            )
            state.update(result)
            return state

    # -- Run the workflow on the dev server ---------------------------------

    task_queue = "test-temporal-startdev"

    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[TrivialTestWorkflow],
        activities=[test_agent_activity],
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
    from temporalio import workflow  # noqa: PLC0415
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: PLC0415

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

    # -- Define a workflow that calls the real call_agent_activity ----------

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
                schedule_to_close_timeout=workflow.unsafe.timedelta(seconds=30),
            )
            state.update(result)
            return state

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
        patch(
            "movate.core.workflow.temporal_activities.load_agent",
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
