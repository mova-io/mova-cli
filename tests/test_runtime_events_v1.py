"""HTTP runtime — ``GET /api/v1/events`` events outbox feed (ADR 035 D1).

Coverage:

* Empty store returns ``{events: [], count: 0}``.
* Recorded events surface in oldest-first order, with the right filters
  (``kind`` / ``subject`` / ``since`` / ``until``).
* Cursor pagination via ``after_id`` walks forward without overlap or gap.
* Tenant scoping — a tenant only sees its own events.
* fleet-admin ``?tenant=<id>`` override scopes to another tenant; a non-
  fleet-admin caller silently ignores ``tenant=`` and reads its own.
* Auth gates — 401 unauthenticated, 403 without ``read``, 422 on bad
  ``limit``.

Hermetic: no agents on disk, no worker process; tests seed the outbox by
calling ``storage.record_event`` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.events import Event, EventKind
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


async def _mint(
    storage: InMemoryStorage,
    *,
    scopes: list[str],
    tenant_id: str | None = None,
) -> tuple[str, str]:
    """Mint a key with the given scopes; return (tenant_id, bearer header)."""
    tid = tenant_id or uuid4().hex
    minted = mint_api_key(tenant_id=tid, env=ApiKeyEnv.LIVE, label="events-tests", scopes=scopes)
    await storage.save_api_key(minted.record)
    return tid, f"Bearer {minted.full_key}"


def _seed_event(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    kind: str = EventKind.RUN_COMPLETED.value,
    subject: str = "faq-agent",
    data: dict | None = None,
    created_at: datetime | None = None,
) -> Event:
    e = Event(
        tenant_id=tenant_id,
        kind=kind,
        subject=subject,
        data=data or {},
        created_at=created_at or datetime.now(UTC),
    )
    storage.events.append(e)
    return e


# ---------------------------------------------------------------------------
# Empty store + basic list
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_empty_store_returns_empty_list(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, bearer = await _mint(storage, scopes=["read"])
    resp = client.get("/api/v1/events", headers={"Authorization": bearer})
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["count"] == 0
    assert body["next_after_id"] is None


@pytest.mark.unit
async def test_list_returns_recorded_events_oldest_first(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["read"])
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="run-1",
        created_at=t0,
    )
    _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="run-2",
        created_at=t0 + timedelta(minutes=1),
    )
    _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="run-3",
        created_at=t0 + timedelta(minutes=2),
    )
    resp = client.get("/api/v1/events", headers={"Authorization": bearer})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert [e["subject"] for e in body["events"]] == ["run-1", "run-2", "run-3"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_filter_by_kind(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["read"])
    _seed_event(storage, tenant_id=tenant_id, kind="run.completed", subject="a")
    _seed_event(storage, tenant_id=tenant_id, kind="run.failed", subject="b")
    resp = client.get("/api/v1/events?kind=run.failed", headers={"Authorization": bearer})
    body = resp.json()
    assert body["count"] == 1
    assert body["events"][0]["kind"] == "run.failed"
    assert body["events"][0]["subject"] == "b"


@pytest.mark.unit
async def test_filter_by_subject(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["read"])
    _seed_event(storage, tenant_id=tenant_id, subject="faq-agent")
    _seed_event(storage, tenant_id=tenant_id, subject="support-agent")
    _seed_event(storage, tenant_id=tenant_id, subject="faq-agent")
    resp = client.get("/api/v1/events?subject=faq-agent", headers={"Authorization": bearer})
    body = resp.json()
    assert body["count"] == 2
    assert all(e["subject"] == "faq-agent" for e in body["events"])


@pytest.mark.unit
async def test_default_since_window_is_24h(client: TestClient, storage: InMemoryStorage) -> None:
    """Default ``since`` is now-24h — events outside the window are
    invisible to a caller who didn't pin ``since`` explicitly."""
    tenant_id, bearer = await _mint(storage, scopes=["read"])
    # Old event, well outside the default window.
    _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="old",
        created_at=datetime.now(UTC) - timedelta(days=7),
    )
    # Recent event, inside the default window.
    _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="recent",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    resp = client.get("/api/v1/events", headers={"Authorization": bearer})
    body = resp.json()
    subjects = [e["subject"] for e in body["events"]]
    assert "recent" in subjects
    assert "old" not in subjects
    # Pinning ``since`` to a wider window includes the old one too.
    resp = client.get(
        "/api/v1/events?since=2000-01-01T00:00:00Z",
        headers={"Authorization": bearer},
    )
    subjects = [e["subject"] for e in resp.json()["events"]]
    assert subjects == ["old", "recent"]


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cursor_pagination_via_after_id(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["read"])
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    for i in range(5):
        _seed_event(
            storage,
            tenant_id=tenant_id,
            subject=f"run-{i}",
            created_at=t0 + timedelta(minutes=i),
        )
    # Page 1: limit=2 → first 2, next_after_id populated (truncated).
    resp = client.get("/api/v1/events?limit=2", headers={"Authorization": bearer})
    body = resp.json()
    assert [e["subject"] for e in body["events"]] == ["run-0", "run-1"]
    assert body["next_after_id"] is not None
    cursor = body["next_after_id"]
    # Page 2.
    resp = client.get(
        f"/api/v1/events?limit=2&after_id={cursor}",
        headers={"Authorization": bearer},
    )
    body = resp.json()
    assert [e["subject"] for e in body["events"]] == ["run-2", "run-3"]
    cursor = body["next_after_id"]
    # Page 3: partial — one row, no further cursor.
    resp = client.get(
        f"/api/v1/events?limit=2&after_id={cursor}",
        headers={"Authorization": bearer},
    )
    body = resp.json()
    assert [e["subject"] for e in body["events"]] == ["run-4"]
    assert body["next_after_id"] is None


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_non_admin_sees_only_own_tenant(client: TestClient, storage: InMemoryStorage) -> None:
    """A read key sees only its own tenant's events — even if it tries
    to pass ``?tenant=<other>``."""
    tenant_a, bearer_a = await _mint(storage, scopes=["read"])
    tenant_b = uuid4().hex
    _seed_event(storage, tenant_id=tenant_a, subject="from-a")
    _seed_event(storage, tenant_id=tenant_b, subject="from-b")
    # Default scope.
    resp = client.get("/api/v1/events", headers={"Authorization": bearer_a})
    body = resp.json()
    assert [e["subject"] for e in body["events"]] == ["from-a"]
    # Attempt to peek into tenant_b — silently ignored (no fleet-admin).
    resp = client.get(f"/api/v1/events?tenant={tenant_b}", headers={"Authorization": bearer_a})
    body = resp.json()
    assert [e["subject"] for e in body["events"]] == ["from-a"]


@pytest.mark.unit
async def test_fleet_admin_can_override_tenant(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """A fleet-admin key may scope the read to a different tenant by
    passing ``?tenant=<id>``."""
    admin_tid, bearer = await _mint(storage, scopes=["fleet-admin"])
    target_tid = uuid4().hex
    _seed_event(storage, tenant_id=admin_tid, subject="own")
    _seed_event(storage, tenant_id=target_tid, subject="target")
    resp = client.get(f"/api/v1/events?tenant={target_tid}", headers={"Authorization": bearer})
    body = resp.json()
    assert [e["subject"] for e in body["events"]] == ["target"]


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_401_without_bearer(client: TestClient) -> None:
    resp = client.get("/api/v1/events")
    assert resp.status_code == 401


@pytest.mark.unit
async def test_403_without_read_scope(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, bearer = await _mint(storage, scopes=["run"])  # no "read"
    resp = client.get("/api/v1/events", headers={"Authorization": bearer})
    assert resp.status_code == 403


@pytest.mark.unit
async def test_422_on_bad_limit(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, bearer = await _mint(storage, scopes=["read"])
    # limit > 1000 fails the FastAPI ge/le validation.
    resp = client.get("/api/v1/events?limit=5000", headers={"Authorization": bearer})
    assert resp.status_code == 422
    # limit < 1 also fails.
    resp = client.get("/api/v1/events?limit=0", headers={"Authorization": bearer})
    assert resp.status_code == 422
