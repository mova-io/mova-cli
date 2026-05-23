"""Tests for ``GET /api/v1/agents/{name}`` — agent profile detail view.

BACKLOG Group G item 56. Drives the Mova iO Angular agent-profile
page: the user clicks an agent in the catalog, the UI fetches this
single endpoint and renders the spec, prompt, schemas, dataset stats,
model config, marketplace metadata, and the canonical files list.

Coverage:

* **Happy path**: GET after POST returns the full detail view with
  all the fields the Angular profile page renders.
* **Marketplace fields** (item 29) round-trip: role / persona /
  capabilities / tags. Default-empty for pre-v0.8 agents.
* **Dataset stats**: when ``evals/dataset.jsonl`` exists, response
  carries case count + size + sha-prefix. When absent, ``dataset``
  is null (UI shows "no eval set configured").
* **Skills / contexts** lists round-trip.
* **Model fallback chain** renders as a list of provider strings.
* **Canonical files list** reflects what's actually on disk (no
  optional files = shorter list).
* **404** for unknown agent name.
* **401** unauthed.
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
# Fixtures + helpers (mirror the item-76 test file)
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


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="get-agent-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _agent_yaml(
    *,
    name: str = "demo",
    description: str = "A demo agent",
    role: str = "",
    persona: str = "",
    capabilities: tuple[str, ...] = (),
    owner: str = "",
    tags: tuple[str, ...] = (),
) -> bytes:
    """Build an agent.yaml with optional marketplace metadata. Keeps
    each test's setup focused on the variant it's actually testing."""
    extras = []
    if role:
        extras.append(f"role: {role}")
    if persona:
        extras.append(f"persona: {persona!r}")
    if capabilities:
        extras.append("capabilities:")
        extras.extend(f"  - {c}" for c in capabilities)
    if owner:
        extras.append(f"owner: {owner}")
    if tags:
        extras.append("tags:")
        extras.extend(f"  - {t}" for t in tags)
    extras_text = "\n".join(extras)
    if extras_text:
        extras_text += "\n"
    return (
        f"api_version: movate/v1\n"
        f"kind: Agent\n"
        f"name: {name}\n"
        f"version: 0.1.0\n"
        f"description: {description}\n"
        f"{extras_text}"
        f"model:\n"
        f"  provider: openai/gpt-4o-mini-2024-07-18\n"
        f"  params:\n"
        f"    temperature: 0.0\n"
        f"  fallback:\n"
        f"    - provider: anthropic/claude-haiku-4-5-20251001\n"
        f"prompt: ./prompt.md\n"
        f"schema:\n"
        f"  input: ./schema/input.json\n"
        f"  output: ./schema/output.json\n"
        f"evals:\n"
        f"  dataset: ./evals/dataset.jsonl\n"
    ).encode()


_PROMPT = b"You are demo. Answer: {{ input.text }}\n"

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
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
).encode("utf-8")

_DATASET = (
    b'{"input": {"text": "one"}, "expected": {"answer": "1"}}\n'
    b'{"input": {"text": "two"}, "expected": {"answer": "2"}}\n'
    b'{"input": {"text": "three"}, "expected": {"answer": "3"}}\n'
)


def _create_via_post(
    client: TestClient,
    auth_header: dict[str, str],
    *,
    agent_yaml: bytes | None = None,
    include_dataset: bool = True,
) -> None:
    """Use POST /api/v1/agents to land an agent in the registry —
    keeps the test setup honest (no direct filesystem fudging)."""
    yaml_bytes = agent_yaml if agent_yaml is not None else _agent_yaml()
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("agent_yaml", ("agent.yaml", yaml_bytes, "application/x-yaml")),
        ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
        ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
        ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
    ]
    if include_dataset:
        files.append(("dataset", ("dataset.jsonl", _DATASET, "application/jsonl")))
    r = client.post("/api/v1/agents", files=files, headers=auth_header)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Happy path — round-trip POST → GET
# ---------------------------------------------------------------------------


def test_get_returns_full_detail_view(client: TestClient, auth_header: dict[str, str]) -> None:
    """All the fields the Angular profile page reads are present in
    a single GET response."""
    _create_via_post(client, auth_header)
    r = client.get("/api/v1/agents/demo", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()

    # Identity
    assert body["name"] == "demo"
    assert body["version"] == "0.1.0"
    assert body["description"] == "A demo agent"

    # Model
    assert body["model_provider"] == "openai/gpt-4o-mini-2024-07-18"
    assert body["model_params"] == {"temperature": 0.0}
    assert body["model_fallback"] == ["anthropic/claude-haiku-4-5-20251001"]
    assert body["runtime"] == "litellm"

    # Prompt + hash
    assert body["prompt"].startswith("You are demo.")
    assert len(body["prompt_hash"]) == 64  # SHA-256 hex

    # Schemas
    assert body["input_schema"]["type"] == "object"
    assert body["output_schema"]["type"] == "object"

    # Operational
    assert body["timeout_call_ms"] > 0
    assert body["timeout_total_ms"] > 0
    assert body["max_cost_usd_per_run"] > 0

    # Files actually on disk
    assert "agent.yaml" in body["files"]
    assert "prompt.md" in body["files"]
    assert "schema/input.json" in body["files"]
    assert "schema/output.json" in body["files"]
    assert "evals/dataset.jsonl" in body["files"]
    assert body["agent_dir"] == "demo"


# ---------------------------------------------------------------------------
# Marketplace metadata (item 29 / Group F)
# ---------------------------------------------------------------------------


def test_get_includes_marketplace_metadata_when_populated(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Agents that opt into role / persona / capabilities surface
    those in the detail view for the Angular marketplace UI."""
    _create_via_post(
        client,
        auth_header,
        agent_yaml=_agent_yaml(
            role="support-triage",
            persona="Concise and technical; 1-2 line answers.",
            capabilities=("faq-lookup", "ticket-routing"),
            tags=("alpha", "demo"),
            owner="platform-team",
        ),
    )
    r = client.get("/api/v1/agents/demo", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "support-triage"
    assert body["persona"].startswith("Concise")
    assert sorted(body["capabilities"]) == ["faq-lookup", "ticket-routing"]
    assert sorted(body["tags"]) == ["alpha", "demo"]
    assert body["owner"] == "platform-team"


def test_get_marketplace_fields_default_empty(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Pre-v0.8 agents (no marketplace metadata in agent.yaml) get
    empty strings + empty lists — never null. Lets the Angular UI
    render the profile without null-checks on every field."""
    _create_via_post(client, auth_header)
    r = client.get("/api/v1/agents/demo", headers=auth_header)
    body = r.json()
    assert body["role"] == ""
    assert body["persona"] == ""
    assert body["capabilities"] == []
    assert body["tags"] == []
    assert body["owner"] == ""


# ---------------------------------------------------------------------------
# Dataset stats
# ---------------------------------------------------------------------------


def test_get_dataset_stats_populated_when_present(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    _create_via_post(client, auth_header)
    r = client.get("/api/v1/agents/demo", headers=auth_header)
    body = r.json()
    ds = body["dataset"]
    assert ds is not None
    assert ds["path"] == "./evals/dataset.jsonl"
    assert ds["case_count"] == 3  # _DATASET has 3 lines
    assert len(ds["sha256_prefix"]) == 12
    assert ds["size_bytes"] == len(_DATASET)


def test_get_dataset_null_when_absent(
    client: TestClient, auth_header: dict[str, str], agents_path: Path
) -> None:
    """An agent.yaml that doesn't declare ``evals.dataset`` — or
    declares one that doesn't exist on disk — surfaces ``dataset:
    null`` so the UI shows 'no eval set configured'."""
    # Create without the dataset field in agent.yaml at all
    yaml_no_dataset = (
        b"api_version: movate/v1\n"
        b"kind: Agent\n"
        b"name: demo-no-ds\n"
        b"version: 0.1.0\n"
        b"description: No dataset\n"
        b"model:\n"
        b"  provider: openai/gpt-4o-mini-2024-07-18\n"
        b"prompt: ./prompt.md\n"
        b"schema:\n"
        b"  input: ./schema/input.json\n"
        b"  output: ./schema/output.json\n"
    )
    _create_via_post(
        client,
        auth_header,
        agent_yaml=yaml_no_dataset,
        include_dataset=False,
    )
    r = client.get("/api/v1/agents/demo-no-ds", headers=auth_header)
    assert r.status_code == 200
    assert r.json()["dataset"] is None


# ---------------------------------------------------------------------------
# Canonical files list reflects disk reality
# ---------------------------------------------------------------------------


def test_get_files_list_excludes_optional_dataset_when_absent(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    yaml_no_dataset = (
        b"api_version: movate/v1\n"
        b"kind: Agent\n"
        b"name: lean\n"
        b"version: 0.1.0\n"
        b"description: lean\n"
        b"model:\n"
        b"  provider: openai/gpt-4o-mini-2024-07-18\n"
        b"prompt: ./prompt.md\n"
        b"schema:\n"
        b"  input: ./schema/input.json\n"
        b"  output: ./schema/output.json\n"
    )
    _create_via_post(
        client,
        auth_header,
        agent_yaml=yaml_no_dataset,
        include_dataset=False,
    )
    r = client.get("/api/v1/agents/lean", headers=auth_header)
    body = r.json()
    assert sorted(body["files"]) == [
        "agent.yaml",
        "prompt.md",
        "schema/input.json",
        "schema/output.json",
    ]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_get_nonexistent_agent_returns_404(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.get("/api/v1/agents/never-created", headers=auth_header)
    assert r.status_code == 404
    body = r.json()
    # Matches the existing not_found shape from other 404 paths.
    assert body["detail"]["error"]["code"] == "not_found"


def test_get_without_auth_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/agents/demo")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Zip-bundle round-trip — POST a zip, GET back the full detail
# ---------------------------------------------------------------------------


def test_get_after_zip_bundle_create_returns_detail(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """The two POST modes (individual files + zip bundle) should
    produce identical GET output. Guards against a regression where
    one mode persists differently than the other."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zippy/agent.yaml", _agent_yaml(name="zippy"))
        zf.writestr("zippy/prompt.md", _PROMPT)
        zf.writestr("zippy/schema/input.json", _INPUT_SCHEMA)
        zf.writestr("zippy/schema/output.json", _OUTPUT_SCHEMA)
        zf.writestr("zippy/evals/dataset.jsonl", _DATASET)
    r1 = client.post(
        "/api/v1/agents",
        files={"bundle": ("zippy.zip", buf.getvalue(), "application/zip")},
        headers=auth_header,
    )
    assert r1.status_code == 201

    r2 = client.get("/api/v1/agents/zippy", headers=auth_header)
    assert r2.status_code == 200
    body = r2.json()
    assert body["name"] == "zippy"
    assert body["dataset"]["case_count"] == 3
    assert body["files"] == [
        "agent.yaml",
        "evals/dataset.jsonl",
        "prompt.md",
        "schema/input.json",
        "schema/output.json",
    ]
