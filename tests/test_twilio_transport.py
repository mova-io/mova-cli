"""Tests for the Twilio telephony transport (ADR 074 D3).

Mock-based -- no real Twilio account required. Verifies:
- Twilio Media Stream protocol handling (connected/start/media/stop)
- mu-law audio decoding via telephony.py's codec helpers
- Pipeline receives PCM16 AudioChunks at the pipeline rate
- Outbound TTS audio is mu-law encoded and base64'd for Twilio
- The TwiML webhook returns correct XML
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

from movate.voice.base import AudioChunk
from movate.voice.telephony import (
    PIPELINE_RATE,
    pcm16_to_mulaw,
)
from movate.voice.transports.twilio import TwilioTransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _make_twilio_msg(event: str, **extra: Any) -> str:
    """Build a JSON string mimicking a Twilio Media Stream message."""
    msg: dict[str, Any] = {"event": event}
    msg.update(extra)
    return json.dumps(msg)


def _media_msg(mulaw_bytes: bytes, stream_sid: str = "MS123") -> str:
    """Build a Twilio ``media`` event with base64-encoded mu-law payload."""
    return _make_twilio_msg(
        "media",
        media={"payload": base64.b64encode(mulaw_bytes).decode("ascii")},
        streamSid=stream_sid,
    )


class FakeWebSocket:
    """Minimal fake WebSocket that yields pre-loaded messages and captures sends."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self._idx = 0
        self.sent: list[str] = []

    async def receive_text(self) -> str:
        if self._idx >= len(self._messages):
            raise Exception("WebSocket closed")
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


# ---------------------------------------------------------------------------
# TwilioTransport tests
# ---------------------------------------------------------------------------


async def test_receive_audio_decodes_mulaw_to_pcm16() -> None:
    """media frames are decoded from mu-law to PCM16 AudioChunks."""
    # Encode 80 PCM16 samples (10 ms @ 8 kHz) to mu-law.
    pcm_data = _pcm([1000] * 80)
    mulaw = pcm16_to_mulaw(pcm_data)

    messages = [
        _make_twilio_msg("connected", protocol="Call"),
        _make_twilio_msg(
            "start",
            start={
                "streamSid": "MS123",
                "callSid": "CA456",
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
            },
        ),
        _media_msg(mulaw),
        _make_twilio_msg("stop"),
    ]
    ws = FakeWebSocket(messages)
    transport = TwilioTransport(ws)

    chunks = [c async for c in transport.receive_audio()]
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.codec == "pcm16"
    assert chunk.sample_rate == PIPELINE_RATE
    # 80 samples @ 8 kHz upsampled to 16 kHz -> ~160 samples (320 bytes).
    assert len(chunk.data) // 2 == 160


async def test_receive_audio_sets_stream_and_call_sids() -> None:
    """The transport captures streamSid and callSid from the start event."""
    messages = [
        _make_twilio_msg("connected"),
        _make_twilio_msg(
            "start",
            start={"streamSid": "MS_abc", "callSid": "CA_xyz"},
        ),
        _make_twilio_msg("stop"),
    ]
    ws = FakeWebSocket(messages)
    transport = TwilioTransport(ws)

    _ = [c async for c in transport.receive_audio()]
    assert transport.stream_sid == "MS_abc"
    assert transport.call_sid == "CA_xyz"


async def test_receive_audio_skips_empty_payloads() -> None:
    """media events with empty payloads produce no AudioChunks."""
    messages = [
        _make_twilio_msg("connected"),
        _make_twilio_msg("start", start={"streamSid": "MS1", "callSid": "CA1"}),
        _make_twilio_msg("media", media={"payload": ""}),
        _make_twilio_msg("stop"),
    ]
    ws = FakeWebSocket(messages)
    transport = TwilioTransport(ws)

    chunks = [c async for c in transport.receive_audio()]
    assert chunks == []


async def test_send_audio_encodes_pcm16_to_mulaw_base64() -> None:
    """TTS AudioChunks are mu-law encoded and sent as base64 media events."""
    ws = FakeWebSocket([])
    transport = TwilioTransport(ws)
    # Manually set the stream_sid (normally set by the start event).
    transport._stream_sid = "MS_test"
    transport._stopped = False

    chunk = AudioChunk(data=_pcm([2000] * 80), codec="pcm16", sample_rate=PIPELINE_RATE)
    await transport.send_audio(chunk)

    assert len(ws.sent) == 1
    msg = json.loads(ws.sent[0])
    assert msg["event"] == "media"
    assert msg["streamSid"] == "MS_test"
    # The payload should be valid base64 mu-law bytes.
    payload_bytes = base64.b64decode(msg["media"]["payload"])
    assert len(payload_bytes) > 0


async def test_send_audio_noop_when_stopped() -> None:
    """send_audio does nothing after the stream has stopped."""
    ws = FakeWebSocket([])
    transport = TwilioTransport(ws)
    transport._stream_sid = "MS_test"
    transport._stopped = True

    chunk = AudioChunk(data=_pcm([1000] * 80), codec="pcm16", sample_rate=16000)
    await transport.send_audio(chunk)
    assert ws.sent == []


async def test_send_audio_noop_before_start() -> None:
    """send_audio does nothing if stream_sid is not yet set."""
    ws = FakeWebSocket([])
    transport = TwilioTransport(ws)

    chunk = AudioChunk(data=_pcm([1000] * 80), codec="pcm16", sample_rate=16000)
    await transport.send_audio(chunk)
    assert ws.sent == []


async def test_multiple_media_frames() -> None:
    """Multiple media frames each produce an AudioChunk."""
    pcm_data = _pcm([500] * 40)
    mulaw = pcm16_to_mulaw(pcm_data)

    messages = [
        _make_twilio_msg("connected"),
        _make_twilio_msg("start", start={"streamSid": "MS1", "callSid": "CA1"}),
        _media_msg(mulaw),
        _media_msg(mulaw),
        _media_msg(mulaw),
        _make_twilio_msg("stop"),
    ]
    ws = FakeWebSocket(messages)
    transport = TwilioTransport(ws)

    chunks = [c async for c in transport.receive_audio()]
    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.codec == "pcm16"
        assert chunk.sample_rate == PIPELINE_RATE


async def test_disconnect_stops_transport() -> None:
    """disconnect() sets the stopped flag."""
    ws = FakeWebSocket([])
    transport = TwilioTransport(ws)
    assert not transport._stopped
    await transport.disconnect()
    assert transport._stopped


async def test_connect_returns_receive_audio_iterator() -> None:
    """connect() returns the receive_audio iterator (TelephonyTransport Protocol)."""
    messages = [_make_twilio_msg("stop")]
    ws = FakeWebSocket(messages)
    transport = TwilioTransport(ws)

    audio_iter = await transport.connect({})
    # Should be an async iterator (same as receive_audio).
    chunks = [c async for c in audio_iter]
    assert chunks == []


async def test_publish_delegates_to_send_audio() -> None:
    """publish() delegates to send_audio (TelephonyTransport Protocol)."""
    ws = FakeWebSocket([])
    transport = TwilioTransport(ws)
    transport._stream_sid = "MS_pub"
    transport._stopped = False

    chunk = AudioChunk(data=_pcm([1000] * 80), codec="pcm16", sample_rate=PIPELINE_RATE)
    await transport.publish(chunk)

    assert len(ws.sent) == 1
    msg = json.loads(ws.sent[0])
    assert msg["event"] == "media"


# ---------------------------------------------------------------------------
# TwiML webhook tests
# ---------------------------------------------------------------------------


async def test_twiml_webhook_returns_xml() -> None:
    """The TwiML webhook returns correct XML with a Stream directive."""
    from starlette.testclient import TestClient  # noqa: PLC0415

    from movate.runtime.app import build_app  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    storage = InMemoryStorage()
    app = build_app(storage)

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/test-agent/call/twilio",
            headers={"Host": "example.com"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/xml"
    body = resp.text
    assert "<Response>" in body
    assert "<Connect>" in body
    assert "<Stream" in body
    assert "test-agent" in body
    assert "example.com" in body


async def test_twiml_webhook_uses_wss_for_https() -> None:
    """The TwiML webhook generates wss:// URLs when X-Forwarded-Proto is https."""
    from starlette.testclient import TestClient  # noqa: PLC0415

    from movate.runtime.app import build_app  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    storage = InMemoryStorage()
    app = build_app(storage)

    # Simulate HTTPS via the request scheme (TestClient uses http by default).
    with TestClient(app, base_url="https://secure.example.com") as client:
        resp = client.post("/api/v1/agents/my-agent/call/twilio")
    body = resp.text
    assert "wss://" in body
