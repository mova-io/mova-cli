"""WorkflowRunRecord storage — HITL checkpoint round-trip (ADR 017 D5, PR 1).

Exercises the additive nullable ``paused_node_id`` / ``paused_state`` /
``human_task`` columns: a PAUSED record round-trips them; an old-style record
(checkpoint fields ``None``) still loads. Runs across the shared ``storage``
fixture (InMemoryStorage, SqliteProvider, and PostgresProvider — the last
skipped when ``MOVATE_PG_TEST_URL`` is unset). Mirrors tests/test_canary_storage.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from movate.core.models import WorkflowRunRecord, WorkflowStatus


def _make_run(
    *,
    status: WorkflowStatus = WorkflowStatus.SUCCESS,
    tenant_id: str = "tenant-a",
    paused_node_id: str | None = None,
    paused_state: dict | None = None,
    human_task: dict | None = None,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=str(uuid4()),
        tenant_id=tenant_id,
        workflow="approval-flow",
        workflow_version="0.1.0",
        status=status,
        initial_state={"text": "seed"},
        final_state={"text": "seed", "step1": "done"},
        created_at=datetime.now(UTC),
        paused_node_id=paused_node_id,
        paused_state=paused_state,
        human_task=human_task,
    )


@pytest.mark.unit
async def test_paused_checkpoint_round_trips(storage) -> None:
    """A PAUSED record's checkpoint fields round-trip across the backend."""
    rec = _make_run(
        status=WorkflowStatus.PAUSED,
        paused_node_id="approve-gate",
        paused_state={"text": "seed", "step1": "done"},
        human_task={"prompt": "Approve this refund?", "output_contract": ["decision"]},
    )
    await storage.save_workflow_run(rec)
    got = await storage.get_workflow_run(rec.workflow_run_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status is WorkflowStatus.PAUSED
    assert got.paused_node_id == "approve-gate"
    assert got.paused_state == {"text": "seed", "step1": "done"}
    assert got.human_task == {
        "prompt": "Approve this refund?",
        "output_contract": ["decision"],
    }


@pytest.mark.unit
async def test_non_paused_record_has_null_checkpoint(storage) -> None:
    """An old-style / non-paused record reads the new columns back as None."""
    rec = _make_run(status=WorkflowStatus.SUCCESS)
    await storage.save_workflow_run(rec)
    got = await storage.get_workflow_run(rec.workflow_run_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status is WorkflowStatus.SUCCESS
    assert got.paused_node_id is None
    assert got.paused_state is None
    assert got.human_task is None


@pytest.mark.unit
async def test_paused_checkpoint_is_tenant_scoped(storage) -> None:
    rec = _make_run(
        status=WorkflowStatus.PAUSED,
        tenant_id="tenant-a",
        paused_node_id="gate",
        paused_state={"k": "v"},
        human_task={"prompt": "ok?", "output_contract": []},
    )
    await storage.save_workflow_run(rec)
    assert await storage.get_workflow_run(rec.workflow_run_id, tenant_id="tenant-a") is not None
    assert await storage.get_workflow_run(rec.workflow_run_id, tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_paused_checkpoint_survives_list(storage) -> None:
    rec = _make_run(
        status=WorkflowStatus.PAUSED,
        paused_node_id="gate",
        paused_state={"k": "v"},
        human_task={"prompt": "ok?", "output_contract": ["decision"]},
    )
    await storage.save_workflow_run(rec)
    rows = await storage.list_workflow_runs(tenant_id="tenant-a", workflow="approval-flow")
    assert len(rows) == 1
    assert rows[0].status is WorkflowStatus.PAUSED
    assert rows[0].paused_node_id == "gate"
    assert rows[0].human_task == {"prompt": "ok?", "output_contract": ["decision"]}
