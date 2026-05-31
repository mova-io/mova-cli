"""ADR 062 — durable HITL end-to-end: a HUMAN node parks the Temporal workflow
until a ``human_response`` signal arrives, then resumes with the decision merged
into state.

Uses ``WorkflowEnvironment.start_time_skipping()`` and **starts** the workflow
(rather than ``execute`` it) so we can deliver the signal mid-flight — the real
durability proof: without the signal the workflow stays parked; with it, it
resumes exactly where it paused. The fixture is a HUMAN-entrypoint workflow so
no agent/skill activities are needed — the node is pure wait → merge → return.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from movate.core.workflow.ir import NodeType, WorkflowGraph, WorkflowNode

pytest.importorskip("temporalio", reason="temporal extra not installed")

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.runtime.workflow_backend import (
    DEFAULT_TASK_QUEUE,
    load_compiled_workflow_class,
)


def _human_only_graph() -> WorkflowGraph:
    """A one-node workflow whose entrypoint is the HUMAN gate (no activities)."""
    approval = WorkflowNode(
        id="approval",
        type=NodeType.HUMAN,
        ref="",
        metadata={"prompt": "Approve the refund?", "output_contract": ["approved"]},
    )
    return WorkflowGraph(
        name="refund-approval",
        version="0.1.0",
        description="",
        entrypoint="approval",
        nodes={"approval": approval},
        edges=[],
        state_schema={},
        workflow_dir=Path("."),
    )


async def _start_worker_and_workflow(env: WorkflowEnvironment, wf_id: str):
    compiled = TemporalCompiler().compile(_human_only_graph())
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )
    worker = Worker(
        env.client,
        task_queue=DEFAULT_TASK_QUEUE,
        workflows=[workflow_cls],
        activities=[],  # HUMAN-only graph calls no activities
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    handle = await env.client.start_workflow(
        workflow_cls.run,
        {"case_id": "rf-1", "tenant_id": "local"},
        id=wf_id,
        task_queue=DEFAULT_TASK_QUEUE,
    )
    return worker, handle


async def test_human_node_resumes_on_signal() -> None:
    """The workflow parks at the HUMAN node and completes only after the
    ``human_response`` signal — the decision is merged into final state."""
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        worker, handle = await _start_worker_and_workflow(env, "hitl-resume")
        async with worker:
            # Deliver the human decision — resumes the parked workflow.
            await handle.signal(
                "human_response", args=["approval", {"approved": True, "amount": 100}]
            )
            final: dict[str, Any] = await handle.result()

    final.pop("tenant_id", None)
    # The decision merged into state (native parity, ADR 017 D5).
    assert final["approved"] is True
    assert final["amount"] == 100
    # Original state preserved.
    assert final["case_id"] == "rf-1"


async def test_human_signal_before_node_is_retained() -> None:
    """A signal delivered BEFORE the workflow reaches the node is retained in
    the inbox (instance state), so the node resumes immediately — no lost
    wakeup race."""
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        worker, handle = await _start_worker_and_workflow(env, "hitl-early-signal")
        async with worker:
            # Even delivered right away, the inbox retains it for the node.
            await handle.signal("human_response", args=["approval", {"approved": False}])
            final = await handle.result()
    assert final["approved"] is False
