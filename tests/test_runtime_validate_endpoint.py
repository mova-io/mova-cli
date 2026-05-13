"""Tests for ``POST /api/v1/agents/{name}/validate``.

BACKLOG Group G item 58. Drives the Mova iO Angular "is this agent
shippable?" gate. Wraps the existing `mdk validate` machinery
(prompt linter + cost forecast) and surfaces it as a structured
JSON response.

Coverage:

* **Happy path — clean bundle**: zero errors + warnings, `passed: True`,
  cost_forecast populated when the agent has a dataset.
* **Linter warnings surface**: prompts that trigger lint warnings
  (e.g. no JSON instruction) come back in `warnings[]` but
  `passed` stays True.
* **Cost forecast null when no dataset**: forecast field is
  `None` so UI can skip the cost chip.
* **404** on unknown agent.
* **401** unauthed.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
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
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="validate-endpoint-tests",
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# Clean agent — mentions output schema fields + has JSON instruction
# so it passes all four lint warnings (the same bundle the docs show).
_CLEAN_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: clean-demo
version: 0.1.0
description: A linter-clean demo agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
evals:
  dataset: ./evals/dataset.jsonl
"""

_CLEAN_PROMPT = b"""\
Respond with valid JSON containing an `answer` field.

User said: {{ input.text }}
"""

_INPUT_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
).encode()

_DATASET = (
    b'{"input": {"text": "hi"}, "expected": {"answer": "hello"}}\n'
    b'{"input": {"text": "bye"}, "expected": {"answer": "goodbye"}}\n'
)


def _create(
    client: TestClient,
    auth_header: dict[str, str],
    *,
    agent_yaml: bytes = _CLEAN_AGENT_YAML,
    prompt: bytes = _CLEAN_PROMPT,
    include_dataset: bool = True,
) -> None:
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("agent_yaml", ("agent.yaml", agent_yaml, "application/x-yaml")),
        ("prompt", ("prompt.md", prompt, "text/markdown")),
        ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
        ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
    ]
    if include_dataset:
        files.append(("dataset", ("dataset.jsonl", _DATASET, "application/jsonl")))
    r = client.post("/api/v1/agents", files=files, headers=auth_header)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_validate_clean_agent_passes(client: TestClient, auth_header: dict[str, str]) -> None:
    """A linter-clean agent returns ``passed: True``, no errors,
    no warnings."""
    _create(client, auth_header)
    r = client.post("/api/v1/agents/clean-demo/validate", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True
    assert body["errors"] == []
    assert body["warnings"] == []


def test_validate_response_shape(client: TestClient, auth_header: dict[str, str]) -> None:
    """Response always has these four keys, regardless of pass/fail."""
    _create(client, auth_header)
    r = client.post("/api/v1/agents/clean-demo/validate", headers=auth_header)
    body = r.json()
    assert set(body.keys()) == {"passed", "errors", "warnings", "cost_forecast"}


# ---------------------------------------------------------------------------
# Cost forecast
# ---------------------------------------------------------------------------


def test_validate_includes_cost_forecast_when_dataset_present(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """The forecast surfaces alongside validate output so the UI can
    show "this eval will cost ~$X" before the user clicks Run Eval."""
    _create(client, auth_header)
    r = client.post("/api/v1/agents/clean-demo/validate", headers=auth_header)
    body = r.json()
    forecast = body["cost_forecast"]
    assert forecast is not None
    assert forecast["model_provider"] == "openai/gpt-4o-mini-2024-07-18"
    assert forecast["cases"] == 2  # _DATASET has 2 lines
    assert forecast["input_tokens_per_call"] > 0
    assert forecast["output_tokens_per_call"] > 0
    assert forecast["cost_per_call_usd"] > 0
    assert forecast["total_cost_usd"] >= forecast["cost_per_call_usd"]


def test_validate_cost_forecast_null_when_no_dataset(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """No dataset → no forecast. UI hides the cost chip."""
    yaml_no_ds = (
        b"api_version: movate/v1\n"
        b"kind: Agent\n"
        b"name: no-ds\n"
        b"version: 0.1.0\n"
        b"description: no dataset\n"
        b"model:\n"
        b"  provider: openai/gpt-4o-mini-2024-07-18\n"
        b"prompt: ./prompt.md\n"
        b"schema:\n"
        b"  input: ./schema/input.json\n"
        b"  output: ./schema/output.json\n"
    )
    _create(client, auth_header, agent_yaml=yaml_no_ds, include_dataset=False)
    r = client.post("/api/v1/agents/no-ds/validate", headers=auth_header)
    body = r.json()
    assert body["cost_forecast"] is None


# ---------------------------------------------------------------------------
# Linter warnings
# ---------------------------------------------------------------------------


def test_validate_surfaces_warning_when_prompt_lacks_json_instruction(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """A prompt with no "JSON" / "json" word triggers
    MISSING_JSON_INSTRUCTION warning."""
    sparse_prompt = b"Say hello to {{ input.text }}\n"
    _create(client, auth_header, prompt=sparse_prompt)
    r = client.post("/api/v1/agents/clean-demo/validate", headers=auth_header)
    body = r.json()
    # Warning surfaces but doesn't fail the gate.
    assert body["passed"] is True
    codes = [w["code"] for w in body["warnings"]]
    assert "MISSING_JSON_INSTRUCTION" in codes
    # Each warning has the expected fields.
    for w in body["warnings"]:
        assert w["severity"] == "warning"
        assert w["message"]


def test_validate_surfaces_warning_when_prompt_doesnt_reference_output_schema(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Output schema requires `answer` — a prompt that doesn't
    mention any output field name triggers NO_OUTPUT_SCHEMA_REFERENCE."""
    prompt_no_schema_ref = b"Reply with JSON. User: {{ input.text }}\n"
    _create(client, auth_header, prompt=prompt_no_schema_ref)
    r = client.post("/api/v1/agents/clean-demo/validate", headers=auth_header)
    codes = [w["code"] for w in r.json()["warnings"]]
    assert "NO_OUTPUT_SCHEMA_REFERENCE" in codes


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_validate_nonexistent_agent_returns_404(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post("/api/v1/agents/never-existed/validate", headers=auth_header)
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


def test_validate_without_auth_returns_401(client: TestClient) -> None:
    r = client.post("/api/v1/agents/anything/validate")
    assert r.status_code == 401
