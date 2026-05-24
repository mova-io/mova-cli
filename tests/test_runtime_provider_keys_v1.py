"""Per-tenant BYOK provider-key endpoints (ADR 018).

* PUT    /api/v1/provider-keys/{provider} — set/rotate (admin); value never returned
* GET    /api/v1/provider-keys — list configured providers + fingerprints (read)
* DELETE /api/v1/provider-keys/{provider} — delete (admin)

Mirrors tests/test_runtime_triggers_v1.py: a normal mvt_* key + AuthContext,
tenant-scoped. Asserts scope gating (admin to write/delete, read to list),
that the plaintext key is NEVER in any response, and tenant isolation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage

_FERNET_KEY = Fernet.generate_key()


@pytest.fixture(autouse=True)
def _provider_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # The PUT endpoint encrypts at the edge → needs the data key set.
    monkeypatch.setenv("MOVATE_PROVIDER_KEY_SECRET", _FERNET_KEY.decode())


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
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="byok-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


async def _ro_header(storage: InMemoryStorage) -> dict:
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Set / list / delete happy path — value never returned
# ---------------------------------------------------------------------------


def test_list_empty_before_any_set(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    r = client.get("/api/v1/provider-keys", headers=header)
    assert r.status_code == 200, r.text
    assert r.json() == {"provider_keys": [], "count": 0}


def test_set_then_list_value_never_returned(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    secret = "sk-super-secret-WXYZ"
    r = client.put("/api/v1/provider-keys/openai", json={"api_key": secret}, headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    # The response carries ONLY metadata + a masked fingerprint — never the key.
    assert body["provider"] == "openai"
    assert body["fingerprint"] == "…WXYZ"
    assert secret not in r.text
    assert "api_key" not in body and "ciphertext" not in body

    lst = client.get("/api/v1/provider-keys", headers=header)
    assert lst.json()["count"] == 1
    item = lst.json()["provider_keys"][0]
    assert item["provider"] == "openai"
    assert item["fingerprint"] == "…WXYZ"
    assert secret not in lst.text


def test_set_normalizes_provider_casing(client: TestClient, auth_setup) -> None:
    # The path param is a bare provider name (a slash can't ride in a path
    # segment); normalization still lowercases it defensively.
    header, _ = auth_setup
    r = client.put("/api/v1/provider-keys/OpenAI", json={"api_key": "sk-abcd"}, headers=header)
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "openai"


def test_set_rotates_in_place(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    client.put("/api/v1/provider-keys/openai", json={"api_key": "sk-1111"}, headers=header)
    client.put("/api/v1/provider-keys/openai", json={"api_key": "sk-2222"}, headers=header)
    lst = client.get("/api/v1/provider-keys", headers=header)
    assert lst.json()["count"] == 1
    assert lst.json()["provider_keys"][0]["fingerprint"] == "…2222"


def test_delete_then_gone(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    client.put("/api/v1/provider-keys/anthropic", json={"api_key": "sk-x"}, headers=header)
    r = client.delete("/api/v1/provider-keys/anthropic", headers=header)
    assert r.status_code == 204
    assert client.get("/api/v1/provider-keys", headers=header).json()["count"] == 0
    # Idempotent: deleting again is still 204.
    assert client.delete("/api/v1/provider-keys/anthropic", headers=header).status_code == 204


# ---------------------------------------------------------------------------
# Scope gating
# ---------------------------------------------------------------------------


async def test_set_requires_admin_scope(storage: InMemoryStorage, client: TestClient) -> None:
    ro = await _ro_header(storage)
    r = client.put("/api/v1/provider-keys/openai", json={"api_key": "sk-x"}, headers=ro)
    assert r.status_code == 403


async def test_delete_requires_admin_scope(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    admin_header, _ = auth_setup
    client.put("/api/v1/provider-keys/openai", json={"api_key": "sk-x"}, headers=admin_header)
    ro = await _ro_header(storage)
    assert client.delete("/api/v1/provider-keys/openai", headers=ro).status_code == 403


async def test_list_allowed_with_read_scope(storage: InMemoryStorage, client: TestClient) -> None:
    ro = await _ro_header(storage)
    r = client.get("/api/v1/provider-keys", headers=ro)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_list_is_tenant_scoped(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    header_a, _ = auth_setup
    client.put("/api/v1/provider-keys/openai", json={"api_key": "sk-a"}, headers=header_a)

    minted_b = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="tenant-b", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted_b.record)
    header_b = {"Authorization": f"Bearer {minted_b.full_key}"}

    # Tenant B sees none of tenant A's keys.
    assert client.get("/api/v1/provider-keys", headers=header_b).json()["count"] == 0
    # Tenant A still has its own.
    assert client.get("/api/v1/provider-keys", headers=header_a).json()["count"] == 1
