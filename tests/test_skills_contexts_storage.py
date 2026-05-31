"""Durable skills + contexts storage — save/get/list/delete round-trip,
versioning, and tenant isolation (ADR 060 D1).

Mirrors ``tests/test_agent_registry_storage.py``: the same three backends in
scope via the shared ``storage`` fixture in conftest.py — ``InMemoryStorage``,
``SqliteProvider``, and ``PostgresProvider`` (skipped when
``MOVATE_PG_TEST_URL`` is unset; CI runs the PG branch).

Asserts the contract the ADR 060 API CRUD/attach surface relies on, for BOTH
managed resources:

* ``save_*`` then ``get_*`` round-trips the payload (skill ``files`` map /
  context ``body``) + every metadata field; a wrong-tenant name returns
  ``None`` (404-not-403, no existence leak).
* Versioning: save v1 then v2 of the same name; ``get(version=None)`` →
  latest (v2), ``get(version="v1")`` → v1, ``list_*_versions`` → both
  newest-first.
* ``list_*`` → latest-per-name, newest-first, tenant-scoped, limit honored.
* ``delete_*(name, version=...)`` removes one version; ``delete_*(name)``
  removes all; returns counts; tenant-scoped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import ContextRecord, SkillRecord

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _files_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _make_skill(
    *,
    name: str = "web-search",
    tenant_id: str = "tenant-a",
    version: str = "v1",
    created_by: str | None = "alice",
    created_at: datetime | None = None,
    files: dict[str, str] | None = None,
    description: str = "search the web",
) -> SkillRecord:
    files = files or {
        "skill.yaml": f"name: {name}\nversion: {version}\ndescription: {description}\n",
        "impl.py": "def run(args):\n    return {}\n",
        "corpus.json": '{"docs": []}',
        "README.md": "# Web search\n",
    }
    return SkillRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by=created_by,
        content_hash=_files_hash(files),
        description=description,
        files=files,
        created_at=created_at or datetime.now(UTC),
    )


def _make_context(
    *,
    name: str = "company-tone",
    tenant_id: str = "tenant-a",
    version: str = "v1",
    created_by: str | None = "alice",
    created_at: datetime | None = None,
    body: str | None = None,
    description: str = "house voice + tone",
) -> ContextRecord:
    body = body if body is not None else f"# {name}\n\nBe concise and friendly.\n"
    return ContextRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by=created_by,
        content_hash=_body_hash(body),
        description=description,
        body=body,
        created_at=created_at or datetime.now(UTC),
    )


# ===========================================================================
# Skills
# ===========================================================================


@pytest.mark.unit
async def test_save_and_get_skill(storage) -> None:
    skill = _make_skill()
    await storage.save_skill(skill)
    got = await storage.get_skill("web-search", tenant_id="tenant-a")
    assert got is not None
    assert got.name == "web-search"
    assert got.tenant_id == "tenant-a"
    assert got.version == "v1"
    assert got.created_by == "alice"
    assert got.description == "search the web"
    assert got.content_hash == skill.content_hash
    # The full files map survives the JSON round-trip.
    assert got.files == skill.files
    assert got.files["impl.py"] == "def run(args):\n    return {}\n"


@pytest.mark.unit
async def test_save_and_get_skill_null_created_by(storage) -> None:
    await storage.save_skill(_make_skill(created_by=None))
    got = await storage.get_skill("web-search", tenant_id="tenant-a")
    assert got is not None
    assert got.created_by is None


@pytest.mark.unit
async def test_get_skill_returns_none_for_missing(storage) -> None:
    assert await storage.get_skill("ghost", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_get_skill_is_tenant_scoped(storage) -> None:
    """A wrong-tenant name returns None — no existence leak across tenants."""
    await storage.save_skill(_make_skill(tenant_id="tenant-a"))
    assert await storage.get_skill("web-search", tenant_id="tenant-a") is not None
    assert await storage.get_skill("web-search", tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_skill_versions_latest_and_explicit(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_skill(_make_skill(version="v1", created_at=base - timedelta(seconds=10)))
    await storage.save_skill(_make_skill(version="v2", created_at=base))

    latest = await storage.get_skill("web-search", tenant_id="tenant-a")
    assert latest is not None and latest.version == "v2"

    got_v1 = await storage.get_skill("web-search", tenant_id="tenant-a", version="v1")
    assert got_v1 is not None and got_v1.version == "v1"
    assert await storage.get_skill("web-search", tenant_id="tenant-a", version="v9") is None


@pytest.mark.unit
async def test_list_skill_versions_newest_first(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_skill(_make_skill(version="v1", created_at=base - timedelta(seconds=10)))
    await storage.save_skill(_make_skill(version="v2", created_at=base))
    versions = await storage.list_skill_versions("web-search", tenant_id="tenant-a")
    assert [v.version for v in versions] == ["v2", "v1"]


@pytest.mark.unit
async def test_list_skill_versions_is_tenant_scoped(storage) -> None:
    await storage.save_skill(_make_skill(version="v1", tenant_id="tenant-a"))
    assert await storage.list_skill_versions("web-search", tenant_id="tenant-b") == []


@pytest.mark.unit
async def test_list_skills_latest_per_name_newest_first(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_skill(
        _make_skill(name="foo", version="v1", created_at=base - timedelta(seconds=30))
    )
    await storage.save_skill(
        _make_skill(name="foo", version="v2", created_at=base - timedelta(seconds=20))
    )
    await storage.save_skill(
        _make_skill(name="bar", version="v1", created_at=base - timedelta(seconds=5))
    )
    skills = await storage.list_skills(tenant_id="tenant-a")
    assert [(s.name, s.version) for s in skills] == [("bar", "v1"), ("foo", "v2")]


@pytest.mark.unit
async def test_list_skills_is_tenant_scoped(storage) -> None:
    await storage.save_skill(_make_skill(name="foo", tenant_id="tenant-a"))
    await storage.save_skill(_make_skill(name="bar", tenant_id="tenant-b"))
    only_a = await storage.list_skills(tenant_id="tenant-a")
    assert {s.name for s in only_a} == {"foo"}


@pytest.mark.unit
async def test_list_skills_honors_limit(storage) -> None:
    base = datetime.now(UTC)
    for i in range(5):
        await storage.save_skill(
            _make_skill(name=f"skill-{i}", created_at=base - timedelta(seconds=i))
        )
    assert len(await storage.list_skills(tenant_id="tenant-a", limit=3)) == 3


@pytest.mark.unit
async def test_delete_one_skill_version(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_skill(_make_skill(version="v1", created_at=base - timedelta(seconds=10)))
    await storage.save_skill(_make_skill(version="v2", created_at=base))
    assert await storage.delete_skill("web-search", tenant_id="tenant-a", version="v1") == 1
    assert await storage.get_skill("web-search", tenant_id="tenant-a", version="v1") is None
    latest = await storage.get_skill("web-search", tenant_id="tenant-a")
    assert latest is not None and latest.version == "v2"


@pytest.mark.unit
async def test_delete_all_skill_versions(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_skill(_make_skill(version="v1", created_at=base - timedelta(seconds=10)))
    await storage.save_skill(_make_skill(version="v2", created_at=base))
    assert await storage.delete_skill("web-search", tenant_id="tenant-a") == 2
    assert await storage.get_skill("web-search", tenant_id="tenant-a") is None
    assert await storage.list_skill_versions("web-search", tenant_id="tenant-a") == []


@pytest.mark.unit
async def test_delete_skill_is_tenant_scoped(storage) -> None:
    await storage.save_skill(_make_skill(tenant_id="tenant-a"))
    assert await storage.delete_skill("web-search", tenant_id="tenant-b") == 0
    assert await storage.get_skill("web-search", tenant_id="tenant-a") is not None


@pytest.mark.unit
async def test_delete_missing_skill_returns_zero(storage) -> None:
    assert await storage.delete_skill("ghost", tenant_id="tenant-a") == 0


# ===========================================================================
# Contexts
# ===========================================================================


@pytest.mark.unit
async def test_save_and_get_context(storage) -> None:
    ctx = _make_context()
    await storage.save_context(ctx)
    got = await storage.get_context("company-tone", tenant_id="tenant-a")
    assert got is not None
    assert got.name == "company-tone"
    assert got.tenant_id == "tenant-a"
    assert got.version == "v1"
    assert got.created_by == "alice"
    assert got.description == "house voice + tone"
    assert got.content_hash == ctx.content_hash
    assert got.body == ctx.body


@pytest.mark.unit
async def test_save_and_get_context_null_created_by(storage) -> None:
    await storage.save_context(_make_context(created_by=None))
    got = await storage.get_context("company-tone", tenant_id="tenant-a")
    assert got is not None
    assert got.created_by is None


@pytest.mark.unit
async def test_get_context_returns_none_for_missing(storage) -> None:
    assert await storage.get_context("ghost", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_get_context_is_tenant_scoped(storage) -> None:
    await storage.save_context(_make_context(tenant_id="tenant-a"))
    assert await storage.get_context("company-tone", tenant_id="tenant-a") is not None
    assert await storage.get_context("company-tone", tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_context_versions_latest_and_explicit(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_context(
        _make_context(version="v1", body="v1 body", created_at=base - timedelta(seconds=10))
    )
    await storage.save_context(_make_context(version="v2", body="v2 body", created_at=base))

    latest = await storage.get_context("company-tone", tenant_id="tenant-a")
    assert latest is not None and latest.version == "v2"
    assert latest.body == "v2 body"

    got_v1 = await storage.get_context("company-tone", tenant_id="tenant-a", version="v1")
    assert got_v1 is not None and got_v1.body == "v1 body"
    assert await storage.get_context("company-tone", tenant_id="tenant-a", version="v9") is None


@pytest.mark.unit
async def test_list_context_versions_newest_first(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_context(_make_context(version="v1", created_at=base - timedelta(seconds=10)))
    await storage.save_context(_make_context(version="v2", created_at=base))
    versions = await storage.list_context_versions("company-tone", tenant_id="tenant-a")
    assert [v.version for v in versions] == ["v2", "v1"]


@pytest.mark.unit
async def test_list_contexts_latest_per_name_newest_first(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_context(
        _make_context(name="foo", version="v1", created_at=base - timedelta(seconds=30))
    )
    await storage.save_context(
        _make_context(name="foo", version="v2", created_at=base - timedelta(seconds=20))
    )
    await storage.save_context(
        _make_context(name="bar", version="v1", created_at=base - timedelta(seconds=5))
    )
    contexts = await storage.list_contexts(tenant_id="tenant-a")
    assert [(c.name, c.version) for c in contexts] == [("bar", "v1"), ("foo", "v2")]


@pytest.mark.unit
async def test_list_contexts_is_tenant_scoped(storage) -> None:
    await storage.save_context(_make_context(name="foo", tenant_id="tenant-a"))
    await storage.save_context(_make_context(name="bar", tenant_id="tenant-b"))
    only_a = await storage.list_contexts(tenant_id="tenant-a")
    assert {c.name for c in only_a} == {"foo"}


@pytest.mark.unit
async def test_list_contexts_honors_limit(storage) -> None:
    base = datetime.now(UTC)
    for i in range(5):
        await storage.save_context(
            _make_context(name=f"ctx-{i}", created_at=base - timedelta(seconds=i))
        )
    assert len(await storage.list_contexts(tenant_id="tenant-a", limit=3)) == 3


@pytest.mark.unit
async def test_delete_one_context_version(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_context(_make_context(version="v1", created_at=base - timedelta(seconds=10)))
    await storage.save_context(_make_context(version="v2", created_at=base))
    assert await storage.delete_context("company-tone", tenant_id="tenant-a", version="v1") == 1
    assert await storage.get_context("company-tone", tenant_id="tenant-a", version="v1") is None
    latest = await storage.get_context("company-tone", tenant_id="tenant-a")
    assert latest is not None and latest.version == "v2"


@pytest.mark.unit
async def test_delete_all_context_versions(storage) -> None:
    base = datetime.now(UTC)
    await storage.save_context(_make_context(version="v1", created_at=base - timedelta(seconds=10)))
    await storage.save_context(_make_context(version="v2", created_at=base))
    assert await storage.delete_context("company-tone", tenant_id="tenant-a") == 2
    assert await storage.get_context("company-tone", tenant_id="tenant-a") is None
    assert await storage.list_context_versions("company-tone", tenant_id="tenant-a") == []


@pytest.mark.unit
async def test_delete_context_is_tenant_scoped(storage) -> None:
    await storage.save_context(_make_context(tenant_id="tenant-a"))
    assert await storage.delete_context("company-tone", tenant_id="tenant-b") == 0
    assert await storage.get_context("company-tone", tenant_id="tenant-a") is not None


@pytest.mark.unit
async def test_delete_missing_context_returns_zero(storage) -> None:
    assert await storage.delete_context("ghost", tenant_id="tenant-a") == 0
