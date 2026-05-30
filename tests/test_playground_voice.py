"""Pure-logic tests for the playground voice WS client (no Chainlit, no socket).

Voice mode is opt-in + additive (``mdk playground serve --voice``); this file
covers the transport half — :mod:`movate.playground.voice` — which mirrors the
runtime voice route's wire protocol (ADR 048 D4). Coverage:

* **URL building** — http→ws / https→wss scheme flip, the ``?token=`` auth
  param (a browser WS handshake can't carry an Authorization header), agent +
  token URL-encoding, base-path preservation.
* **Capability hint** — ``runtime_advertises_voice`` reads the same ``features``
  shapes ``parse_capabilities`` reads, and is conservative (None / unknown →
  False) but never a hard gate.
* **Frame parsing** — control frames → typed :class:`VoiceFrame`; the
  ``tts.audio`` JSON header pairs with the binary frame that follows; malformed
  frames are dropped, not fatal.
* **The client turn loop** — driven against a fake socket: config/audio/end
  framing out, transcript / agent.token / tts.audio (header+binary) / done in;
  ``aclose`` sends ``close`` and never raises; a connect failure raises
  :class:`VoiceNotEnabledError`.

These import WITHOUT Chainlit (the pure modules do), so they run on a no-extras
install — mirroring the other ``test_playground_*`` pure tests.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from movate.playground.capabilities import parse_capabilities
from movate.playground.voice import (
    VoiceFrame,
    VoiceNotEnabledError,
    VoiceWSClient,
    build_voice_ws_url,
    close_frame,
    collect_audio,
    config_frame,
    end_frame,
    interrupt_frame,
    parse_control_frame,
    runtime_advertises_voice,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


def test_url_http_to_ws_with_token() -> None:
    url = build_voice_ws_url("http://127.0.0.1:8000", "echo", token="tok")
    assert url == "ws://127.0.0.1:8000/api/v1/agents/echo/voice?token=tok"


def test_url_https_to_wss() -> None:
    url = build_voice_ws_url("https://prod.example.com", "echo", token="tok")
    assert url.startswith("wss://prod.example.com/api/v1/agents/echo/voice?token=tok")


def test_url_trailing_slash_and_no_token() -> None:
    url = build_voice_ws_url("http://h:8000/", "a")
    assert url == "ws://h:8000/api/v1/agents/a/voice"


def test_url_encodes_agent_and_token() -> None:
    """Reserved chars in the agent name / token can't corrupt the URL."""
    url = build_voice_ws_url("https://h", "a b/c", token="t+k=ey")
    assert "/agents/a%20b%2Fc/voice" in url
    assert "token=t%2Bk%3Dey" in url


def test_url_preserves_base_path() -> None:
    """A runtime behind a path prefix keeps that prefix in the WS URL."""
    url = build_voice_ws_url("https://gw.example.com/runtime", "echo", token="t")
    assert url == "wss://gw.example.com/runtime/api/v1/agents/echo/voice?token=t"


# ---------------------------------------------------------------------------
# Capability hint
# ---------------------------------------------------------------------------


def test_advertises_voice_dict_features() -> None:
    assert runtime_advertises_voice({"features": {"voice": True}}) is True
    assert runtime_advertises_voice({"features": {"voice_ws": True}}) is True


def test_advertises_voice_list_features() -> None:
    assert runtime_advertises_voice({"features": ["sse_events", "voice"]}) is True


def test_advertises_voice_false_when_absent_or_none() -> None:
    assert runtime_advertises_voice(None) is False
    assert runtime_advertises_voice({"features": {"sse_events": True}}) is False
    assert runtime_advertises_voice({"features": ["threads"]}) is False
    assert runtime_advertises_voice({}) is False


def test_parse_capabilities_surfaces_voice_flag() -> None:
    """The RuntimeCapabilities dataclass carries voice (default OFF)."""
    assert parse_capabilities(None).voice is False
    assert parse_capabilities({"features": {"sessions": True}}).voice is False
    assert parse_capabilities({"features": {"voice": True}}).voice is True


# ---------------------------------------------------------------------------
# Control-frame builders + parsing
# ---------------------------------------------------------------------------


def test_config_frame_only_sends_overrides() -> None:
    """Defaults are omitted so the runtime keeps its own (text input, etc.)."""
    assert json.loads(config_frame()) == {"type": "config"}
    parsed = json.loads(config_frame(input_key="q", language="en-US", voice_id="nova", mock=True))
    assert parsed == {
        "type": "config",
        "input_key": "q",
        "language": "en-US",
        "voice_id": "nova",
        "mock": True,
    }


def test_end_and_close_frames() -> None:
    assert json.loads(end_frame()) == {"type": "end"}
    assert json.loads(close_frame()) == {"type": "close"}


def test_interrupt_frame() -> None:
    """The barge-in control frame the client sends to cancel an in-flight answer."""
    assert json.loads(interrupt_frame()) == {"type": "interrupt"}


def test_latency_frame_exposes_badge() -> None:
    """A ``latency`` frame is recognized and surfaces its ready-to-render badge."""
    f = parse_control_frame(
        json.dumps({"type": "latency", "badge": "⚡ responded in 480ms", "responded_in_ms": 480.0})
    )
    assert f is not None
    assert f.is_latency
    assert f.latency_badge == "⚡ responded in 480ms"
    # A frame without a badge yields an empty string (never raises).
    assert VoiceFrame(type="latency", data={"type": "latency"}).latency_badge == ""


def test_speech_started_is_barge_in_cue() -> None:
    """The realtime ``speech_started`` control frame is flagged as the barge-in cue."""
    f = parse_control_frame(json.dumps({"type": "speech_started"}))
    assert f is not None
    assert f.is_speech_started


def test_parse_control_frame_typed() -> None:
    f = parse_control_frame(json.dumps({"type": "transcript.final", "text": "hi"}))
    assert f is not None
    assert f.is_final_transcript
    assert f.text == "hi"


def test_parse_control_frame_drops_malformed() -> None:
    assert parse_control_frame("not json") is None
    assert parse_control_frame(json.dumps([1, 2, 3])) is None  # not an object
    assert parse_control_frame(json.dumps({"no": "type"})) is None


def test_collect_audio_concatenates() -> None:
    frames = [
        VoiceFrame(type="tts.audio", audio=b"ab"),
        VoiceFrame(type="agent.token", data={"text": "x"}),
        VoiceFrame(type="tts.audio", audio=b"cd"),
    ]
    assert collect_audio(frames) == b"abcd"


# ---------------------------------------------------------------------------
# The client turn loop — against a fake socket
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Minimal stand-in for a ``websockets`` client connection.

    ``sent`` records every outbound frame (str control / bytes audio). The
    inbound side replays a scripted list of server frames when iterated, so a
    turn can be driven with no real network. ``closed`` flips on ``close()``.
    """

    def __init__(self, inbound: list[str | bytes]) -> None:
        self.sent: list[str | bytes] = []
        self._inbound = inbound
        self.closed = False

    async def send(self, frame: str | bytes) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        async def _gen() -> AsyncIterator[str | bytes]:
            for f in self._inbound:
                yield f

        return _gen()


def _hdr(**kw: object) -> str:
    return json.dumps({"type": "tts.audio", **kw})


async def test_client_turn_round_trip() -> None:
    """A full turn: config + audio + end out; transcript/token/audio/done in."""
    inbound: list[str | bytes] = [
        json.dumps({"type": "transcript.partial", "text": "turn the"}),
        json.dumps({"type": "transcript.final", "text": "turn the lights on"}),
        json.dumps({"type": "agent.token", "text": "Okay"}),
        _hdr(codec="pcm16", sample_rate=24000, bytes=4),
        b"\x01\x02\x03\x04",
        json.dumps({"type": "done", "run_id": "run-1", "status": "success"}),
    ]
    sock = _FakeSocket(inbound)
    client = VoiceWSClient(runtime_url="http://h:8000", agent="echo", token="t")
    client._ws = sock  # inject the fake (bypass connect)

    await client.send_config(mock=True)
    await client.send_audio(b"\x00\x01")
    await client.send_audio(b"")  # empty chunk is a no-op
    await client.end_turn()

    frames = [f async for f in client.iter_turn()]
    kinds = [f.type for f in frames]
    assert kinds == [
        "transcript.partial",
        "transcript.final",
        "agent.token",
        "tts.audio",
        "done",
    ]
    # The audio header paired with the binary frame that followed it.
    audio_frame = next(f for f in frames if f.is_audio)
    assert audio_frame.audio == b"\x01\x02\x03\x04"
    assert audio_frame.data["codec"] == "pcm16"
    assert collect_audio(frames) == b"\x01\x02\x03\x04"

    # Outbound framing: config (json), one audio chunk (bytes, empty dropped),
    # end (json). The empty send_audio added nothing.
    assert json.loads(sock.sent[0])["type"] == "config"
    assert sock.sent[1] == b"\x00\x01"
    assert json.loads(sock.sent[2]) == {"type": "end"}


async def test_client_iter_turn_stops_on_error() -> None:
    """An error frame is terminal — iteration stops there (graceful degrade)."""
    inbound: list[str | bytes] = [
        json.dumps({"type": "agent.token", "text": "partial"}),
        json.dumps({"type": "error", "message": "tts down", "code": "tts_error", "stage": "tts"}),
        json.dumps({"type": "done", "run_id": "x", "status": "ok"}),  # never reached
    ]
    client = VoiceWSClient(runtime_url="http://h", agent="a")
    client._ws = _FakeSocket(inbound)
    frames = [f async for f in client.iter_turn()]
    assert [f.type for f in frames] == ["agent.token", "error"]
    assert frames[-1].is_error
    assert frames[-1].data["stage"] == "tts"


async def test_client_aclose_sends_close_and_never_raises() -> None:
    sock = _FakeSocket([])
    client = VoiceWSClient(runtime_url="http://h", agent="a")
    client._ws = sock
    await client.aclose()
    assert any(isinstance(s, str) and json.loads(s).get("type") == "close" for s in sock.sent)
    assert sock.closed is True
    # Idempotent: a second close is a no-op, still no raise.
    await client.aclose()


async def test_client_aclose_swallows_send_failure() -> None:
    """A socket that errors on send/close must not surface into cleanup."""

    class _BadSocket:
        async def send(self, frame: object) -> None:
            raise RuntimeError("already closed")

        async def close(self) -> None:
            raise RuntimeError("boom")

    client = VoiceWSClient(runtime_url="http://h", agent="a")
    client._ws = _BadSocket()
    await client.aclose()  # no raise


async def test_connect_failure_raises_voice_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused/absent voice route normalizes to VoiceNotEnabledError."""
    pytest.importorskip("websockets")
    import websockets.asyncio.client as wsclient  # noqa: PLC0415

    async def _boom(*_a: object, **_k: object) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(wsclient, "connect", _boom)
    client = VoiceWSClient(runtime_url="http://h:8000", agent="echo", token="t")
    with pytest.raises(VoiceNotEnabledError):
        await client.connect()
