"""HTTP runtime — webhook subscription endpoints (ADR 035 D2).

Coverage:

* ``POST /api/v1/webhooks`` returns the secret EXACTLY ONCE; subsequent
  reads return ``secret_hint`` only.
* ``GET /api/v1/webhooks`` and ``GET /api/v1/webhooks/{id}`` are
  tenant-scoped (cross-tenant id 404s).
* ``DELETE`` is idempotent; ``PATCH`` toggles ``enabled``.
* ``GET /api/v1/webhooks/{id}/attempts`` surfaces the delivery log.
* Auth gates — 401 unauthenticated, 403 without the right scope.
* URL validation — HTTPS only at create time (422 on http://).

Hermetic: no delivery worker, no real network — we seed the attempts
log directly via ``record_webhook_attempt``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.webhooks import WILDCARD_KIND, WebhookAttempt
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


async def _mint(
    storage: InMemoryStorage,
    *,
    scopes: list[str],
    tenant_id: str | None = None,
) -> tuple[str, str]:
    tid = tenant_id or uuid4().hex
    minted = mint_api_key(tenant_id=tid, env=ApiKeyEnv.LIVE, label="webhook-tests", scopes=scopes)
    await storage.save_api_key(minted.record)
    return tid, f"Bearer {minted.full_key}"


# ---------------------------------------------------------------------------
# Create — secret returned once
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_create_returns_secret_once(client: TestClient, storage: InMemoryStorage) -> None:
    _tid, bearer = await _mint(storage, scopes=["admin", "read"])
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={
            "url": "https://example.com/hook",
            "kind_filter": ["run.completed"],
            "enabled": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["secret"]
    assert len(body["secret"]) > 20
    assert body["secret_hint"].endswith(body["secret"][-4:])
    assert body["secret_hint"] != body["secret"]
    webhook_id = body["id"]

    # Now GET — the full secret must NOT appear.
    one = client.get(f"/api/v1/webhooks/{webhook_id}", headers={"Authorization": bearer})
    assert one.status_code == 200
    one_body = one.json()
    assert "secret" not in one_body  # only secret_hint is on this view
    assert one_body["secret_hint"].endswith(body["secret"][-4:])


@pytest.mark.unit
async def test_create_rejects_http_url(client: TestClient, storage: InMemoryStorage) -> None:
    """422 on http:// — pydantic validator fires before the handler."""
    _tid, bearer = await _mint(storage, scopes=["admin"])
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={"url": "http://example.com/hook", "kind_filter": ["*"]},
    )
    assert resp.status_code == 422


@pytest.mark.unit
async def test_create_defaults_to_wildcard_filter(
    client: TestClient, storage: InMemoryStorage
) -> None:
    _tid, bearer = await _mint(storage, scopes=["admin"])
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={"url": "https://example.com/hook"},
    )
    assert resp.status_code == 201
    assert resp.json()["kind_filter"] == [WILDCARD_KIND]


# ---------------------------------------------------------------------------
# List / get / tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_returns_only_tenant_rows(client: TestClient, storage: InMemoryStorage) -> None:
    _tid_a, bearer_a = await _mint(storage, scopes=["admin", "read"])
    _tid_b, bearer_b = await _mint(storage, scopes=["admin", "read"])
    client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer_a},
        json={"url": "https://a.example/hook"},
    )
    client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer_b},
        json={"url": "https://b.example/hook"},
    )
    resp_a = client.get("/api/v1/webhooks", headers={"Authorization": bearer_a})
    resp_b = client.get("/api/v1/webhooks", headers={"Authorization": bearer_b})
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    a_urls = [w["url"] for w in resp_a.json()["webhooks"]]
    b_urls = [w["url"] for w in resp_b.json()["webhooks"]]
    assert a_urls == ["https://a.example/hook"]
    assert b_urls == ["https://b.example/hook"]


@pytest.mark.unit
async def test_get_cross_tenant_returns_404(client: TestClient, storage: InMemoryStorage) -> None:
    _tid_a, bearer_a = await _mint(storage, scopes=["admin", "read"])
    _tid_b, bearer_b = await _mint(storage, scopes=["read"])
    created = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer_a},
        json={"url": "https://example.com/hook"},
    ).json()
    # tenant-b can't see tenant-a's webhook — 404, not 403.
    resp = client.get(f"/api/v1/webhooks/{created['id']}", headers={"Authorization": bearer_b})
    assert resp.status_code == 404


@pytest.mark.unit
async def test_list_never_carries_full_secret(client: TestClient, storage: InMemoryStorage) -> None:
    _tid, bearer = await _mint(storage, scopes=["admin", "read"])
    created = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={"url": "https://example.com/hook"},
    ).json()
    full_secret = created["secret"]
    resp = client.get("/api/v1/webhooks", headers={"Authorization": bearer})
    assert resp.status_code == 200
    rendered = resp.text
    assert full_secret not in rendered


# ---------------------------------------------------------------------------
# Patch / delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_patch_enabled(client: TestClient, storage: InMemoryStorage) -> None:
    _tid, bearer = await _mint(storage, scopes=["admin", "read"])
    created = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={"url": "https://example.com/hook"},
    ).json()
    resp = client.patch(
        f"/api/v1/webhooks/{created['id']}",
        headers={"Authorization": bearer},
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


@pytest.mark.unit
async def test_delete_idempotent(client: TestClient, storage: InMemoryStorage) -> None:
    _tid, bearer = await _mint(storage, scopes=["admin"])
    created = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={"url": "https://example.com/hook"},
    ).json()
    resp1 = client.delete(f"/api/v1/webhooks/{created['id']}", headers={"Authorization": bearer})
    resp2 = client.delete(f"/api/v1/webhooks/{created['id']}", headers={"Authorization": bearer})
    assert resp1.status_code == 204
    assert resp2.status_code == 204


# ---------------------------------------------------------------------------
# Attempts feed
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_attempts_feed_surfaces_recorded_rows(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tid, bearer = await _mint(storage, scopes=["admin", "read"])
    created = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={"url": "https://example.com/hook"},
    ).json()
    webhook_id = created["id"]
    # Seed two attempts directly.
    await storage.record_webhook_attempt(
        WebhookAttempt(
            webhook_id=webhook_id,
            event_id="ev-1",
            tenant_id=tid,
            attempted_at=datetime.now(UTC),
            status_code=200,
            response_excerpt="ok",
            error_kind="ok",
            attempt_n=1,
        )
    )
    await storage.record_webhook_attempt(
        WebhookAttempt(
            webhook_id=webhook_id,
            event_id="ev-2",
            tenant_id=tid,
            attempted_at=datetime.now(UTC),
            status_code=500,
            response_excerpt="boom",
            error_kind="http_error",
            attempt_n=1,
        )
    )
    resp = client.get(
        f"/api/v1/webhooks/{webhook_id}/attempts",
        headers={"Authorization": bearer},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    kinds = {a["error_kind"] for a in body["attempts"]}
    assert kinds == {"ok", "http_error"}


@pytest.mark.unit
async def test_attempts_feed_404_for_unknown_id(
    client: TestClient, storage: InMemoryStorage
) -> None:
    _tid, bearer = await _mint(storage, scopes=["read"])
    resp = client.get(
        "/api/v1/webhooks/does-not-exist/attempts",
        headers={"Authorization": bearer},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth / scopes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unauthenticated_create_returns_401(client: TestClient) -> None:
    resp = client.post("/api/v1/webhooks", json={"url": "https://example.com/hook"})
    assert resp.status_code == 401


@pytest.mark.unit
async def test_create_requires_admin_scope(client: TestClient, storage: InMemoryStorage) -> None:
    _tid, bearer = await _mint(storage, scopes=["read"])
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer},
        json={"url": "https://example.com/hook"},
    )
    assert resp.status_code == 403


@pytest.mark.unit
async def test_list_requires_read_scope(client: TestClient, storage: InMemoryStorage) -> None:
    # A key with only ``run`` (no ``read``) is rejected — the
    # endpoint is read-gated. Note: empty-scope mints fall back to
    # ``LEGACY_DEFAULT_SCOPES`` which DOES include ``read``, so we
    # have to set an explicit narrow scope to exercise this gate.
    _tid, bearer = await _mint(storage, scopes=["run"])
    resp = client.get("/api/v1/webhooks", headers={"Authorization": bearer})
    assert resp.status_code == 403


@pytest.mark.unit
async def test_delete_requires_admin_scope(client: TestClient, storage: InMemoryStorage) -> None:
    _tid_a, bearer_admin = await _mint(storage, scopes=["admin", "read"])
    _tid_b, bearer_read = await _mint(storage, scopes=["read"])
    created = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": bearer_admin},
        json={"url": "https://example.com/hook"},
    ).json()
    # Different tenant + read-only scope can't DELETE: the scope check
    # fires first, so 403 (the row also wouldn't be visible cross-tenant
    # anyway, but scope is the FIRST gate).
    resp = client.delete(
        f"/api/v1/webhooks/{created['id']}", headers={"Authorization": bearer_read}
    )
    assert resp.status_code == 403
