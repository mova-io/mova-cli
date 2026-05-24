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

from movate.core.models import JobKind, JobRecord, WorkflowRunRecord, WorkflowStatus


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


# ---------------------------------------------------------------------------
# save_workflow_run upsert + status filter (ADR 017 D5, PR 2 — resume)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_workflow_run_upserts_on_id(storage) -> None:
    """A resume re-saves the SAME workflow_run_id. save_workflow_run must
    upsert (not duplicate / not violate the PK): the second save UPDATES the
    row in place. Proves the run continues, doesn't fork."""
    rid = str(uuid4())
    paused = WorkflowRunRecord(
        workflow_run_id=rid,
        tenant_id="tenant-a",
        workflow="approval-flow",
        workflow_version="0.1.0",
        status=WorkflowStatus.PAUSED,
        initial_state={"text": "seed"},
        final_state={"text": "seed", "step1": "done"},
        paused_node_id="gate",
        paused_state={"text": "seed", "step1": "done"},
        human_task={"prompt": "ok?", "output_contract": ["decision"]},
    )
    await storage.save_workflow_run(paused)

    # Resume to completion: same id, now SUCCESS, checkpoint fields cleared.
    completed = WorkflowRunRecord(
        workflow_run_id=rid,
        tenant_id="tenant-a",
        workflow="approval-flow",
        workflow_version="0.1.0",
        status=WorkflowStatus.SUCCESS,
        initial_state={"text": "seed"},
        final_state={"text": "seed", "step1": "done", "step2": "out", "decision": "approve"},
    )
    await storage.save_workflow_run(completed)

    # Exactly one row (upserted), reflecting the terminal state.
    rows = await storage.list_workflow_runs(tenant_id="tenant-a", workflow="approval-flow")
    assert len(rows) == 1
    got = await storage.get_workflow_run(rid, tenant_id="tenant-a")
    assert got is not None
    assert got.status is WorkflowStatus.SUCCESS
    assert got.paused_node_id is None
    assert got.final_state["decision"] == "approve"


@pytest.mark.unit
async def test_list_workflow_runs_status_filter(storage) -> None:
    """list_workflow_runs(status=PAUSED) returns only paused runs — the HITL
    queue the signal endpoint + CLI surface."""
    await storage.save_workflow_run(_make_run(status=WorkflowStatus.SUCCESS))
    paused = _make_run(
        status=WorkflowStatus.PAUSED,
        paused_node_id="gate",
        paused_state={"k": "v"},
        human_task={"prompt": "ok?", "output_contract": ["d"]},
    )
    await storage.save_workflow_run(paused)

    only_paused = await storage.list_workflow_runs(
        tenant_id="tenant-a", status=WorkflowStatus.PAUSED
    )
    assert len(only_paused) == 1
    assert only_paused[0].workflow_run_id == paused.workflow_run_id
    # No filter returns both.
    assert len(await storage.list_workflow_runs(tenant_id="tenant-a")) == 2


# ---------------------------------------------------------------------------
# JobRecord.resume_workflow_run_id — additive nullable round-trip (PR 2)
# ---------------------------------------------------------------------------


def _make_job(*, resume_workflow_run_id: str | None) -> JobRecord:
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id="tenant-a",
        kind=JobKind.WORKFLOW,
        target="approval-flow",
        input={},
        resume_workflow_run_id=resume_workflow_run_id,
    )


@pytest.mark.unit
async def test_job_resume_id_round_trips(storage) -> None:
    job = _make_job(resume_workflow_run_id="wf-123")
    await storage.save_job(job)
    got = await storage.get_job(job.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.resume_workflow_run_id == "wf-123"


@pytest.mark.unit
async def test_job_resume_id_defaults_none(storage) -> None:
    """A normal (non-resume) job reads back None — pre-PR-2 / unchanged path."""
    job = _make_job(resume_workflow_run_id=None)
    await storage.save_job(job)
    got = await storage.get_job(job.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.resume_workflow_run_id is None


@pytest.mark.unit
async def test_job_resume_id_survives_claim(storage) -> None:
    """The worker's claim path must preserve resume_workflow_run_id (it drives
    the resume branch in dispatch)."""
    job = _make_job(resume_workflow_run_id="wf-123")
    await storage.save_job(job)
    claimed = await storage.claim_next_job(tenant_id="tenant-a")
    assert claimed is not None
    assert claimed.resume_workflow_run_id == "wf-123"
