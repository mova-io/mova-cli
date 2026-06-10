"""Tests for the generic cron-schedule endpoints (ADR 017 D2).

* PUT    /api/v1/schedules/{name} — upsert a schedule (run scope)
* GET    /api/v1/schedules — list this tenant's schedules (read scope)
* GET    /api/v1/schedules/{name} — fetch one (read scope)
* DELETE /api/v1/schedules/{name} — clear (run scope)

Mirrors tests/test_runtime_eval_schedule_v1.py. Asserts additive/default-off
(empty list before any PUT), upsert idempotency, tenant scoping, the 404 on
an unknown name, and the scope gate (writes need ``run``; reads need
``read``). Unlike the eval endpoints, target existence is NOT validated here
(mirrors POST /run — the worker surfaces an unknown target when it claims
the job), so no agent fixture is needed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
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
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="sched-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


# ---------------------------------------------------------------------------
# Default-off + happy path
# ---------------------------------------------------------------------------


def test_list_empty_before_any_set(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/schedules", headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json() == {"schedules": [], "count": 0}


def test_set_then_list_and_get(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/schedules/nightly",
        json={
            "kind": "workflow",
            "target": "returns-pipeline",
            "cadence_seconds": 86400,
            "input": {"region": "us"},
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "nightly"
    assert body["kind"] == "workflow"
    assert body["target"] == "returns-pipeline"
    assert body["cadence_seconds"] == 86400
    assert body["input"] == {"region": "us"}
    assert body["last_enqueued_at"] is None

    lst = client.get("/api/v1/schedules", headers=auth_header)
    assert lst.json()["count"] == 1
    assert lst.json()["schedules"][0]["name"] == "nightly"

    one = client.get("/api/v1/schedules/nightly", headers=auth_header)
    assert one.status_code == 200
    assert one.json()["target"] == "returns-pipeline"


def test_set_is_idempotent_upsert(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    client.put(
        "/api/v1/schedules/nightly",
        json={"target": "faq", "cadence_seconds": 3600},
        headers=auth_header,
    )
    client.put(
        "/api/v1/schedules/nightly",
        json={"target": "faq", "cadence_seconds": 600},
        headers=auth_header,
    )
    lst = client.get("/api/v1/schedules", headers=auth_header)
    assert lst.json()["count"] == 1
    assert lst.json()["schedules"][0]["cadence_seconds"] == 600


def test_delete_clears_schedule(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    client.put(
        "/api/v1/schedules/nightly",
        json={"target": "faq", "cadence_seconds": 3600},
        headers=auth_header,
    )
    d = client.delete("/api/v1/schedules/nightly", headers=auth_header)
    assert d.status_code == 204
    assert client.get("/api/v1/schedules", headers=auth_header).json()["count"] == 0
    # Idempotent: deleting again is still 204.
    again = client.delete("/api/v1/schedules/nightly", headers=auth_header)
    assert again.status_code == 204


# ---------------------------------------------------------------------------
# Cron passthrough (ADR 100 D1)
# ---------------------------------------------------------------------------


def test_set_cron_schedule_round_trips(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/schedules/briefing",
        json={
            "kind": "workflow",
            "target": "exec-briefing",
            "cron": "0 7 * * 1-5",
            "timezone": "America/New_York",
            "input": {"audience": "leadership"},
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cron"] == "0 7 * * 1-5"
    assert body["timezone"] == "America/New_York"
    assert body["cadence_seconds"] == 0

    one = client.get("/api/v1/schedules/briefing", headers=auth_header)
    assert one.status_code == 200
    assert one.json()["cron"] == "0 7 * * 1-5"
    assert one.json()["timezone"] == "America/New_York"


def test_interval_schedule_view_carries_null_cron(client: TestClient, auth_setup) -> None:
    """Back-compat: an interval schedule reads back with cron/timezone null."""
    auth_header, _ = auth_setup
    client.put(
        "/api/v1/schedules/nightly",
        json={"target": "faq", "cadence_seconds": 3600},
        headers=auth_header,
    )
    body = client.get("/api/v1/schedules/nightly", headers=auth_header).json()
    assert body["cron"] is None
    assert body["timezone"] is None
    assert body["cadence_seconds"] == 3600


def test_set_both_cron_and_cadence_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/schedules/briefing",
        json={"target": "faq", "cadence_seconds": 3600, "cron": "0 7 * * *"},
        headers=auth_header,
    )
    assert r.status_code == 422


def test_set_neither_cron_nor_cadence_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/schedules/briefing",
        json={"target": "faq"},
        headers=auth_header,
    )
    assert r.status_code == 422


def test_set_invalid_cron_expression_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/schedules/briefing",
        json={"target": "faq", "cron": "99 99 * * *"},
        headers=auth_header,
    )
    assert r.status_code == 422


def test_set_timezone_without_cron_422(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/schedules/briefing",
        json={"target": "faq", "cadence_seconds": 3600, "timezone": "America/New_York"},
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Errors + scope gate + validation
# ---------------------------------------------------------------------------


def test_get_unknown_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/schedules/ghost", headers=auth_header)
    assert r.status_code == 404


def test_set_eval_kind_422(client: TestClient, auth_setup) -> None:
    """The model validator rejects eval/bench kinds → 422."""
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/schedules/nightly",
        json={"kind": "eval", "target": "faq", "cadence_seconds": 3600},
        headers=auth_header,
    )
    assert r.status_code == 422


def test_set_unauthed_401(client: TestClient) -> None:
    r = client.put("/api/v1/schedules/nightly", json={"target": "faq", "cadence_seconds": 3600})
    assert r.status_code == 401


async def test_set_requires_run_scope(storage: InMemoryStorage, client: TestClient) -> None:
    """A read-only key cannot write a schedule (403); writes gate on `run`."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.put(
        "/api/v1/schedules/nightly",
        json={"target": "faq", "cadence_seconds": 3600},
        headers=header,
    )
    assert r.status_code == 403


async def test_read_only_key_can_list(storage: InMemoryStorage, client: TestClient) -> None:
    """Reads gate on `read` — a read-only key lists fine."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.get("/api/v1/schedules", headers=header)
    assert r.status_code == 200


async def test_list_is_tenant_scoped(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    auth_header, _ = auth_setup
    client.put(
        "/api/v1/schedules/nightly",
        json={"target": "faq", "cadence_seconds": 3600},
        headers=auth_header,
    )
    other = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    r = client.get("/api/v1/schedules", headers=other_header)
    assert r.json()["count"] == 0
    # And the other tenant gets a 404 (not the row) on a direct fetch.
    assert client.get("/api/v1/schedules/nightly", headers=other_header).status_code == 404
