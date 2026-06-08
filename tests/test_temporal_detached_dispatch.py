"""ADR 089 / #759 — non-blocking dispatch for durable HITL Temporal workflows.

The dispatcher used to ``await client.execute_workflow``, holding a worker slot
for the entire (unbounded) human pause and starving the queue. For a HUMAN-node
graph in detached mode it now STARTS the workflow non-blocking and returns a
``PAUSED`` outcome immediately; the long-lived temporal worker hosts it and the
run-record lifecycle (ADR 062 pause record + ADR 080 terminal sync) carries the
result.

These cover the new units without a live Temporal: the HUMAN-node detector and
the non-blocking start (start_workflow called, never execute_workflow; result is
PAUSED; a start failure maps to a retryable ERROR).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from movate.core.models import WorkflowStatus
from movate.core.workflow.ir import NodeType, WorkflowEdge, WorkflowGraph, WorkflowNode
from movate.runtime.workflow_backend import _graph_has_human_node, _start_on_temporal


def _node(nid: str, ntype: NodeType = NodeType.AGENT) -> WorkflowNode:
    return WorkflowNode(id=nid, type=ntype, ref=f"/agents/{nid}", metadata={})


def _graph(nodes: list[WorkflowNode], edges: list[WorkflowEdge]) -> WorkflowGraph:
    return WorkflowGraph(
        name="t",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint=nodes[0].id,
        nodes={n.id: n for n in nodes},
        edges=edges,
        workflow_dir=Path("/tmp/fake"),
    )


@pytest.mark.unit
def test_graph_has_human_node_true() -> None:
    g = _graph(
        [_node("start"), _node("approval", NodeType.HUMAN), _node("done")],
        [
            WorkflowEdge(from_id="start", to_id="approval"),
            WorkflowEdge(from_id="approval", to_id="done"),
        ],
    )
    assert _graph_has_human_node(g) is True


@pytest.mark.unit
def test_graph_has_human_node_false() -> None:
    g = _graph(
        [_node("a"), _node("b")],
        [WorkflowEdge(from_id="a", to_id="b")],
    )
    assert _graph_has_human_node(g) is False


class _FakeWorkflowCls:
    @staticmethod
    def run(state: Any) -> Any:  # the @workflow.defn run method handle start_workflow takes
        return state


class _RecordingClient:
    """Fake Temporal client: records start_workflow, fails execute_workflow."""

    def __init__(self) -> None:
        self.started: dict[str, Any] = {}

    async def start_workflow(self, wf: Any, arg: Any, **kwargs: Any) -> object:
        self.started = {"wf": wf, "arg": arg, **kwargs}
        return object()  # a handle; the detached path does NOT await a result

    async def execute_workflow(self, *a: Any, **k: Any) -> Any:  # must NOT be called
        raise AssertionError("execute_workflow must not be called on the detached path")


class _FailingClient:
    async def start_workflow(self, *a: Any, **k: Any) -> object:
        raise RuntimeError("temporal frontend unreachable")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_on_temporal_returns_paused_and_starts_only() -> None:
    pytest.importorskip("temporalio")
    client = _RecordingClient()
    state = {"request": "refund please", "tenant_id": "t"}
    final, status, error = await _start_on_temporal(
        client=client, workflow_cls=_FakeWorkflowCls, wf_id="run-123", run_state=state
    )
    # Non-blocking: started, not executed.
    assert client.started["id"] == "run-123"
    assert client.started["arg"] == state
    assert client.started["task_queue"] == "mdk-workflows"
    # Idempotent start (ADR 089 D3).
    from temporalio.common import WorkflowIDReusePolicy  # noqa: PLC0415

    assert client.started["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    # Outcome is PAUSED (→ _workflow_result_to_outcome maps to accepted job).
    assert status == WorkflowStatus.PAUSED
    assert error is None
    assert final == state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_on_temporal_maps_start_failure_to_retryable_error() -> None:
    pytest.importorskip("temporalio")
    _final, status, error = await _start_on_temporal(
        client=_FailingClient(), workflow_cls=_FakeWorkflowCls, wf_id="run-9", run_state={"x": 1}
    )
    assert status == WorkflowStatus.ERROR
    assert error is not None
    assert error.type == "temporal_workflow_start_failed"
    assert error.retryable is True
