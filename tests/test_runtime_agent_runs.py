"""Tests for ``POST /api/v1/agents/{name}/runs`` — agent-scoped run.

BACKLOG Group G item 59. URL-anchored variant of today's ``POST /run``;
the agent name comes from the path, ``kind=AGENT`` is implicit, the
body only carries the input payload + optional notify_email. REST-clean
for the Angular resource model.

Coverage:

* **Happy path**: returns 202 with `{job_id, status: queued}`; the
  job lands in storage with the right tenant + agent target.
* **Notify email** field round-trips into the JobRecord.
* **404** when the agent isn't in the registry.
* **401** unauthed.
* **422** when the body is missing the required `input` field.
* **Body shape**: input must be a dict; arbitrary content allowed
  (validation happens in the worker against the agent's schema).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import JobKind, JobStatus
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
    """Returns (auth_header, tenant_id) — agent-runs tests need the
    tenant_id to verify the job landed with the right scope."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="agent-runs-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: runs-demo
version: 0.1.0
description: demo for agent-scoped runs
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Hi {{ input.text }}\n"

_INPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
).encode()


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_202_with_job_id(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Successful POST returns 202 + queued job_id; job is persisted
    with the right kind, target, and tenant."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/runs-demo/runs",
        json={"input": {"text": "world"}},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]

    # JobRecord landed in storage with the right scope.
    record = await storage.get_job(body["job_id"], tenant_id=tenant_id)
    assert record is not None
    assert record.kind == JobKind.AGENT
    assert record.target == "runs-demo"
    assert record.tenant_id == tenant_id
    assert record.input == {"text": "world"}
    assert record.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_run_notify_email_propagates(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """notify_email round-trips into the JobRecord so the worker
    knows where to send the terminal-status email."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/runs-demo/runs",
        json={
            "input": {"text": "world"},
            "notify_email": "ops@example.com",
        },
        headers=auth_header,
    )
    assert r.status_code == 202
    record = await storage.get_job(r.json()["job_id"], tenant_id=tenant_id)
    assert record is not None
    assert record.notify_email == "ops@example.com"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_run_nonexistent_agent_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/agents/never-existed/runs",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 404


def test_run_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/runs-demo/runs",
        json={"input": {"text": "hi"}},
    )
    assert r.status_code == 401


def test_run_missing_input_returns_422(client: TestClient, auth_setup) -> None:
    """FastAPI's body validation rejects {} — input is required."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/runs-demo/runs",
        json={},
        headers=auth_header,
    )
    assert r.status_code == 422


def test_run_extra_fields_rejected(client: TestClient, auth_setup) -> None:
    """``extra="forbid"`` on the schema means unknown top-level fields
    (e.g. operator sending `kind` from the legacy /run shape) fail
    fast rather than getting silently dropped."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/runs-demo/runs",
        json={"input": {"text": "hi"}, "kind": "agent"},  # extra field
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ?wait=true inline mode (item 110)
# ---------------------------------------------------------------------------


def test_run_wait_true_with_mock_returns_200_and_runview(client: TestClient, auth_setup) -> None:
    """``?wait=true`` + ``mock=true`` executes inline at the endpoint
    using the deterministic MockProvider and returns the RunView
    directly. Same Executor stack the worker uses; same persisted
    RunRecord; just a different transport (HTTP response body vs.
    polling)."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    r = client.post(
        "/api/v1/agents/runs-demo/runs?wait=true",
        json={"input": {"text": "hello world"}, "mock": True},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # RunView shape — note these fields are NOT in RunAccepted.
    assert "run_id" in body
    assert "output" in body
    assert "metrics" in body
    assert body["agent"] == "runs-demo"
    assert body["status"] in {"success", "error"}
    # MockProvider returns deterministic output; we just confirm
    # something landed rather than asserting exact text (the
    # MockProvider's response shape can evolve).
    assert body["output"] is not None or body["error"] is not None


def test_run_wait_false_default_returns_202_and_runaccepted(client: TestClient, auth_setup) -> None:
    """Default behavior (?wait omitted or false) is unchanged from
    item 59 — queue a job, return 202 + ``{job_id, status: queued}``.
    Regression guard against the new query param changing the
    async path."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    r = client.post(
        "/api/v1/agents/runs-demo/runs",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    # RunAccepted shape — NOT a RunView.
    assert body["status"] == "queued"
    assert "job_id" in body
    assert "run_id" not in body  # async mode has no run_id yet
    assert "output" not in body


def test_run_wait_false_explicit_returns_202(client: TestClient, auth_setup) -> None:
    """Same as the default test but with ``?wait=false`` explicit
    in the URL. Confirms the param parses correctly."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/runs-demo/runs?wait=false",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 202
    assert r.json()["status"] == "queued"


def test_run_wait_true_unknown_agent_returns_404(client: TestClient, auth_setup) -> None:
    """Same 404 surface in inline mode as in async mode — agent
    lookup happens before the executor builds."""
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/agents/never-existed/runs?wait=true",
        json={"input": {"text": "hi"}, "mock": True},
        headers=auth_header,
    )
    assert r.status_code == 404


def test_run_wait_true_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/runs-demo/runs?wait=true",
        json={"input": {"text": "hi"}, "mock": True},
    )
    assert r.status_code == 401
