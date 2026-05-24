"""Tests for the batch-inference endpoints (item 17).

* ``POST /api/v1/agents/{name}/batch`` — JSONL upload + inline JSON body;
  enqueues one AGENT job per row sharing a batch_id; 422 empty; 413 oversized;
  404 unknown agent; 403 without ``run`` scope; tenant isolation.
* ``GET /api/v1/batches/{batch_id}`` — per-status aggregate + derived state;
  404 on unknown / other-tenant.
* ``GET /api/v1/batches`` — recent batches list, tenant-scoped.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.core.models import JobKind, JobStatus
from movate.runtime import build_app
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    """(auth_header, tenant_id) for a full-scope key."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="batch-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


@pytest.fixture
async def read_only_setup(storage: InMemoryStorage):
    """(auth_header, tenant_id) for a key with ONLY the read scope —
    can view batches but cannot submit one."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="batch-readonly", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: batch-demo
version: 0.1.0
description: demo for batch inference
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Hi {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()
_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
).encode()


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


_THREE_ROWS = b'{"text": "a"}\n{"text": "b"}\n{"text": "c"}\n'


# ---------------------------------------------------------------------------
# Submit — JSONL upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_jsonl_enqueues_one_job_per_row(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        files={"file": ("dataset.jsonl", _THREE_ROWS, "application/x-ndjson")},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["total"] == 3
    batch_id = body["batch_id"]
    assert batch_id

    # Exactly 3 AGENT jobs, all carrying the same batch_id + scoped to tenant.
    children = await storage.list_jobs(tenant_id=tenant_id, batch_id=batch_id, limit=100)
    assert len(children) == 3
    assert {c.batch_id for c in children} == {batch_id}
    assert all(c.kind == JobKind.AGENT for c in children)
    assert all(c.target == "batch-demo" for c in children)
    assert all(c.tenant_id == tenant_id for c in children)
    assert all(c.status == JobStatus.QUEUED for c in children)
    assert {json.dumps(c.input, sort_keys=True) for c in children} == {
        json.dumps({"text": t}, sort_keys=True) for t in ("a", "b", "c")
    }

    # Parent BatchRecord persisted.
    batch = await storage.get_batch(batch_id, tenant_id=tenant_id)
    assert batch is not None
    assert batch.agent == "batch-demo"
    assert batch.total == 3


@pytest.mark.asyncio
async def test_batch_notify_email_propagates_to_every_child(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        files={"file": ("dataset.jsonl", _THREE_ROWS, "application/x-ndjson")},
        data={"notify_email": "ops@example.com"},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    batch_id = r.json()["batch_id"]
    children = await storage.list_jobs(tenant_id=tenant_id, batch_id=batch_id, limit=100)
    assert all(c.notify_email == "ops@example.com" for c in children)


# ---------------------------------------------------------------------------
# Submit — inline JSON body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_inline_json_body(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        json={"inputs": [{"text": "a"}, {"text": "b"}]},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["total"] == 2
    children = await storage.list_jobs(tenant_id=tenant_id, batch_id=body["batch_id"], limit=100)
    assert len(children) == 2


# ---------------------------------------------------------------------------
# Submit — error paths
# ---------------------------------------------------------------------------


def test_batch_empty_dataset_returns_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        json={"inputs": []},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


def test_batch_empty_jsonl_returns_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        files={"file": ("dataset.jsonl", b"\n\n", "application/x-ndjson")},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


def test_batch_malformed_jsonl_returns_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        files={"file": ("dataset.jsonl", b'{"text": "ok"}\nnot json\n', "application/x-ndjson")},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


def test_batch_non_object_row_returns_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        files={"file": ("dataset.jsonl", b'{"text": "ok"}\n[1, 2, 3]\n', "application/x-ndjson")},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


def test_batch_oversized_returns_413(
    client: TestClient, auth_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dataset over the row cap is rejected 413. We tighten the cap via the
    documented MDK_BATCH_MAX_ROWS env var so the test stays fast."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    monkeypatch.setenv("MDK_BATCH_MAX_ROWS", "2")
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        json={"inputs": [{"text": "a"}, {"text": "b"}, {"text": "c"}]},
        headers=auth_header,
    )
    assert r.status_code == 413, r.text


def test_batch_unknown_agent_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/agents/never-existed/batch",
        json={"inputs": [{"text": "a"}]},
        headers=auth_header,
    )
    assert r.status_code == 404, r.text


def test_batch_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        json={"inputs": [{"text": "a"}]},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_batch_without_run_scope_returns_403(
    client: TestClient, storage: InMemoryStorage, auth_setup, read_only_setup
) -> None:
    """A read-only key cannot submit a batch (submit gates on ``run``)."""
    full_header, _ = auth_setup
    _create_agent(client, full_header)
    ro_header, _ = read_only_setup
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        json={"inputs": [{"text": "a"}]},
        headers=ro_header,
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_batch_tenant_isolation_on_get(
    client: TestClient, storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """Another tenant cannot read this batch's status — 404, never the data."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        json={"inputs": [{"text": "a"}]},
        headers=auth_header,
    )
    batch_id = r.json()["batch_id"]

    # A second tenant with its own key.
    other_tenant = uuid4().hex
    other = mint_api_key(
        tenant_id=other_tenant, env=ApiKeyEnv.LIVE, label="other", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}

    r2 = client.get(f"/api/v1/batches/{batch_id}", headers=other_header)
    assert r2.status_code == 404, r2.text


# ---------------------------------------------------------------------------
# Status aggregate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_status_reports_all_queued(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        files={"file": ("dataset.jsonl", _THREE_ROWS, "application/x-ndjson")},
        headers=auth_header,
    )
    batch_id = r.json()["batch_id"]

    status = client.get(f"/api/v1/batches/{batch_id}", headers=auth_header)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["total"] == 3
    assert body["agent"] == "batch-demo"
    assert body["counts"]["queued"] == 3
    assert body["counts"]["success"] == 0
    assert body["state"] == "running"  # nothing terminal yet
    assert len(body["job_ids"]) == 3


@pytest.mark.asyncio
async def test_batch_status_aggregates_mixed_terminal_states(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Flip some children to terminal states and assert the aggregate +
    derived overall state."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        files={"file": ("dataset.jsonl", _THREE_ROWS, "application/x-ndjson")},
        headers=auth_header,
    )
    batch_id = r.json()["batch_id"]

    children = await storage.list_jobs(tenant_id=tenant_id, batch_id=batch_id, limit=100)
    assert len(children) == 3
    # One success, one error, one still queued → state stays "running".
    await storage.claim_next_job(tenant_id=tenant_id)  # flips one to RUNNING
    await storage.update_job(children[0].job_id, tenant_id=tenant_id, status=JobStatus.SUCCESS)
    await storage.update_job(
        children[1].job_id,
        tenant_id=tenant_id,
        status=JobStatus.ERROR,
        error={"type": "x", "message": "boom", "retryable": False},
    )

    body = client.get(f"/api/v1/batches/{batch_id}", headers=auth_header).json()
    assert body["counts"]["success"] == 1
    assert body["counts"]["error"] == 1
    # Third child: either RUNNING (if it was the one claimed) or QUEUED — both
    # non-terminal, so the batch is still "running".
    assert body["counts"]["queued"] + body["counts"]["running"] == 1
    assert body["state"] == "running"


@pytest.mark.asyncio
async def test_batch_status_complete_when_all_terminal(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/batch-demo/batch",
        json={"inputs": [{"text": "a"}, {"text": "b"}]},
        headers=auth_header,
    )
    batch_id = r.json()["batch_id"]
    children = await storage.list_jobs(tenant_id=tenant_id, batch_id=batch_id, limit=100)
    for c in children:
        await storage.update_job(c.job_id, tenant_id=tenant_id, status=JobStatus.SUCCESS)

    body = client.get(f"/api/v1/batches/{batch_id}", headers=auth_header).json()
    assert body["counts"]["success"] == 2
    assert body["state"] == "complete"


def test_batch_status_unknown_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/batches/never-existed", headers=auth_header)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_batches_returns_recent(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    for _ in range(2):
        client.post(
            "/api/v1/agents/batch-demo/batch",
            json={"inputs": [{"text": "a"}]},
            headers=auth_header,
        )
    r = client.get("/api/v1/batches", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert len(body["batches"]) == 2
    assert all(b["agent"] == "batch-demo" for b in body["batches"])


def test_list_batches_without_auth_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/batches")
    assert r.status_code == 401
