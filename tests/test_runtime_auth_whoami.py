"""Tests for ``GET /api/v1/auth/me`` (item 29 — whoami endpoint).

Coverage:
* 200 with key identity when bearer is valid.
* Returns key_id, tenant_id, env fields.
* 401 with no bearer.
* 401 with a bogus token.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
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
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="whoami-test"
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def minted_record(storage: InMemoryStorage):
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="whoami-test-2"
    )
    await storage.save_api_key(minted.record)
    return minted


@pytest.mark.unit
class TestAuthWhoami:
    def test_whoami_returns_key_identity(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        import asyncio  # noqa: PLC0415

        minted = mint_api_key(
            tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="whoami-identity"
        )
        asyncio.get_event_loop().run_until_complete(storage.save_api_key(minted.record))
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key_id"] == minted.record.key_id
        assert data["tenant_id"] == minted.record.tenant_id
        assert data["env"] == "live"
        assert data["label"] == "whoami-identity"

    def test_whoami_401_no_bearer(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_whoami_401_bogus_token(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer mvt_live_bogus_token"},
        )
        assert resp.status_code == 401
