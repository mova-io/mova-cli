"""HTTP runtime — agent version history, optimistic concurrency, revert.

ADR 014 D3 (versioning UX). Covers the three new/changed surfaces:

* ``GET /api/v1/agents/{name}/versions`` — durable-registry version
  history, newest-first, with ``created_by`` audit; ``read`` scope;
  tenant-scoped; the newest row flagged ``is_current``.
* ``PUT /api/v1/agents/{name}`` optimistic concurrency — an ``If-Match``
  header carrying the expected current version (or content_hash):
  stale → 409, current → succeeds (+ a new version row), absent →
  succeeds (last-write-wins back-compat, unchanged).
* ``POST /api/v1/agents/{name}/revert`` — re-publishes a prior version
  forward as the new latest (non-destructive — old versions survive);
  ``admin`` scope; unknown ``to_version`` → 404; tenant-scoped.

The versions/revert paths read + write the durable registry directly,
so most tests seed it via ``storage.save_agent_bundle`` rather than the
multipart publish path. The concurrency test exercises the real PUT
(which dual-writes), so it posts an agent first.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import AgentBundleRecord
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
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


async def _mint(storage: InMemoryStorage, *, scopes: list[str]) -> tuple[str, str]:
    """Mint a key with the given scopes; return (tenant_id, bearer header value)."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="versioning-tests", scopes=scopes
    )
    await storage.save_api_key(minted.record)
    return tenant_id, f"Bearer {minted.full_key}"


def _seed(
    storage: InMemoryStorage,
    *,
    name: str,
    tenant_id: str,
    version: str,
    created_by: str | None,
    created_at: datetime,
    body: str = "x",
) -> AgentBundleRecord:
    """Append one immutable registry row (a published version)."""
    files = {"agent.yaml": f"name: {name}\nversion: {version}\n", "prompt.md": body}
    rec = AgentBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by=created_by,
        content_hash=f"hash-{version}-{body}",
        files=files,
        created_at=created_at,
    )
    storage.agent_bundles.append(rec)
    return rec


# ---------------------------------------------------------------------------
# Multipart publish helpers (for the real PUT concurrency path)
# ---------------------------------------------------------------------------

_BASE = "demo"


def _agent_yaml(version: str) -> bytes:
    return (
        "api_version: movate/v1\n"
        "kind: Agent\n"
        f"name: {_BASE}\n"
        f"version: {version}\n"
        "description: versioning test agent\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n"
    ).encode()


def _zip_bundle(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("agent.yaml", _agent_yaml(version))
        zf.writestr("prompt.md", b"Hello {{ input.text }}\n")
        zf.writestr(
            "schema/input.json",
            json.dumps({"type": "object", "properties": {"text": {"type": "string"}}}).encode(),
        )
        zf.writestr(
            "schema/output.json",
            json.dumps({"type": "object", "properties": {"out": {"type": "string"}}}).encode(),
        )
    return buf.getvalue()


def _post(client: TestClient, bearer: str, version: str) -> object:
    return client.post(
        "/api/v1/agents",
        files={"bundle": (f"{_BASE}.zip", _zip_bundle(version), "application/zip")},
        headers={"Authorization": bearer},
    )


def _put(
    client: TestClient,
    bearer: str,
    version: str,
    *,
    if_match: str | None = None,
) -> object:
    headers = {"Authorization": bearer}
    if if_match is not None:
        headers["If-Match"] = if_match
    return client.put(
        f"/api/v1/agents/{_BASE}",
        files={"bundle": (f"{_BASE}.zip", _zip_bundle(version), "application/zip")},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# GET /agents/{name}/versions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_versions_lists_newest_first_with_audit(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["read"])
    now = datetime.now(UTC)
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.1.0",
        created_by="alice",
        created_at=now - timedelta(hours=2),
    )
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.2.0",
        created_by="bob",
        created_at=now - timedelta(hours=1),
    )

    r = client.get("/api/v1/agents/faq/versions", headers={"Authorization": bearer})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "faq"
    assert body["count"] == 2
    versions = body["versions"]
    # Newest-first.
    assert [v["version"] for v in versions] == ["0.2.0", "0.1.0"]
    # created_by audit surfaced.
    assert versions[0]["created_by"] == "bob"
    assert versions[1]["created_by"] == "alice"
    # Exactly the newest is current.
    assert versions[0]["is_current"] is True
    assert versions[1]["is_current"] is False
    assert "content_hash" in versions[0]


@pytest.mark.asyncio
async def test_versions_requires_read_scope(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["run"])  # no "read"
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.1.0",
        created_by="alice",
        created_at=datetime.now(UTC),
    )
    r = client.get("/api/v1/agents/faq/versions", headers={"Authorization": bearer})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_versions_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/agents/faq/versions")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_versions_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    owner, _ = await _mint(storage, scopes=["read"])
    _other, other_bearer = await _mint(storage, scopes=["read"])
    _seed(
        storage,
        name="faq",
        tenant_id=owner,
        version="0.1.0",
        created_by="alice",
        created_at=datetime.now(UTC),
    )
    # A different tenant sees an empty history, not the owner's versions.
    r = client.get("/api/v1/agents/faq/versions", headers={"Authorization": other_bearer})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0


# ---------------------------------------------------------------------------
# PUT optimistic concurrency (If-Match)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_no_if_match_succeeds_back_compat(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """Absent If-Match → today's last-write-wins behavior, unchanged."""
    _tenant, bearer = await _mint(storage, scopes=["admin"])
    assert _post(client, bearer, "0.1.0").status_code == 201
    r = _put(client, bearer, "0.2.0")  # no If-Match
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "0.2.0"
    assert r.json()["previous_version"] == "0.1.0"


@pytest.mark.asyncio
async def test_put_current_if_match_succeeds_and_creates_version(
    client: TestClient, storage: InMemoryStorage
) -> None:
    _tenant, bearer = await _mint(storage, scopes=["admin", "read"])
    assert _post(client, bearer, "0.1.0").status_code == 201

    # If-Match the current version → write goes through.
    r = _put(client, bearer, "0.2.0", if_match="0.1.0")
    assert r.status_code == 200, r.text

    # A new registry version row now exists (history has both).
    hist = client.get("/api/v1/agents/demo/versions", headers={"Authorization": bearer})
    versions = [v["version"] for v in hist.json()["versions"]]
    assert "0.2.0" in versions
    assert "0.1.0" in versions


@pytest.mark.asyncio
async def test_put_current_if_match_by_content_hash_succeeds(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """The content_hash is an accepted If-Match precondition value too."""
    tenant_id, bearer = await _mint(storage, scopes=["admin", "read"])
    assert _post(client, bearer, "0.1.0").status_code == 201
    current = await storage.get_agent_bundle("demo", tenant_id=tenant_id)
    assert current is not None
    r = _put(client, bearer, "0.2.0", if_match=current.content_hash)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_put_stale_if_match_conflict_409(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["admin"])
    assert _post(client, bearer, "0.1.0").status_code == 201

    # Simulate a teammate publishing 0.2.0 in between (now latest is 0.2.0).
    _seed(
        storage,
        name="demo",
        tenant_id=tenant_id,
        version="0.2.0",
        created_by="teammate",
        created_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    # Our PUT believes 0.1.0 is current → stale → 409.
    r = _put(client, bearer, "0.3.0", if_match="0.1.0")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"]["code"] == "conflict"


@pytest.mark.asyncio
async def test_put_if_match_tolerates_etag_quoting(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """Quoted + weak-validator ETag forms normalize to the bare version."""
    _tenant, bearer = await _mint(storage, scopes=["admin"])
    assert _post(client, bearer, "0.1.0").status_code == 201
    r = _put(client, bearer, "0.2.0", if_match='W/"0.1.0"')
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# POST /agents/{name}/revert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_republishes_target_as_new_latest(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["admin", "read"])
    now = datetime.now(UTC)
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.1.0",
        created_by="alice",
        created_at=now - timedelta(hours=2),
        body="v1-body",
    )
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.2.0",
        created_by="bob",
        created_at=now - timedelta(hours=1),
        body="v2-body",
    )

    r = client.post(
        "/api/v1/agents/faq/revert",
        json={"to_version": "0.1.0"},
        headers={"Authorization": bearer},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reverted_from"] == "0.1.0"
    assert body["previous_version"] == "0.2.0"

    # The new latest resolves to the 0.1.0 bundle (same content_hash).
    latest = await storage.get_agent_bundle("faq", tenant_id=tenant_id)
    assert latest is not None
    assert latest.content_hash == "hash-0.1.0-v1-body"
    assert latest.version == body["version"]
    # And the reverting identity is recorded as the publisher.
    assert latest.created_by is not None


@pytest.mark.asyncio
async def test_revert_is_non_destructive(client: TestClient, storage: InMemoryStorage) -> None:
    """Reverting NEVER deletes/rewrites prior versions — history grows."""
    tenant_id, bearer = await _mint(storage, scopes=["admin", "read"])
    now = datetime.now(UTC)
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.1.0",
        created_by="alice",
        created_at=now - timedelta(hours=2),
    )
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.2.0",
        created_by="bob",
        created_at=now - timedelta(hours=1),
    )

    before = await storage.list_agent_versions("faq", tenant_id=tenant_id, limit=100)
    assert {r.version for r in before} == {"0.1.0", "0.2.0"}

    r = client.post(
        "/api/v1/agents/faq/revert",
        json={"to_version": "0.1.0"},
        headers={"Authorization": bearer},
    )
    assert r.status_code == 200, r.text

    after = await storage.list_agent_versions("faq", tenant_id=tenant_id, limit=100)
    # Both originals still present, PLUS the new revert row.
    versions_after = {r.version for r in after}
    assert "0.1.0" in versions_after
    assert "0.2.0" in versions_after
    assert len(after) == 3  # nothing destroyed; one appended


@pytest.mark.asyncio
async def test_revert_accepts_query_param(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["admin"])
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.1.0",
        created_by="alice",
        created_at=datetime.now(UTC),
    )
    r = client.post(
        "/api/v1/agents/faq/revert?to_version=0.1.0",
        headers={"Authorization": bearer},
    )
    assert r.status_code == 200, r.text
    assert r.json()["reverted_from"] == "0.1.0"


@pytest.mark.asyncio
async def test_revert_unknown_version_404(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["admin"])
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.1.0",
        created_by="alice",
        created_at=datetime.now(UTC),
    )
    r = client.post(
        "/api/v1/agents/faq/revert",
        json={"to_version": "9.9.9"},
        headers={"Authorization": bearer},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_revert_missing_version_400(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, bearer = await _mint(storage, scopes=["admin"])
    r = client.post("/api/v1/agents/faq/revert", json={}, headers={"Authorization": bearer})
    # Empty body has no to_version → 400 from the handler (or 422 from
    # pydantic if the model requires it). Accept either as "rejected".
    assert r.status_code in (400, 422), r.text


@pytest.mark.asyncio
async def test_revert_requires_admin_scope(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["read"])  # no admin
    _seed(
        storage,
        name="faq",
        tenant_id=tenant_id,
        version="0.1.0",
        created_by="alice",
        created_at=datetime.now(UTC),
    )
    r = client.post(
        "/api/v1/agents/faq/revert",
        json={"to_version": "0.1.0"},
        headers={"Authorization": bearer},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_revert_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    owner, _ = await _mint(storage, scopes=["admin"])
    _other, other_bearer = await _mint(storage, scopes=["admin"])
    _seed(
        storage,
        name="faq",
        tenant_id=owner,
        version="0.1.0",
        created_by="alice",
        created_at=datetime.now(UTC),
    )
    # Another tenant can't revert the owner's agent — the version is
    # indistinguishable from missing → 404.
    r = client.post(
        "/api/v1/agents/faq/revert",
        json={"to_version": "0.1.0"},
        headers={"Authorization": other_bearer},
    )
    assert r.status_code == 404, r.text
