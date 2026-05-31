"""Tests for ``/api/v1/skills*`` + ``/api/v1/contexts*`` — managed-resource
CRUD + versions + agent-attach (ADR 060 D2).

Hermetic: constructs the FastAPI app in-process over :class:`InMemoryStorage`,
mints real API keys (explicit ``scopes=...``) so the actual auth + scope gate
flows through, and exercises the surface the front end / hosted operator
depends on. Mirrors ``tests/test_runtime_projects_v1.py``.

Coverage:

* Skills: list/get/versions/update(PUT)/delete + attach-to-agent; the
  pre-existing ``POST /skills`` create now dual-writes the registry row.
* Contexts: full CRUD (create/get/list/update/delete) + versions + attach.
* Scope gate: ``read`` on GET, ``admin`` on mutate (403 for a read-only key).
* Tenant isolation: tenant B can't see/mutate tenant A's skill/context (404,
  never the record).
* Immutability: re-publishing an existing ``(name, version)`` is 409.
* Validation: a PUT skill with a bad/missing ``skill.yaml`` is 422.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def admin_tenant(storage: InMemoryStorage) -> tuple[str, dict[str, str]]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="skills-v1-admin",
        scopes=["admin", "read"],
    )
    await storage.save_api_key(minted.record)
    return tenant_id, {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def reader_in_admin_tenant(
    storage: InMemoryStorage,
    admin_tenant: tuple[str, dict[str, str]],
) -> dict[str, str]:
    tenant_id, _ = admin_tenant
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="skills-v1-reader",
        scopes=["read"],
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def other_tenant(storage: InMemoryStorage) -> tuple[str, dict[str, str]]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="skills-v1-other",
        scopes=["admin", "read"],
    )
    await storage.save_api_key(minted.record)
    return tenant_id, {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_yaml(name: str = "web-search", version: str = "1.0.0") -> str:
    return (
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        f"version: {version}\n"
        "description: Search the web.\n"
        "schema:\n"
        "  input:\n"
        "    query: string\n"
        "  output:\n"
        "    result: string\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: myproject.skills.search:run\n"
    )


def _skill_files(name: str = "web-search", version: str = "1.0.0") -> dict[str, str]:
    return {"skill.yaml": _skill_yaml(name, version), "README.md": f"# {name}\n"}


def _seed_agent(storage: InMemoryStorage, tenant_id: str, name: str = "faq-bot") -> None:
    """Append an agent bundle directly into the in-memory store.

    ``InMemoryStorage`` is a plain list under the hood, so we bypass the
    async ``save_agent_bundle`` wrapper to seed from sync test bodies
    (the TestClient drives the app on its own thread/loop).
    """
    storage.agent_bundles.append(
        AgentBundleRecord(
            name=name,
            tenant_id=tenant_id,
            version="v1",
            created_by="seed",
            content_hash="seed-hash",
            files={"agent.yaml": f"name: {name}\nversion: v1\n"},
            created_at=datetime.now(UTC),
        )
    )


# ===========================================================================
# Skills
# ===========================================================================


def test_skill_create_dual_writes_and_get(
    storage: InMemoryStorage,
    admin_tenant: tuple[str, dict[str, str]],
    tmp_path,
) -> None:
    """The pre-existing ``POST /skills`` (disk persist) now ALSO dual-writes
    the managed registry row so the new GET/list surface sees it."""
    _tenant, headers = admin_tenant
    # ``POST /skills`` persists to disk → needs a skills_path on the app.
    client = TestClient(build_app(storage, skills_path=tmp_path / "skills"))
    resp = client.post(
        "/api/v1/skills",
        headers=headers,
        files={"skill_yaml": ("skill.yaml", _skill_yaml(), "application/x-yaml")},
    )
    assert resp.status_code == 201, resp.text

    # The managed registry now sees it via the new GET.
    got = client.get("/api/v1/skills/web-search", headers=headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["name"] == "web-search"
    assert body["version"] == "1.0.0"
    assert "skill.yaml" in body["files"]

    listed = client.get("/api/v1/skills", headers=headers)
    assert listed.status_code == 200
    assert {s["name"] for s in listed.json()["skills"]} == {"web-search"}


def test_skill_put_new_version_and_versions(
    client: TestClient, admin_tenant: tuple[str, dict[str, str]]
) -> None:
    _tenant, headers = admin_tenant
    r1 = client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "1.0.0", "files": _skill_files(version="1.0.0")},
    )
    assert r1.status_code == 201, r1.text
    r2 = client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "2.0.0", "files": _skill_files(version="2.0.0")},
    )
    assert r2.status_code == 201, r2.text

    # Latest is v2; explicit pin works.
    latest = client.get("/api/v1/skills/web-search", headers=headers).json()
    assert latest["version"] == "2.0.0"
    pinned = client.get("/api/v1/skills/web-search?version=1.0.0", headers=headers).json()
    assert pinned["version"] == "1.0.0"

    versions = client.get("/api/v1/skills/web-search/versions", headers=headers).json()
    assert [v["version"] for v in versions["versions"]] == ["2.0.0", "1.0.0"]
    assert versions["count"] == 2


def test_skill_put_duplicate_version_conflicts(
    client: TestClient, admin_tenant: tuple[str, dict[str, str]]
) -> None:
    _tenant, headers = admin_tenant
    client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "1.0.0", "files": _skill_files()},
    )
    dup = client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "1.0.0", "files": _skill_files()},
    )
    assert dup.status_code == 409, dup.text


def test_skill_put_bad_yaml_is_422(
    client: TestClient, admin_tenant: tuple[str, dict[str, str]]
) -> None:
    _tenant, headers = admin_tenant
    # Missing skill.yaml entirely.
    r = client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "1.0.0", "files": {"README.md": "# x"}},
    )
    assert r.status_code == 422, r.text
    # skill.yaml present but invalid (name mismatch).
    r2 = client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "1.0.0", "files": _skill_files(name="other-name")},
    )
    assert r2.status_code == 422, r2.text


def test_skill_delete(client: TestClient, admin_tenant: tuple[str, dict[str, str]]) -> None:
    _tenant, headers = admin_tenant
    client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "1.0.0", "files": _skill_files()},
    )
    d = client.delete("/api/v1/skills/web-search", headers=headers)
    assert d.status_code == 200, d.text
    assert client.get("/api/v1/skills/web-search", headers=headers).status_code == 404


def test_skill_read_scope_can_read_not_mutate(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
    reader_in_admin_tenant: dict[str, str],
) -> None:
    _tenant, admin_headers = admin_tenant
    client.put(
        "/api/v1/skills/web-search",
        headers=admin_headers,
        json={"version": "1.0.0", "files": _skill_files()},
    )
    # Read works.
    assert client.get("/api/v1/skills", headers=reader_in_admin_tenant).status_code == 200
    # Mutate is forbidden.
    put = client.put(
        "/api/v1/skills/web-search",
        headers=reader_in_admin_tenant,
        json={"version": "2.0.0", "files": _skill_files(version="2.0.0")},
    )
    assert put.status_code == 403
    dele = client.delete("/api/v1/skills/web-search", headers=reader_in_admin_tenant)
    assert dele.status_code == 403


def test_skill_tenant_isolation(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
    other_tenant: tuple[str, dict[str, str]],
) -> None:
    _ta, headers_a = admin_tenant
    _tb, headers_b = other_tenant
    client.put(
        "/api/v1/skills/web-search",
        headers=headers_a,
        json={"version": "1.0.0", "files": _skill_files()},
    )
    # Tenant B can't see it.
    assert client.get("/api/v1/skills/web-search", headers=headers_b).status_code == 404
    assert client.get("/api/v1/skills", headers=headers_b).json()["count"] == 0
    # Tenant B's delete removes nothing (404).
    assert client.delete("/api/v1/skills/web-search", headers=headers_b).status_code == 404
    # Tenant A's record is untouched.
    assert client.get("/api/v1/skills/web-search", headers=headers_a).status_code == 200


def test_skill_attach_to_agent(
    client: TestClient, storage: InMemoryStorage, admin_tenant: tuple[str, dict[str, str]]
) -> None:
    tenant, headers = admin_tenant
    client.put(
        "/api/v1/skills/web-search",
        headers=headers,
        json={"version": "1.0.0", "files": _skill_files()},
    )
    _seed_agent(storage, tenant)

    r = client.post("/api/v1/agents/faq-bot/skills", headers=headers, json={"ref": "web-search"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attached"] is True
    assert body["kind"] == "skill"
    assert body["ref"] == "web-search"

    # The wiring landed in a new agent bundle version whose agent.yaml lists
    # the skill (a new content-addressed row in the registry).
    versions = {b.version for b in storage.agent_bundles if b.name == "faq-bot"}
    assert "v1+skill-web-search" in versions
    new_bundle = next(b for b in storage.agent_bundles if b.version == "v1+skill-web-search")
    assert "web-search" in new_bundle.files["agent.yaml"]

    # Idempotent re-attach.
    r2 = client.post("/api/v1/agents/faq-bot/skills", headers=headers, json={"ref": "web-search"})
    assert r2.status_code == 200
    assert r2.json()["attached"] is False


def test_skill_attach_unknown_agent_or_ref_404(
    client: TestClient, storage: InMemoryStorage, admin_tenant: tuple[str, dict[str, str]]
) -> None:
    tenant, headers = admin_tenant
    r = client.post("/api/v1/agents/ghost/skills", headers=headers, json={"ref": "web-search"})
    assert r.status_code == 404
    _seed_agent(storage, tenant)
    r2 = client.post("/api/v1/agents/faq-bot/skills", headers=headers, json={"ref": "nope"})
    assert r2.status_code == 404


# ===========================================================================
# Contexts
# ===========================================================================


def test_context_full_crud(client: TestClient, admin_tenant: tuple[str, dict[str, str]]) -> None:
    _tenant, headers = admin_tenant
    created = client.post(
        "/api/v1/contexts",
        headers=headers,
        json={"name": "company-tone", "body": "# Tone\nBe concise.", "description": "voice"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["version"] == "v1"

    got = client.get("/api/v1/contexts/company-tone", headers=headers)
    assert got.status_code == 200
    assert got.json()["body"] == "# Tone\nBe concise."

    listed = client.get("/api/v1/contexts", headers=headers)
    assert {c["name"] for c in listed.json()["contexts"]} == {"company-tone"}

    # New version via PUT.
    put = client.put(
        "/api/v1/contexts/company-tone",
        headers=headers,
        json={"version": "v2", "body": "# Tone v2"},
    )
    assert put.status_code == 201, put.text
    assert client.get("/api/v1/contexts/company-tone", headers=headers).json()["version"] == "v2"

    versions = client.get("/api/v1/contexts/company-tone/versions", headers=headers).json()
    assert [v["version"] for v in versions["versions"]] == ["v2", "v1"]

    d = client.delete("/api/v1/contexts/company-tone", headers=headers)
    assert d.status_code == 200
    assert client.get("/api/v1/contexts/company-tone", headers=headers).status_code == 404


def test_context_create_duplicate_conflicts(
    client: TestClient, admin_tenant: tuple[str, dict[str, str]]
) -> None:
    _tenant, headers = admin_tenant
    client.post("/api/v1/contexts", headers=headers, json={"name": "tone", "body": "x"})
    dup = client.post("/api/v1/contexts", headers=headers, json={"name": "tone", "body": "x"})
    assert dup.status_code == 409, dup.text


def test_context_read_scope_can_read_not_mutate(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
    reader_in_admin_tenant: dict[str, str],
) -> None:
    _tenant, admin_headers = admin_tenant
    client.post("/api/v1/contexts", headers=admin_headers, json={"name": "tone", "body": "x"})
    assert client.get("/api/v1/contexts", headers=reader_in_admin_tenant).status_code == 200
    post = client.post(
        "/api/v1/contexts", headers=reader_in_admin_tenant, json={"name": "other", "body": "y"}
    )
    assert post.status_code == 403


def test_context_tenant_isolation(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
    other_tenant: tuple[str, dict[str, str]],
) -> None:
    _ta, headers_a = admin_tenant
    _tb, headers_b = other_tenant
    client.post("/api/v1/contexts", headers=headers_a, json={"name": "tone", "body": "x"})
    assert client.get("/api/v1/contexts/tone", headers=headers_b).status_code == 404
    assert client.get("/api/v1/contexts", headers=headers_b).json()["count"] == 0
    assert client.delete("/api/v1/contexts/tone", headers=headers_b).status_code == 404
    assert client.get("/api/v1/contexts/tone", headers=headers_a).status_code == 200


def test_context_attach_to_agent(
    client: TestClient, storage: InMemoryStorage, admin_tenant: tuple[str, dict[str, str]]
) -> None:
    tenant, headers = admin_tenant
    client.post("/api/v1/contexts", headers=headers, json={"name": "policy", "body": "# Policy"})
    _seed_agent(storage, tenant)
    r = client.post("/api/v1/agents/faq-bot/contexts", headers=headers, json={"ref": "policy"})
    assert r.status_code == 200, r.text
    assert r.json()["attached"] is True
    assert r.json()["kind"] == "context"
