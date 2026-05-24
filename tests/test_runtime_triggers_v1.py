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


# ---------------------------------------------------------------------------
# Idempotency / replay (item 23 — X-Movate-Delivery-Id dedup)
# ---------------------------------------------------------------------------


def _fire(client: TestClient, created: dict, raw: bytes, *, delivery_id: str | None = None):
    headers = {"X-Movate-Signature": _sign(created, raw), "Content-Type": "application/json"}
    if delivery_id is not None:
        headers["X-Movate-Delivery-Id"] = delivery_id
    return client.post(
        f"/api/v1/triggers/{created['trigger_id']}/events",
        content=raw,
        headers=headers,
    )


def test_fire_same_delivery_id_twice_enqueues_one_job(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """Same delivery-id twice → exactly ONE job; the second is a flagged replay."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = json.dumps({"ticket": 1}).encode()

    first = _fire(client, created, raw, delivery_id="delivery-abc")
    assert first.status_code == 202, first.text
    assert first.json()["deduplicated"] is False
    job_id = first.json()["job_id"]

    second = _fire(client, created, raw, delivery_id="delivery-abc")
    assert second.status_code == 202, second.text
    # Same job_id, flagged as a replay.
    assert second.json()["job_id"] == job_id
    assert second.json()["deduplicated"] is True

    # Exactly ONE job was enqueued (the retry did NOT double-enqueue).
    assert len(storage.jobs) == 1
    assert storage.jobs[0].job_id == job_id


def test_fire_different_delivery_ids_enqueue_two_jobs(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = json.dumps({"ticket": 1}).encode()

    r1 = _fire(client, created, raw, delivery_id="delivery-1")
    r2 = _fire(client, created, raw, delivery_id="delivery-2")
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["deduplicated"] is False
    assert r2.json()["deduplicated"] is False
    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert len(storage.jobs) == 2


def test_fire_no_delivery_id_enqueues_each_call(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """Back-compat: with no delivery-id header, EVERY valid call enqueues."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = json.dumps({"ticket": 1}).encode()

    r1 = _fire(client, created, raw)
    r2 = _fire(client, created, raw)
    assert r1.status_code == 202 and r2.status_code == 202
    # No dedup → default False, two distinct jobs.
    assert r1.json()["deduplicated"] is False
    assert r2.json()["deduplicated"] is False
    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert len(storage.jobs) == 2
    # Nothing recorded in the dedup store.
    assert storage.trigger_deliveries == {}


def test_fire_empty_delivery_id_treated_as_absent(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """An empty / whitespace delivery-id is ignored → no dedup, always enqueue."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = json.dumps({"ticket": 1}).encode()

    r1 = _fire(client, created, raw, delivery_id="   ")
    r2 = _fire(client, created, raw, delivery_id="   ")
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["deduplicated"] is False
    assert r2.json()["deduplicated"] is False
    assert len(storage.jobs) == 2
    assert storage.trigger_deliveries == {}


def test_fire_overlong_delivery_id_treated_as_absent(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """An over-long delivery-id is ignored (storage-bloat guard) → always enqueue."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = json.dumps({"ticket": 1}).encode()
    too_long = "x" * 201

    r1 = _fire(client, created, raw, delivery_id=too_long)
    r2 = _fire(client, created, raw, delivery_id=too_long)
    assert r1.status_code == 202 and r2.status_code == 202
    assert len(storage.jobs) == 2
    assert storage.trigger_deliveries == {}


def test_fire_bad_signature_with_delivery_id_records_nothing(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """Auth gates BEFORE dedup: a bad signature → 401, nothing read/written."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    r = client.post(
        f"/api/v1/triggers/{created['trigger_id']}/events",
        content=b"{}",
        headers={"X-Movate-Signature": "sha256=deadbeef", "X-Movate-Delivery-Id": "delivery-abc"},
    )
    assert r.status_code == 401
    assert storage.jobs == []
    assert storage.trigger_deliveries == {}


def test_fire_unknown_trigger_with_delivery_id_records_nothing(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """An unknown trigger 404s before any dedup read/write."""
    r = client.post(
        "/api/v1/triggers/does-not-exist/events",
        content=b"{}",
        headers={"X-Movate-Signature": "sha256=deadbeef", "X-Movate-Delivery-Id": "delivery-abc"},
    )
    assert r.status_code == 404
    assert storage.jobs == []
    assert storage.trigger_deliveries == {}


def test_fire_disabled_trigger_with_delivery_id_records_nothing(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """A disabled trigger 404s before any dedup read/write."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk", enabled=False)
    r = _fire(client, created, b"{}", delivery_id="delivery-abc")
    assert r.status_code == 404
    assert storage.jobs == []
    assert storage.trigger_deliveries == {}


def test_fire_replay_does_not_restamp_last_fired_at(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """A replay must NOT re-touch the trigger's last_fired_at."""
    header, _ = auth_setup
    created = _create(client, header, name="zendesk")
    raw = b"{}"

    _fire(client, created, raw, delivery_id="delivery-abc")
    stamped = client.get("/api/v1/triggers/zendesk", headers=header).json()["last_fired_at"]
    assert stamped is not None

    # Replay: last_fired_at must be unchanged.
    second = _fire(client, created, raw, delivery_id="delivery-abc")
    assert second.json()["deduplicated"] is True
    after = client.get("/api/v1/triggers/zendesk", headers=header).json()["last_fired_at"]
    assert after == stamped
