"""Tests for the workflow HITL signal endpoints (ADR 017 D5, PR 2).

* GET  /api/v1/workflow-runs[?status=paused] — list (read scope), tenant-scoped
* POST /api/v1/workflow-runs/{id}/signal — resume a paused run (run scope)

Mirrors tests/test_runtime_job_schedule_v1.py for auth + tenant-scoping setup.
Asserts: a valid signal returns 202 + enqueues a continuation JobKind.WORKFLOW
job with resume_workflow_run_id set + flips the record out of "awaiting a
signal" (re-signal 409s); 404 on unknown/other-tenant; 409 on a non-paused
run; 422 when the decision omits a required output_contract key; the read +
write scope gates; tenant isolation on the list.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, SCOPE_RUN, ApiKeyEnv, mint_api_key
from movate.core.models import JobKind, WorkflowRunRecord, WorkflowStatus
from movate.runtime import build_app
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="signal-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


async def _seed_paused(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    workflow_run_id: str = "wf-1",
    output_contract: list[str] | None = None,
    status: WorkflowStatus = WorkflowStatus.PAUSED,
) -> WorkflowRunRecord:
    record = WorkflowRunRecord(
        workflow_run_id=workflow_run_id,
        tenant_id=tenant_id,
        workflow="approval-flow",
        workflow_version="0.1.0",
        status=status,
        initial_state={"text": "seed"},
        final_state={"text": "seed", "step1": "done"},
        paused_node_id="gate" if status is WorkflowStatus.PAUSED else None,
        paused_state={"text": "seed", "step1": "done"} if status is WorkflowStatus.PAUSED else None,
        human_task=(
            {"prompt": "Approve?", "output_contract": output_contract or ["decision"]}
            if status is WorkflowStatus.PAUSED
            else None
        ),
    )
    await storage.save_workflow_run(record)
    return record


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_empty(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/workflow-runs", headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json() == {"workflow_runs": [], "count": 0}


async def test_list_paused_surfaces_human_task(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_paused(storage, tenant_id=tenant_id)
    r = client.get("/api/v1/workflow-runs?status=paused", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    row = body["workflow_runs"][0]
    assert row["status"] == "paused"
    assert row["paused_node_id"] == "gate"
    assert row["human_task"]["prompt"] == "Approve?"
    assert row["human_task"]["output_contract"] == ["decision"]


# ---------------------------------------------------------------------------
# Signal — happy path + enqueue + idempotency
# ---------------------------------------------------------------------------


async def test_signal_valid_enqueues_continuation_job(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_paused(storage, tenant_id=tenant_id)

    r = client.post(
        "/api/v1/workflow-runs/wf-1/signal",
        json={"decision": {"decision": "approve"}},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    # A continuation JobKind.WORKFLOW job is enqueued carrying the resume id.
    job = await storage.get_job(job_id, tenant_id=tenant_id)
    assert job is not None
    assert job.kind is JobKind.WORKFLOW
    assert job.target == "approval-flow"
    assert job.resume_workflow_run_id == "wf-1"

    # The record was flipped out of "awaiting a signal": the decision is merged
    # into paused_state and the human_task carries the consumed marker.
    record = await storage.get_workflow_run("wf-1", tenant_id=tenant_id)
    assert record is not None
    assert record.paused_state["decision"] == "approve"
    assert record.human_task["signaled"] is True


async def test_resignal_is_409(client: TestClient, auth_setup, storage: InMemoryStorage) -> None:
    """Idempotency: a second signal on an already-signalled run → 409 (no
    double-resume)."""
    auth_header, tenant_id = auth_setup
    await _seed_paused(storage, tenant_id=tenant_id)

    first = client.post(
        "/api/v1/workflow-runs/wf-1/signal",
        json={"decision": {"decision": "approve"}},
        headers=auth_header,
    )
    assert first.status_code == 202

    second = client.post(
        "/api/v1/workflow-runs/wf-1/signal",
        json={"decision": {"decision": "approve"}},
        headers=auth_header,
    )
    assert second.status_code == 409, second.text
    # Only ONE continuation job was enqueued.
    jobs = await storage.list_jobs(tenant_id=tenant_id)
    assert len([j for j in jobs if j.resume_workflow_run_id == "wf-1"]) == 1


# ---------------------------------------------------------------------------
# Signal — errors
# ---------------------------------------------------------------------------


def test_signal_unknown_run_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/workflow-runs/ghost/signal",
        json={"decision": {"decision": "approve"}},
        headers=auth_header,
    )
    assert r.status_code == 404


async def test_signal_non_paused_run_409(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_paused(storage, tenant_id=tenant_id, status=WorkflowStatus.SUCCESS)
    r = client.post(
        "/api/v1/workflow-runs/wf-1/signal",
        json={"decision": {"decision": "approve"}},
        headers=auth_header,
    )
    assert r.status_code == 409


async def test_signal_missing_contract_key_422(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_paused(storage, tenant_id=tenant_id, output_contract=["decision", "reason"])
    r = client.post(
        "/api/v1/workflow-runs/wf-1/signal",
        json={"decision": {"decision": "approve"}},  # missing "reason"
        headers=auth_header,
    )
    assert r.status_code == 422, r.text
    assert "reason" in r.json()["detail"]["error"]["message"]


# ---------------------------------------------------------------------------
# Scope gates + tenant isolation
# ---------------------------------------------------------------------------


def test_signal_unauthed_401(client: TestClient) -> None:
    r = client.post("/api/v1/workflow-runs/wf-1/signal", json={"decision": {"decision": "approve"}})
    assert r.status_code == 401


async def test_signal_requires_run_scope(storage: InMemoryStorage, client: TestClient) -> None:
    """A read-only key cannot signal (403); signal gates on `run`."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    await _seed_paused(storage, tenant_id=tenant_id)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.post(
        "/api/v1/workflow-runs/wf-1/signal",
        json={"decision": {"decision": "approve"}},
        headers=header,
    )
    assert r.status_code == 403


async def test_list_requires_read_scope_only(storage: InMemoryStorage, client: TestClient) -> None:
    """A run-only key (no read) cannot list (403); list gates on `read`."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="run-only", scopes=[SCOPE_RUN]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.get("/api/v1/workflow-runs", headers=header)
    assert r.status_code == 403


async def test_signal_other_tenant_404(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """A different tenant cannot see or signal this tenant's paused run."""
    _, tenant_id = auth_setup
    await _seed_paused(storage, tenant_id=tenant_id)
    other = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    # Not listed for the other tenant.
    lst = client.get("/api/v1/workflow-runs?status=paused", headers=other_header)
    assert lst.json()["count"] == 0
    # And a direct signal is a 404 (never 403, which would leak existence).
    sig = client.post(
        "/api/v1/workflow-runs/wf-1/signal",
        json={"decision": {"decision": "approve"}},
        headers=other_header,
    )
    assert sig.status_code == 404
