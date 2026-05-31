"""POST /api/v1/agents/{name}/voice — the one-shot REST transport (ADR 050 D2).

The request/response parity to the streaming WS (``test_runtime_voice_ws``):
audio in → STT → the UNCHANGED Executor → TTS → ``{transcript, response_text,
audio_*}`` out in a single request. Fake STT/TTS are injected via ``app.state``
(no provider SDK / network / key) and the agent stage is driven by
``MockProvider`` via the ``mock`` form flag (the UNCHANGED Executor runs
offline). Coverage:

* a full one-shot turn (audio → transcript + response + inline audio + cost
  header), proving it is the SAME pipeline as the WS;
* ``?audio=stream`` returns the synthesized audio as the binary body with the
  transcript/response/latency in headers (ADR 050 D10);
* ``?audio=none`` (transcribe-only) returns the transcript, no audio;
* the ``text``-in path (``mdk voice say``) bypasses STT;
* auth: missing ``run`` scope → 403; unknown agent → 404; no input → 400;
* the run is persisted (proof a voice turn IS a run, ADR 050 D1).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

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
description: demo agent for the voice REST transport
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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Happy path — the full one-shot turn (inline audio, default)
# ---------------------------------------------------------------------------


async def test_voice_rest_full_turn_inline(client, storage, auth_setup, app_and_fakes) -> None:
    token, tenant_id = auth_setup
    _create_agent(client, token)
    _app, _stt, tts = app_and_fakes

    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        params={"audio": "inline"},
        files={"audio": ("clip.wav", b"\x00\x01\x02\x03", "application/octet-stream")},
        data={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # The transcript is what STT produced; the response is the agent's answer.
    assert body["transcript"] == "turn the lights on"
    assert body["response_text"]  # the mock agent answered
    assert body["status"] == "success"
    assert body["run_id"]

    # Inline audio decodes back to the agent's spoken answer (round-trip proof
    # the SAME pipeline drove TTS).
    assert body["audio_bytes_b64"]
    decoded = base64.b64decode(body["audio_bytes_b64"])
    assert decoded  # non-empty synthesized audio
    assert tts.spoken  # TTS was driven with the answer

    # A voice turn IS a run (ADR 050 D1): it persisted exactly like a text run.
    record = await storage.get_run(body["run_id"], tenant_id=tenant_id)
    assert record is not None
    assert record.agent == "voice-demo"
    assert record.input == {"text": "turn the lights on"}


async def test_voice_rest_emits_latency_headers(client, auth_setup) -> None:
    """Per-stage voice latency rides additive X-MDK-Voice-Latency-* headers."""
    token, _ = auth_setup
    _create_agent(client, token)
    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        files={"audio": ("clip.wav", b"\x00\x01", "application/octet-stream")},
        data={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    # The headline "responded in" latency is present (a successful turn reached
    # the first-audio milestone).
    assert "X-MDK-Voice-Latency-Responded-Ms" in r.headers


# ---------------------------------------------------------------------------
# Audio transport variants (ADR 050 D10)
# ---------------------------------------------------------------------------


async def test_voice_rest_stream_audio_body(client, auth_setup) -> None:
    """``?audio=stream`` → the synthesized audio is the binary body; the
    transcript/response ride headers (the telephony-bridge path)."""
    token, _ = auth_setup
    _create_agent(client, token)
    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        params={"audio": "stream"},
        files={"audio": ("clip.wav", b"\x00\x01", "application/octet-stream")},
        data={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.content  # the raw audio bytes are the body
    assert r.headers.get("X-MDK-Voice-Transcript") == "turn the lights on"
    assert r.headers.get("X-MDK-Voice-Response")  # the agent's answer echoed
    assert r.headers.get("X-MDK-Voice-Run-Id")


async def test_voice_rest_transcribe_only(client, auth_setup, app_and_fakes) -> None:
    """``?audio=none`` → transcript returned, no synthesized audio."""
    token, _ = auth_setup
    _create_agent(client, token)
    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        params={"audio": "none"},
        files={"audio": ("clip.wav", b"\x00\x01", "application/octet-stream")},
        data={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == "turn the lights on"
    assert body["audio_bytes_b64"] is None  # no audio in the envelope


async def test_voice_rest_text_in_bypasses_stt(client, auth_setup, app_and_fakes) -> None:
    """The ``text``-in path (``mdk voice say``) skips STT: the text IS the
    transcript and the SAME pipeline runs agent → TTS."""
    token, _ = auth_setup
    _create_agent(client, token)
    _app, stt, _tts = app_and_fakes
    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        data={"text": "please dim the lights", "mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == "please dim the lights"  # the text became the transcript
    assert body["response_text"]
    assert body["audio_bytes_b64"]  # the answer was synthesized
    # The real STT adapter was NOT driven (no audio path).
    assert stt.received == []


# ---------------------------------------------------------------------------
# Errors + auth
# ---------------------------------------------------------------------------


async def test_voice_rest_no_input_is_400(client, auth_setup) -> None:
    token, _ = auth_setup
    _create_agent(client, token)
    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        data={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text


async def test_voice_rest_unknown_agent_is_404(client, auth_setup) -> None:
    token, _ = auth_setup
    r = client.post(
        "/api/v1/agents/nope/voice",
        files={"audio": ("c.wav", b"\x00\x01", "application/octet-stream")},
        data={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 404, r.text


async def test_voice_rest_requires_run_scope(client, storage, auth_setup) -> None:
    token, _ = auth_setup
    _create_agent(client, token)
    ro = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        files={"audio": ("c.wav", b"\x00\x01", "application/octet-stream")},
        data={"mock": "true"},
        headers=_auth(ro.full_key),
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# BYOK — the REST path reuses the WS edge's key resolver (ADR 048 D6)
# ---------------------------------------------------------------------------


async def test_voice_rest_threads_no_tenant_key_as_none(storage, agents_path, auth_setup) -> None:
    """No tenant BYOK key → the adapters receive ``api_key=None`` → env default,
    byte-for-byte the WS path's back-compat behavior."""
    app = build_app(storage, agents_path=agents_path)
    stt = FakeSTT("hello there")
    tts = FakeTTS()
    app.state.voice_stt_factory = lambda: stt
    app.state.voice_tts_factory = lambda: tts
    client = TestClient(app)
    token, _ = auth_setup
    _create_agent(client, token)

    r = client.post(
        "/api/v1/agents/voice-demo/voice",
        files={"audio": ("c.wav", b"\x00\x01", "application/octet-stream")},
        data={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert stt.api_keys == [None]
    assert tts.api_keys == [None]
