"""Durable workflow-registry storage — save/get/list/delete round-trip,
versioning, publish/revert, tenant isolation (ADR 037 D1).

Mirrors ``tests/test_agent_registry_storage.py`` row-for-row: the same three
backends in scope via the shared ``storage`` fixture in conftest.py —
:class:`InMemoryStorage`, :class:`SqliteProvider`, and
:class:`PostgresProvider` (skipped when ``MOVATE_PG_TEST_URL`` is unset).

Asserts the contract the runtime ``/api/v1/workflows`` routes rely on:

* ``save_workflow_bundle`` + ``get_workflow_bundle`` round-trip; wrong-tenant
  returns ``None`` (404-not-403, no existence leak).
* Versioning: save v1 → v2 → ``get(version=None)`` returns latest (v2);
  ``get(version="v1")`` pins. ``list_workflow_versions`` newest-first.
* ``list_workflows`` returns latest-per-name newest-first, tenant-scoped,
  ``published_only`` filter narrows to names with at least one published row.
* ``delete_workflow_bundle`` removes one (version=) or all versions; counts;
  tenant-scoped.
* ``publish_workflow_version`` flips target ``published=True`` and clears
  every other version of the same name; 404 on missing version; idempotent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import WorkflowBundleRecord


def _content_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _make_bundle(
    *,
    name: str = "demo-workflow",
    tenant_id: str = "tenant-a",
    version: str = "v1",
    created_by: str | None = "alice",
    created_at: datetime | None = None,
    files: dict[str, str] | None = None,
    published: bool = False,
) -> WorkflowBundleRecord:
    files = files or {
        "workflow.yaml": (
            f"api_version: movate/v1\n"
            f"kind: Workflow\n"
            f"name: {name}\n"
            f"version: 0.1.0\n"
            f"state_schema: ./schema/state.json\n"
            f"entrypoint: first\n"
            f"nodes:\n"
            f"  - id: first\n"
            f"    type: agent\n"
            f"    ref: ./agents/first\n"
        ),
        "schema/state.json": '{"type": "object"}',
    }
    return WorkflowBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by=created_by,
        content_hash=_content_hash(files),
        files=files,
        published=published,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_workflow_bundle(storage) -> None:
    bundle = _make_bundle()
    await storage.save_workflow_bundle(bundle)
    got = await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a")
    assert got is not None
    assert got.name == "demo-workflow"
    assert got.tenant_id == "tenant-a"
    assert got.version == "v1"
    assert got.created_by == "alice"
    assert got.content_hash == bundle.content_hash
    assert got.files == bundle.files
    assert got.published is False


@pytest.mark.unit
async def test_get_returns_none_for_missing(storage) -> None:
    assert await storage.get_workflow_bundle("ghost", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_get_is_tenant_scoped(storage) -> None:
    """A wrong-tenant name returns None — no existence leak."""
    await storage.save_workflow_bundle(_make_bundle(tenant_id="tenant-a"))
    assert await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a") is not None
    assert await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-b") is None


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_versions_latest_and_explicit(storage) -> None:
    base = datetime.now(UTC)
    v1 = _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    v2 = _make_bundle(version="v2", created_at=base)
    await storage.save_workflow_bundle(v1)
    await storage.save_workflow_bundle(v2)

    latest = await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a")
    assert latest is not None
    assert latest.version == "v2"

    got_v1 = await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a", version="v1")
    assert got_v1 is not None
    assert got_v1.version == "v1"

    assert (
        await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a", version="v9")
        is None
    )


@pytest.mark.unit
async def test_list_workflow_versions_newest_first(storage) -> None:
    base = datetime.now(UTC)
    v1 = _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    v2 = _make_bundle(version="v2", created_at=base)
    await storage.save_workflow_bundle(v1)
    await storage.save_workflow_bundle(v2)
    versions = await storage.list_workflow_versions("demo-workflow", tenant_id="tenant-a")
    assert [v.version for v in versions] == ["v2", "v1"]


@pytest.mark.unit
async def test_list_workflow_versions_is_tenant_scoped(storage) -> None:
    await storage.save_workflow_bundle(_make_bundle(tenant_id="tenant-a"))
    assert await storage.list_workflow_versions("demo-workflow", tenant_id="tenant-b") == []


# ---------------------------------------------------------------------------
# list_workflows — latest per name + published_only
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_workflows_latest_per_name_newest_first(storage) -> None:
    base = datetime.now(UTC)
    # "foo" has two versions; only the latest should appear.
    await storage.save_workflow_bundle(
        _make_bundle(name="foo", version="v1", created_at=base - timedelta(seconds=30))
    )
    await storage.save_workflow_bundle(
        _make_bundle(name="foo", version="v2", created_at=base - timedelta(seconds=20))
    )
    # "bar" published most recently → sorts first.
    await storage.save_workflow_bundle(
        _make_bundle(name="bar", version="v1", created_at=base - timedelta(seconds=5))
    )

    rows = await storage.list_workflows(tenant_id="tenant-a")
    assert [(r.name, r.version) for r in rows] == [("bar", "v1"), ("foo", "v2")]


@pytest.mark.unit
async def test_list_workflows_is_tenant_scoped(storage) -> None:
    await storage.save_workflow_bundle(_make_bundle(name="foo", tenant_id="tenant-a"))
    await storage.save_workflow_bundle(_make_bundle(name="bar", tenant_id="tenant-b"))
    rows = await storage.list_workflows(tenant_id="tenant-a")
    assert {r.name for r in rows} == {"foo"}


@pytest.mark.unit
async def test_list_workflows_published_only_filter(storage) -> None:
    """``published_only=True`` narrows to names with at least one published row;
    the returned row is still the latest version of each name."""
    base = datetime.now(UTC)
    # foo has two versions; only v1 is published.
    await storage.save_workflow_bundle(
        _make_bundle(
            name="foo",
            version="v1",
            created_at=base - timedelta(seconds=30),
            published=True,
        )
    )
    await storage.save_workflow_bundle(
        _make_bundle(name="foo", version="v2", created_at=base - timedelta(seconds=10))
    )
    # bar has no published versions → excluded.
    await storage.save_workflow_bundle(_make_bundle(name="bar", version="v1"))

    rows = await storage.list_workflows(tenant_id="tenant-a", published_only=True)
    assert {r.name for r in rows} == {"foo"}
    # The returned row is still the *latest* (v2), even though v1 is the published one.
    foo_row = next(r for r in rows if r.name == "foo")
    assert foo_row.version == "v2"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_one_version(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_workflow_bundle(
        _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    )
    await storage.save_workflow_bundle(_make_bundle(version="v2", created_at=base))

    deleted = await storage.delete_workflow_bundle(
        "demo-workflow", tenant_id="tenant-a", version="v1"
    )
    assert deleted == 1
    latest = await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a")
    assert latest is not None
    assert latest.version == "v2"


@pytest.mark.unit
async def test_delete_all_versions(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_workflow_bundle(
        _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    )
    await storage.save_workflow_bundle(_make_bundle(version="v2", created_at=base))
    deleted = await storage.delete_workflow_bundle("demo-workflow", tenant_id="tenant-a")
    assert deleted == 2
    assert await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_delete_is_tenant_scoped(storage) -> None:
    await storage.save_workflow_bundle(_make_bundle(tenant_id="tenant-a"))
    deleted = await storage.delete_workflow_bundle("demo-workflow", tenant_id="tenant-b")
    assert deleted == 0
    assert await storage.get_workflow_bundle("demo-workflow", tenant_id="tenant-a") is not None


# ---------------------------------------------------------------------------
# publish_workflow_version — promote/clear semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_publish_sets_target_and_clears_others(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_workflow_bundle(
        _make_bundle(version="v1", created_at=base - timedelta(seconds=30))
    )
    await storage.save_workflow_bundle(
        _make_bundle(version="v2", created_at=base - timedelta(seconds=20))
    )
    await storage.save_workflow_bundle(_make_bundle(version="v3", created_at=base, published=True))

    ok = await storage.publish_workflow_version("demo-workflow", tenant_id="tenant-a", version="v2")
    assert ok is True

    versions = await storage.list_workflow_versions("demo-workflow", tenant_id="tenant-a")
    flags = {v.version: v.published for v in versions}
    assert flags == {"v1": False, "v2": True, "v3": False}


@pytest.mark.unit
async def test_publish_missing_version_returns_false(storage) -> None:
    await storage.save_workflow_bundle(_make_bundle(version="v1"))
    ok = await storage.publish_workflow_version(
        "demo-workflow", tenant_id="tenant-a", version="ghost"
    )
    assert ok is False


@pytest.mark.unit
async def test_publish_is_idempotent(storage) -> None:
    await storage.save_workflow_bundle(_make_bundle(version="v1"))
    assert (
        await storage.publish_workflow_version("demo-workflow", tenant_id="tenant-a", version="v1")
        is True
    )
    # Re-publish the same version — still True, still exactly one publication.
    assert (
        await storage.publish_workflow_version("demo-workflow", tenant_id="tenant-a", version="v1")
        is True
    )
    versions = await storage.list_workflow_versions("demo-workflow", tenant_id="tenant-a")
    assert [v.published for v in versions] == [True]


@pytest.mark.unit
async def test_publish_is_tenant_scoped(storage) -> None:
    """A cross-tenant publish does nothing and returns False — no leak."""
    await storage.save_workflow_bundle(_make_bundle(version="v1", tenant_id="tenant-a"))
    ok = await storage.publish_workflow_version("demo-workflow", tenant_id="tenant-b", version="v1")
    assert ok is False
    versions = await storage.list_workflow_versions("demo-workflow", tenant_id="tenant-a")
    assert versions[0].published is False  # the real owner's flag is untouched
