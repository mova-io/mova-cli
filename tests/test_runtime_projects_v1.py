"""Tests for ``/api/v1/projects*`` — Project CRUD + membership endpoints.

ADR 040 step 2 (API layer, on top of the storage step). Hermetic:
constructs the FastAPI app in-process over :class:`InMemoryStorage`, mints
real API keys (with explicit ``scopes=...``) so the actual auth + scope
gate flows through, and exercises every state transition the front end's
project surface depends on.

Coverage:

* Full CRUD lifecycle on a project (create → get → list → update → archive).
* Member CRUD with role transitions (viewer → editor → owner → remove).
* "Cannot remove last owner" rejection (422) on demote AND on remove.
* "Cannot delete default project" rejection (422).
* Tenant isolation: a project from tenant A is invisible to tenant B's
  bearer (404, never the actual record).
* Reserved-name rejection (``name == "default"``) on create.
* ``admin`` scope vs ``owner`` project role compose: either grants the
  write gate (PUT/DELETE/member-mutations).
* ``ETag`` round-trip + ``If-Match`` optimistic concurrency (412 on stale).
"""

from __future__ import annotations

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


@pytest.fixture
async def admin_tenant(storage: InMemoryStorage) -> tuple[str, dict[str, str]]:
    """An ``admin``-scoped key in a fresh tenant — the standard "tenant
    admin doing project CRUD" identity used by most cases below."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="projects-v1-admin",
        scopes=["admin", "read"],
    )
    await storage.save_api_key(minted.record)
    return tenant_id, {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def reader_in_admin_tenant(
    storage: InMemoryStorage,
    admin_tenant: tuple[str, dict[str, str]],
) -> dict[str, str]:
    """A ``read``-only key in the SAME tenant as ``admin_tenant`` — used
    to assert "reads work without admin scope; writes don't"."""
    tenant_id, _ = admin_tenant
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="projects-v1-reader",
        scopes=["read"],
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def other_tenant(storage: InMemoryStorage) -> tuple[str, dict[str, str]]:
    """A SECOND tenant's admin key — for tenant-isolation assertions."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="projects-v1-other-tenant",
        scopes=["admin", "read"],
    )
    await storage.save_api_key(minted.record)
    return tenant_id, {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Full CRUD lifecycle
# ---------------------------------------------------------------------------


def test_project_crud_lifecycle(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant

    # Create.
    r = client.post(
        "/api/v1/projects",
        json={"name": "alpha", "description": "team alpha"},
        headers=hdr,
    )
    assert r.status_code == 201, r.text
    created = r.json()
    pid = created["project_id"]
    assert created["name"] == "alpha"
    assert created["description"] == "team alpha"
    assert created["archived_at"] is None
    assert created["etag"]

    # Get.
    r = client.get(f"/api/v1/projects/{pid}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["project_id"] == pid

    # List.
    r = client.get("/api/v1/projects", headers=hdr)
    assert r.status_code == 200
    listing = r.json()
    assert listing["count"] >= 1
    assert any(p["project_id"] == pid for p in listing["projects"])

    # Update (no If-Match — last-write-wins).
    r = client.put(
        f"/api/v1/projects/{pid}",
        json={"name": "alpha-renamed", "description": "renamed"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["name"] == "alpha-renamed"
    assert updated["description"] == "renamed"

    # Archive — DELETE returns the soft-deleted record.
    r = client.delete(f"/api/v1/projects/{pid}", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["archived_at"] is not None

    # Default listing now hides it.
    r = client.get("/api/v1/projects", headers=hdr)
    assert all(p["project_id"] != pid for p in r.json()["projects"])

    # include_archived=true reveals it.
    r = client.get("/api/v1/projects?include_archived=true", headers=hdr)
    assert any(p["project_id"] == pid for p in r.json()["projects"])


def test_create_rejects_reserved_default_name(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant
    r = client.post("/api/v1/projects", json={"name": "default"}, headers=hdr)
    assert r.status_code == 422
    assert "reserved" in r.json()["detail"]["error"]["message"].lower()


def test_create_duplicate_name_returns_409(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant
    r = client.post("/api/v1/projects", json={"name": "dupe"}, headers=hdr)
    assert r.status_code == 201
    r = client.post("/api/v1/projects", json={"name": "dupe"}, headers=hdr)
    assert r.status_code == 409


def test_get_unknown_returns_404(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant
    r = client.get("/api/v1/projects/prj_doesnotexist", headers=hdr)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def test_member_crud_and_role_transitions(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant
    r = client.post("/api/v1/projects", json={"name": "team-a"}, headers=hdr)
    pid = r.json()["project_id"]

    # The creator was auto-added as an ``owner`` member (D4) — so the
    # "at least one owner" invariant is satisfied without an explicit
    # POST. Verify it before we layer more roles on top.
    r = client.get(f"/api/v1/projects/{pid}/members", headers=hdr)
    assert r.status_code == 200
    members = r.json()["members"]
    owner_count = sum(1 for m in members if m["role"] == "owner")
    assert owner_count >= 1

    # Add bob as viewer.
    r = client.post(
        f"/api/v1/projects/{pid}/members",
        json={"principal_id": "api_key:bob", "role": "viewer"},
        headers=hdr,
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "viewer"

    # GET single member.
    r = client.get(f"/api/v1/projects/{pid}/members/api_key:bob", headers=hdr)
    assert r.status_code == 200
    assert r.json()["principal_id"] == "api_key:bob"

    # Viewer → editor.
    r = client.patch(
        f"/api/v1/projects/{pid}/members/api_key:bob",
        json={"role": "editor"},
        headers=hdr,
    )
    assert r.status_code == 200
    assert r.json()["role"] == "editor"

    # Editor → owner.
    r = client.patch(
        f"/api/v1/projects/{pid}/members/api_key:bob",
        json={"role": "owner"},
        headers=hdr,
    )
    assert r.status_code == 200
    assert r.json()["role"] == "owner"

    # Remove.
    r = client.delete(f"/api/v1/projects/{pid}/members/api_key:bob", headers=hdr)
    assert r.status_code == 204
    # Idempotent re-remove.
    r = client.delete(f"/api/v1/projects/{pid}/members/api_key:bob", headers=hdr)
    assert r.status_code == 204


def test_add_duplicate_member_returns_409(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant
    pid = client.post("/api/v1/projects", json={"name": "dupes"}, headers=hdr).json()["project_id"]
    client.post(
        f"/api/v1/projects/{pid}/members",
        json={"principal_id": "api_key:eve", "role": "viewer"},
        headers=hdr,
    )
    r = client.post(
        f"/api/v1/projects/{pid}/members",
        json={"principal_id": "api_key:eve", "role": "editor"},
        headers=hdr,
    )
    assert r.status_code == 409


def test_cannot_demote_last_owner_returns_422(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant
    pid = client.post("/api/v1/projects", json={"name": "solo"}, headers=hdr).json()["project_id"]
    # The creator IS the sole owner. PATCH-demote that principal must
    # 422 — there'd be zero owners afterwards.
    members = client.get(f"/api/v1/projects/{pid}/members", headers=hdr).json()["members"]
    sole_owner = next(m["principal_id"] for m in members if m["role"] == "owner")
    r = client.patch(
        f"/api/v1/projects/{pid}/members/{sole_owner}",
        json={"role": "editor"},
        headers=hdr,
    )
    assert r.status_code == 422
    assert "owner" in r.json()["detail"]["error"]["message"].lower()


def test_cannot_remove_last_owner_returns_422(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr = admin_tenant
    create = client.post("/api/v1/projects", json={"name": "solo-rm"}, headers=hdr).json()
    pid = create["project_id"]
    members = client.get(f"/api/v1/projects/{pid}/members", headers=hdr).json()["members"]
    sole_owner = next(m["principal_id"] for m in members if m["role"] == "owner")
    r = client.delete(f"/api/v1/projects/{pid}/members/{sole_owner}", headers=hdr)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Default project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_archive_default_project(
    client: TestClient,
    storage: InMemoryStorage,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    """The per-tenant default project is auto-created by storage; the
    API refuses to archive it (422)."""
    tenant_id, hdr = admin_tenant
    default = await storage.get_or_create_default_project(tenant_id)
    r = client.delete(f"/api/v1/projects/{default.project_id}", headers=hdr)
    assert r.status_code == 422
    assert "default" in r.json()["detail"]["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_project_invisible_to_other_tenant(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
    other_tenant: tuple[str, dict[str, str]],
) -> None:
    _, hdr_a = admin_tenant
    _, hdr_b = other_tenant
    pid = client.post("/api/v1/projects", json={"name": "tenant-a-private"}, headers=hdr_a).json()[
        "project_id"
    ]

    # Tenant B's list omits A's project entirely.
    r = client.get("/api/v1/projects", headers=hdr_b)
    assert all(p["project_id"] != pid for p in r.json()["projects"])

    # Direct GET from B's bearer is a 404, not a 403 — no existence leak.
    r = client.get(f"/api/v1/projects/{pid}", headers=hdr_b)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Authz: admin scope vs project owner role compose
# ---------------------------------------------------------------------------


def test_reader_cannot_create_project(
    client: TestClient,
    reader_in_admin_tenant: dict[str, str],
) -> None:
    """A ``read``-only key gets 403 on create (admin scope is the static
    gate enforced by ``_scope("admin")`` on POST)."""
    r = client.post("/api/v1/projects", json={"name": "denied"}, headers=reader_in_admin_tenant)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_owner_role_grants_write_without_admin_scope(
    client: TestClient,
    storage: InMemoryStorage,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    """The composed gate (ADR 040 D4): a non-admin can mutate a project
    they ``own`` even without the ``admin`` scope. We:

    1. Have the tenant admin create a project (so it exists).
    2. Mint a SECOND ``read``-only key in the same tenant.
    3. Add that key's principal as ``owner`` of the project.
    4. Confirm PUT works for the reader-but-owner.
    """
    tenant_id, admin_hdr = admin_tenant
    create = client.post(
        "/api/v1/projects", json={"name": "owned-by-reader"}, headers=admin_hdr
    ).json()
    pid = create["project_id"]

    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="non-admin-owner",
        scopes=["read"],
    )
    await storage.save_api_key(minted.record)
    reader_principal = f"api_key:{minted.record.key_id}"
    reader_hdr = {"Authorization": f"Bearer {minted.full_key}"}

    # Promote the reader as an owner so the role-gate applies.
    r = client.post(
        f"/api/v1/projects/{pid}/members",
        json={"principal_id": reader_principal, "role": "owner"},
        headers=admin_hdr,
    )
    assert r.status_code == 201, r.text

    # Reader-but-owner can now PUT — no admin scope, but the owner role
    # composes correctly.
    r = client.put(
        f"/api/v1/projects/{pid}",
        json={"description": "renamed by non-admin owner"},
        headers=reader_hdr,
    )
    assert r.status_code == 200, r.text

    # Sanity: a different reader (non-member) still 403s.
    other = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="non-member-reader",
        scopes=["read"],
    )
    await storage.save_api_key(other.record)
    r = client.put(
        f"/api/v1/projects/{pid}",
        json={"description": "should fail"},
        headers={"Authorization": f"Bearer {other.full_key}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# ETag + If-Match optimistic concurrency
# ---------------------------------------------------------------------------


def test_etag_if_match_optimistic_concurrency(
    client: TestClient,
    admin_tenant: tuple[str, dict[str, str]],
) -> None:
    """Sending the ETag we just received as ``If-Match`` succeeds; a
    stale ETag (the one from BEFORE a concurrent edit) 412s on the next
    write."""
    _, hdr = admin_tenant
    create = client.post("/api/v1/projects", json={"name": "etag-test"}, headers=hdr).json()
    pid = create["project_id"]
    stale_etag = create["etag"]

    # First PUT with the fresh ETag succeeds — and yields a NEW etag.
    r = client.put(
        f"/api/v1/projects/{pid}",
        json={"description": "first edit"},
        headers={**hdr, "If-Match": f'"{stale_etag}"'},
    )
    assert r.status_code == 200, r.text
    new_etag = r.json()["etag"]
    assert new_etag != stale_etag

    # Re-sending the stale ETag is rejected with 412.
    r = client.put(
        f"/api/v1/projects/{pid}",
        json={"description": "second edit, stale precondition"},
        headers={**hdr, "If-Match": stale_etag},
    )
    assert r.status_code == 412

    # Absent If-Match → last-write-wins (back-compat).
    r = client.put(
        f"/api/v1/projects/{pid}",
        json={"description": "no precondition"},
        headers=hdr,
    )
    assert r.status_code == 200
