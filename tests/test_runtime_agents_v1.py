"""Tests for ``POST /api/v1/agents`` — agent creation with canonical layout.

BACKLOG Group G item 76. Foundation of Pillar 1 (agent creation +
GitHub version control) for the Friday Mova iO Angular deliverable.

Coverage:

* **Happy path — individual files**: agent_yaml + prompt + schemas +
  optional dataset arrive as separate multipart fields, persist to
  ``<agents_path>/<name>/`` in the canonical layout, response includes
  the resolved spec.
* **Happy path — zipped bundle**: single ``bundle`` multipart field
  containing the canonical layout (including the common case where
  the zip has a single top-level dir like ``faq-bot/...``).
* **Two-mode contract**: 400 on neither mode, 400 on both modes
  supplied together.
* **409 on conflict**: posting an existing name fails fast WITHOUT
  writing to disk (no partial state).
* **422 on malformed bundle**: invalid YAML, missing required files,
  unknown files outside the canonical layout, invalid agent.yaml
  content (validator failure).
* **401 unauthed**: no bearer token → 401, like every other authed
  endpoint.
* **Registry refresh**: a successful POST is followed by GET /agents
  returning the new agent (the in-memory registry is updated).
"""

from __future__ import annotations

import io
import json
import zipfile
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
    """Per-test agents directory. Fresh tmp_path means no cross-test
    bleed of persisted bundles."""
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    """Mint a fresh API key + return the bearer header. Tenant id is
    new per test so cross-test scoping is hermetic."""
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="agents-v1-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Canonical bundle helpers
# ---------------------------------------------------------------------------


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: demo
version: 0.1.0
description: A demo agent created via POST /api/v1/agents
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Hello, {{ input.text }}!\n"

_INPUT_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
).encode("utf-8")

_OUTPUT_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"greeting": {"type": "string"}},
        "required": ["greeting"],
    }
).encode("utf-8")

_DATASET = b'{"input": {"text": "world"}, "expected": {"greeting": "Hello, world!"}}\n'


def _individual_files_form(
    agent_yaml: bytes = _AGENT_YAML,
    prompt: bytes = _PROMPT,
    input_schema: bytes = _INPUT_SCHEMA,
    output_schema: bytes = _OUTPUT_SCHEMA,
    dataset: bytes | None = _DATASET,
) -> list[tuple[str, tuple[str, bytes, str]]]:
    """Build the requests `files=` payload for the individual-files mode.

    Returns the list-of-tuples shape requests expects so multipart
    parsing works correctly even when fields share a name (which we
    don't here, but the shape is right for any future expansion).
    """
    payload: list[tuple[str, tuple[str, bytes, str]]] = [
        ("agent_yaml", ("agent.yaml", agent_yaml, "application/x-yaml")),
        ("prompt", ("prompt.md", prompt, "text/markdown")),
        ("input_schema", ("input.json", input_schema, "application/json")),
        ("output_schema", ("output.json", output_schema, "application/json")),
    ]
    if dataset is not None:
        payload.append(("dataset", ("dataset.jsonl", dataset, "application/jsonl")))
    return payload


def _zipped_bundle(
    *,
    prefix: str = "",
    include_dataset: bool = True,
    extra_entries: dict[str, bytes] | None = None,
) -> bytes:
    """Build a zip of the canonical bundle, optionally with a leading
    directory prefix (mimics ``zip -r faq-bot.zip faq-bot/``).
    """
    buf = io.BytesIO()
    pre = f"{prefix}/" if prefix else ""
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{pre}agent.yaml", _AGENT_YAML)
        zf.writestr(f"{pre}prompt.md", _PROMPT)
        zf.writestr(f"{pre}schema/input.json", _INPUT_SCHEMA)
        zf.writestr(f"{pre}schema/output.json", _OUTPUT_SCHEMA)
        if include_dataset:
            zf.writestr(f"{pre}evals/dataset.jsonl", _DATASET)
        for path, content in (extra_entries or {}).items():
            zf.writestr(f"{pre}{path}", content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Happy path — individual files
# ---------------------------------------------------------------------------


def test_create_individual_files_persists_canonical_layout(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """The canonical 4 required files + optional dataset land in the
    expected disk layout, and the response carries the resolved spec."""
    r = client.post(
        "/api/v1/agents",
        files=_individual_files_form(),
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "demo"
    assert body["version"] == "0.1.0"
    assert body["agent_dir"] == "demo"
    assert body["description"].startswith("A demo agent")
    assert sorted(body["files_persisted"]) == [
        "agent.yaml",
        "evals/dataset.jsonl",
        "prompt.md",
        "schema/input.json",
        "schema/output.json",
    ]
    # Files actually exist on disk under the canonical layout.
    bundle_dir = agents_path / "demo"
    assert (bundle_dir / "agent.yaml").exists()
    assert (bundle_dir / "prompt.md").exists()
    assert (bundle_dir / "schema/input.json").exists()
    assert (bundle_dir / "schema/output.json").exists()
    assert (bundle_dir / "evals/dataset.jsonl").exists()


def test_create_individual_files_without_dataset_succeeds(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Dataset is optional — agents that haven't built one yet still
    persist cleanly."""
    r = client.post(
        "/api/v1/agents",
        files=_individual_files_form(dataset=None),
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "evals/dataset.jsonl" not in body["files_persisted"]
    assert not (agents_path / "demo" / "evals" / "dataset.jsonl").exists()


# ---------------------------------------------------------------------------
# Happy path — zipped bundle
# ---------------------------------------------------------------------------


def test_create_zip_bundle_persists_canonical_layout(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """A flat zip (no top-level prefix dir) round-trips cleanly."""
    zip_bytes = _zipped_bundle()
    r = client.post(
        "/api/v1/agents",
        files={"bundle": ("demo.zip", zip_bytes, "application/zip")},
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    assert (agents_path / "demo" / "agent.yaml").exists()
    assert (agents_path / "demo" / "evals/dataset.jsonl").exists()


def test_create_zip_bundle_with_top_level_dir_strips_prefix(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """``zip -r demo.zip demo/`` puts everything under a ``demo/``
    prefix. The unzipper strips a single common prefix so the
    canonical layout still validates."""
    zip_bytes = _zipped_bundle(prefix="demo")
    r = client.post(
        "/api/v1/agents",
        files={"bundle": ("demo.zip", zip_bytes, "application/zip")},
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    # Final on-disk path matches the agent's NAME, not the zip's
    # prefix (operators can name the zip whatever they want).
    assert (agents_path / "demo" / "agent.yaml").exists()


def test_create_zip_bundle_with_skills_subdir_routes_to_global_registry(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Bundles that ship a reference ``skills/<name>/`` folder
    (what `mdk init` produces) must upload cleanly AND register the
    skill in the GLOBAL skill registry — not as documentary detritus
    inside the agent dir. Without this, the next agent that declares
    ``skills: [<name>]`` would 422 with "empty registry".
    """
    full_skill_yaml = (
        b"api_version: movate/v1\n"
        b"kind: Skill\n"
        b"name: example-skill\n"
        b"version: 0.1.0\n"
        b"description: example skill bundled with the agent\n"
        b"schema:\n"
        b"  input:\n"
        b"    q: string\n"
        b"  output:\n"
        b"    r: string\n"
        b"implementation:\n"
        b"  kind: python\n"
        b"  entry: example_skill.impl:run\n"
    )
    zip_bytes = _zipped_bundle(
        extra_entries={
            "skills/example-skill/skill.yaml": full_skill_yaml,
            "skills/example-skill/README.md": b"# example\n",
        },
    )
    r = client.post(
        "/api/v1/agents",
        files={"bundle": ("demo.zip", zip_bytes, "application/zip")},
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    # Skill files land in the GLOBAL skill registry, NOT inside the
    # agent dir. This is what makes a follow-up agent upload that
    # declares `skills: [example-skill]` resolve.
    assert (agents_path / "skills" / "example-skill" / "skill.yaml").exists()
    assert (agents_path / "skills" / "example-skill" / "README.md").exists()
    # Conversely, the agent dir no longer carries the skill files
    # (no per-agent duplication).
    assert not (agents_path / "demo" / "skills").exists()


# ---------------------------------------------------------------------------
# Two-mode contract — 400 errors
# ---------------------------------------------------------------------------


def test_create_with_no_files_returns_400(client: TestClient, auth_header: dict[str, str]) -> None:
    """Empty multipart form — the user forgot to attach anything."""
    r = client.post("/api/v1/agents", files={}, headers=auth_header)
    # FastAPI returns 422 for unsupported media type before reaching
    # our handler when no `files=` is supplied. Either a 400 (our
    # handler reaches the "no files" branch) or 422 (FastAPI's form
    # validation) is correct — the wire contract is "non-2xx".
    assert r.status_code in (400, 422)


def test_create_with_both_modes_returns_400(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Mixing a zipped bundle with individual files is ambiguous —
    reject early with a clear error."""
    zip_bytes = _zipped_bundle()
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        *_individual_files_form(),
        ("bundle", ("demo.zip", zip_bytes, "application/zip")),
    ]
    r = client.post("/api/v1/agents", files=files, headers=auth_header)
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error"]["code"] == "bad_request"


def test_create_with_partial_individual_files_returns_400(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Sending only agent_yaml + prompt (missing schemas) violates the
    canonical layout contract."""
    files = [
        ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
        ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
    ]
    r = client.post("/api/v1/agents", files=files, headers=auth_header)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Conflict (409) — agent name already exists
# ---------------------------------------------------------------------------


def test_create_conflict_returns_409(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Second POST with the same name must fail; the existing bundle
    is NOT overwritten."""
    r1 = client.post(
        "/api/v1/agents",
        files=_individual_files_form(),
        headers=auth_header,
    )
    assert r1.status_code == 201
    # Capture the first version's prompt content so we can verify the
    # second POST didn't clobber it.
    prompt_path = agents_path / "demo" / "prompt.md"
    original_prompt = prompt_path.read_bytes()

    r2 = client.post(
        "/api/v1/agents",
        files=_individual_files_form(prompt=b"OVERWRITTEN -- should not land\n"),
        headers=auth_header,
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"]["error"]["code"] == "already_exists"
    # File on disk is unchanged.
    assert prompt_path.read_bytes() == original_prompt


# ---------------------------------------------------------------------------
# Invalid bundle (422)
# ---------------------------------------------------------------------------


def test_create_with_unparseable_yaml_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/agents",
        files=_individual_files_form(agent_yaml=b"::: not valid yaml :::"),
        headers=auth_header,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "invalid_bundle"


def test_create_with_yaml_missing_name_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """A YAML without `name` can't be persisted (no target dir)."""
    bad_yaml = _AGENT_YAML.replace(b"name: demo\n", b"")
    r = client.post(
        "/api/v1/agents",
        files=_individual_files_form(agent_yaml=bad_yaml),
        headers=auth_header,
    )
    assert r.status_code == 422


def test_create_with_extra_files_in_zip_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """A zip containing files outside the canonical layout (e.g.
    ``README.md`` at the bundle root) is rejected — we don't write
    files we don't recognize."""
    zip_bytes = _zipped_bundle(
        extra_entries={"README.md": b"# Demo agent"},
    )
    r = client.post(
        "/api/v1/agents",
        files={"bundle": ("demo.zip", zip_bytes, "application/zip")},
        headers=auth_header,
    )
    assert r.status_code == 422
    assert "canonical layout" in r.json()["detail"]["error"]["message"].lower()


def test_create_with_invalid_zip_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/agents",
        files={"bundle": ("demo.zip", b"not a zip", "application/zip")},
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_create_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents",
        files=_individual_files_form(),
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Runtime built without agents_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_without_agents_path_returns_503() -> None:
    """Defensive: if the runtime was built without an ``agents_path``
    (test misconfiguration or a deploy that didn't pass it), the
    endpoint reports unavailable rather than 500-ing with a confusing
    AttributeError."""
    storage = InMemoryStorage()
    await storage.init()
    # No agents_path kwarg — simulates a misconfigured deployment.
    client = TestClient(build_app(storage))
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="no-agents-path", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)

    r = client.post(
        "/api/v1/agents",
        files=_individual_files_form(),
        headers={"Authorization": f"Bearer {minted.full_key}"},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["code"] == "agent_persistence_unavailable"


# ---------------------------------------------------------------------------
# Registry refresh — successful POST surfaces in GET /agents
# ---------------------------------------------------------------------------


def test_create_refreshes_registry_for_get_agents(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """After POSTing a new agent, the existing ``GET /agents``
    endpoint should immediately return it — the in-memory registry is
    refreshed by the handler. Critical for Angular UX: user creates an
    agent, then the list view shows it on the very next render
    without a server restart."""
    # Pre-condition: registry empty.
    r0 = client.get("/agents", headers=auth_header)
    assert r0.status_code == 200
    assert r0.json()["agents"] == []

    # Create.
    r1 = client.post(
        "/api/v1/agents",
        files=_individual_files_form(),
        headers=auth_header,
    )
    assert r1.status_code == 201

    # Post-condition: registry includes the new agent.
    r2 = client.get("/agents", headers=auth_header)
    assert r2.status_code == 200
    agents = r2.json()["agents"]
    assert len(agents) == 1
    assert agents[0]["name"] == "demo"
    assert agents[0]["version"] == "0.1.0"
