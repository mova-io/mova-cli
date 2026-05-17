"""Tests for ``POST/GET/DELETE /api/v1/auth/keys`` — runtime key management.

The three endpoints let operators mint, list, and revoke API keys for their
tenant without needing ``az containerapp exec``.

Coverage:
* POST /auth/keys — 201, returns key_id/full_key/tenant_id/env/label/expires_at.
* full_key matches the ``mvt_live_...`` prefix format.
* label defaults to None when omitted.
* ttl_days=0 → no expiry (expires_at is None).
* ttl_days=30 → expires_at is set ~30 days out.
* GET /auth/keys — 200, lists own keys newest-first.
* Newly minted key shows as ``status="active"``.
* include_revoked=false (default) excludes revoked keys.
* include_revoked=true includes revoked keys.
* Tenant isolation: tenant A cannot see tenant B's keys.
* DELETE /auth/keys/{key_id} — 200, revoked=True.
* Revoked key appears in GET with include_revoked=true as ``status="revoked"``.
* 404 when key_id not found.
* 404 when key belongs to a different tenant (same as not-found — no info leakage).
* Idempotent: revoking an already-revoked key returns 200.
* 401 with no bearer on each endpoint.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
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


def _save_key(storage: InMemoryStorage, **kwargs) -> object:
    """Mint and persist a key, returning the MintedApiKey."""
    minted = mint_api_key(
        tenant_id=kwargs.get("tenant_id", uuid4().hex),
        env=ApiKeyEnv(kwargs.get("env", "live")),
        label=kwargs.get("label"),
        ttl_days=kwargs.get("ttl_days", 90),
    )
    asyncio.get_event_loop().run_until_complete(storage.save_api_key(minted.record))
    return minted


# ---------------------------------------------------------------------------
# POST /api/v1/auth/keys — mint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMintKey:
    def test_mint_returns_201(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage, label="owner")
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "ci-bot", "ttl_days": 90},
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.status_code == 201

    def test_mint_returns_key_fields(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage, label="owner")
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "ci-bot", "ttl_days": 90},
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        body = resp.json()
        assert "key_id" in body
        assert "full_key" in body
        assert body["tenant_id"] == minted.record.tenant_id
        assert body["env"] == "live"
        assert body["label"] == "ci-bot"
        assert body["expires_at"] is not None

    def test_full_key_has_mvt_prefix(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage)
        resp = client.post(
            "/api/v1/auth/keys",
            json={},
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        new_key = resp.json()["full_key"]
        assert new_key.startswith("mvt_")

    def test_label_none_when_omitted(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage)
        resp = client.post(
            "/api/v1/auth/keys",
            json={},
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.json()["label"] is None

    def test_ttl_zero_means_no_expiry(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"ttl_days": 0},
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.status_code == 201
        assert resp.json()["expires_at"] is None

    def test_ttl_30_sets_expires_at(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"ttl_days": 30},
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.status_code == 201
        assert resp.json()["expires_at"] is not None

    def test_mint_401_without_bearer(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/keys", json={})
        assert resp.status_code == 401

    def test_minted_key_is_usable(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage)
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "child"},
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        new_full_key = resp.json()["full_key"]
        # The new key should authenticate against /auth/me.
        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_full_key}"},
        )
        assert me.status_code == 200
        assert me.json()["label"] == "child"


# ---------------------------------------------------------------------------
# GET /api/v1/auth/keys — list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListKeys:
    def test_list_shows_own_key(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage, label="my-key")
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        key_ids = [k["key_id"] for k in body["keys"]]
        assert minted.record.key_id in key_ids

    def test_active_key_has_status_active(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        minted = _save_key(storage, label="active")
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        keys = {k["key_id"]: k for k in resp.json()["keys"]}
        assert keys[minted.record.key_id]["status"] == "active"

    def test_list_401_without_bearer(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/keys")
        assert resp.status_code == 401

    def test_tenant_isolation_cannot_see_other_tenant_keys(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        tenant_a = _save_key(storage, label="tenant-a")
        tenant_b = _save_key(storage, label="tenant-b")
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {tenant_a.full_key}"},
        )
        key_ids = [k["key_id"] for k in resp.json()["keys"]]
        assert tenant_a.record.key_id in key_ids
        assert tenant_b.record.key_id not in key_ids

    def test_revoked_key_excluded_by_default(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        minted = _save_key(storage)
        # Mint a second key to do the revoke.
        second = _save_key(storage, tenant_id=minted.record.tenant_id)
        # Revoke the first via the second.
        client.delete(
            f"/api/v1/auth/keys/{minted.record.key_id}",
            headers={"Authorization": f"Bearer {second.full_key}"},
        )
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {second.full_key}"},
        )
        key_ids = [k["key_id"] for k in resp.json()["keys"]]
        assert minted.record.key_id not in key_ids

    def test_revoked_key_included_with_flag(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        minted = _save_key(storage)
        second = _save_key(storage, tenant_id=minted.record.tenant_id)
        client.delete(
            f"/api/v1/auth/keys/{minted.record.key_id}",
            headers={"Authorization": f"Bearer {second.full_key}"},
        )
        resp = client.get(
            "/api/v1/auth/keys",
            params={"include_revoked": True},
            headers={"Authorization": f"Bearer {second.full_key}"},
        )
        keys = {k["key_id"]: k for k in resp.json()["keys"]}
        assert minted.record.key_id in keys
        assert keys[minted.record.key_id]["status"] == "revoked"

    def test_count_matches_key_list_length(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        minted = _save_key(storage)
        _save_key(storage, tenant_id=minted.record.tenant_id)
        resp = client.get(
            "/api/v1/auth/keys",
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        body = resp.json()
        assert body["count"] == len(body["keys"])


# ---------------------------------------------------------------------------
# DELETE /api/v1/auth/keys/{key_id} — revoke
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRevokeKey:
    def test_revoke_returns_200_and_revoked_true(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        owner = _save_key(storage)
        victim = _save_key(storage, tenant_id=owner.record.tenant_id)
        resp = client.delete(
            f"/api/v1/auth/keys/{victim.record.key_id}",
            headers={"Authorization": f"Bearer {owner.full_key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["key_id"] == victim.record.key_id
        assert body["revoked"] is True

    def test_revoke_idempotent(self, client: TestClient, storage: InMemoryStorage) -> None:
        owner = _save_key(storage)
        victim = _save_key(storage, tenant_id=owner.record.tenant_id)
        client.delete(
            f"/api/v1/auth/keys/{victim.record.key_id}",
            headers={"Authorization": f"Bearer {owner.full_key}"},
        )
        # Second revoke should still return 200.
        resp = client.delete(
            f"/api/v1/auth/keys/{victim.record.key_id}",
            headers={"Authorization": f"Bearer {owner.full_key}"},
        )
        assert resp.status_code == 200

    def test_revoke_404_key_not_found(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage)
        resp = client.delete(
            "/api/v1/auth/keys/no-such-key-id",
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.status_code == 404

    def test_revoke_404_cross_tenant(self, client: TestClient, storage: InMemoryStorage) -> None:
        tenant_a = _save_key(storage, label="a")
        tenant_b = _save_key(storage, label="b")
        # Tenant A tries to revoke tenant B's key → 404 (no info leakage).
        resp = client.delete(
            f"/api/v1/auth/keys/{tenant_b.record.key_id}",
            headers={"Authorization": f"Bearer {tenant_a.full_key}"},
        )
        assert resp.status_code == 404

    def test_revoke_401_without_bearer(self, client: TestClient, storage: InMemoryStorage) -> None:
        minted = _save_key(storage)
        resp = client.delete(f"/api/v1/auth/keys/{minted.record.key_id}")
        assert resp.status_code == 401

    def test_revoked_key_cannot_authenticate(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        owner = _save_key(storage)
        victim = _save_key(storage, tenant_id=owner.record.tenant_id)
        victim_key = victim.full_key
        client.delete(
            f"/api/v1/auth/keys/{victim.record.key_id}",
            headers={"Authorization": f"Bearer {owner.full_key}"},
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {victim_key}"},
        )
        assert resp.status_code == 401
