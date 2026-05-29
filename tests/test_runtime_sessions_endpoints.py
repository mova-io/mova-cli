"""Tests for stateful-session runtime endpoints (ADR 045 D10).

POST /api/v1/sessions, GET /api/v1/sessions, GET /api/v1/sessions/{id},
DELETE /api/v1/sessions/{id}, plus ``session_id`` threading on the
inline (``?wait=true``) + streaming run endpoints.

Coverage:
* Create/list/get/delete sessions — happy paths + 401 + 422 + 404 +
  cross-tenant isolation (404-not-403).
* Run with session_id (inline): the FIRST turn appends to the session;
  the SECOND turn's persisted run input carries the prior turn under
  ``conversation_history`` (server-managed memory threads history in).
* GET /sessions/{id} returns the turn history + the per-session cost
  rollup.
* ``memory="client"`` opt-out: prior turns are NOT injected, but the
  turn is still recorded for the rollup.
* Stateless default preserved: a run WITHOUT session_id is byte-for-byte
  unchanged (no conversation_history injected).
* Tenant isolation: tenant B cannot read tenant A's session, and cannot
  run against it (404).
* Async path rejects session_id with a clear 400 (the turn can't be
  appended before the run exists).
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
    return TestClient(build_app(storage, agents_path=agents_path))


async def _mint(storage: InMemoryStorage, label: str) -> tuple[dict[str, str], str]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label=label,
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


@pytest.fixture
async def auth_setup(storage: InMemoryStorage) -> tuple[dict[str, str], str]:
    return await _mint(storage, "session-tests")


@pytest.fixture
async def other_tenant(storage: InMemoryStorage) -> tuple[dict[str, str], str]:
    return await _mint(storage, "other-tenant")


# A loose-schema agent that the MockProvider's default output satisfies.
# The input schema does NOT set additionalProperties:false, so the
# injected ``conversation_history`` field is tolerated.
_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: chat-demo
version: 0.1.0
description: demo agent for stateful sessions
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Answer {{ input.text }}\n"

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
        "properties": {"message": {"type": "string"}},
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


def _create_session(
    client: TestClient,
    auth_header: dict[str, str],
    agent: str = "chat-demo",
) -> str:
    r = client.post("/api/v1/sessions", json={"agent": agent}, headers=auth_header)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event: str | None = None
    data_lines: list[str] = []
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if line == "":
            if event is not None or data_lines:
                payload = "\n".join(data_lines)
                events.append((event or "message", json.loads(payload) if payload else {}))
            event = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip(" "))
    return events


# ---------------------------------------------------------------------------
# Create / list / get / delete
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_session_returns_201(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/sessions",
        json={"agent": "chat-demo", "title": "Support"},
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session_id"]
    assert body["agent"] == "chat-demo"
    assert body["title"] == "Support"
    assert body["turn_count"] == 0
    assert body["total_cost_usd"] == 0.0
    # Bare create omits the message history.
    assert body["messages"] is None


@pytest.mark.integration
def test_create_session_requires_auth(client: TestClient) -> None:
    r = client.post("/api/v1/sessions", json={"agent": "chat-demo"})
    assert r.status_code == 401


@pytest.mark.integration
def test_create_session_422_on_missing_agent(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post("/api/v1/sessions", json={}, headers=auth_header)
    assert r.status_code == 422


@pytest.mark.integration
def test_list_sessions_tenant_scoped(client: TestClient, auth_setup, other_tenant) -> None:
    auth_header, _ = auth_setup
    other_header, _ = other_tenant
    _create_session(client, auth_header)
    _create_session(client, auth_header)
    r = client.get("/api/v1/sessions", headers=auth_header)
    assert r.status_code == 200
    assert r.json()["count"] == 2
    # Other tenant sees none.
    r2 = client.get("/api/v1/sessions", headers=other_header)
    assert r2.json()["count"] == 0


@pytest.mark.integration
def test_get_session_404_cross_tenant(client: TestClient, auth_setup, other_tenant) -> None:
    auth_header, _ = auth_setup
    other_header, _ = other_tenant
    sid = _create_session(client, auth_header)
    # Owner sees it.
    assert client.get(f"/api/v1/sessions/{sid}", headers=auth_header).status_code == 200
    # Cross-tenant → 404 (never 403, never leaks existence).
    assert client.get(f"/api/v1/sessions/{sid}", headers=other_header).status_code == 404


@pytest.mark.integration
def test_get_missing_session_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    assert client.get("/api/v1/sessions/nope", headers=auth_header).status_code == 404


@pytest.mark.integration
def test_delete_session(client: TestClient, auth_setup, other_tenant) -> None:
    auth_header, _ = auth_setup
    other_header, _ = other_tenant
    sid = _create_session(client, auth_header)
    # Cross-tenant delete → 404, leaves the session intact.
    assert client.delete(f"/api/v1/sessions/{sid}", headers=other_header).status_code == 404
    assert client.get(f"/api/v1/sessions/{sid}", headers=auth_header).status_code == 200
    # Owner delete → 204; gone afterwards.
    assert client.delete(f"/api/v1/sessions/{sid}", headers=auth_header).status_code == 204
    assert client.get(f"/api/v1/sessions/{sid}", headers=auth_header).status_code == 404


# ---------------------------------------------------------------------------
# Server-managed memory threading (the core D10 behavior)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_second_run_sees_prior_turns(client: TestClient, storage, auth_setup) -> None:
    """First run appends to the session; the SECOND run's persisted input
    carries the first turn under ``conversation_history`` — proving the
    runtime threads prior turns in as context."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    sid = _create_session(client, auth_header)

    # Turn 1.
    r1 = client.post(
        "/api/v1/agents/chat-demo/runs?wait=true",
        json={"input": {"text": "hello"}, "mock": True, "session_id": sid},
        headers=auth_header,
    )
    assert r1.status_code == 200, r1.text

    # Turn 2.
    r2 = client.post(
        "/api/v1/agents/chat-demo/runs?wait=true",
        json={"input": {"text": "again"}, "mock": True, "session_id": sid},
        headers=auth_header,
    )
    assert r2.status_code == 200, r2.text
    run2_id = r2.json()["run_id"]

    # The second run's PERSISTED input was augmented with the prior turn.
    record2 = await storage.get_run(run2_id, tenant_id=tenant_id)
    assert record2 is not None
    history = record2.input.get("conversation_history")
    assert history, "second run should see prior turns"
    assert history[0]["input"] == {"text": "hello"}
    assert history[0]["output"] is not None
    # The submitted text is still present (history is additive, not a
    # replacement of the real input).
    assert record2.input["text"] == "again"


@pytest.mark.integration
def test_session_history_endpoint_returns_turns_and_rollup(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    sid = _create_session(client, auth_header)

    for text in ("q1", "q2"):
        r = client.post(
            "/api/v1/agents/chat-demo/runs?wait=true",
            json={"input": {"text": text}, "mock": True, "session_id": sid},
            headers=auth_header,
        )
        assert r.status_code == 200, r.text

    r = client.get(f"/api/v1/sessions/{sid}", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    # Two turns → two user + two assistant messages.
    assert body["turn_count"] == 2
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    # User rows carry the submitted input.
    assert body["messages"][0]["content"] == {"text": "q1"}
    assert body["messages"][2]["content"] == {"text": "q2"}
    # Assistant rows reference their run + carry per-turn economics.
    assert body["messages"][1]["run_id"]
    # Rollup totals are present (>= 0; mock cost may be 0).
    assert body["total_tokens_in"] >= 0
    assert isinstance(body["total_cost_usd"], (int, float))


@pytest.mark.integration
async def test_client_memory_opt_out_skips_history(client, storage, auth_setup) -> None:
    """``memory="client"`` does NOT inject prior turns, but still records
    the turn for the rollup (R3 opt-out)."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    sid = _create_session(client, auth_header)

    # Seed one server-memory turn so there IS prior history to (not) inject.
    client.post(
        "/api/v1/agents/chat-demo/runs?wait=true",
        json={"input": {"text": "one"}, "mock": True, "session_id": sid},
        headers=auth_header,
    )
    # Client-memory turn.
    r = client.post(
        "/api/v1/agents/chat-demo/runs?wait=true",
        json={
            "input": {"text": "two"},
            "mock": True,
            "session_id": sid,
            "memory": "client",
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    record = await storage.get_run(run_id, tenant_id=tenant_id)
    assert record is not None
    # No history injected for client mode.
    assert "conversation_history" not in record.input
    # But the turn was still recorded → rollup advanced to 2 turns.
    got = await storage.get_session(sid, tenant_id=tenant_id)
    assert got is not None
    assert got.turn_count == 2


@pytest.mark.integration
async def test_stateless_default_unchanged(client, storage, auth_setup) -> None:
    """A run WITHOUT session_id is byte-for-byte today's behavior — no
    conversation_history injected, no session created."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/chat-demo/runs?wait=true",
        json={"input": {"text": "plain"}, "mock": True},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    record = await storage.get_run(r.json()["run_id"], tenant_id=tenant_id)
    assert record is not None
    assert record.input == {"text": "plain"}
    assert "conversation_history" not in record.input


# ---------------------------------------------------------------------------
# Tenant isolation + invalid-session + async-path guard
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_run_with_other_tenants_session_404(client: TestClient, auth_setup, other_tenant) -> None:
    """Tenant B cannot run against tenant A's session (404-not-403)."""
    auth_header, _ = auth_setup
    other_header, _ = other_tenant
    # The agent lives on the shared filesystem fallback, so it resolves
    # for BOTH tenants — the 404 below is the SESSION check (tenant B
    # can't see tenant A's session), not an agent miss.
    _create_agent(client, auth_header)
    sid = _create_session(client, auth_header)
    r = client.post(
        "/api/v1/agents/chat-demo/runs?wait=true",
        json={"input": {"text": "hi"}, "mock": True, "session_id": sid},
        headers=other_header,
    )
    assert r.status_code == 404


@pytest.mark.integration
def test_run_with_missing_session_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/chat-demo/runs?wait=true",
        json={"input": {"text": "hi"}, "mock": True, "session_id": "nope"},
        headers=auth_header,
    )
    assert r.status_code == 404


@pytest.mark.integration
def test_async_path_rejects_session_id(client: TestClient, auth_setup) -> None:
    """The queue path can't append the turn before the run exists → 400."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    sid = _create_session(client, auth_header)
    r = client.post(
        "/api/v1/agents/chat-demo/runs",  # no ?wait=true → async
        json={"input": {"text": "hi"}, "mock": True, "session_id": sid},
        headers=auth_header,
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Streaming variant
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_stream_with_session_appends_turn(client, storage, auth_setup) -> None:
    """The streaming run path also threads + appends a session turn."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    sid = _create_session(client, auth_header)

    with client.stream(
        "POST",
        "/api/v1/agents/chat-demo/runs/stream",
        json={"input": {"text": "streamed"}, "mock": True, "session_id": sid},
        headers=auth_header,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    assert any(e == "done" for (e, _) in events)

    got = await storage.get_session(sid, tenant_id=tenant_id)
    assert got is not None
    assert got.turn_count == 1
    msgs = await storage.list_session_messages(sid, tenant_id=tenant_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == {"text": "streamed"}
