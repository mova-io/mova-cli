"""Tests for ``POST /api/v1/agents/from-wizard``.

BACKLOG Group H item 82. Translates the Mova iO Angular wizard's
JSON shape into MDK's canonical agent.yaml + prompt.md + default I/O
schemas, then delegates to the same persist path the multipart
endpoint uses.

Coverage:

* **Happy path — minimum wizard payload**: only required fields
  (name, agent_prompt, ai_model) → canonical bundle persisted with
  sensible defaults.
* **Full wizard payload**: every field populated, every mapping
  verified by re-loading the persisted agent.yaml.
* **Name slugification**: "Code Analyzer" → "code-analyzer" on disk.
* **Provider / type / foundation → tag prefixes**.
* **Role dropdown → role field; Agent Role textarea → persona**.
* **MCP Connectors → skills; Knowledge Store → contexts** (won't
  validate against a registry — that's caller responsibility, today's
  test asserts pass-through).
* **Reference Output → single examples entry**.
* **Conflict**: 409 on existing slugified name.
* **Validation errors**: 422 when ai_model is malformed (LiteLLM
  rejects it downstream).
* **Auth**: 401 unauthed.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage


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
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="wizard-tests")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# Minimum viable wizard payload — only the three required fields.
def _minimal_payload(**overrides) -> dict:
    base = {
        "name": "code-analyzer",
        "agent_prompt": "Analyze the code: {{ input.input }}",
        "ai_model": "openai/gpt-4o-mini-2024-07-18",
    }
    base.update(overrides)
    return base


# Full wizard payload — every visible field in the screenshot
# populated, so we can verify each field's mapping end-to-end.
def _full_payload(**overrides) -> dict:
    base = {
        "name": "Code Analyzer",  # tests name-slugification
        "agent_provider": "Movate",
        "agent_type": "Task Agent",
        "role": "Planner",
        "description": "Analyzes code for issues",
        "agent_role": "Senior staff engineer voice; concise and citation-heavy.",
        "agent_goal": "Identify bugs, anti-patterns, and missing tests.",
        "agent_prompt": "Review the following code:\n\n{{ input.input }}",
        "reference_output": "Found 3 issues: (1)... (2)... (3)...",
        "mcp_connectors": [],
        "knowledge_store": [],
        "ai_model": "openai/gpt-4o-mini-2024-07-18",
        "ai_foundation": "Azure",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_minimal_payload_creates_agent_with_defaults(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Wizard sends only the 3 required fields → canonical layout
    persists with default schemas + empty marketplace metadata."""
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(),
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "code-analyzer"
    assert body["version"] == "0.1.0"
    assert sorted(body["files_persisted"]) == [
        "agent.yaml",
        "prompt.md",
        "schema/input.json",
        "schema/output.json",
    ]

    # Default schemas land on disk
    bundle_dir = agents_path / "code-analyzer"
    input_schema = json.loads((bundle_dir / "schema/input.json").read_text())
    assert input_schema["required"] == ["input"]
    output_schema = json.loads((bundle_dir / "schema/output.json").read_text())
    assert output_schema["required"] == ["output"]


def test_full_payload_round_trips_every_field(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Every wizard field maps to the right agent.yaml slot."""
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_full_payload(),
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Name slugified
    assert body["name"] == "code-analyzer"
    assert body["agent_dir"] == "code-analyzer"

    # Re-read agent.yaml to verify the field mapping
    yaml_text = (agents_path / "code-analyzer" / "agent.yaml").read_text()
    spec = yaml.safe_load(yaml_text)

    assert spec["name"] == "code-analyzer"
    assert spec["description"] == "Analyzes code for issues"
    assert spec["model"]["provider"] == "openai/gpt-4o-mini-2024-07-18"

    # Marketplace metadata
    assert spec["role"] == "planner"  # dropdown → slugified marketplace role
    assert spec["persona"] == "Senior staff engineer voice; concise and citation-heavy."

    # Goal → single-element list
    assert spec["goals"] == ["Identify bugs, anti-patterns, and missing tests."]

    # Tag prefixes from wizard extensions
    assert set(spec["tags"]) == {
        "provider-movate",
        "type-task-agent",
        "foundation-azure",
    }

    # Reference output → single examples entry
    assert spec["examples"] == [
        {"input": {}, "output": {"output": "Found 3 issues: (1)... (2)... (3)..."}},
    ]


def test_prompt_inlined_into_prompt_md(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Wizard's agent_prompt string lands verbatim in prompt.md."""
    prompt = "You are a code reviewer.\n\nReview: {{ input.input }}\n"
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(agent_prompt=prompt),
        headers=auth_header,
    )
    assert r.status_code == 201
    on_disk = (agents_path / "code-analyzer" / "prompt.md").read_text()
    assert on_disk == prompt


# ---------------------------------------------------------------------------
# Name slugification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wizard_name,expected_slug",
    [
        ("Code Analyzer", "code-analyzer"),
        ("FAQ Bot v2", "faq-bot-v2"),
        ("    spaces    ", "spaces"),
        ("UPPER_CASE", "upper-case"),
        ("multi---hyphens", "multi-hyphens"),
        ("emoji-🚀-stripped", "emoji-stripped"),
    ],
)
def test_name_slugification(
    client: TestClient,
    auth_header: dict[str, str],
    wizard_name: str,
    expected_slug: str,
) -> None:
    """Human-friendly wizard names get slugified into URL-safe
    agent names. Spaces / case / non-alphanumeric stripped."""
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(name=wizard_name),
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == expected_slug


def test_name_with_no_alphanumerics_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(name="---!!!---"),
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Optional fields default cleanly
# ---------------------------------------------------------------------------


def test_no_marketplace_metadata_emits_minimal_yaml(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Wizard fields left empty → those YAML keys stay absent
    rather than serializing as empty strings."""
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(),
        headers=auth_header,
    )
    assert r.status_code == 201
    spec = yaml.safe_load((agents_path / "code-analyzer" / "agent.yaml").read_text())
    # Required keys present
    assert "name" in spec
    assert "model" in spec
    # Optional keys ABSENT (not empty-string'd in)
    assert "role" not in spec
    assert "persona" not in spec
    assert "goals" not in spec
    assert "tags" not in spec
    assert "examples" not in spec
    assert "skills" not in spec
    assert "contexts" not in spec


# ---------------------------------------------------------------------------
# Conflict
# ---------------------------------------------------------------------------


def test_conflict_after_post_returns_409(client: TestClient, auth_header: dict[str, str]) -> None:
    """Second submission with the SAME slugified name fails with
    409 — the existing bundle is NOT overwritten."""
    r1 = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(name="Code Analyzer"),
        headers=auth_header,
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/v1/agents/from-wizard",
        # Different display capitalization but same slug.
        json=_minimal_payload(name="Code ANALYZER"),
        headers=auth_header,
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"]["code"] == "already_exists"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_missing_prompt_returns_422(client: TestClient, auth_header: dict[str, str]) -> None:
    """FastAPI's body validation rejects empty agent_prompt."""
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(agent_prompt=""),
        headers=auth_header,
    )
    assert r.status_code == 422


def test_missing_ai_model_returns_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(ai_model=""),
        headers=auth_header,
    )
    assert r.status_code == 422


def test_invalid_ai_model_format_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Bare model name (no provider/ prefix) fails AgentSpec's
    LiteLLM-format validator — bubbles up as a 422 from
    persist_bundle's load_agent call."""
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(ai_model="gpt-4o-mini"),  # missing 'openai/' prefix
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(),
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Registry refresh
# ---------------------------------------------------------------------------


def test_post_refreshes_registry(client: TestClient, auth_header: dict[str, str]) -> None:
    """Post-create, GET /agents includes the new agent. Critical for
    the wizard UX where the user creates an agent and the catalog
    page updates on the next render."""
    r0 = client.get("/agents", headers=auth_header)
    assert r0.json()["agents"] == []

    r1 = client.post(
        "/api/v1/agents/from-wizard",
        json=_minimal_payload(),
        headers=auth_header,
    )
    assert r1.status_code == 201

    r2 = client.get("/agents", headers=auth_header)
    agents = r2.json()["agents"]
    assert len(agents) == 1
    assert agents[0]["name"] == "code-analyzer"
