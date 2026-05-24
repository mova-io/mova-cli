"""Tests for ``POST /api/v1/agents/{name}/runs/stream`` — SSE live token
streaming of an agent run (BACKLOG #75).

The streaming sibling of the inline ``?wait=true`` run path: same Executor
stack, same bundle resolution, same persistence — the only difference is
the transport (Server-Sent Events vs. a single JSON body).

Coverage:

* **Happy path** (``mock=true`` → MockProvider): ``content-type`` is
  ``text/event-stream``; the body carries ≥1 ``token`` event whose
  concatenated ``text`` reconstructs the agent output, then a ``done``
  event with a ``run_id`` + ``status=success``.
* **Persistence**: the streamed run lands a ``RunRecord`` — ``get_run``
  and ``GET /runs/{id}`` both return it after the stream closes.
* **Scope-gating**: a key without the ``run`` scope → 403.
* **Tenant-scoping**: the persisted run carries the caller's tenant; a
  different tenant 404s on ``GET /runs/{id}``.
* **Error path**: a schema-violating output (mock default doesn't satisfy
  a required field) emits an ``error`` event instead of ``done``.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, SCOPE_RUN, ApiKeyEnv, mint_api_key
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
    """Returns (auth_header, tenant_id) — a full-scope key (agent
    creation needs ``admin``; streaming needs ``run``)."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="stream-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


# Output schema accepts ``message`` so the MockProvider's default
# ``{"message": "mock response"}`` validates → run succeeds.
_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: stream-demo
version: 0.1.0
description: demo for SSE streaming
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

# Output schema REQUIRES ``answer`` which the mock default does NOT
# provide → schema validation fails → run errors.
_AGENT_YAML_STRICT = b"""\
api_version: movate/v1
kind: Agent
name: strict-demo
version: 0.1.0
description: demo agent whose output schema the mock cannot satisfy
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

# Loose output schema — accepts the mock's default {"message": ...}.
_OUTPUT_SCHEMA_LOOSE = json.dumps(
    {
        "type": "object",
        "properties": {"message": {"type": "string"}},
    }
).encode()

# Strict output schema — requires {"answer": ...}, which the mock default
# lacks → schema validation fails.
_OUTPUT_SCHEMA_STRICT = json.dumps(
    {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
).encode()


def _create_agent(
    client: TestClient,
    auth_header: dict[str, str],
    *,
    agent_yaml: bytes = _AGENT_YAML,
    output_schema: bytes = _OUTPUT_SCHEMA_LOOSE,
) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", agent_yaml, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", output_schema, "application/json")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse a raw SSE body into a list of ``(event, data)`` tuples."""
    events: list[tuple[str, dict]] = []
    event: str | None = None
    data_lines: list[str] = []
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if line == "":
            if event is not None or data_lines:
                payload = "\n".join(data_lines)
                data = json.loads(payload) if payload else {}
                events.append((event or "message", data))
            event = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip(" "))
    return events


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_stream_returns_event_stream_with_tokens_and_done(client: TestClient, auth_setup) -> None:
    """The endpoint streams ``text/event-stream``; the body carries
    token events whose concatenated text reconstructs the output, plus a
    terminal ``done`` event with a run_id + status=success."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    with client.stream(
        "POST",
        "/api/v1/agents/stream-demo/runs/stream",
        json={"input": {"text": "hi"}, "mock": True},
        headers=auth_header,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("cache-control") == "no-cache"
        assert resp.headers.get("x-accel-buffering") == "no"
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    token_events = [d for (e, d) in events if e == "token"]
    done_events = [d for (e, d) in events if e == "done"]

    assert len(token_events) >= 1
    assert len(done_events) == 1

    # Concatenating the deltas reconstructs the model's raw output, which
    # parses to the same dict the run persisted as ``output``.
    streamed_text = "".join(d["text"] for d in token_events)
    done = done_events[0]
    assert json.loads(streamed_text) == done["output"]

    assert done["status"] == "success"
    assert done["run_id"]
    assert "metrics" in done


async def test_stream_run_is_persisted(client: TestClient, storage, auth_setup) -> None:
    """The streamed run writes its RunRecord exactly like a non-streamed
    run — get_run + GET /runs/{id} both return it after the stream."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)

    with client.stream(
        "POST",
        "/api/v1/agents/stream-demo/runs/stream",
        json={"input": {"text": "hi"}, "mock": True},
        headers=auth_header,
    ) as resp:
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    run_id = next(d["run_id"] for (e, d) in events if e == "done")

    # Persisted in storage under the caller's tenant.
    record = await storage.get_run(run_id, tenant_id=tenant_id)
    assert record is not None
    assert record.agent == "stream-demo"
    assert record.tenant_id == tenant_id

    # And reachable over the read API.
    r = client.get(f"/runs/{run_id}", headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == run_id


# ---------------------------------------------------------------------------
# Scope + tenant gating
# ---------------------------------------------------------------------------


async def test_stream_requires_run_scope(client: TestClient, storage, auth_setup) -> None:
    """A key WITHOUT the ``run`` scope is rejected with 403 (least
    privilege) — read-only keys can't drive a run."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    ro = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    ro_header = {"Authorization": f"Bearer {ro.full_key}"}

    r = client.post(
        "/api/v1/agents/stream-demo/runs/stream",
        json={"input": {"text": "hi"}, "mock": True},
        headers=ro_header,
    )
    assert r.status_code == 403


def test_stream_without_auth_returns_401(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/stream-demo/runs/stream",
        json={"input": {"text": "hi"}, "mock": True},
    )
    assert r.status_code == 401


def test_stream_unknown_agent_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/agents/never-existed/runs/stream",
        json={"input": {"text": "hi"}, "mock": True},
        headers=auth_header,
    )
    assert r.status_code == 404


async def test_stream_run_is_tenant_scoped(client: TestClient, storage, auth_setup) -> None:
    """The persisted run belongs to the caller's tenant — a DIFFERENT
    tenant 404s on GET /runs/{id} (never sees the run)."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    with client.stream(
        "POST",
        "/api/v1/agents/stream-demo/runs/stream",
        json={"input": {"text": "hi"}, "mock": True},
        headers=auth_header,
    ) as resp:
        body = "".join(resp.iter_text())
    run_id = next(d["run_id"] for (e, d) in _parse_sse(body) if e == "done")

    # A different tenant's key cannot fetch the run.
    other = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other", scopes=[SCOPE_READ, SCOPE_RUN]
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    r = client.get(f"/runs/{run_id}", headers=other_header)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def test_stream_executor_error_emits_error_event(client: TestClient, auth_setup) -> None:
    """When the run fails (here: the mock's default output can't satisfy
    the agent's strict output schema), the stream emits an ``error``
    event carrying a message + code instead of ``done``."""
    auth_header, _ = auth_setup
    _create_agent(
        client,
        auth_header,
        agent_yaml=_AGENT_YAML_STRICT,
        output_schema=_OUTPUT_SCHEMA_STRICT,
    )

    with client.stream(
        "POST",
        "/api/v1/agents/strict-demo/runs/stream",
        json={"input": {"text": "hi"}, "mock": True},
        headers=auth_header,
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    error_events = [d for (e, d) in events if e == "error"]
    done_events = [d for (e, d) in events if e == "done"]

    assert len(error_events) == 1
    assert not done_events
    err = error_events[0]
    assert "message" in err
    assert "code" in err
