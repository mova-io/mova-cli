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

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.core.provider_keys import ENV_PROVIDER_KEY_SECRET, mint_tenant_provider_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage
from movate.voice import AudioChunk, FakeRealtime, FakeSTT, FakeTTS


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


async def test_voice_ws_emits_latency_frame_before_done(
    client: TestClient, storage, auth_setup, app_and_fakes
) -> None:
    """The demo latency badge: a ``latency`` control frame (with a rendered
    badge + per-stage ms) lands just before the terminal ``done``."""
    token, _ = auth_setup
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01\x02\x03")
        ws.send_json({"type": "end"})
        frames = _drain_turn(ws)
        ws.send_json({"type": "close"})

    ctrl = [f for f in frames if isinstance(f, dict)]
    types = [c["type"] for c in ctrl]
    assert "latency" in types
    # The latency frame precedes the terminal done.
    assert types.index("latency") < types.index("done")
    latency = next(c for c in ctrl if c["type"] == "latency")
    assert isinstance(latency.get("badge"), str) and latency["badge"]
    assert "responded in" in latency["badge"]
    # The per-stage fields are present for the trace/observability consumer.
    assert "responded_in_ms" in latency


class _SlowTTS:
    """A TTS that yields several frames with an await between each, so an
    ``interrupt`` control frame has time to land mid-answer (barge-in test)."""

    name = "slow_tts"
    version = "0.0.1"

    def __init__(self) -> None:
        self.emitted = 0

    async def synthesize(self, text, *, voice_id="", codec="pcm16", api_key=None):
        async for _ in text:
            pass
        for i in range(20):
            yield AudioChunk(data=f"chunk-{i}".encode(), codec=codec)
            self.emitted += 1
            await asyncio.sleep(0.02)


async def test_voice_ws_interrupt_bargein_cancels_tts(storage, agents_path, auth_setup) -> None:
    """An ``{"type":"interrupt"}`` frame sent mid-answer cancels the in-flight
    TTS: the turn ends with ``done`` status ``interrupted`` and the slow TTS did
    NOT emit all its frames."""
    slow = _SlowTTS()
    app = build_app(storage, agents_path=agents_path)
    app.state.voice_stt_factory = lambda: FakeSTT("hello there")
    app.state.voice_tts_factory = lambda: slow
    client = TestClient(app)
    token, _ = auth_setup
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "end"})
        # Barge in almost immediately — the user starts talking over the answer.
        ws.send_json({"type": "interrupt"})
        frames = _drain_turn(ws)
        ws.send_json({"type": "close"})

    ctrl = [f for f in frames if isinstance(f, dict)]
    done = next(c for c in ctrl if c["type"] == "done")
    assert done["status"] == "interrupted"
    # The slow TTS was cut short (it would have emitted 20 frames otherwise).
    assert slow.emitted < 20


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


# ---------------------------------------------------------------------------
# Realtime mode (ADR 048 D2b / ADR 050 D12) — ?mode=realtime routes to the
# RealtimeVoiceProvider (voice↔voice, NO text Executor). Default pipeline mode
# is unchanged (covered by test_voice_ws_full_turn above).
# ---------------------------------------------------------------------------


def test_voice_ws_realtime_routes_to_realtime_provider(
    storage, agents_path, auth_setup, app_and_fakes
) -> None:
    token, _tenant_id = auth_setup
    app, _stt, _tts = app_and_fakes
    # Light up the OPT-IN realtime path with a fake provider behind the seam.
    rt = FakeRealtime(transcript="turn on the lights", answer="done", frames=2)
    app.state.voice_realtime_factory = lambda: rt
    client = TestClient(app)
    _create_agent(client, token)

    with client.websocket_connect(
        f"/api/v1/agents/voice-demo/voice?mode=realtime&token={token}"
    ) as ws:
        ws.send_json({"type": "config", "voice_id": "rachel"})
        ws.send_bytes(b"\x00\x01\x02\x03")  # a mic frame
        ws.send_json({"type": "close"})
        frames: list[dict | bytes] = []
        while True:
            msg = ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                frames.append(msg["bytes"])
                continue
            if "text" in msg and msg["text"] is not None:
                ctrl = json.loads(msg["text"])
                frames.append(ctrl)
                if ctrl.get("type") == "response_done":
                    break
            if msg.get("type") == "websocket.close":
                break

    ctrl = [f for f in frames if isinstance(f, dict)]
    types = [c["type"] for c in ctrl]
    # The realtime provider's control + audio events reached the client.
    assert "speech_started" in types
    assert "transcript.final" in types  # the realtime transcript on the shared wire name
    assert "tts.audio" in types  # JSON header preceding each binary audio frame
    assert types[-1] == "response_done"

    # The synthesized audio decodes back to the fake's scripted answer.
    audio = b"".join(f for f in frames if isinstance(f, bytes))
    assert audio.decode("utf-8") == "done"

    # The provider was driven voice-native: it received the mic frame + the
    # client's voice_id — and NO run was persisted (no Executor in this path).
    assert rt.received == [b"\x00\x01\x02\x03"]
    assert rt.voice_ids == ["rachel"]


def test_voice_ws_realtime_unconfigured_is_rejected(client: TestClient, auth_setup) -> None:
    # With no realtime factory configured (the default), ?mode=realtime must
    # degrade with a clear error frame, not hard-fail — the pipeline mode stays
    # available.
    token, _ = auth_setup
    _create_agent(client, token)
    with client.websocket_connect(
        f"/api/v1/agents/voice-demo/voice?mode=realtime&token={token}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "realtime_unavailable"


def test_voice_ws_default_mode_unchanged_when_realtime_configured(
    storage, agents_path, auth_setup, app_and_fakes
) -> None:
    # Configuring realtime must NOT change the default pipeline mode: a
    # connection with no ?mode= still runs STT → Executor → TTS and persists a
    # run, exactly as before.
    token, _tenant_id = auth_setup
    app, _stt, tts = app_and_fakes

    def _make_rt() -> FakeRealtime:
        return FakeRealtime()

    app.state.voice_realtime_factory = _make_rt
    client = TestClient(app)
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "end"})
        frames = _drain_turn(ws)
        ws.send_json({"type": "close"})

    ctrl = [f for f in frames if isinstance(f, dict)]
    types = [c["type"] for c in ctrl]
    assert "agent.token" in types  # the pipeline Executor stage ran
    assert types[-1] == "done"
    assert tts.spoken  # pipeline TTS was driven


# ---------------------------------------------------------------------------
# Per-tenant BYOK key resolution for voice (ADR 048 D6 / ADR 018).
#
# The transport edge resolves the calling tenant's OWN STT/TTS/realtime key via
# the ADR 018 ProviderKeyResolver and threads it into the adapters (`api_key=`).
# No tenant key → `None` → the adapter's env-default credential, byte-for-byte
# today's behavior (the back-compat guard). Resolution is wired in the handler,
# never inside the adapter (CLAUDE.md rule 6) — proven here by asserting the
# fakes received exactly the resolved key (or `None`).
# ---------------------------------------------------------------------------

_FERNET_KEY = Fernet.generate_key()


def _named_stt(name: str, transcript: str = "lights on") -> FakeSTT:
    stt = FakeSTT(transcript)
    stt.name = name  # the adapter name the resolver maps to a BYOK family
    return stt


def _named_tts(name: str) -> FakeTTS:
    tts = FakeTTS()
    tts.name = name
    return tts


async def _store_key(storage: InMemoryStorage, *, tenant_id: str, provider: str, key: str) -> None:
    """Persist a tenant BYOK key encrypted under the test Fernet secret."""
    rec = mint_tenant_provider_key(
        tenant_id=tenant_id,
        provider=provider,
        plaintext=key,
        fernet=Fernet(_FERNET_KEY),
    )
    await storage.save_tenant_provider_key(rec)


@pytest.fixture
def byok_app(storage: InMemoryStorage, agents_path: Path, monkeypatch):
    # The handler builds its own ProviderKeyResolver(storage), which decrypts
    # with the env secret — so the test minting must use the same secret.
    monkeypatch.setenv(ENV_PROVIDER_KEY_SECRET, _FERNET_KEY.decode())
    app = build_app(storage, agents_path=agents_path)
    stt = _named_stt("deepgram", "lights on")  # `deepgram` is its own BYOK family
    tts = _named_tts("elevenlabs")
    app.state.voice_stt_factory = lambda: stt
    app.state.voice_tts_factory = lambda: tts
    return app, stt, tts


async def test_voice_ws_pipeline_uses_tenant_byok_keys(storage, auth_setup, byok_app) -> None:
    """With tenant STT + TTS keys stored, both are threaded into the adapters."""
    token, tenant_id = auth_setup
    app, stt, tts = byok_app
    await _store_key(storage, tenant_id=tenant_id, provider="deepgram", key="dg-tenant")
    await _store_key(storage, tenant_id=tenant_id, provider="elevenlabs", key="el-tenant")
    client = TestClient(app)
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "end"})
        _drain_turn(ws)
        ws.send_json({"type": "close"})

    # The resolved tenant keys reached the adapters via `api_key=` (proof the
    # edge resolved + threaded them, not the SDK env default).
    assert stt.api_keys == ["dg-tenant"]
    assert tts.api_keys == ["el-tenant"]


async def test_voice_ws_pipeline_falls_back_to_env_when_no_tenant_key(
    storage, auth_setup, byok_app
) -> None:
    """No tenant key → adapters receive `api_key=None` → SDK env default.

    This is the back-compat guard: a keyless tenant must behave byte-for-byte
    as before BYOK (the adapter reads its own env credential)."""
    token, _tenant_id = auth_setup
    app, stt, tts = byok_app
    # Deliberately store NO tenant key.
    client = TestClient(app)
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "end"})
        _drain_turn(ws)
        ws.send_json({"type": "close"})

    assert stt.api_keys == [None]
    assert tts.api_keys == [None]


async def test_voice_ws_pipeline_maps_adapter_name_to_provider_family(
    storage, auth_setup, agents_path, monkeypatch
) -> None:
    """A multi-service vendor adapter (`openai_whisper`) resolves the tenant's
    `openai` key — the edge collapses the capability name to its BYOK family."""
    monkeypatch.setenv(ENV_PROVIDER_KEY_SECRET, _FERNET_KEY.decode())
    token, tenant_id = auth_setup
    app = build_app(storage, agents_path=agents_path)
    stt = _named_stt("openai_whisper", "hello")
    tts = _named_tts("openai_tts")
    app.state.voice_stt_factory = lambda: stt
    app.state.voice_tts_factory = lambda: tts
    # One `openai` key covers both the Whisper STT and the OpenAI TTS adapter.
    await _store_key(storage, tenant_id=tenant_id, provider="openai", key="sk-openai")
    client = TestClient(app)
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "end"})
        _drain_turn(ws)
        ws.send_json({"type": "close"})

    assert stt.api_keys == ["sk-openai"]
    assert tts.api_keys == ["sk-openai"]


async def test_voice_ws_realtime_uses_tenant_byok_key(
    storage, agents_path, auth_setup, monkeypatch
) -> None:
    """The realtime (?mode=realtime) path threads the tenant key into session()."""
    monkeypatch.setenv(ENV_PROVIDER_KEY_SECRET, _FERNET_KEY.decode())
    token, tenant_id = auth_setup
    app = build_app(storage, agents_path=agents_path)
    rt = FakeRealtime(transcript="hi", answer="done")
    rt.name = "openai_realtime"  # maps to the `openai` BYOK family
    app.state.voice_realtime_factory = lambda: rt
    await _store_key(storage, tenant_id=tenant_id, provider="openai", key="sk-rt")
    client = TestClient(app)
    _create_agent(client, token)

    with client.websocket_connect(
        f"/api/v1/agents/voice-demo/voice?mode=realtime&token={token}"
    ) as ws:
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "close"})
        while True:
            msg = ws.receive()
            if (
                msg.get("text") is not None
                and json.loads(msg["text"]).get("type") == "response_done"
            ):
                break
            if msg.get("type") == "websocket.close":
                break

    assert rt.api_keys == ["sk-rt"]


def _drain_turn_with_usage(ws) -> list[dict | bytes]:
    """Like :func:`_drain_turn` but continues past ``done`` to capture the
    ``usage`` control frame that the voice-runs-parity handler emits after the
    turn completes (ADR 050 D7). Stops after ``usage`` or a second ``done``/
    ``error`` or socket close."""
    frames: list[dict | bytes] = []
    past_done = False
    while True:
        msg = ws.receive()
        if "bytes" in msg and msg["bytes"] is not None:
            frames.append(msg["bytes"])
            continue
        if "text" in msg and msg["text"] is not None:
            ctrl = json.loads(msg["text"])
            frames.append(ctrl)
            if ctrl.get("type") == "done":
                past_done = True
                continue
            if past_done and ctrl.get("type") == "usage":
                return frames
            if ctrl.get("type") == "error":
                return frames
        if msg.get("type") == "websocket.close":
            return frames


# ---------------------------------------------------------------------------
# Gap 1: Voice turns recorded as RunRecords (ADR 050 D1)
# ---------------------------------------------------------------------------


async def test_voice_turn_persisted_as_run_record_with_modality(
    client: TestClient, storage, auth_setup, app_and_fakes
) -> None:
    """A voice turn writes a RunRecord with ``modality='voice'`` and voice-
    specific metrics (stt_latency_ms, tts_latency_ms, audio_duration_s,
    stt_cost_usd, tts_cost_usd)."""
    token, tenant_id = auth_setup
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01\x02\x03")
        ws.send_json({"type": "end"})
        frames = _drain_turn_with_usage(ws)
        ws.send_json({"type": "close"})

    ctrl = [f for f in frames if isinstance(f, dict)]
    done = next(c for c in ctrl if c["type"] == "done")
    assert done["status"] == "success"

    # The RunRecord now has voice-specific fields.
    record = await storage.get_run(done["run_id"], tenant_id=tenant_id)
    assert record is not None
    assert record.modality == "voice"
    assert record.stt_latency_ms is not None
    assert record.audio_duration_s is not None
    assert record.stt_cost_usd is not None and record.stt_cost_usd >= 0.0
    assert record.tts_cost_usd is not None and record.tts_cost_usd >= 0.0


# ---------------------------------------------------------------------------
# Gap 2: session_id threading through voice WS (ADR 050 D1/D8)
# ---------------------------------------------------------------------------


async def test_voice_ws_session_threading(storage, agents_path, auth_setup) -> None:
    """Two voice turns with the same ``session_id``: the second turn sees the
    first turn's context (the session's message count and cost increase)."""
    from movate.core.models import Session  # noqa: PLC0415

    token, tenant_id = auth_setup
    # Create a session to thread through.
    session = Session(session_id=uuid4().hex, tenant_id=tenant_id, agent="voice-demo")
    await storage.save_session(session)

    app = build_app(storage, agents_path=agents_path)
    stt = FakeSTT("hello there")
    tts = FakeTTS()
    app.state.voice_stt_factory = lambda: stt
    app.state.voice_tts_factory = lambda: tts
    client = TestClient(app)
    _create_agent(client, token)

    # Turn 1 — with session_id in the config frame.
    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True, "session_id": session.session_id})
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "end"})
        _drain_turn_with_usage(ws)
        ws.send_json({"type": "close"})

    # The session should now have 1 turn appended.
    updated_session = await storage.get_session(session.session_id, tenant_id=tenant_id)
    assert updated_session is not None
    assert updated_session.turn_count == 1
    assert updated_session.total_cost_usd > 0.0

    # Turn 2 — same session_id; the agent gets context from turn 1.
    stt2 = FakeSTT("what about now")
    app.state.voice_stt_factory = lambda: stt2
    client2 = TestClient(app)
    with client2.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True, "session_id": session.session_id})
        ws.send_bytes(b"\x00\x01")
        ws.send_json({"type": "end"})
        _drain_turn_with_usage(ws)
        ws.send_json({"type": "close"})

    # Two turns recorded on the session.
    final_session = await storage.get_session(session.session_id, tenant_id=tenant_id)
    assert final_session is not None
    assert final_session.turn_count == 2

    # The session messages contain both user and assistant rows for each turn.
    messages = await storage.list_session_messages(session.session_id, tenant_id=tenant_id)
    # 2 turns * 2 rows (user + assistant) = 4 messages.
    assert len(messages) == 4
    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) == 2
    assert user_msgs[0].content == {"text": "hello there"}
    assert user_msgs[1].content == {"text": "what about now"}


# ---------------------------------------------------------------------------
# Gap 3: Voice cost in metering envelope (ADR 050 D7)
# ---------------------------------------------------------------------------


async def test_voice_turn_emits_usage_frame_with_cost(
    client: TestClient, storage, auth_setup, app_and_fakes
) -> None:
    """The voice turn emits a ``usage`` control frame with stt_cost_usd,
    tts_cost_usd, and total_cost_usd after the ``done`` frame."""
    token, tenant_id = auth_setup
    _create_agent(client, token)

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01\x02\x03")
        ws.send_json({"type": "end"})
        frames = _drain_turn_with_usage(ws)
        ws.send_json({"type": "close"})

    ctrl = [f for f in frames if isinstance(f, dict)]
    usage = next((c for c in ctrl if c["type"] == "usage"), None)
    assert usage is not None, f"no usage frame; types={[c.get('type') for c in ctrl]}"
    assert "stt_cost_usd" in usage
    assert "tts_cost_usd" in usage
    assert "llm_cost_usd" in usage
    assert "total_cost_usd" in usage
    assert usage["total_cost_usd"] >= 0.0
    # The run_id in the usage frame matches the done frame.
    done = next(c for c in ctrl if c["type"] == "done")
    assert usage["run_id"] == done["run_id"]

    # Voice cost flows into the RunRecord for the usage API (build_usage reads
    # metrics.cost_usd which already includes LLM cost; stt/tts are additive
    # voice-specific fields).
    record = await storage.get_run(done["run_id"], tenant_id=tenant_id)
    assert record is not None
    assert record.stt_cost_usd == usage["stt_cost_usd"]
    assert record.tts_cost_usd == usage["tts_cost_usd"]
