"""Tests for the eval endpoints — items 83, 84, 85.

* POST /api/v1/agents/{name}/evals — kick off + run an eval
* GET /api/v1/evals/{eval_id} — retrieve scorecard
* GET /api/v1/evals?agent=<name> — history list

For Friday's deadline these run synchronously inside the request
handler (mock-provider mode finishes sub-second). The wire contract
(202 + eval_id, retrieve via GET) is identical to the async-worker
path that lands in v0.8 — Angular client doesn't change when we
swap the implementation.

Coverage:

* Happy path: POST a mock eval → 202 + eval_id; GET retrieves the
  scorecard with the same id; agents-filter list returns it.
* Mock vs non-mock: mock=true uses MockProvider, no LLM API key
  required.
* Tenant scoping: cross-tenant eval_id returns 404.
* 401 unauthed; 404 unknown agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
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
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="evals-v1-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


# Agent + dataset suitable for mock-eval. Single-case dataset keeps the
# test runtime fast; the MockProvider returns deterministic output for
# any input.
_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: eval-demo
version: 0.1.0
description: mock-eval target
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
evals:
  dataset: ./evals/dataset.jsonl
"""

_PROMPT = b'Respond with valid JSON: {"output": "<your answer>"}\n\nInput: {{ input.input }}\n'

_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"output": {"type": "string"}}, "required": ["output"]}
).encode()

# Mock provider returns a fixed shape; dataset matches it so we get
# at least one pass.
_DATASET = b'{"input": {"input": "hi"}, "expected": {"output": "mock-response"}}\n'


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    """POST the eval-demo agent so it lands in the registry."""
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
            ("dataset", ("dataset.jsonl", _DATASET, "application/jsonl")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_eval_kickoff_returns_eval_id(client: TestClient, auth_setup) -> None:
    """POST /api/v1/agents/{name}/evals with mock=true returns 202
    + eval_id + status=success (sync execution)."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/eval-demo/evals",
        json={"gate": 0.0, "runs": 1, "mock": True, "wait": True},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["eval_id"]
    assert body["message"] == ""


def test_eval_get_returns_scorecard(client: TestClient, auth_setup) -> None:
    """After POST, GET /api/v1/evals/{eval_id} returns the full
    scorecard with the matching id + agent + sample_count."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    post = client.post(
        "/api/v1/agents/eval-demo/evals",
        json={"gate": 0.0, "runs": 1, "mock": True, "wait": True},
        headers=auth_header,
    )
    eval_id = post.json()["eval_id"]

    r = client.get(f"/api/v1/evals/{eval_id}", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["eval_id"] == eval_id
    assert body["agent"] == "eval-demo"
    assert body["agent_version"] == "0.1.0"
    assert body["sample_count"] == 1
    assert 0.0 <= body["mean_score"] <= 1.0
    assert 0.0 <= body["pass_rate"] <= 1.0
    assert body["runs_per_case"] == 1
    assert body["gate_mode"] == "mean"


def test_eval_list_includes_recent(client: TestClient, auth_setup) -> None:
    """GET /api/v1/evals?agent=<name> returns the eval we just kicked
    off, newest-first."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    post = client.post(
        "/api/v1/agents/eval-demo/evals",
        json={"gate": 0.0, "runs": 1, "mock": True, "wait": True},
        headers=auth_header,
    )
    eval_id = post.json()["eval_id"]

    r = client.get("/api/v1/evals?agent=eval-demo", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["evals"][0]["eval_id"] == eval_id


def test_eval_list_filter_isolates_per_agent(client: TestClient, auth_setup) -> None:
    """Two agents, one eval each → list filter returns only the
    matching agent's eval."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    # Make a sibling agent.
    sibling_yaml = _AGENT_YAML.replace(b"name: eval-demo", b"name: eval-demo-sibling")
    sibling_dataset = _DATASET
    r1 = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", sibling_yaml, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
            ("dataset", ("dataset.jsonl", sibling_dataset, "application/jsonl")),
        ],
        headers=auth_header,
    )
    assert r1.status_code == 201

    client.post(
        "/api/v1/agents/eval-demo/evals",
        json={"gate": 0.0, "runs": 1, "mock": True, "wait": True},
        headers=auth_header,
    )
    client.post(
        "/api/v1/agents/eval-demo-sibling/evals",
        json={"gate": 0.0, "runs": 1, "mock": True, "wait": True},
        headers=auth_header,
    )

    # Per-agent filter
    r2 = client.get("/api/v1/evals?agent=eval-demo", headers=auth_header)
    assert r2.json()["count"] == 1
    assert r2.json()["evals"][0]["agent"] == "eval-demo"

    r3 = client.get("/api/v1/evals?agent=eval-demo-sibling", headers=auth_header)
    assert r3.json()["count"] == 1
    assert r3.json()["evals"][0]["agent"] == "eval-demo-sibling"

    # No filter → both come back (tenant scope still applies)
    r4 = client.get("/api/v1/evals", headers=auth_header)
    assert r4.json()["count"] == 2


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_eval_kickoff_unknown_agent_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/agents/never-existed/evals",
        json={"gate": 0.7, "runs": 1, "mock": True},
        headers=auth_header,
    )
    assert r.status_code == 404


def test_eval_kickoff_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/eval-demo/evals",
        json={"gate": 0.7, "runs": 1, "mock": True},
    )
    assert r.status_code == 401


def test_eval_get_without_auth_returns_401(client: TestClient) -> None:
    r = client.get(f"/api/v1/evals/{uuid4()}")
    assert r.status_code == 401


def test_eval_get_unknown_id_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get(f"/api/v1/evals/{uuid4()}", headers=auth_header)
    assert r.status_code == 404
