"""Tests for the read-only catalog endpoints (BACKLOG #67 / #68).

* GET /api/v1/pricing — the versioned pricing table.
* GET /api/v1/models — model catalog: pricing + capabilities.
* GET /api/v1/models/{model_id} — one model; 404 on unknown id.

These mirror the ``mdk pricing`` / ``mdk models`` CLI surfaces over HTTP.
The data is static (no storage / tenant scoping) but every endpoint is
auth-gated for consistency with the rest of ``/api/v1``.

Coverage:

* Happy path: pricing lists entries with a version; models lists every
  model with caps; single-model fetch returns the matching id.
* The API matches the shared catalogue the CLI uses (same model set).
* Unknown model id → 404.
* All three require auth (401 without a bearer token).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.providers.model_catalog import model_catalog
from movate.providers.pricing import load_pricing
from movate.runtime import build_app
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="catalog-v1-tests")
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


# ---------------------------------------------------------------------------
# GET /api/v1/pricing
# ---------------------------------------------------------------------------


def test_pricing_returns_entries(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/pricing", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    table = load_pricing()
    assert body["version"] == table.version
    assert body["last_verified"] == table.last_verified
    assert body["count"] == len(table.models)
    assert body["count"] == len(body["entries"])
    # Each entry carries the raw per-1K prices.
    row = body["entries"][0]
    for key in ("model_id", "input_per_1k", "output_per_1k"):
        assert key in row
    # Ids match the canonical table exactly.
    api_ids = {e["model_id"] for e in body["entries"]}
    assert api_ids == set(table.models)


def test_pricing_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/pricing").status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/models
# ---------------------------------------------------------------------------


def test_models_lists_with_caps(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/models", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    catalog = model_catalog()
    assert body["count"] == len(catalog)
    assert body["count"] == len(body["models"])
    # Same model set as the shared catalogue the CLI uses.
    api_ids = [m["model_id"] for m in body["models"]]
    assert api_ids == [info.model_id for info in catalog]
    # Each row carries pricing (per-1M) + capability fields.
    row = body["models"][0]
    for key in (
        "model_id",
        "provider",
        "context_window",
        "input_per_1m",
        "output_per_1m",
        "supports_tools",
        "supports_vision",
    ):
        assert key in row, f"missing key {key!r}"


def test_models_sorted_by_provider_then_id(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    body = client.get("/api/v1/models", headers=auth_header).json()
    ids = [m["model_id"] for m in body["models"]]
    sorted_ids = sorted(ids, key=lambda x: (x.split("/")[0] if "/" in x else x, x))
    assert ids == sorted_ids


def test_models_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/models").status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/models/{model_id}
# ---------------------------------------------------------------------------


def test_get_model_returns_one(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    model_id = "anthropic/claude-sonnet-4-6"
    r = client.get(f"/api/v1/models/{model_id}", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_id"] == model_id
    assert body["provider"] == "anthropic"
    assert body["in_pricing_table"] is True
    assert isinstance(body["input_per_1m"], float)
    assert isinstance(body["context_window"], int)
    assert isinstance(body["supports_tools"], bool)
    assert isinstance(body["supports_vision"], bool)


def test_get_model_matches_catalog_entry(client: TestClient, auth_setup) -> None:
    """Every model the catalog lists is individually fetchable + identical."""
    auth_header, _ = auth_setup
    listed = client.get("/api/v1/models", headers=auth_header).json()["models"]
    for entry in listed:
        one = client.get(f"/api/v1/models/{entry['model_id']}", headers=auth_header)
        assert one.status_code == 200, one.text
        assert one.json() == entry


def test_get_model_unknown_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/models/openai/does-not-exist-9999", headers=auth_header)
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"]["code"] == "not_found"


def test_get_model_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/models/anthropic/claude-sonnet-4-6").status_code == 401
