"""Durable agent-registry storage — save/get/list/delete round-trip,
versioning, and tenant isolation (ADR 014 step 1).

Mirrors ``tests/test_bench_storage.py``: the same three backends in scope
via the shared ``storage`` fixture in conftest.py — ``InMemoryStorage``,
``SqliteProvider``, and ``PostgresProvider`` (skipped when
``MOVATE_PG_TEST_URL`` is unset; CI runs the PG branch).

Asserts the contract the runtime resolve-from-registry step (step 2) will
rely on:

* ``save_agent_bundle`` then ``get_agent_bundle`` round-trips the ``files``
  map + every metadata field; a wrong-tenant name returns ``None``
  (404-not-403, no existence leak).
* Versioning: save v1 then v2 of the same name; ``get(version=None)`` →
  latest (v2), ``get(version="v1")`` → v1, ``list_agent_versions`` →
  both newest-first.
* ``list_agents`` → latest-per-name, newest-first, tenant-scoped, limit
  honored.
* ``delete_agent_bundle(name, version=...)`` removes one version;
  ``delete_agent_bundle(name)`` removes all; returns counts; tenant-scoped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import AgentBundleRecord

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _content_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _make_bundle(
    *,
    name: str = "demo-agent",
    tenant_id: str = "tenant-a",
    version: str = "v1",
    created_by: str | None = "alice",
    created_at: datetime | None = None,
    files: dict[str, str] | None = None,
) -> AgentBundleRecord:
    files = files or {
        "agent.yaml": f"name: {name}\nversion: {version}\n",
        "prompt.md": "You are a helpful assistant.\n",
        "schema/input.json": '{"type": "object"}',
        "schema/output.json": '{"type": "object"}',
        "evals/dataset.jsonl": '{"input": {"text": "hi"}}\n',
        "skills/lookup.py": "def lookup():\n    return 42\n",
        "contexts/policy.md": "# Policy\n",
    }
    return AgentBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by=created_by,
        content_hash=_content_hash(files),
        files=files,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_agent_bundle(storage) -> None:
    bundle = _make_bundle()
    await storage.save_agent_bundle(bundle)
    got = await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a")
    assert got is not None
    assert got.name == "demo-agent"
    assert got.tenant_id == "tenant-a"
    assert got.version == "v1"
    assert got.created_by == "alice"
    assert got.content_hash == bundle.content_hash
    # The full files map survives the JSON round-trip.
    assert got.files == bundle.files
    assert got.files["agent.yaml"] == "name: demo-agent\nversion: v1\n"
    assert got.files["skills/lookup.py"] == "def lookup():\n    return 42\n"


@pytest.mark.unit
async def test_save_and_get_agent_bundle_null_created_by(storage) -> None:
    """A system/seed import has no auth identity → created_by is None."""
    bundle = _make_bundle(created_by=None)
    await storage.save_agent_bundle(bundle)
    got = await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a")
    assert got is not None
    assert got.created_by is None


@pytest.mark.unit
async def test_get_agent_bundle_returns_none_for_missing(storage) -> None:
    assert await storage.get_agent_bundle("ghost", tenant_id="tenant-a") is None


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_agent_bundle_is_tenant_scoped(storage) -> None:
    """A wrong-tenant name returns None — no existence leak across tenants."""
    await storage.save_agent_bundle(_make_bundle(tenant_id="tenant-a"))
    assert await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a") is not None
    # Wrong tenant gets None (404-not-403).
    assert await storage.get_agent_bundle("demo-agent", tenant_id="tenant-b") is None


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_versions_latest_and_explicit(storage) -> None:
    base = datetime.now(UTC)
    v1 = _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    v2 = _make_bundle(version="v2", created_at=base)
    await storage.save_agent_bundle(v1)
    await storage.save_agent_bundle(v2)

    # version=None → the latest version (v2).
    latest = await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a")
    assert latest is not None
    assert latest.version == "v2"

    # explicit version pins exactly.
    got_v1 = await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a", version="v1")
    assert got_v1 is not None
    assert got_v1.version == "v1"
    got_v2 = await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a", version="v2")
    assert got_v2 is not None
    assert got_v2.version == "v2"

    # unknown version → None.
    assert await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a", version="v9") is None


@pytest.mark.unit
async def test_list_agent_versions_newest_first(storage) -> None:
    base = datetime.now(UTC)
    v1 = _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    v2 = _make_bundle(version="v2", created_at=base)
    await storage.save_agent_bundle(v1)
    await storage.save_agent_bundle(v2)

    versions = await storage.list_agent_versions("demo-agent", tenant_id="tenant-a")
    assert [v.version for v in versions] == ["v2", "v1"]


@pytest.mark.unit
async def test_list_agent_versions_is_tenant_scoped(storage) -> None:
    await storage.save_agent_bundle(_make_bundle(version="v1", tenant_id="tenant-a"))
    assert await storage.list_agent_versions("demo-agent", tenant_id="tenant-b") == []


@pytest.mark.unit
async def test_list_agent_versions_honors_limit(storage) -> None:
    base = datetime.now(UTC)
    for i in range(5):
        await storage.save_agent_bundle(
            _make_bundle(version=f"v{i}", created_at=base - timedelta(seconds=i))
        )
    versions = await storage.list_agent_versions("demo-agent", tenant_id="tenant-a", limit=3)
    assert len(versions) == 3
    # Still newest-first within the limited window.
    assert [v.version for v in versions] == ["v0", "v1", "v2"]


# ---------------------------------------------------------------------------
# list_agents — latest per name
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_agents_latest_per_name_newest_first(storage) -> None:
    base = datetime.now(UTC)
    # "foo" has two versions; only the latest should appear.
    await storage.save_agent_bundle(
        _make_bundle(name="foo", version="v1", created_at=base - timedelta(seconds=30))
    )
    await storage.save_agent_bundle(
        _make_bundle(name="foo", version="v2", created_at=base - timedelta(seconds=20))
    )
    # "bar" published most recently → sorts first.
    await storage.save_agent_bundle(
        _make_bundle(name="bar", version="v1", created_at=base - timedelta(seconds=5))
    )

    agents = await storage.list_agents(tenant_id="tenant-a")
    # One row per name, newest publish first.
    assert [(a.name, a.version) for a in agents] == [("bar", "v1"), ("foo", "v2")]


@pytest.mark.unit
async def test_list_agents_is_tenant_scoped(storage) -> None:
    await storage.save_agent_bundle(_make_bundle(name="foo", tenant_id="tenant-a"))
    await storage.save_agent_bundle(_make_bundle(name="bar", tenant_id="tenant-b"))

    only_a = await storage.list_agents(tenant_id="tenant-a")
    assert {a.name for a in only_a} == {"foo"}


@pytest.mark.unit
async def test_list_agents_honors_limit(storage) -> None:
    base = datetime.now(UTC)
    for i in range(5):
        await storage.save_agent_bundle(
            _make_bundle(name=f"agent-{i}", created_at=base - timedelta(seconds=i))
        )
    agents = await storage.list_agents(tenant_id="tenant-a", limit=3)
    assert len(agents) == 3


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_one_version(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_agent_bundle(
        _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    )
    await storage.save_agent_bundle(_make_bundle(version="v2", created_at=base))

    deleted = await storage.delete_agent_bundle("demo-agent", tenant_id="tenant-a", version="v1")
    assert deleted == 1

    # v1 gone, v2 remains and is now the latest.
    assert await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a", version="v1") is None
    latest = await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a")
    assert latest is not None
    assert latest.version == "v2"


@pytest.mark.unit
async def test_delete_all_versions(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_agent_bundle(
        _make_bundle(version="v1", created_at=base - timedelta(seconds=10))
    )
    await storage.save_agent_bundle(_make_bundle(version="v2", created_at=base))

    deleted = await storage.delete_agent_bundle("demo-agent", tenant_id="tenant-a")
    assert deleted == 2
    assert await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a") is None
    assert await storage.list_agent_versions("demo-agent", tenant_id="tenant-a") == []


@pytest.mark.unit
async def test_delete_is_tenant_scoped(storage) -> None:
    """Deleting with the wrong tenant removes nothing and returns 0."""
    await storage.save_agent_bundle(_make_bundle(tenant_id="tenant-a"))
    deleted = await storage.delete_agent_bundle("demo-agent", tenant_id="tenant-b")
    assert deleted == 0
    # The real owner's bundle is untouched.
    assert await storage.get_agent_bundle("demo-agent", tenant_id="tenant-a") is not None


@pytest.mark.unit
async def test_delete_missing_returns_zero(storage) -> None:
    assert await storage.delete_agent_bundle("ghost", tenant_id="tenant-a") == 0
