"""Realtime voice seam conformance + adapter event mapping (ADR 048 D2b / Phase 2).

The optional full-duplex :class:`RealtimeVoiceProvider` seam (voice↔voice, NO
text Executor) and its first impls — :class:`OpenAIRealtime` /
:class:`AzureOpenAIRealtime` — both speaking the OpenAI Realtime wire protocol.
These tests pin:

* the :class:`RealtimeChunk` envelope shape + ``extra="forbid"`` strictness;
* runtime-checkable conformance of :class:`FakeRealtime` AND the two OpenAI
  realtime adapters against the Protocol (same ``isinstance`` story as the
  pipeline seams);
* the fake's scripted voice↔voice round-trip (mic in → audio out decodes back
  to the answer);
* the adapters' lazy SDK import (constructing them with an injected fake
  ``connect`` does NOT require the ``openai`` package) + the server-event →
  ``RealtimeChunk`` mapping (audio delta, transcripts, VAD turn signals,
  response.done, error) driven through the shared session loop with no socket.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from movate.voice import (
    AudioChunk,
    AzureOpenAIRealtime,
    FakeRealtime,
    OpenAIRealtime,
    RealtimeChunk,
    RealtimeVoiceProvider,
)


async def _audio_stream(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


# ---------------------------------------------------------------------------
# Chunk type
# ---------------------------------------------------------------------------


def test_realtime_chunk_shape_and_strictness() -> None:
    audio = RealtimeChunk(kind="audio", audio=AudioChunk(data=b"\x00\x01"))
    assert audio.kind == "audio"
    assert audio.audio is not None and audio.audio.data == b"\x00\x01"

    t = RealtimeChunk(kind="transcript", text="hi", is_final=True)
    assert t.text == "hi"
    assert t.is_final is True

    err = RealtimeChunk(kind="error", message="boom", code="bad")
    assert err.message == "boom"
    assert err.code == "bad"

    # extra="forbid" — a typo'd field is rejected, not silently dropped.
    with pytest.raises(Exception):
        RealtimeChunk(kind="audio", nonsense=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Protocol conformance (runtime_checkable)
# ---------------------------------------------------------------------------


def test_fake_realtime_satisfies_protocol() -> None:
    assert isinstance(FakeRealtime(), RealtimeVoiceProvider)


def test_openai_realtime_adapters_satisfy_protocol() -> None:
    # Constructing with no connect callable must NOT import the openai SDK (lazy).
    assert isinstance(OpenAIRealtime(), RealtimeVoiceProvider)
    assert isinstance(AzureOpenAIRealtime(deployment="rt"), RealtimeVoiceProvider)


# ---------------------------------------------------------------------------
# Fake double — scripted voice↔voice round-trip
# ---------------------------------------------------------------------------


async def test_fake_realtime_round_trips_audio_and_records_input() -> None:
    rt = FakeRealtime(transcript="hello there", answer="hi back", frames=2)
    chunks = [
        c
        async for c in rt.session(
            _audio_stream(b"aa", b"bb"),
            voice_id="rachel",
            instructions="be brief",
            api_key="k",
        )
    ]
    kinds = [c.kind for c in chunks]
    assert kinds[0] == "speech_started"
    assert "transcript" in kinds
    assert kinds[-1] == "response_done"

    # The synthesized audio decodes back to the scripted answer (round-trip).
    audio = b"".join(c.audio.data for c in chunks if c.kind == "audio" and c.audio)
    assert audio.decode("utf-8") == "hi back"

    # It drained + recorded the inbound mic frames and the call kwargs.
    assert rt.received == [b"aa", b"bb"]
    assert rt.voice_ids == ["rachel"]
    assert rt.instructions == ["be brief"]
    assert rt.api_keys == ["k"]


# ---------------------------------------------------------------------------
# OpenAI / Azure realtime adapters — injected fake connection (no SDK/network)
# ---------------------------------------------------------------------------


class _FakeRealtimeConn:
    """A fake of the SDK's realtime-connection async context manager.

    ``async with`` yields self; ``send`` records the outbound events; iterating
    the connection replays the scripted server events. Mirrors the shape the
    ``openai`` SDK's ``beta.realtime.connect(...)`` object exposes closely
    enough for the adapter's loop.
    """

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.sent: list[dict] = []

    async def __aenter__(self) -> _FakeRealtimeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send(self, event: dict) -> None:
        self.sent.append(event)

    async def __aiter__(self):
        for ev in self._events:
            # Yield to the loop between events so the adapter's concurrently
            # scheduled mic-pump task gets to run — mirrors a real socket where
            # events arrive over awaited network I/O (not all at once).
            await asyncio.sleep(0)
            yield ev


def _server_events() -> list[dict]:
    """A representative OpenAI Realtime server-event sequence."""
    return [
        {"type": "input_audio_buffer.speech_started"},
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "turn on the lights",
        },
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.audio_transcript.delta", "delta": "Sure"},
        {"type": "response.audio.delta", "delta": base64.b64encode(b"PCMDATA").decode()},
        {"type": "session.updated"},  # an event the transport does NOT surface
        {"type": "response.done"},
    ]


async def test_openai_realtime_maps_server_events_and_configures_session() -> None:
    conn = _FakeRealtimeConn(_server_events())
    rt = OpenAIRealtime(connect=lambda _key, _model: conn, default_voice="alloy")

    out = [
        c
        async for c in rt.session(
            _audio_stream(b"mic1", b"mic2"),
            voice_id="echo",
            instructions="be helpful",
            api_key="ignored",
        )
    ]
    kinds = [c.kind for c in out]
    # The unsurfaced session.updated event is skipped; everything else maps.
    assert kinds == [
        "speech_started",
        "transcript",  # final input transcription
        "speech_stopped",
        "transcript",  # partial output transcript delta
        "audio",
        "response_done",
    ]
    # The audio delta decoded base64 → raw PCM bytes.
    audio = next(c for c in out if c.kind == "audio")
    assert audio.audio is not None
    assert audio.audio.data == b"PCMDATA"
    # The session was configured (voice + pcm16 both ways), then the mic frames
    # were appended as base64.
    session_updates = [e for e in conn.sent if e.get("type") == "session.update"]
    assert session_updates and session_updates[0]["session"]["voice"] == "echo"
    assert session_updates[0]["session"]["input_audio_format"] == "pcm16"
    assert session_updates[0]["session"]["instructions"] == "be helpful"
    # The mic frames were forwarded as base64 ``append`` events. The pump runs
    # concurrently with event consumption, so how many land before the (short,
    # fake) event stream ends is scheduling-dependent — assert what's sent is a
    # PREFIX of the mic stream (a real socket drains all of them).
    appends = [
        base64.b64decode(e["audio"])
        for e in conn.sent
        if e.get("type") == "input_audio_buffer.append"
    ]
    assert appends == [b"mic1", b"mic2"][: len(appends)]
    assert appends  # at least one mic frame was forwarded


async def test_openai_realtime_surfaces_error_event() -> None:
    events = [{"type": "error", "error": {"message": "rate limited", "code": "rate_limit"}}]
    conn = _FakeRealtimeConn(events)
    rt = OpenAIRealtime(connect=lambda _key, _model: conn)
    out = [c async for c in rt.session(_audio_stream(b"x"))]
    assert len(out) == 1
    assert out[0].kind == "error"
    assert out[0].code == "rate_limit"
    assert "rate limited" in out[0].message


async def test_azure_realtime_uses_deployment_and_shares_event_mapping() -> None:
    conn = _FakeRealtimeConn(_server_events())
    seen: dict[str, Any] = {}

    def _connect(api_key: str | None, target: str) -> _FakeRealtimeConn:
        seen["api_key"] = api_key
        seen["target"] = target  # Azure passes the DEPLOYMENT name here
        return conn

    rt = AzureOpenAIRealtime(deployment="my-rt-deployment", connect=_connect)
    out = [c async for c in rt.session(_audio_stream(b"mic"), voice_id="shimmer", api_key="azk")]

    # Same event mapping as the public adapter (shared driver).
    assert [c.kind for c in out] == [
        "speech_started",
        "transcript",
        "speech_stopped",
        "transcript",
        "audio",
        "response_done",
    ]
    # Azure routed by the DEPLOYMENT name + the BYOK key.
    assert seen["target"] == "my-rt-deployment"
    assert seen["api_key"] == "azk"


async def test_azure_realtime_missing_deployment_raises_clearly(monkeypatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_REALTIME_DEPLOYMENT", raising=False)
    rt = AzureOpenAIRealtime()  # no deployment, no env → clear ValueError
    with pytest.raises(ValueError, match="deployment"):
        async for _ in rt.session(_audio_stream(b"x")):
            pass
