"""Tests for the event/webhook trigger endpoints (ADR 017 D2).

Management CRUD (normal mvt_* key + AuthContext, tenant-scoped):

* POST   /api/v1/triggers — create (admin scope); secret returned ONCE
* GET    /api/v1/triggers — list (read scope), no secrets
* GET    /api/v1/triggers/{name} — fetch one (read scope)
* DELETE /api/v1/triggers/{name} — delete (admin scope)

The FIRE endpoint (per-trigger secret, NOT an API key):

* POST   /api/v1/triggers/{trigger_id}/events — HMAC-signed event → 202 + job

Mirrors tests/test_runtime_job_schedule_v1.py for the management surface and
adds the signed-fire path (the test signs requests the way a real caller
would, via the public core helpers).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.core.models import JobKind, JobStatus
from movate.core.triggers import expected_signature, signing_key
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
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="trigger-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


def _create(client: TestClient, header: dict, **body) -> dict:
    payload = {"target": "triage-agent", **body}
    r = client.post("/api/v1/triggers", json=payload, headers=header)
    assert r.status_code == 201, r.text
    return r.json()


def _sign(created: dict, raw_body: bytes) -> str:
    """Sign a body the way an external caller would, from the create response."""
    key = signing_key(created["secret"], created["salt"])
    return expected_signature(key, raw_body)


# ---------------------------------------------------------------------------
# Management CRUD
# ---------------------------------------------------------------------------


def test_list_empty_before_any_create(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    r = client.get("/api/v1/triggers", headers=header)
    assert r.status_code == 200, r.text
    assert r.json() == {"triggers": [], "count": 0}


def test_create_returns_secret_once_then_list_and_get(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    created = _create(
        client, header, name="zendesk", kind="workflow", input_defaults={"source": "zd"}
    )
    assert created["name"] == "zendesk"
    assert created["kind"] == "workflow"
    assert created["target"] == "triage-agent"
    assert created["input_defaults"] == {"source": "zd"}
    assert created["enabled"] is True
    assert created["last_fired_at"] is None
    # Secret + salt + webhook path are present at creation.
    assert created["secret"]
    assert created["salt"]
    assert created["webhook_path"] == f"/api/v1/triggers/{created['trigger_id']}/events"

    # List does NOT carry the secret (no-leak).
    lst = client.get("/api/v1/triggers", headers=header)
    assert lst.json()["count"] == 1
    item = lst.json()["triggers"][0]
    assert item["name"] == "zendesk"
    assert "secret" not in item

    one = client.get("/api/v1/triggers/zendesk", headers=header)
    assert one.status_code == 200
    assert one.json()["target"] == "triage-agent"
    assert "secret" not in one.json()


def test_create_upserts_on_name(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    _create(client, header, name="zendesk", target="agent-a")
    _create(client, header, name="zendesk", target="agent-b")
    lst = client.get("/api/v1/triggers", headers=header)
    assert lst.json()["count"] == 1
    assert lst.json()["triggers"][0]["target"] == "agent-b"


def test_delete_removes_trigger(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    _create(client, header, name="zendesk")
    d = client.delete("/api/v1/triggers/zendesk", headers=header)
    assert d.status_code == 204
    assert client.get("/api/v1/triggers", headers=header).json()["count"] == 0
    # Idempotent.
    assert client.delete("/api/v1/triggers/zendesk", headers=header).status_code == 204


def test_get_unknown_404(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    assert client.get("/api/v1/triggers/ghost", headers=header).status_code == 404


def test_create_eval_kind_422(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    r = client.post(
        "/api/v1/triggers",
        json={"kind": "eval", "target": "x"},
        headers=header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Scope gating
# ---------------------------------------------------------------------------


def test_create_unauthed_401(client: TestClient) -> None:
    r = client.post("/api/v1/triggers", json={"target": "x"})
    assert r.status_code == 401


async def test_create_requires_admin_scope(storage: InMemoryStorage, client: TestClient) -> None:
    """A read-only key cannot create (403); create gates on `admin`."""
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.post("/api/v1/triggers", json={"target": "x"}, headers=header)
    assert r.status_code == 403


async def test_delete_requires_admin_scope(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    admin_header, _ = auth_setup
    _create(client, admin_header, name="zendesk")
    # A read-only key can't delete.
    ro = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(ro.record)
    ro_header = {"Authorization": f"Bearer {ro.full_key}"}
    assert client.delete("/api/v1/triggers/zendesk", headers=ro_header).status_code == 403


async def test_read_only_key_can_list(storage: InMemoryStorage, client: TestClient) -> None:
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    assert client.get("/api/v1/triggers", headers=header).status_code == 200


async def test_list_is_tenant_scoped(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _ = auth_setup
    _create(client, header, name="zendesk")
    other = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    assert client.get("/api/v1/triggers", headers=other_header).json()["count"] == 0
    assert client.get("/api/v1/triggers/zendesk", headers=other_header).status_code == 404


# ---------------------------------------------------------------------------
# The fire endpoint (per-trigger secret, NOT an API key)
# ---------------------------------------------------------------------------


def test_fire_valid_signature_enqueues_job(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, tenant_id = auth_setup
    created = _create(client, header, name="zendesk", input_defaults={"source": "zd"})
    trigger_id = created["trigger_id"]

    raw = json.dumps({"ticket": 7, "priority": "high"}).encode()
    sig = _sign(created, raw)
    r = client.post(
        f"/api/v1/triggers/{trigger_id}/events",
        content=raw,
        headers={"X-Movate-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == JobStatus.QUEUED.value
    job_id = body["job_id"]

    # A job was enqueued, scoped to the TRIGGER's tenant, with merged input.
    job = storage.jobs[0]
    assert job.job_id == job_id
    assert job.tenant_id == tenant_id
    assert job.kind == JobKind.AGENT
    assert job.target == "triage-agent"
    assert job.input == {"source": "zd", "ticket": 7, "priority": "high"}

    # last_fired_at got stamped.
    fetched = client.get("/api/v1/triggers/zendesk", headers=header).json()
    assert fetched["last_fired_at"] is not None


def test_fire_no_api_key_needed(client: TestClient, auth_setup) -> None:
    """The fire endpoint is NOT behind api-key auth — only the signature."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = b"{}"
    sig = _sign(created, raw)
    # No Authorization header at all.
    r = client.post(
        f"/api/v1/triggers/{created['trigger_id']}/events",
        content=raw,
        headers={"X-Movate-Signature": sig},
    )
    assert r.status_code == 202, r.text


def test_fire_invalid_signature_401(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    r = client.post(
        f"/api/v1/triggers/{created['trigger_id']}/events",
        content=b"{}",
        headers={"X-Movate-Signature": "sha256=deadbeef"},
    )
    assert r.status_code == 401


def test_fire_missing_signature_header_401(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    r = client.post(f"/api/v1/triggers/{created['trigger_id']}/events", content=b"{}")
    assert r.status_code == 401


def test_fire_unknown_trigger_404(client: TestClient) -> None:
    r = client.post(
        "/api/v1/triggers/does-not-exist/events",
        content=b"{}",
        headers={"X-Movate-Signature": "sha256=deadbeef"},
    )
    assert r.status_code == 404


def test_fire_disabled_trigger_404(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """A disabled trigger 404s on fire (no existence leak to the caller)."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk", enabled=False)
    raw = b"{}"
    sig = _sign(created, raw)
    r = client.post(
        f"/api/v1/triggers/{created['trigger_id']}/events",
        content=raw,
        headers={"X-Movate-Signature": sig},
    )
    assert r.status_code == 404
    # Nothing enqueued.
    assert storage.jobs == []


def test_fire_non_object_body_400(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = b"[1, 2, 3]"
    sig = _sign(created, raw)
    r = client.post(
        f"/api/v1/triggers/{created['trigger_id']}/events",
        content=raw,
        headers={"X-Movate-Signature": sig},
    )
    assert r.status_code == 400
