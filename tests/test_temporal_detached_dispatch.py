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
    # No memo passed → forwarded as None (temporalio's own default; the
    # pre-memo call is byte-identical).
    assert client.started["memo"] is None


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


# ---------------------------------------------------------------------------
# Memo provenance (ADR 100 D4 follow-through) — origin → Temporal memo.
# ---------------------------------------------------------------------------


class _ExecutingClient:
    """Fake Temporal client recording execute_workflow kwargs (blocking path)."""

    def __init__(self) -> None:
        self.executed: dict[str, Any] = {}

    async def execute_workflow(self, wf: Any, arg: Any, **kwargs: Any) -> Any:
        self.executed = {"wf": wf, "arg": arg, **kwargs}
        return dict(arg)


class _NoopWorker:
    """Fake temporalio Worker — accepts any wiring, no-op async context."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _NoopWorker:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_on_temporal_threads_memo_to_start_workflow() -> None:
    pytest.importorskip("temporalio")
    client = _RecordingClient()
    await _start_on_temporal(
        client=client,
        workflow_cls=_FakeWorkflowCls,
        wf_id="run-sched",
        run_state={"x": 1},
        memo={"mdk_origin": "schedule:nightly-returns"},
    )
    assert client.started["memo"] == {"mdk_origin": "schedule:nightly-returns"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_on_temporal_threads_memo_and_defaults_to_none() -> None:
    pytest.importorskip("temporalio")
    from movate.runtime.workflow_backend import _execute_on_temporal  # noqa: PLC0415

    client = _ExecutingClient()
    state = {"x": 1}
    _final, status, error = await _execute_on_temporal(
        client=client,
        worker_cls=_NoopWorker,
        workflow_cls=_FakeWorkflowCls,
        activities=[],
        wf_id="run-trig",
        run_state=state,
        memo={"mdk_origin": "trigger:tr-123"},
    )
    assert status == WorkflowStatus.SUCCESS
    assert error is None
    assert client.executed["memo"] == {"mdk_origin": "trigger:tr-123"}

    plain = _ExecutingClient()
    await _execute_on_temporal(
        client=plain,
        worker_cls=_NoopWorker,
        workflow_cls=_FakeWorkflowCls,
        activities=[],
        wf_id="run-manual",
        run_state=state,
    )
    assert plain.executed["memo"] is None  # manual run — no provenance memo


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_passes_job_origin_as_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_workflow_on_backend` stamps `{"mdk_origin": job.origin}` as the
    memo for schedule:/trigger:-originated jobs, and NO memo (None) for a
    manual submit — the defaulted-param compatibility contract."""
    pytest.importorskip("temporalio")
    pytest.importorskip("fastapi")
    from uuid import uuid4  # noqa: PLC0415

    from movate.core.executor import Executor  # noqa: PLC0415
    from movate.core.models import JobKind, JobRecord  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.runtime import workflow_backend  # noqa: PLC0415
    from movate.runtime.dispatch import WorkerDispatch  # noqa: PLC0415
    from movate.testing import InMemoryStorage, MockProvider, NullTracer  # noqa: PLC0415

    captured: list[dict[str, Any] | None] = []

    async def _fake_run_temporal_workflow(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("memo"))
        return object()  # opaque result; the caller returns it verbatim

    monkeypatch.setattr(workflow_backend, "run_temporal_workflow", _fake_run_temporal_workflow)

    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    dispatch = WorkerDispatch(storage=storage, executor=executor, use_mock_for_eval=True)
    graph = _graph([_node("a"), _node("b")], [WorkflowEdge(from_id="a", to_id="b")])

    def _job(origin: str | None) -> JobRecord:
        return JobRecord(
            job_id=str(uuid4()),
            tenant_id="t",
            kind=JobKind.WORKFLOW,
            target="t",
            input={"mock": True},
            origin=origin,
        )

    await dispatch._run_workflow_on_backend(_job("schedule:nightly-returns"), graph, "temporal")
    await dispatch._run_workflow_on_backend(_job("trigger:tr-123"), graph, "temporal")
    await dispatch._run_workflow_on_backend(_job(None), graph, "temporal")
    assert captured == [
        {"mdk_origin": "schedule:nightly-returns"},
        {"mdk_origin": "trigger:tr-123"},
        None,
    ]
