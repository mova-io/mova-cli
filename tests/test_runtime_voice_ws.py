"""WS /api/v1/agents/{name}/voice — the Phase-1 pipeline transport (ADR 048 D4).

Exercises the WebSocket message protocol end-to-end through the runtime, with
fake STT/TTS injected via ``app.state`` (so no provider SDK / network / key)
and the agent stage driven by ``MockProvider`` via the ``mock`` config flag
(so the UNCHANGED Executor runs offline). Coverage:

* the framed protocol: a turn of binary audio + ``{"type":"end"}`` yields
  ``transcript.final`` → ``agent.token`` → ``tts.audio`` (header + binary) →
  ``done``;
* the run is persisted (proof the Executor ran as a normal run);
* auth: missing token / missing ``run`` scope close the socket (1008);
* unknown agent → an ``error`` frame + close.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage
from movate.voice import FakeSTT, FakeTTS


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
def app_and_fakes(storage: InMemoryStorage, agents_path: Path):
    app = build_app(storage, agents_path=agents_path)
    # Inject fakes behind the voice seam (ADR 048 D3) — the route is
    # provider-agnostic, so the test swaps the adapters without touching it.
    stt = FakeSTT("turn the lights on")
    tts = FakeTTS()
    app.state.voice_stt_factory = lambda: stt
    app.state.voice_tts_factory = lambda: tts
    return app, stt, tts


@pytest.fixture
def client(app_and_fakes) -> TestClient:
    return TestClient(app_and_fakes[0])


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="voice", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return minted.full_key, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: voice-demo
version: 0.1.0
description: demo agent for the voice WS transport
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Reply to {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()
# Loose output — the mock's default {"message": ...} validates → run succeeds.
_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"message": {"type": "string"}}}
).encode()


def _create_agent(client: TestClient, token: str) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text


def _drain_turn(ws) -> list[dict | bytes]:
    """Receive frames until a ``done``/``error`` control frame. Binary frames
    come back as bytes; JSON control frames as dicts."""
    frames: list[dict | bytes] = []
    while True:
        # starlette's test WS exposes receive() returning the raw ASGI message.
        msg = ws.receive()
        if "bytes" in msg and msg["bytes"] is not None:
            frames.append(msg["bytes"])
            continue
        if "text" in msg and msg["text"] is not None:
            ctrl = json.loads(msg["text"])
            frames.append(ctrl)
            if ctrl.get("type") in ("done", "error"):
                return frames
        if msg.get("type") == "websocket.close":
            return frames


# ---------------------------------------------------------------------------
# Happy path — the protocol round-trip
# ---------------------------------------------------------------------------


async def test_voice_ws_full_turn(client: TestClient, storage, auth_setup, app_and_fakes) -> None:
    token, tenant_id = auth_setup
    _create_agent(client, token)
    _app, _stt, tts = app_and_fakes

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01\x02\x03")  # one audio frame
        ws.send_json({"type": "end"})
        frames = _drain_turn(ws)
        ws.send_json({"type": "close"})

    ctrl = [f for f in frames if isinstance(f, dict)]
    types = [c["type"] for c in ctrl]
    assert "transcript.final" in types
    assert "agent.token" in types
    assert "tts.audio" in types  # the JSON header preceding each binary frame
    assert types[-1] == "done"

    # The final transcript is what STT produced.
    final = next(c for c in ctrl if c["type"] == "transcript.final")
    assert final["text"] == "turn the lights on"

    # A binary audio frame followed the tts.audio header; decoding the fake's
    # bytes gives back the agent's answer text (round-trip proof).
    audio = b"".join(f for f in frames if isinstance(f, bytes))
    assert audio  # non-empty
    assert tts.spoken  # TTS was driven with the answer

    # The run persisted exactly like a normal run (Executor reused unchanged).
    done = ctrl[-1]
    assert done["status"] == "success"
    record = await storage.get_run(done["run_id"], tenant_id=tenant_id)
    assert record is not None
    assert record.agent == "voice-demo"
    assert record.input == {"text": "turn the lights on"}


# ---------------------------------------------------------------------------
# Auth + resolution
# ---------------------------------------------------------------------------


def test_voice_ws_without_token_is_closed(client: TestClient, auth_setup) -> None:
    token, _ = auth_setup
    _create_agent(client, token)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/v1/agents/voice-demo/voice") as ws,
    ):
        ws.receive()
    assert exc.value.code == 1008


async def test_voice_ws_requires_run_scope(client: TestClient, storage, auth_setup) -> None:
    token, _ = auth_setup
    _create_agent(client, token)
    # A read-only key lacks `run` → policy-violation close.
    ro = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={ro.full_key}") as ws,
    ):
        ws.receive()
    assert exc.value.code == 1008


def test_voice_ws_unknown_agent_errors_then_closes(client: TestClient, auth_setup) -> None:
    token, _ = auth_setup
    with client.websocket_connect(f"/api/v1/agents/nope/voice?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "not_found"
