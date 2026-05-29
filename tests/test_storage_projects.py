"""Projects storage — CRUD, soft-delete, members, junctions, default project.

ADR 040 step 1 (storage layer). Mirrors ``tests/test_agent_registry_storage.py``:
the same three backends in scope via the shared ``storage`` fixture in
``conftest.py`` — :class:`InMemoryStorage`, :class:`SqliteProvider`, and
:class:`PostgresProvider` (skipped when ``MOVATE_PG_TEST_URL`` is unset; CI
runs the PG branch).

Asserts the contract the /api/v1 surface (separate PR) will rely on:

* Create + read + list + update + archive round-trips on the ``Project``.
* The lazy default project (D5): created on first read, can be re-fetched,
  ``archive_project`` rejects it.
* ``ProjectMember`` CRUD covers role transitions (viewer → editor → owner).
* Agent attachments (M:N): attach + detach + idempotency + reverse lookup
  collapses to the default project when no explicit attachment exists.
* Soft-delete (D6): archived projects vanish from the default listing and
  reappear with ``include_archived=True``.
* Tenant isolation: every getter / lister filters by ``tenant_id`` and
  returns nothing for a wrong tenant — no existence leak (mirrors the
  other ``_storage`` test suites).
"""

from __future__ import annotations

import pytest

from movate.core.models import (
    Project,
    ProjectKbMode,
    ProjectMemberRole,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_project(
    *,
    tenant_id: str = "tenant-a",
    name: str = "alpha",
    description: str | None = "team alpha workspace",
    owner_principal_id: str = "api_key:alice",
) -> Project:
    return Project(
        tenant_id=tenant_id,
        name=name,
        description=description,
        owner_principal_id=owner_principal_id,
    )


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_create_and_get_project(storage) -> None:
    project = _make_project()
    created = await storage.create_project(project)
    assert created.project_id == project.project_id
    got = await storage.get_project("tenant-a", project.project_id)
    assert got is not None
    assert got.name == "alpha"
    assert got.tenant_id == "tenant-a"
    assert got.description == "team alpha workspace"
    assert got.owner_principal_id == "api_key:alice"
    assert got.archived_at is None


@pytest.mark.unit
async def test_get_project_by_name(storage) -> None:
    project = await storage.create_project(_make_project(name="beta"))
    got = await storage.get_project_by_name("tenant-a", "beta")
    assert got is not None
    assert got.project_id == project.project_id
    # Unknown name → None (no existence leak).
    assert await storage.get_project_by_name("tenant-a", "ghost") is None


@pytest.mark.unit
async def test_create_project_duplicate_name_rejected(storage) -> None:
    await storage.create_project(_make_project(name="dupe"))
    with pytest.raises(ValueError, match="already exists"):
        await storage.create_project(_make_project(name="dupe"))


@pytest.mark.unit
async def test_update_project(storage) -> None:
    project = await storage.create_project(_make_project())
    updated = await storage.update_project(
        "tenant-a",
        project.project_id,
        name="alpha-renamed",
        description="updated",
    )
    assert updated is not None
    assert updated.name == "alpha-renamed"
    assert updated.description == "updated"
    assert updated.updated_at >= project.updated_at


@pytest.mark.unit
async def test_update_project_partial(storage) -> None:
    """Only-name and only-description PATCHes both work."""
    project = await storage.create_project(_make_project(description="original"))
    only_name = await storage.update_project("tenant-a", project.project_id, name="only-name")
    assert only_name is not None
    assert only_name.name == "only-name"
    assert only_name.description == "original"
    only_desc = await storage.update_project(
        "tenant-a", project.project_id, description="only-desc"
    )
    assert only_desc is not None
    assert only_desc.name == "only-name"
    assert only_desc.description == "only-desc"


@pytest.mark.unit
async def test_update_project_rename_collision(storage) -> None:
    await storage.create_project(_make_project(name="taken"))
    other = await storage.create_project(_make_project(name="free"))
    with pytest.raises(ValueError, match="already exists"):
        await storage.update_project("tenant-a", other.project_id, name="taken")


@pytest.mark.unit
async def test_update_project_unknown_returns_none(storage) -> None:
    got = await storage.update_project("tenant-a", "prj_does_not_exist", name="x")
    assert got is None


@pytest.mark.unit
async def test_list_projects_newest_first(storage) -> None:
    p1 = await storage.create_project(_make_project(name="one"))
    p2 = await storage.create_project(_make_project(name="two"))
    p3 = await storage.create_project(_make_project(name="three"))
    rows = await storage.list_projects("tenant-a")
    ids = [r.project_id for r in rows]
    # Most-recently created first.
    assert ids[0] == p3.project_id
    assert ids[-1] == p1.project_id
    assert p2.project_id in ids


@pytest.mark.unit
async def test_list_projects_limit(storage) -> None:
    for i in range(5):
        await storage.create_project(_make_project(name=f"p{i}"))
    rows = await storage.list_projects("tenant-a", limit=2)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Default project (D5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_project_auto_created(storage) -> None:
    default = await storage.get_or_create_default_project("tenant-a")
    assert default.name == "default"
    assert default.tenant_id == "tenant-a"
    assert default.owner_principal_id == "tenant-system"
    assert default.archived_at is None
    # Idempotent — the second call returns the same row.
    again = await storage.get_or_create_default_project("tenant-a")
    assert again.project_id == default.project_id


@pytest.mark.unit
async def test_default_project_per_tenant(storage) -> None:
    a = await storage.get_or_create_default_project("tenant-a")
    b = await storage.get_or_create_default_project("tenant-b")
    assert a.project_id != b.project_id
    assert a.tenant_id == "tenant-a"
    assert b.tenant_id == "tenant-b"


@pytest.mark.unit
async def test_default_project_cannot_be_archived(storage) -> None:
    default = await storage.get_or_create_default_project("tenant-a")
    with pytest.raises(ValueError, match="default project"):
        await storage.archive_project("tenant-a", default.project_id)
    # And it stays unarchived.
    again = await storage.get_project("tenant-a", default.project_id)
    assert again is not None
    assert again.archived_at is None


# ---------------------------------------------------------------------------
# Soft delete (D6)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_archive_project_soft_delete(storage) -> None:
    project = await storage.create_project(_make_project(name="archivable"))
    ok = await storage.archive_project("tenant-a", project.project_id)
    assert ok is True
    # Default list hides it.
    visible = await storage.list_projects("tenant-a")
    assert all(p.project_id != project.project_id for p in visible)
    # include_archived=True reveals it.
    everything = await storage.list_projects("tenant-a", include_archived=True)
    assert any(p.project_id == project.project_id for p in everything)
    # Get-by-id still returns it (the detail view).
    detail = await storage.get_project("tenant-a", project.project_id)
    assert detail is not None
    assert detail.archived_at is not None


@pytest.mark.unit
async def test_archive_project_idempotent(storage) -> None:
    project = await storage.create_project(_make_project(name="twice"))
    assert await storage.archive_project("tenant-a", project.project_id) is True
    # Second call: nothing newly changed.
    assert await storage.archive_project("tenant-a", project.project_id) is False


@pytest.mark.unit
async def test_archive_project_unknown(storage) -> None:
    assert await storage.archive_project("tenant-a", "prj_nope") is False


# ---------------------------------------------------------------------------
# Members + role transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_member_crud_and_role_transitions(storage) -> None:
    project = await storage.create_project(_make_project())
    pid = project.project_id
    # Add bob as viewer.
    await storage.add_project_member(
        pid, "api_key:bob", ProjectMemberRole.VIEWER, added_by="api_key:alice"
    )
    member = await storage.get_project_member(pid, "api_key:bob")
    assert member is not None
    assert member.role == ProjectMemberRole.VIEWER
    assert member.added_by == "api_key:alice"
    # Viewer → editor.
    updated = await storage.update_project_member(pid, "api_key:bob", role=ProjectMemberRole.EDITOR)
    assert updated is not None
    assert updated.role == ProjectMemberRole.EDITOR
    # Editor → owner.
    promoted = await storage.update_project_member(pid, "api_key:bob", role=ProjectMemberRole.OWNER)
    assert promoted is not None
    assert promoted.role == ProjectMemberRole.OWNER
    # Remove.
    assert await storage.remove_project_member(pid, "api_key:bob") is True
    assert await storage.get_project_member(pid, "api_key:bob") is None
    # Idempotent re-remove.
    assert await storage.remove_project_member(pid, "api_key:bob") is False


@pytest.mark.unit
async def test_add_duplicate_member_rejected(storage) -> None:
    project = await storage.create_project(_make_project())
    pid = project.project_id
    await storage.add_project_member(
        pid, "api_key:bob", ProjectMemberRole.VIEWER, added_by="api_key:alice"
    )
    with pytest.raises(ValueError, match="already on project"):
        await storage.add_project_member(
            pid, "api_key:bob", ProjectMemberRole.EDITOR, added_by="api_key:alice"
        )


@pytest.mark.unit
async def test_list_project_members(storage) -> None:
    project = await storage.create_project(_make_project())
    pid = project.project_id
    await storage.add_project_member(
        pid, "api_key:alice", ProjectMemberRole.OWNER, added_by="api_key:alice"
    )
    await storage.add_project_member(
        pid, "api_key:bob", ProjectMemberRole.VIEWER, added_by="api_key:alice"
    )
    members = await storage.list_project_members(pid)
    ids = {m.principal_id for m in members}
    assert ids == {"api_key:alice", "api_key:bob"}


@pytest.mark.unit
async def test_update_unknown_member_returns_none(storage) -> None:
    project = await storage.create_project(_make_project())
    got = await storage.update_project_member(
        project.project_id, "api_key:ghost", role=ProjectMemberRole.OWNER
    )
    assert got is None


# ---------------------------------------------------------------------------
# Agent attachments + D5 default fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_attach_and_detach_agent(storage) -> None:
    project = await storage.create_project(_make_project())
    pid = project.project_id
    await storage.attach_agent_to_project(pid, "support-bot")
    await storage.attach_agent_to_project(pid, "code-reviewer")
    assert set(await storage.list_project_agents(pid)) == {
        "support-bot",
        "code-reviewer",
    }
    assert await storage.detach_agent_from_project(pid, "support-bot") is True
    assert await storage.list_project_agents(pid) == ["code-reviewer"]
    # Idempotent detach.
    assert await storage.detach_agent_from_project(pid, "support-bot") is False


@pytest.mark.unit
async def test_attach_agent_idempotent(storage) -> None:
    project = await storage.create_project(_make_project())
    pid = project.project_id
    await storage.attach_agent_to_project(pid, "a")
    await storage.attach_agent_to_project(pid, "a")  # silent no-op
    assert await storage.list_project_agents(pid) == ["a"]


@pytest.mark.unit
async def test_list_projects_for_agent_attached(storage) -> None:
    p1 = await storage.create_project(_make_project(name="p1"))
    p2 = await storage.create_project(_make_project(name="p2"))
    await storage.attach_agent_to_project(p1.project_id, "shared")
    await storage.attach_agent_to_project(p2.project_id, "shared")
    ids = await storage.list_projects_for_agent("tenant-a", "shared")
    assert set(ids) == {p1.project_id, p2.project_id}


@pytest.mark.unit
async def test_list_projects_for_unattached_agent_returns_default(storage) -> None:
    # No explicit attachment → D5 implicit-default; the helper creates and
    # returns the default project's id.
    ids = await storage.list_projects_for_agent("tenant-a", "stray")
    default = await storage.get_or_create_default_project("tenant-a")
    assert ids == [default.project_id]


# ---------------------------------------------------------------------------
# Workflow attachments
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_workflow_attach_detach(storage) -> None:
    project = await storage.create_project(_make_project())
    pid = project.project_id
    await storage.attach_workflow_to_project(pid, "wf-onboarding")
    assert await storage.list_project_workflows(pid) == ["wf-onboarding"]
    assert await storage.detach_workflow_from_project(pid, "wf-onboarding") is True
    assert await storage.list_project_workflows(pid) == []


@pytest.mark.unit
async def test_list_projects_for_unattached_workflow_returns_default(storage) -> None:
    ids = await storage.list_projects_for_workflow("tenant-a", "wf-stray")
    default = await storage.get_or_create_default_project("tenant-a")
    assert ids == [default.project_id]


# ---------------------------------------------------------------------------
# KB attachments (modes)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_kb_attach_modes_and_share_promotion(storage) -> None:
    owner = await storage.create_project(_make_project(name="owning"))
    consumer = await storage.create_project(_make_project(name="reader"))
    await storage.attach_kb_to_project(owner.project_id, "kb_42", ProjectKbMode.OWNED)
    await storage.attach_kb_to_project(consumer.project_id, "kb_42", ProjectKbMode.SHARED_REFERENCE)
    owner_kbs = dict(await storage.list_project_kbs(owner.project_id))
    consumer_kbs = dict(await storage.list_project_kbs(consumer.project_id))
    assert owner_kbs == {"kb_42": ProjectKbMode.OWNED}
    assert consumer_kbs == {"kb_42": ProjectKbMode.SHARED_REFERENCE}
    # Mode promotion: shared_reference → shared_copy is an in-place update.
    await storage.attach_kb_to_project(consumer.project_id, "kb_42", ProjectKbMode.SHARED_COPY)
    consumer_after = dict(await storage.list_project_kbs(consumer.project_id))
    assert consumer_after == {"kb_42": ProjectKbMode.SHARED_COPY}


@pytest.mark.unit
async def test_kb_detach(storage) -> None:
    project = await storage.create_project(_make_project())
    pid = project.project_id
    await storage.attach_kb_to_project(pid, "kb_x", ProjectKbMode.OWNED)
    assert await storage.detach_kb_from_project(pid, "kb_x") is True
    assert await storage.list_project_kbs(pid) == []
    assert await storage.detach_kb_from_project(pid, "kb_x") is False


@pytest.mark.unit
async def test_list_projects_for_kb_no_default_fallback(storage) -> None:
    # Unlike agents/workflows, KBs don't fall back to the default project.
    assert await storage.list_projects_for_kb("tenant-a", "kb_orphan") == []


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_project_wrong_tenant_returns_none(storage) -> None:
    project = await storage.create_project(_make_project(tenant_id="tenant-a"))
    assert await storage.get_project("tenant-b", project.project_id) is None


@pytest.mark.unit
async def test_list_projects_wrong_tenant_empty(storage) -> None:
    await storage.create_project(_make_project(tenant_id="tenant-a", name="x"))
    assert await storage.list_projects("tenant-b") == []


@pytest.mark.unit
async def test_archive_project_wrong_tenant_noop(storage) -> None:
    project = await storage.create_project(_make_project(tenant_id="tenant-a"))
    assert await storage.archive_project("tenant-b", project.project_id) is False
    # Original is untouched.
    detail = await storage.get_project("tenant-a", project.project_id)
    assert detail is not None
    assert detail.archived_at is None


@pytest.mark.unit
async def test_update_project_wrong_tenant_returns_none(storage) -> None:
    project = await storage.create_project(_make_project(tenant_id="tenant-a"))
    assert await storage.update_project("tenant-b", project.project_id, name="x") is None


@pytest.mark.unit
async def test_list_projects_for_agent_isolates_tenants(storage) -> None:
    # Same agent name attached in two tenants; reverse lookup never crosses.
    a_proj = await storage.create_project(_make_project(tenant_id="tenant-a", name="ta"))
    b_proj = await storage.create_project(_make_project(tenant_id="tenant-b", name="tb"))
    await storage.attach_agent_to_project(a_proj.project_id, "shared")
    await storage.attach_agent_to_project(b_proj.project_id, "shared")
    for_a = await storage.list_projects_for_agent("tenant-a", "shared")
    for_b = await storage.list_projects_for_agent("tenant-b", "shared")
    assert for_a == [a_proj.project_id]
    assert for_b == [b_proj.project_id]
