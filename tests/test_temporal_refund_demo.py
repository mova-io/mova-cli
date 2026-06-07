"""End-to-end durable-HITL smoke for the shipped ``workflows/refund-approval`` demo.

Compiles the REAL ``workflows/refund-approval`` workflow (``runtime: temporal``)
and runs it on the time-skipping test server: triage agent → durable HUMAN pause
→ ``human_response`` signal → finalize agent. This proves the shipped demo
actually registers, pauses durably, and resumes — i.e. the deployed Temporal
worker has a real ``runtime: temporal`` workflow to execute (ADR 062 / 080).

Hermetic: a deterministic offline provider (no network, no keys) +
:class:`InMemoryStorage` + the SDK's in-memory time-skipping server. The module
``importorskip`` s ``temporalio`` so it skips cleanly without the ``[temporal]``
extra.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "temporalio",
    reason="the [temporal] extra is not installed; refund-approval demo smoke skipped",
)

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from movate.core.models import WorkflowStatus
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.temporal_activities import (
    call_agent_activity,
    call_gate_activity,
    call_human_activity,
    call_judge_activity,
    call_skill_activity,
    configure_activities,
    persist_workflow_result_activity,
)
from movate.providers.base import BaseLLMProvider, CompletionRequest, CompletionResponse
from movate.providers.pricing import load_pricing
from movate.runtime.workflow_backend import DEFAULT_TASK_QUEUE, load_compiled_workflow_class
from movate.testing import InMemoryStorage, NullTracer

_DEMO = Path(__file__).resolve().parent.parent / "workflows" / "refund-approval"


class _RefundProvider(BaseLLMProvider):
    """Deterministic offline provider for the two demo agents.

    Branches on which agent's prompt is calling: the triage prompt names
    ``recommended_decision``; the finalize prompt does not. Returns JSON that
    satisfies each agent's output schema so native and Temporal see identical
    state (no real LLM, no keys, no network).
    """

    name = "refund_demo"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "recommended_decision" in body:
            return CompletionResponse(
                text=(
                    '{"summary": "Duplicate $40 charge on order #123; clear billing '
                    'error.", "recommended_decision": "approve"}'
                )
            )
        return CompletionResponse(
            text='{"outcome": "Your refund has been approved; 3-5 business days."}'
        )

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def _worker(env: Any, wf_cls: Any) -> Any:
    return Worker(
        env.client,
        task_queue=DEFAULT_TASK_QUEUE,
        workflows=[wf_cls],
        activities=[
            call_agent_activity,
            call_skill_activity,
            call_gate_activity,
            call_judge_activity,
            call_human_activity,
            persist_workflow_result_activity,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


@pytest.mark.smoke
async def test_refund_approval_demo_compiles_pauses_and_resumes() -> None:
    """The shipped refund-approval demo: triage → durable HUMAN pause → signal →
    finalize, reaching SUCCESS with the human's decision merged into state."""
    spec, parent = load_workflow_spec(_DEMO)
    assert spec.runtime == "temporal", "the demo must declare runtime: temporal"

    graph = compile_workflow(spec, parent)
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=load_pricing(),
        tracer=NullTracer(),
        provider=_RefundProvider(),
        tenant_id="local",
    )
    compiled = TemporalCompiler().compile(graph)
    wf_cls = load_compiled_workflow_class(compiled.module_source, compiled.workflow_class_name)

    env = await WorkflowEnvironment.start_time_skipping()
    async with env, _worker(env, wf_cls):
        handle = await env.client.start_workflow(
            wf_cls.run,
            {
                "request": "I was charged twice for order #123 — please refund $40.",
                "tenant_id": "local",
            },
            id="refund-demo-1",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        # The run parks at the HUMAN node; deliver the approver's decision.
        await handle.signal(
            "human_response", args=["approval", {"decision": "approve", "approver": "alice"}]
        )
        final = await handle.result()

    # The human's decision (output_contract) merged into state, and both agents ran.
    assert final["decision"] == "approve"
    assert final["approver"] == "alice"
    assert "summary" in final, "triage agent output should be in state"
    assert "outcome" in final, "finalize agent output should be in state"

    # ADR 080 D2 terminal-state sync: the store holds the terminal SUCCESS record.
    rec = await storage.get_workflow_run("refund-demo-1", tenant_id="local")
    assert rec is not None
    assert rec.runtime == "temporal"
    assert rec.status is WorkflowStatus.SUCCESS
