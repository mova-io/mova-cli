"""ADR 060 D4 — runtime resolves an agent's skill/context refs from the managed
store (``hydrate_agent_resources``).

After #650 an agent can be *attached* to a registry skill/context (the ref is
recorded in its ``agent.yaml``) without shipping the files. D4 makes a deployed
agent actually resolve those refs at load time: the runtime pulls them from the
tenant-scoped store into the materialized dir, where the existing filesystem
resolver finds them. The bundle stays the floor (D6) — a shipped resource is
never overwritten.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from movate.core.models import ContextRecord, SkillRecord
from movate.runtime.agent_resolver import hydrate_agent_resources
from movate.testing import InMemoryStorage

TENANT = "tenant-a"


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def _store_with_resources(*, tenant: str = TENANT) -> InMemoryStorage:
    storage = InMemoryStorage()
    await storage.init()
    skill_files = {
        "skill.yaml": "api_version: movate/v1\nkind: Skill\nname: web-search\nversion: 1.0.0\n",
        "impl.py": "def run(args):\n    return {}\n",
    }
    await storage.save_skill(
        SkillRecord(
            name="web-search",
            tenant_id=tenant,
            version="1.0.0",
            content_hash=_h(str(skill_files)),
            description="search",
            files=skill_files,
        )
    )
    body = "# Tone\nBe concise and cite sources.\n"
    await storage.save_context(
        ContextRecord(
            name="tone",
            tenant_id=tenant,
            version="v1",
            content_hash=_h(body),
            description="house voice",
            body=body,
        )
    )
    return storage


def _agent_dir(tmp_path: Path, *, skills: list, contexts: list) -> Path:
    d = tmp_path / "agent"
    d.mkdir()
    (d / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "faq",
                "version": "0.1.0",
                "skills": skills,
                "contexts": contexts,
            },
            sort_keys=False,
        )
    )
    return d


async def test_hydrates_attached_skill_and_context(tmp_path: Path) -> None:
    """A ref present in agent.yaml but NOT shipped on disk is pulled from the
    store into the materialized dir, and the project marker is dropped so the
    loader's project-root walk resolves it."""
    storage = await _store_with_resources()
    agent_dir = _agent_dir(tmp_path, skills=["web-search"], contexts=["tone"])

    n = await hydrate_agent_resources(storage, agent_dir, tenant_id=TENANT)

    assert n == 2
    assert (agent_dir / "skills" / "web-search" / "skill.yaml").is_file()
    assert (agent_dir / "skills" / "web-search" / "impl.py").is_file()
    assert (agent_dir / "contexts" / "tone.md").read_text().startswith("# Tone")
    # project marker dropped so load_agent resolves skills/ + contexts/ here.
    assert (agent_dir / "project.yaml").is_file()


async def test_skillref_dict_form_resolves(tmp_path: Path) -> None:
    """``skills:`` entries in the ``{name, version}`` SkillRef form resolve by
    name (the version is a constraint, checked later against the fetched skill)."""
    storage = await _store_with_resources()
    agent_dir = _agent_dir(
        tmp_path, skills=[{"name": "web-search", "version": "^1.0"}], contexts=[]
    )

    n = await hydrate_agent_resources(storage, agent_dir, tenant_id=TENANT)

    assert n == 1
    assert (agent_dir / "skills" / "web-search" / "skill.yaml").is_file()


async def test_bundle_shipped_resource_wins(tmp_path: Path) -> None:
    """D6: a skill/context already on disk (shipped in the bundle) is NEVER
    overwritten by the store — author-locally bundles resolve unchanged."""
    storage = await _store_with_resources()
    agent_dir = _agent_dir(tmp_path, skills=["web-search"], contexts=["tone"])
    # Pre-ship both resources with sentinel content.
    shipped_skill = agent_dir / "skills" / "web-search"
    shipped_skill.mkdir(parents=True)
    (shipped_skill / "skill.yaml").write_text("SHIPPED-SKILL\n")
    (agent_dir / "contexts").mkdir()
    (agent_dir / "contexts" / "tone.md").write_text("SHIPPED-CONTEXT\n")

    n = await hydrate_agent_resources(storage, agent_dir, tenant_id=TENANT)

    assert n == 0  # nothing hydrated — both were shipped
    assert (shipped_skill / "skill.yaml").read_text() == "SHIPPED-SKILL\n"
    assert (agent_dir / "contexts" / "tone.md").read_text() == "SHIPPED-CONTEXT\n"


async def test_missing_store_ref_is_safe_noop(tmp_path: Path) -> None:
    """A ref that is neither shipped nor in the store is skipped (not hydrated,
    no raise) — the loader later raises its own 'not found' diagnostic."""
    storage = await _store_with_resources()
    agent_dir = _agent_dir(tmp_path, skills=["nonexistent"], contexts=["also-missing"])

    n = await hydrate_agent_resources(storage, agent_dir, tenant_id=TENANT)

    assert n == 0
    assert not (agent_dir / "skills").exists()


async def test_tenant_scoped(tmp_path: Path) -> None:
    """A resource owned by another tenant is invisible — not hydrated."""
    storage = await _store_with_resources(tenant="tenant-b")  # resources under B
    agent_dir = _agent_dir(tmp_path, skills=["web-search"], contexts=["tone"])

    n = await hydrate_agent_resources(storage, agent_dir, tenant_id=TENANT)  # ask as A

    assert n == 0


async def test_no_refs_is_noop(tmp_path: Path) -> None:
    """An agent with no skill/context refs hydrates nothing and drops no marker."""
    storage = await _store_with_resources()
    agent_dir = _agent_dir(tmp_path, skills=[], contexts=[])

    n = await hydrate_agent_resources(storage, agent_dir, tenant_id=TENANT)

    assert n == 0
    assert not (agent_dir / "project.yaml").is_file()


async def test_backend_without_registry_methods_is_noop(tmp_path: Path) -> None:
    """A storage backend lacking get_skill/get_context degrades to a no-op."""

    class _BareStore:
        pass

    agent_dir = _agent_dir(tmp_path, skills=["web-search"], contexts=["tone"])
    n = await hydrate_agent_resources(_BareStore(), agent_dir, tenant_id=TENANT)  # type: ignore[arg-type]
    assert n == 0
