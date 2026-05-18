"""Tests for ``POST/GET/DELETE /api/v1/auth/keys`` — admin-only key management.

The three endpoints require the calling key to have ``scope="fleet-admin"``.
Regular keys (scope=None / any other scope) get 403. Unauthenticated
requests get 401.

Coverage:
* POST /auth/keys — 201 for admin, 403 for non-admin, 401 for unauthed.
* GET /auth/keys — 200 for admin (masked keys), 403 for non-admin, 401 for unauthed.
* DELETE /auth/keys/{key_id} — 204/200 for admin, 403 for non-admin, 401 for unauthed,
  404 for not-found.
* Happy path: admin mints → list shows it → delete → list gone.
* Tenant isolation: admin from tenant A cannot see or delete tenant B's keys.
* Idempotent revoke returns success even when key is already revoked.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# The scope value the endpoints check against.
_ADMIN_SCOPE = "fleet-admin"

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


def _save_key(
    storage: InMemoryStorage,
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    env: str = "live",
    label: str | None = None,
    ttl_days: int = 90,
) -> object:
    """Mint and persist a key synchronously, returning the MintedApiKey.

    Pass ``scope="fleet-admin"`` for an admin key; omit (default None)
    for a regular tenant key.
    """
    tid = tenant_id or uuid4().hex
    minted = mint_api_key(
        tenant_id=tid,
        env=ApiKeyEnv(env),
        label=label,
        ttl_days=ttl_days,
    )
    # Patch scope onto the record (ApiKeyRecord is frozen; use model_copy).
    record_with_scope = minted.record.model_copy(update={"scope": scope})

    async def _save() -> None:
        await storage.save_api_key(record_with_scope)

    asyncio.get_event_loop().run_until_complete(_save())

    # Return a wrapper that gives access to both full_key and the patched record.
    class _Minted:
        full_key = minted.full_key
        record = record_with_scope

    return _Minted()


# ---------------------------------------------------------------------------
# POST /api/v1/auth/keys — mint (admin only)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMintKey:
    def test_admin_mint_returns_201(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "ci-bot", "ttl_days": 90},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 201

    def test_admin_mint_returns_key_fields(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "ci-bot", "ttl_days": 90},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        body = resp.json()
        assert "key_id" in body
        assert "full_key" in body
        assert body["tenant_id"] == admin.record.tenant_id
        assert body["env"] == "live"
        assert body["label"] == "ci-bot"
        assert body["expires_at"] is not None

    def test_full_key_has_mvt_prefix(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys",
            json={},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.json()["full_key"].startswith("mvt_")

    def test_label_none_when_omitted(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys",
            json={},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.json()["label"] is None

    def test_ttl_zero_means_no_expiry(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"ttl_days": 0},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 201
        assert resp.json()["expires_at"] is None

    def test_ttl_30_sets_expires_at(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"ttl_days": 30},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 201
        assert resp.json()["expires_at"] is not None

    def test_non_admin_gets_403(self, client: TestClient, storage: InMemoryStorage) -> None:
        regular = _save_key(storage, scope=None)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "should-fail"},
            headers={"Authorization": f"Bearer {regular.full_key}"},
        )
        assert resp.status_code == 403

    def test_mint_401_without_bearer(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/keys", json={})
        assert resp.status_code == 401

    def test_minted_key_is_usable(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "child"},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        new_full_key = resp.json()["full_key"]
        # The newly minted key (no admin scope) can still authenticate.
        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_full_key}"},
        )
        assert me.status_code == 200
        assert me.json()["label"] == "child"


# ---------------------------------------------------------------------------
# GET /api/v1/auth/keys — list (admin only)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListKeys:
    def test_admin_list_shows_own_keys(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE, label="admin-key")
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        key_ids = [k["key_id"] for k in body["keys"]]
        assert admin.record.key_id in key_ids

    def test_active_key_has_status_active(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE, label="admin-active")
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        keys = {k["key_id"]: k for k in resp.json()["keys"]}
        assert keys[admin.record.key_id]["status"] == "active"

    def test_non_admin_gets_403(self, client: TestClient, storage: InMemoryStorage) -> None:
        regular = _save_key(storage, scope=None)
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {regular.full_key}"},
        )
        assert resp.status_code == 403

    def test_list_401_without_bearer(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/keys")
        assert resp.status_code == 401

    def test_tenant_isolation_cannot_see_other_tenant_keys(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin_a = _save_key(storage, scope=_ADMIN_SCOPE, label="admin-a")
        # Tenant B's key (different tenant_id)
        key_b = _save_key(storage, label="tenant-b-key")
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin_a.full_key}"},
        )
        key_ids = [k["key_id"] for k in resp.json()["keys"]]
        assert admin_a.record.key_id in key_ids
        assert key_b.record.key_id not in key_ids

    def test_revoked_key_excluded_by_default(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        regular = _save_key(storage, tenant_id=admin.record.tenant_id)
        # Admin revokes the regular key.
        client.delete(
            f"/api/v1/auth/keys/{regular.record.key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        key_ids = [k["key_id"] for k in resp.json()["keys"]]
        assert regular.record.key_id not in key_ids

    def test_revoked_key_included_with_flag(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        regular = _save_key(storage, tenant_id=admin.record.tenant_id)
        client.delete(
            f"/api/v1/auth/keys/{regular.record.key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        resp = client.get(
            "/api/v1/auth/keys",
            params={"include_revoked": True},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        keys = {k["key_id"]: k for k in resp.json()["keys"]}
        assert regular.record.key_id in keys
        assert keys[regular.record.key_id]["status"] == "revoked"

    def test_count_matches_key_list_length(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        _save_key(storage, tenant_id=admin.record.tenant_id)
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        body = resp.json()
        assert body["count"] == len(body["keys"])


# ---------------------------------------------------------------------------
# DELETE /api/v1/auth/keys/{key_id} — revoke (admin only)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRevokeKey:
    def test_admin_revoke_returns_success_and_revoked_true(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id)
        resp = client.delete(
            f"/api/v1/auth/keys/{target.record.key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code in (200, 204)
        if resp.status_code == 200:
            body = resp.json()
            assert body["key_id"] == target.record.key_id
            assert body["revoked"] is True

    def test_revoke_idempotent(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        target = _save_key(storage, tenant_id=admin.record.tenant_id)
        client.delete(
            f"/api/v1/auth/keys/{target.record.key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        # Second revoke should still succeed.
        resp = client.delete(
            f"/api/v1/auth/keys/{target.record.key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code in (200, 204)

    def test_revoke_404_key_not_found(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        resp = client.delete(
            "/api/v1/auth/keys/no-such-key-id",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert resp.status_code == 404

    def test_revoke_404_cross_tenant(self, client: TestClient, storage: InMemoryStorage) -> None:
        admin_a = _save_key(storage, scope=_ADMIN_SCOPE)
        key_b = _save_key(storage, label="b")  # different tenant
        # Admin A cannot revoke tenant B's key — 404 (no info leakage).
        resp = client.delete(
            f"/api/v1/auth/keys/{key_b.record.key_id}",
            headers={"Authorization": f"Bearer {admin_a.full_key}"},
        )
        assert resp.status_code == 404

    def test_non_admin_gets_403(self, client: TestClient, storage: InMemoryStorage) -> None:
        regular = _save_key(storage, scope=None)
        target = _save_key(storage, tenant_id=regular.record.tenant_id)
        resp = client.delete(
            f"/api/v1/auth/keys/{target.record.key_id}",
            headers={"Authorization": f"Bearer {regular.full_key}"},
        )
        assert resp.status_code == 403

    def test_revoke_401_without_bearer(self, client: TestClient, storage: InMemoryStorage) -> None:
        key = _save_key(storage)
        resp = client.delete(f"/api/v1/auth/keys/{key.record.key_id}")
        assert resp.status_code == 401

    def test_revoked_key_cannot_authenticate(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        admin = _save_key(storage, scope=_ADMIN_SCOPE)
        victim = _save_key(storage, tenant_id=admin.record.tenant_id)
        victim_key = victim.full_key
        client.delete(
            f"/api/v1/auth/keys/{victim.record.key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {victim_key}"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path end-to-end: mint → list → delete → list again
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHappyPath:
    def test_mint_list_delete_list(self, client: TestClient, storage: InMemoryStorage) -> None:
        """Full lifecycle: admin mints a key, sees it in list, deletes it, it disappears."""
        admin = _save_key(storage, scope=_ADMIN_SCOPE, label="admin")

        # 1. Mint a new key.
        mint_resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "lifecycle-test", "ttl_days": 30},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert mint_resp.status_code == 201
        new_key_id = mint_resp.json()["key_id"]
        assert mint_resp.json()["full_key"].startswith("mvt_")

        # 2. List — newly minted key appears as active.
        list_resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert list_resp.status_code == 200
        key_ids = [k["key_id"] for k in list_resp.json()["keys"]]
        assert new_key_id in key_ids

        # 3. Delete.
        del_resp = client.delete(
            f"/api/v1/auth/keys/{new_key_id}",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        assert del_resp.status_code in (200, 204)

        # 4. List again — key is gone from default (active-only) view.
        list_resp2 = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        key_ids2 = [k["key_id"] for k in list_resp2.json()["keys"]]
        assert new_key_id not in key_ids2

        # 5. With include_revoked=true — key reappears as revoked.
        list_revoked = client.get(
            "/api/v1/auth/keys",
            params={"include_revoked": True},
            headers={"Authorization": f"Bearer {admin.full_key}"},
        )
        keys_by_id = {k["key_id"]: k for k in list_revoked.json()["keys"]}
        assert new_key_id in keys_by_id
        assert keys_by_id[new_key_id]["status"] == "revoked"


# ---------------------------------------------------------------------------
# Tenant isolation — comprehensive
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTenantIsolation:
    def test_admin_a_cannot_list_tenant_b_keys(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        """Admin key for tenant A never sees tenant B's keys."""
        admin_a = _save_key(storage, scope=_ADMIN_SCOPE, label="admin-a")
        _save_key(storage, label="b-key-1")  # tenant B key (random tenant_id)

        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {admin_a.full_key}"},
        )
        returned_ids = {k["key_id"] for k in resp.json()["keys"]}
        # Should only see tenant A's key.
        assert admin_a.record.key_id in returned_ids
        assert all(
            storage.api_keys[
                next(i for i, k in enumerate(storage.api_keys) if k.key_id == kid)
            ].tenant_id
            == admin_a.record.tenant_id
            for kid in returned_ids
        )

    def test_admin_a_cannot_delete_tenant_b_key(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        """Admin key for tenant A gets 404 when trying to delete tenant B's key."""
        admin_a = _save_key(storage, scope=_ADMIN_SCOPE)
        key_b = _save_key(storage)  # different tenant

        resp = client.delete(
            f"/api/v1/auth/keys/{key_b.record.key_id}",
            headers={"Authorization": f"Bearer {admin_a.full_key}"},
        )
        assert resp.status_code == 404
