"""Tests for ``POST /api/v1/skills`` — skill registry creation.

The runtime previously had no way for customers to populate the skill
registry: ``mdk add rag-qa && mdk deploy`` would 422 with
"skills resolution failed: ... Available: (empty registry; add
skills/<name>/skill.yaml)". This PR closes the gap.

Coverage:

* Happy path persists ``skill.yaml`` (+ optional ``impl.py`` /
  ``corpus.json`` / ``README.md``) under ``<skills_path>/<name>/``.
* PUT semantics — re-uploading the same skill name overwrites.
* 422 on malformed bundle (bad YAML, missing required field, bundle
  that fails ``load_skill`` validation).
* 401 unauthed.
* 503 when the runtime was built without a ``skills_path``.
* End-to-end fix: after a skill upload, an agent that declares
  ``skills: [<name>]`` uploads cleanly (was 422 before).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
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
    # skills_path defaults to <agents_path>/skills inside build_app —
    # matches the agent loader's project-root fallback. Tests that
    # need a custom skills_path build their own app.
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="skills-v1-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Canonical skill bytes
# ---------------------------------------------------------------------------


_SKILL_YAML = b"""\
api_version: movate/v1
kind: Skill
name: web-search
version: 0.1.0
description: Searches the public web and returns top results.
schema:
  input:
    query: string
  output:
    result: string
implementation:
  kind: python
  entry: web_search.impl:run
cost:
  per_call_usd: 0.0
side_effects: network
"""

_IMPL_PY = b"async def run(payload): return {'result': payload['query']}\n"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_skill_minimal_payload_persists(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Just ``skill_yaml`` is the minimum upload. Persists the file
    under ``<skills_path>/<name>/skill.yaml`` and returns the resolved
    spec."""
    r = client.post(
        "/api/v1/skills",
        files=[("skill_yaml", ("skill.yaml", _SKILL_YAML, "application/x-yaml"))],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "web-search"
    assert body["skill_dir"] == "web-search"
    assert body["files_persisted"] == ["skill.yaml"]
    # Default skills_path is <agents_path>/skills.
    assert (agents_path / "skills" / "web-search" / "skill.yaml").exists()


def test_create_skill_with_impl_persists_all_files(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """impl.py + README ride along when supplied."""
    r = client.post(
        "/api/v1/skills",
        files=[
            ("skill_yaml", ("skill.yaml", _SKILL_YAML, "application/x-yaml")),
            ("impl", ("impl.py", _IMPL_PY, "text/x-python")),
            ("readme", ("README.md", b"# web-search\n", "text/markdown")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert sorted(body["files_persisted"]) == ["README.md", "impl.py", "skill.yaml"]
    skill_dir = agents_path / "skills" / "web-search"
    assert (skill_dir / "impl.py").read_bytes() == _IMPL_PY
    assert (skill_dir / "README.md").read_text() == "# web-search\n"


# ---------------------------------------------------------------------------
# PUT semantics — replace on conflict
# ---------------------------------------------------------------------------


def test_create_skill_twice_overwrites(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Re-uploading the same skill name must overwrite — agents reference
    skills by name and a re-deploy should follow."""
    r1 = client.post(
        "/api/v1/skills",
        files=[("skill_yaml", ("skill.yaml", _SKILL_YAML, "application/x-yaml"))],
        headers=auth_header,
    )
    assert r1.status_code == 201, r1.text

    # Second upload with an extra file — the previous (minimal) upload
    # should be replaced wholesale, not merged.
    r2 = client.post(
        "/api/v1/skills",
        files=[
            ("skill_yaml", ("skill.yaml", _SKILL_YAML, "application/x-yaml")),
            ("impl", ("impl.py", _IMPL_PY, "text/x-python")),
        ],
        headers=auth_header,
    )
    assert r2.status_code == 201, r2.text
    assert (agents_path / "skills" / "web-search" / "impl.py").exists()


# ---------------------------------------------------------------------------
# 422 — bundle validation failures
# ---------------------------------------------------------------------------


def test_create_skill_bad_yaml_returns_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/skills",
        files=[
            ("skill_yaml", ("skill.yaml", b"::: not valid yaml :::\n", "application/x-yaml")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "invalid_bundle"


def test_create_skill_missing_name_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """skill.yaml without ``name:`` can't address a target dir."""
    yaml_no_name = b"api_version: movate/v1\nkind: Skill\nversion: 0.1.0\n"
    r = client.post(
        "/api/v1/skills",
        files=[("skill_yaml", ("skill.yaml", yaml_no_name, "application/x-yaml"))],
        headers=auth_header,
    )
    assert r.status_code == 422


def test_create_skill_spec_validation_failure_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """SkillSpec rejects bundles missing required fields — surfaced as
    422 here instead of as a downstream agent-load failure later."""
    yaml_incomplete = b"api_version: movate/v1\nkind: Skill\nname: broken\n"
    r = client.post(
        "/api/v1/skills",
        files=[("skill_yaml", ("skill.yaml", yaml_incomplete, "application/x-yaml"))],
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 401 unauthed
# ---------------------------------------------------------------------------


def test_create_skill_unauthed_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/skills",
        files=[("skill_yaml", ("skill.yaml", _SKILL_YAML, "application/x-yaml"))],
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 503 — runtime built without skills_path
# ---------------------------------------------------------------------------


async def test_create_skill_without_skills_path_returns_503() -> None:
    """A runtime built without agents_path AND skills_path can't
    persist anywhere — surface as 503 so operators know to (re-)serve
    with --agents-path."""
    s = InMemoryStorage()
    await s.init()
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="no-skills-path", scopes=list(ALL_SCOPES)
    )
    await s.save_api_key(minted.record)

    app = build_app(s)  # no agents_path, no skills_path
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/skills",
            files=[("skill_yaml", ("skill.yaml", _SKILL_YAML, "application/x-yaml"))],
            headers={"Authorization": f"Bearer {minted.full_key}"},
        )
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# End-to-end: skill upload then agent upload that references it
# ---------------------------------------------------------------------------


def test_agent_with_skill_uploads_after_skill_is_registered(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Repro of the original bug. Pre-fix: agent upload 422'd with
    "skills resolution failed: ... (empty registry)". Post-fix: skill
    upload succeeds, then the agent upload also succeeds."""
    # 1. Upload the skill first.
    r_skill = client.post(
        "/api/v1/skills",
        files=[("skill_yaml", ("skill.yaml", _SKILL_YAML, "application/x-yaml"))],
        headers=auth_header,
    )
    assert r_skill.status_code == 201, r_skill.text

    # 2. Now upload an agent that references the skill by name.
    agent_yaml = b"""\
api_version: movate/v1
kind: Agent
name: rag-qa
version: 0.1.0
description: RAG agent that may call web-search.
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input:
    query: string
  output:
    answer: string
skills:
  - web-search
"""
    prompt = b"Answer: {{ input.query }}\n"

    r_agent = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", agent_yaml, "application/x-yaml")),
            ("prompt", ("prompt.md", prompt, "text/markdown")),
            (
                "input_schema",
                (
                    "input.json",
                    b'{"type":"object","properties":{"query":{"type":"string"}}}',
                    "application/json",
                ),
            ),
            (
                "output_schema",
                (
                    "output.json",
                    b'{"type":"object","properties":{"answer":{"type":"string"}}}',
                    "application/json",
                ),
            ),
        ],
        headers=auth_header,
    )
    assert r_agent.status_code == 201, r_agent.text
