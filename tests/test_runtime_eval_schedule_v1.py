"""Tests for the continuous-eval schedule endpoints (ADR 016 D2).

* PUT  /api/v1/agents/{name}/eval-schedule — upsert a cadence (eval scope)
* GET  /api/v1/eval-schedules — list this tenant's schedules (read scope)
* DELETE /api/v1/agents/{name}/eval-schedule — clear (eval scope)

Mirrors tests/test_runtime_evals_v1.py: in-memory storage, a minted key
with full scopes, an agent created via POST /api/v1/agents. Asserts
additive/default-off (empty list before any PUT), upsert idempotency,
tenant scoping, the 404 on an unknown agent, and the scope gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
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
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="sched-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


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
_DATASET = b'{"input": {"input": "hi"}, "expected": {"output": "mock-response"}}\n'


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
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
# Default-off + happy path
# ---------------------------------------------------------------------------


def test_list_empty_before_any_set(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/eval-schedules", headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json() == {"schedules": [], "count": 0}


def test_set_then_list(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.put(
        "/api/v1/agents/eval-demo/eval-schedule",
        json={"cadence_seconds": 1800, "mock": True, "regression_tolerance": 0.03},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "eval-demo"
    assert body["cadence_seconds"] == 1800
    assert body["mock"] is True
    assert body["regression_tolerance"] == 0.03
    assert body["last_enqueued_at"] is None

    lst = client.get("/api/v1/eval-schedules", headers=auth_header)
    assert lst.json()["count"] == 1
    assert lst.json()["schedules"][0]["agent"] == "eval-demo"


def test_set_is_idempotent_upsert(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    client.put(
        "/api/v1/agents/eval-demo/eval-schedule",
        json={"cadence_seconds": 3600},
        headers=auth_header,
    )
    client.put(
        "/api/v1/agents/eval-demo/eval-schedule",
        json={"cadence_seconds": 600, "mock": True},
        headers=auth_header,
    )
    lst = client.get("/api/v1/eval-schedules", headers=auth_header)
    assert lst.json()["count"] == 1
    assert lst.json()["schedules"][0]["cadence_seconds"] == 600


def test_delete_clears_schedule(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    client.put(
        "/api/v1/agents/eval-demo/eval-schedule",
        json={"cadence_seconds": 3600},
        headers=auth_header,
    )
    d = client.delete("/api/v1/agents/eval-demo/eval-schedule", headers=auth_header)
    assert d.status_code == 204
    assert client.get("/api/v1/eval-schedules", headers=auth_header).json()["count"] == 0
    # Idempotent: deleting again is still 204.
    again = client.delete("/api/v1/agents/eval-demo/eval-schedule", headers=auth_header)
    assert again.status_code == 204


# ---------------------------------------------------------------------------
# Errors + scope gate
# ---------------------------------------------------------------------------


def test_set_unknown_agent_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.put(
        "/api/v1/agents/ghost/eval-schedule",
        json={"cadence_seconds": 3600},
        headers=auth_header,
    )
    assert r.status_code == 404


def test_set_unauthed_401(client: TestClient) -> None:
    r = client.put("/api/v1/agents/eval-demo/eval-schedule", json={"cadence_seconds": 3600})
    assert r.status_code == 401


async def test_set_requires_eval_scope(storage: InMemoryStorage, client: TestClient) -> None:
    """A read-only key cannot write a schedule (403)."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="read-only", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.put(
        "/api/v1/agents/eval-demo/eval-schedule",
        json={"cadence_seconds": 3600},
        headers=header,
    )
    assert r.status_code == 403


async def test_list_is_tenant_scoped(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """A schedule under another tenant isn't visible to this tenant's key."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    client.put(
        "/api/v1/agents/eval-demo/eval-schedule",
        json={"cadence_seconds": 3600},
        headers=auth_header,
    )
    # A second tenant's key sees nothing.
    other = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    r = client.get("/api/v1/eval-schedules", headers=other_header)
    assert r.json()["count"] == 0
