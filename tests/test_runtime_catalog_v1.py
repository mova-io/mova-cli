"""Catalog runtime endpoints — full read API + private submission lifecycle +
rating + sync stub (ADR 041).

Hermetic. Requires the runtime extras (fastapi) — skipped where only core is
installed.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import CatalogEntry, CatalogSource
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


@pytest.fixture
async def auth_setup(storage: InMemoryStorage) -> tuple[dict[str, str], str]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="catalog-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


def _make_public_entry(slug: str, **overrides) -> CatalogEntry:
    return CatalogEntry(
        slug=slug,
        source=CatalogSource.MOVATE,
        tenant_id=None,
        latest_version=overrides.pop("latest_version", "1.0.0"),
        name=overrides.pop("name", slug),
        title=overrides.pop("title", slug),
        description=overrides.pop("description", f"A {slug} entry."),
        tags=overrides.pop("tags", []),
        shape=overrides.pop("shape", "faq"),
    )


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_list_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/catalog/agents")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# List + filter
# ---------------------------------------------------------------------------


async def test_list_unions_public_and_private(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, tenant_id = auth_setup
    await storage.upsert_catalog_entry(_make_public_entry("public-1"))
    await storage.upsert_catalog_entry(_make_public_entry("public-2"))
    await storage.upsert_catalog_entry(
        CatalogEntry(
            slug="mine",
            source=CatalogSource.PRIVATE,
            tenant_id=tenant_id,
            latest_version="0.1.0",
            name="mine",
            title="Mine",
            description="own",
        )
    )
    await storage.upsert_catalog_entry(
        CatalogEntry(
            slug="theirs",
            source=CatalogSource.PRIVATE,
            tenant_id="other-tenant",
            latest_version="0.1.0",
            name="theirs",
            title="Theirs",
            description="other",
        )
    )

    r = client.get("/api/v1/catalog/agents", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    slugs = {e["slug"] for e in body["entries"]}
    assert slugs == {"public-1", "public-2", "mine"}
    assert "theirs" not in slugs


async def test_list_filter_by_tag_and_shape_and_q(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _tenant_id = auth_setup
    await storage.upsert_catalog_entry(_make_public_entry("rag-bot", tags=["rag"], shape="rag_qa"))
    await storage.upsert_catalog_entry(_make_public_entry("faq-bot", tags=["faq"], shape="faq"))

    r = client.get("/api/v1/catalog/agents", params={"tag": "rag"}, headers=header)
    assert {e["slug"] for e in r.json()["entries"]} == {"rag-bot"}

    r = client.get("/api/v1/catalog/agents", params={"shape": "faq"}, headers=header)
    assert {e["slug"] for e in r.json()["entries"]} == {"faq-bot"}

    r = client.get("/api/v1/catalog/agents", params={"q": "RAG"}, headers=header)
    assert {e["slug"] for e in r.json()["entries"]} == {"rag-bot"}


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


async def test_detail_returns_latest_version_digest(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _tenant_id = auth_setup
    await storage.upsert_catalog_entry(_make_public_entry("detail-bot"))
    payload = b"bundle-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    await storage.upsert_catalog_entry_version(
        "detail-bot",
        source=CatalogSource.MOVATE,
        version="1.0.0",
        bundle_tar=payload,
        digest=digest,
    )

    r = client.get("/api/v1/catalog/agents/detail-bot", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "detail-bot"
    assert body["latest_version"] == "1.0.0"
    assert body["latest_version_digest"] == digest


async def test_detail_404_on_unknown(client: TestClient, auth_setup) -> None:
    header, _tenant_id = auth_setup
    r = client.get("/api/v1/catalog/agents/nope", headers=header)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Version endpoints
# ---------------------------------------------------------------------------


async def test_get_version_returns_b64_bytes(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _tenant_id = auth_setup
    await storage.upsert_catalog_entry(_make_public_entry("v-bot"))
    payload = b"hello"
    digest = hashlib.sha256(payload).hexdigest()
    await storage.upsert_catalog_entry_version(
        "v-bot",
        source=CatalogSource.MOVATE,
        version="1.0.0",
        bundle_tar=payload,
        digest=digest,
    )

    r = client.get("/api/v1/catalog/agents/v-bot/versions/1.0.0", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert base64.b64decode(body["bundle_tar_b64"]) == payload
    assert body["digest"] == digest


async def test_list_versions_omits_b64(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _tenant_id = auth_setup
    await storage.upsert_catalog_entry(_make_public_entry("v-bot"))
    await storage.upsert_catalog_entry_version(
        "v-bot",
        source=CatalogSource.MOVATE,
        version="1.0.0",
        bundle_tar=b"bytes",
        digest="d",
    )

    r = client.get("/api/v1/catalog/agents/v-bot/versions", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["bundle_tar_b64"] is None


# ---------------------------------------------------------------------------
# Private submission lifecycle
# ---------------------------------------------------------------------------


async def test_submit_creates_private_entry(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, tenant_id = auth_setup
    bundle = b"private-bundle"
    r = client.post(
        "/api/v1/catalog/agents",
        json={
            "slug": "internal-helper",
            "name": "internal-helper",
            "title": "Internal Helper",
            "description": "An internal reusable agent.",
            "tags": ["internal"],
            "shape": "faq",
            "version": "0.1.0",
            "bundle_tar_b64": base64.b64encode(bundle).decode("ascii"),
        },
        headers=header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "private"
    assert body["tenant_id"] == tenant_id

    # Persisted in storage with the right namespace.
    stored = await storage.get_catalog_entry(
        "internal-helper", source=CatalogSource.PRIVATE, tenant_id=tenant_id
    )
    assert stored is not None
    version = await storage.get_catalog_entry_version(
        "internal-helper",
        source=CatalogSource.PRIVATE,
        version="0.1.0",
        tenant_id=tenant_id,
    )
    assert version is not None and version.bundle_tar == bundle


async def test_submit_duplicate_conflicts(client: TestClient, auth_setup) -> None:
    header, _tenant_id = auth_setup
    body = {
        "slug": "dup",
        "name": "dup",
        "title": "dup",
        "description": "x",
        "bundle_tar_b64": base64.b64encode(b"x").decode("ascii"),
    }
    r1 = client.post("/api/v1/catalog/agents", json=body, headers=header)
    assert r1.status_code == 201
    r2 = client.post("/api/v1/catalog/agents", json=body, headers=header)
    assert r2.status_code == 409, r2.text


async def test_publish_version_bumps_latest(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, tenant_id = auth_setup
    client.post(
        "/api/v1/catalog/agents",
        json={
            "slug": "lifecycle",
            "name": "lifecycle",
            "title": "lifecycle",
            "description": "x",
            "version": "0.1.0",
            "bundle_tar_b64": base64.b64encode(b"v1").decode("ascii"),
        },
        headers=header,
    )
    r = client.post(
        "/api/v1/catalog/agents/lifecycle/versions",
        json={
            "version": "0.2.0",
            "bundle_tar_b64": base64.b64encode(b"v2").decode("ascii"),
        },
        headers=header,
    )
    assert r.status_code == 201, r.text
    stored = await storage.get_catalog_entry(
        "lifecycle", source=CatalogSource.PRIVATE, tenant_id=tenant_id
    )
    assert stored is not None
    assert stored.latest_version == "0.2.0"


async def test_publish_version_404_on_unknown(client: TestClient, auth_setup) -> None:
    header, _tenant_id = auth_setup
    r = client.post(
        "/api/v1/catalog/agents/ghost/versions",
        json={
            "version": "1.0.0",
            "bundle_tar_b64": base64.b64encode(b"x").decode("ascii"),
        },
        headers=header,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------


async def test_rate_entry_updates_summary(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _tenant_id = auth_setup
    await storage.upsert_catalog_entry(_make_public_entry("ratable"))
    r = client.post(
        "/api/v1/catalog/agents/ratable/ratings",
        json={"rating": 4, "comment": "decent"},
        headers=header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["avg"] == 4.0


async def test_rate_community_returns_501(client: TestClient, auth_setup) -> None:
    header, _tenant_id = auth_setup
    r = client.post(
        "/api/v1/catalog/agents/x/ratings",
        json={"rating": 5, "source": "community"},
        headers=header,
    )
    assert r.status_code == 501


# ---------------------------------------------------------------------------
# Sync stub — 202 + advances watermark + fixed stub shape
# ---------------------------------------------------------------------------


async def test_sync_stub_returns_202_and_advances_watermark(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    header, _tenant_id = auth_setup
    assert await storage.get_catalog_sync_watermark(CatalogSource.MOVATE) is None

    r = client.post(
        "/api/v1/catalog/sync",
        json={"source": "movate"},
        headers=header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["source"] == "movate"
    assert body["status"] == "stub"
    assert "watermark" in body
    assert "Production sync" in body["detail"]

    # Watermark must have advanced.
    after = await storage.get_catalog_sync_watermark(CatalogSource.MOVATE)
    assert after is not None


async def test_sync_private_returns_400(client: TestClient, auth_setup) -> None:
    header, _tenant_id = auth_setup
    r = client.post("/api/v1/catalog/sync", json={"source": "private"}, headers=header)
    assert r.status_code == 400


async def test_sync_community_returns_501(client: TestClient, auth_setup) -> None:
    header, _tenant_id = auth_setup
    r = client.post("/api/v1/catalog/sync", json={"source": "community"}, headers=header)
    assert r.status_code == 501


# ---------------------------------------------------------------------------
# Sync stub contract test — the exact shape Mova iO will mock against.
# ---------------------------------------------------------------------------


async def test_sync_stub_response_shape_pinned(client: TestClient, auth_setup) -> None:
    """Pin the sync stub's response keys + types so a future production
    handler can be a drop-in replacement without breaking clients."""
    header, _tenant_id = auth_setup
    r = client.post("/api/v1/catalog/sync", json={"source": "movate"}, headers=header)
    assert r.status_code == 202
    body = r.json()
    assert set(body.keys()) == {"source", "status", "watermark", "detail"}
    assert body["status"] in {"stub", "synced"}
    # watermark parses as an ISO timestamp.
    datetime.fromisoformat(body["watermark"])
