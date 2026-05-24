"""``POST /api/v1/auth/keys/{key_id}/rotate`` + ``POST /api/v1/auth/keys/revoke-all``.

Both endpoints require the ``admin`` scope (ADR 013 D5 + L2) and are
tenant-scoped. Coverage:

* rotate: 201 returns a NEW working key; sets the old key's expiry to
  ~now+grace; BOTH keys authenticate during the grace window (zero
  downtime); after the window the old key 401s while the new one works;
  403 without admin; tenant isolation; 404 on unknown/other-tenant/revoked.
* revoke-all: revokes every active key for the tenant except the spared
  one (the caller's own by default); admin-gated; tenant-scoped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage

_ADMIN_SCOPE = "fleet-admin"  # resolves (via effective_scopes) to the full set incl. admin


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


def _save_key(
    storage: InMemoryStorage,
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    scopes: list[str] | None = None,
    env: str = "live",
    label: str | None = None,
    ttl_days: int = 90,
) -> object:
    tid = tenant_id or uuid4().hex
    minted = mint_api_key(
        tenant_id=tid, env=ApiKeyEnv(env), label=label, ttl_days=ttl_days, scopes=scopes
    )
    record = minted.record.model_copy(update={"scope": scope})

    async def _save() -> None:
        await storage.save_api_key(record)

    asyncio.get_event_loop().run_until_complete(_save())

    class _Minted:
        full_key = minted.full_key
        record_ = record

    m = _Minted()
    m.record = record  # type: ignore[attr-defined]
    return m


# ---------------------------------------------------------------------------
# POST /auth/keys/{key_id}/rotate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRotateKey:
    def test_admin_rotate_returns_201_and_new_working_key(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id, scopes=["read", "run"])
        resp = client.post(
            f"/api/v1/auth/keys/{target.record.key_id}/rotate",
            json={"grace_seconds": 3600},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["full_key"].startswith("mvt_")
        assert body["old_key_id"] == target.record.key_id
        assert body["key_id"] != target.record.key_id
        # New key authenticates.
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['full_key']}"})
        assert me.status_code == 200

    def test_old_key_expiry_set_to_about_now_plus_grace(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id)
        before = datetime.now(UTC)
        resp = client.post(
            f"/api/v1/auth/keys/{target.record.key_id}/rotate",
            json={"grace_seconds": 3600},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        old_expiry = datetime.fromisoformat(resp.json()["old_expires_at"])
        expected = before + timedelta(seconds=3600)
        assert abs((old_expiry - expected).total_seconds()) < 30

    def test_both_keys_valid_during_grace_window(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        """ZERO-DOWNTIME: old + new both authenticate inside the grace window."""
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id)
        old_full = target.full_key
        resp = client.post(
            f"/api/v1/auth/keys/{target.record.key_id}/rotate",
            json={"grace_seconds": 3600},  # 1h window
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        new_full = resp.json()["full_key"]
        # OLD key still works (it's mid-grace).
        old_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_full}"})
        assert old_me.status_code == 200
        # NEW key works.
        new_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_full}"})
        assert new_me.status_code == 200

    def test_old_key_rejected_after_window_lapses(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        """After the grace window the OLD key 401s; the NEW key keeps working."""
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id)
        old_full = target.full_key
        # grace_seconds=0 → old key expires at ~now, so it's already past.
        resp = client.post(
            f"/api/v1/auth/keys/{target.record.key_id}/rotate",
            json={"grace_seconds": 0},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        new_full = resp.json()["full_key"]
        # OLD key is expired → 401.
        old_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_full}"})
        assert old_me.status_code == 401
        # NEW key still works.
        new_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_full}"})
        assert new_me.status_code == 200

    def test_successor_inherits_scopes(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id, scopes=["read", "kb:write"])
        resp = client.post(
            f"/api/v1/auth/keys/{target.record.key_id}/rotate",
            json={},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        new_full = resp.json()["full_key"]
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_full}"})
        assert sorted(me.json()["scopes"]) == ["kb:write", "read"]

    def test_non_admin_gets_403(self, client: TestClient, storage: InMemoryStorage) -> None:
        regular = _save_key(storage, scope=None)
        target = _save_key(storage, tenant_id=regular.record.tenant_id)
        resp = client.post(
            f"/api/v1/auth/keys/{target.record.key_id}/rotate",
            json={},
            headers={"Authorization": f"Bearer {regular.full_key}"},
        )
        assert resp.status_code == 403

    def test_401_without_bearer(self, client: TestClient, storage: InMemoryStorage) -> None:
        target = _save_key(storage)
        resp = client.post(f"/api/v1/auth/keys/{target.record.key_id}/rotate", json={})
        assert resp.status_code == 401

    def test_404_unknown_key(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys/no-such-key/rotate",
            json={},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 404

    def test_404_cross_tenant(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin_a = _save_key(storage, scope=_ADMIN_SCOPE)
        key_b = _save_key(storage)  # different tenant
        resp = client.post(
            f"/api/v1/auth/keys/{key_b.record.key_id}/rotate",
            json={},
            headers={"Authorization": f"Bearer {admin_a.full_key}"},
        )
        assert resp.status_code == 404

    def test_404_already_revoked(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id)
        client.delete(
            f"/api/v1/auth/keys/{target.record.key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        resp = client.post(
            f"/api/v1/auth/keys/{target.record.key_id}/rotate",
            json={},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /auth/keys/revoke-all
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRevokeAll:
    def test_admin_revoke_all_spares_caller_by_default(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        """Safety: the caller's own key is spared so they aren't locked out."""
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        tenant = admin.record.tenant_id
        k1 = _save_key(storage, tenant_id=tenant)
        k2 = _save_key(storage, tenant_id=tenant)
        resp = client.post(
            "/api/v1/auth/keys/revoke-all",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["revoked_count"] == 2  # k1 + k2, NOT the admin caller
        assert body["spared_key_id"] == admin.record.key_id
        # The caller's key still authenticates.
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {admin.full_key}"})
        assert me.status_code == 200
        # The others are dead.
        for k in (k1, k2):
            assert (
                client.get(
                    "/api/v1/auth/me", headers={"Authorization": f"Bearer {k.full_key}"}
                ).status_code
                == 401
            )

    def test_explicit_except_key_id_overrides_caller_spare(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        tenant = admin.record.tenant_id
        keep = _save_key(storage, tenant_id=tenant)
        resp = client.post(
            "/api/v1/auth/keys/revoke-all",
            params={"except_key_id": keep.record.key_id},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        body = resp.json()
        assert body["spared_key_id"] == keep.record.key_id
        # The admin caller is NOT the spared key, so it got revoked.
        assert (
            client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {admin.full_key}"}
            ).status_code
            == 401
        )
        # The explicitly-kept key survives.
        assert (
            client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {keep.full_key}"}
            ).status_code
            == 200
        )

    def test_non_admin_gets_403(self, client: TestClient, storage: InMemoryStorage) -> None:
        regular = _save_key(storage, scope=None)
        resp = client.post(
            "/api/v1/auth/keys/revoke-all",
            headers={"Authorization": f"Bearer {regular.full_key}"},
        )
        assert resp.status_code == 403

    def test_401_without_bearer(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/keys/revoke-all")
        assert resp.status_code == 401

    def test_tenant_scoped(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin_a = _save_key(storage, scope=_ADMIN_SCOPE)
        key_b = _save_key(storage)  # other tenant
        client.post(
            "/api/v1/auth/keys/revoke-all",
            headers={"Authorization": f"Bearer {admin_a.full_key}"},
        )
        # Tenant B's key is untouched.
        assert (
            client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {key_b.full_key}"}
            ).status_code
            == 200
        )
