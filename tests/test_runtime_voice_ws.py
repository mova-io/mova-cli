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
from movate.core.models import VoiceConfig
from movate.core.provider_keys import ENV_PROVIDER_KEY_SECRET, mint_tenant_provider_key
from movate.runtime import build_app
from movate.runtime.app import _seed_voice_turn_config, _send_voice_latency, _VoiceTurnConfig
from movate.testing import InMemoryStorage
from movate.voice import AudioChunk, FakeRealtime, FakeSTT, FakeTTS
from movate.voice.observer import MetricsObserver
from movate.voice.pipeline import VoiceEvent


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
    # A non-speculative turn carries NO speculation block (back-compat).
    assert "speculation" not in latency


class _FakeWS:
    """Minimal WebSocket double capturing ``send_json`` payloads."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)


async def test_send_voice_latency_carries_speculation_block_when_fired() -> None:
    """ADR 070/073 A/B signal: when a speculation fired, the latency frame
    carries a ``speculation`` block (committed / commit_ratio / head-start) the
    demo perf panel + runbook aggregate. Gated on ``started`` so a
    non-speculative turn stays byte-for-byte unchanged."""
    events = [
        VoiceEvent(kind="transcript.final", text="hi", at_ms=100.0),
        VoiceEvent(kind="agent.token", text="he", at_ms=300.0),
    ]
    metrics = MetricsObserver()
    metrics.on_event("speculation_started", chars=2)
    metrics.on_event("speculation_committed", head_start_ms=420)

    ws = _FakeWS()
    await _send_voice_latency(ws, events, metrics)

    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["type"] == "latency"
    spec = frame["speculation"]
    assert spec["committed"] == 1
    assert spec["commit_ratio"] == 1.0
    assert spec["avg_head_start_ms"] == 420.0


async def test_send_voice_latency_omits_speculation_when_idle() -> None:
    """No speculation fired → no ``speculation`` key (additive, opt-in)."""
    events = [
        VoiceEvent(kind="transcript.final", text="hi", at_ms=100.0),
        VoiceEvent(kind="agent.token", text="he", at_ms=300.0),
    ]
    ws = _FakeWS()
    await _send_voice_latency(ws, events, MetricsObserver())

    assert len(ws.sent) == 1
    assert "speculation" not in ws.sent[0]


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


def test_voice_ws_realtime_seeds_voice_id_from_agent_block(
    storage, agents_path, auth_setup
) -> None:
    """ADR 071 D1: an agent's voice.voice_id reaches the realtime provider with
    NO client config frame (the block is the default)."""
    app = build_app(storage, agents_path=agents_path)
    rt = FakeRealtime(transcript="hi", answer="done", frames=1)
    app.state.voice_realtime_factory = lambda: rt
    token, _ = auth_setup
    client = TestClient(app)
    agent_yaml = _AGENT_YAML.replace(
        b"description: demo agent for the voice WS transport",
        b"description: demo agent for the voice WS transport\n"
        b"voice:\n  enabled: true\n  voice_id: rachel",
    )
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", agent_yaml, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text

    with client.websocket_connect(
        f"/api/v1/agents/voice-demo/voice?mode=realtime&token={token}"
    ) as ws:
        ws.send_bytes(b"\x00\x01")  # a mic frame — NO config frame
        ws.send_json({"type": "close"})
        while True:
            msg = ws.receive()
            if msg.get("type") == "websocket.close":
                break
            if msg.get("text") and json.loads(msg["text"]).get("type") == "response_done":
                break

    # The agent block's voice_id reached the provider without a client frame.
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


# ── ADR 071 — per-agent voice block seeds the per-turn config ──────────────────


def _bundle_with_voice(**voice_fields):
    """A minimal stand-in bundle exposing ``.spec.voice`` (a VoiceConfig)."""
    from types import SimpleNamespace  # noqa: PLC0415 - test-local helper

    voice = VoiceConfig(**voice_fields) if voice_fields else None
    return SimpleNamespace(spec=SimpleNamespace(voice=voice))


def test_seed_voice_turn_config_applies_agent_block() -> None:
    """ADR 071 D1-D3: voice_id/language/tts_streaming/speculative seed from the block."""
    cfg = _VoiceTurnConfig()
    _seed_voice_turn_config(
        cfg,
        _bundle_with_voice(
            voice_id="rachel", language="fr-FR", tts_streaming=False, speculative=True
        ),
    )
    assert cfg.voice_id == "rachel"
    assert cfg.language == "fr-FR"
    assert cfg.tts_streaming is False
    assert cfg.speculative is True


def test_seed_voice_turn_config_absent_block_keeps_defaults() -> None:
    """No voice block → runtime defaults untouched (byte-for-byte today)."""
    cfg = _VoiceTurnConfig()
    before = (cfg.voice_id, cfg.language, cfg.tts_streaming, cfg.speculative)
    _seed_voice_turn_config(cfg, _bundle_with_voice())  # voice is None
    assert (cfg.voice_id, cfg.language, cfg.tts_streaming, cfg.speculative) == before


def test_seed_voice_turn_config_tts_streaming_none_keeps_default() -> None:
    """tts_streaming unset (None) leaves the runtime default (True) in place."""
    cfg = _VoiceTurnConfig()
    assert cfg.tts_streaming is True  # runtime default
    _seed_voice_turn_config(cfg, _bundle_with_voice(voice_id="x"))  # no tts_streaming
    assert cfg.tts_streaming is True  # unchanged


def test_seed_voice_turn_config_keyterms() -> None:
    """ADR 071 D4: keyterms seed from the agent block; empty list = no boosting."""
    cfg = _VoiceTurnConfig()
    assert cfg.keyterms == []  # default
    _seed_voice_turn_config(cfg, _bundle_with_voice(keyterms=["VPN", "Okta"]))
    assert cfg.keyterms == ["VPN", "Okta"]
    # Empty keyterms list leaves the (empty) default untouched.
    cfg2 = _VoiceTurnConfig()
    _seed_voice_turn_config(cfg2, _bundle_with_voice(voice_id="x"))
    assert cfg2.keyterms == []


async def test_voice_ws_threads_agent_keyterms_to_stt(storage, agents_path, auth_setup) -> None:
    """End-to-end: an agent's voice.keyterms reach stt.transcribe(keyterms=...)."""
    app = build_app(storage, agents_path=agents_path)
    stt = FakeSTT("turn the lights on")
    app.state.voice_stt_factory = lambda: stt
    app.state.voice_tts_factory = FakeTTS
    token, _ = auth_setup
    client = TestClient(app)
    # Create an agent whose voice block carries keyterms.
    agent_yaml = _AGENT_YAML.replace(
        b"description: demo agent for the voice WS transport",
        b"description: demo agent for the voice WS transport\n"
        b"voice:\n  enabled: true\n  keyterms: ['VPN', 'Okta']",
    )
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", agent_yaml, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text

    with client.websocket_connect(f"/api/v1/agents/voice-demo/voice?token={token}") as ws:
        ws.send_json({"type": "config", "mock": True})
        ws.send_bytes(b"\x00\x01\x02\x03")
        ws.send_json({"type": "end"})
        _drain_turn(ws)
        ws.send_json({"type": "close"})

    # The FakeSTT recorded the keyterms the pipeline passed it.
    assert stt.keyterms_seen == [["VPN", "Okta"]]
