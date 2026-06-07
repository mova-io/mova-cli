"""Cartesia Ink Whisper STT adapter — tests.

Mirrors ``tests/test_voice_deepgram_cartesia.py`` for the second T1 streaming
STT (the Lyzr parity gap A3a). Pins:

* runtime-checkable conformance against :class:`SpeechToTextProvider` (so a
  future provider can be checked the same way);
* lazy SDK import — constructing the adapter with an injected fake client does
  NOT require the ``cartesia`` package (so the whole suite runs without
  ``mdk[cartesia]``);
* the streaming behavior: interim partials → final, audio bytes drained into
  the WebSocket, and the defensive promote-trailing-partial-to-final guarantee;
* the empty-audio safe path (one empty final, never hang);
* the error path (a socket error surfaces as an exception, which the pipeline
  turns into a stage="stt" error event — graceful degrade per ADR 048).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from movate.voice import (
    AudioChunk,
    CartesiaSTT,
    SpeechToTextProvider,
)


async def _audio_stream(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


# ---------------------------------------------------------------------------
# Protocol conformance (runtime_checkable) + lazy import
# ---------------------------------------------------------------------------


def test_cartesia_stt_satisfies_protocol() -> None:
    # Constructing with a None client must NOT import the cartesia SDK (lazy) —
    # this test module runs without the cartesia extra strictly required.
    assert isinstance(CartesiaSTT(), SpeechToTextProvider)


# ---------------------------------------------------------------------------
# Fake Cartesia STT WebSocket — mirrors AsyncSttWebsocket's surface
# ---------------------------------------------------------------------------


class _FakeCartesiaSttWebsocket:
    """Fake of Cartesia's :class:`AsyncSttWebsocket`.

    Exposes a ``transcribe(audio_chunks, **opts)`` async generator that drains
    the inbound audio into ``self.sent`` and replays a scripted sequence of
    result dicts — the same shape the SDK yields (``{"type": "transcript",
    "text": ..., "is_final": ...}`` etc.).
    """

    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = scripted
        self.sent: list[bytes] = []
        self.transcribe_opts: dict[str, Any] | None = None
        self.closed = False

    async def transcribe(
        self, audio_chunks: AsyncIterator[bytes], **opts: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.transcribe_opts = opts
        async for chunk in audio_chunks:
            self.sent.append(chunk)
        for event in self._scripted:
            yield event

    async def close(self) -> None:
        self.closed = True


class _ErroringCartesiaSttWebsocket(_FakeCartesiaSttWebsocket):
    """Variant that raises mid-stream — the SDK does this on a socket error."""

    async def transcribe(
        self, audio_chunks: AsyncIterator[bytes], **opts: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.transcribe_opts = opts
        async for chunk in audio_chunks:
            self.sent.append(chunk)
        # Emit a partial first so the adapter has something to coalesce, then
        # blow up — this mirrors the SDK's "raise RuntimeError(...)" on a
        # transport error mid-stream.
        yield {"type": "transcript", "text": "partial", "is_final": False}
        raise RuntimeError("socket blew up")


class _FakeCartesiaSttModule:
    def __init__(self, ws: _FakeCartesiaSttWebsocket) -> None:
        self._ws = ws
        self.connect_opts: dict[str, Any] | None = None

    async def websocket(self, **opts: Any) -> _FakeCartesiaSttWebsocket:
        self.connect_opts = opts
        return self._ws


class _FakeCartesiaClient:
    def __init__(self, ws: _FakeCartesiaSttWebsocket) -> None:
        self.stt = _FakeCartesiaSttModule(ws)


# ---------------------------------------------------------------------------
# Happy path + endpointing
# ---------------------------------------------------------------------------


async def test_cartesia_stt_streams_partials_then_final_and_sends_audio() -> None:
    ws = _FakeCartesiaSttWebsocket(
        [
            {"type": "transcript", "text": "the", "is_final": False},
            {"type": "transcript", "text": "the full", "is_final": False},
            {"type": "transcript", "text": "the full utterance", "is_final": True},
            {"type": "done"},
        ]
    )
    stt = CartesiaSTT(client=_FakeCartesiaClient(ws))
    chunks = [
        c async for c in stt.transcribe(_audio_stream(b"aa", b"bb"), language="en-US", api_key="k")
    ]
    assert [c.text for c in chunks] == ["the", "the full", "the full utterance"]
    assert [c.is_final for c in chunks] == [False, False, True]
    # The whole inbound audio stream was pumped into the socket.
    assert ws.sent == [b"aa", b"bb"]
    # Language hint reached the WebSocket as ISO-639-1 (the leading 2 chars).
    assert ws.transcribe_opts is not None
    assert ws.transcribe_opts["language"] == "en"
    # PCM encoding declared so raw pcm16 AudioChunks decode without a container.
    assert ws.transcribe_opts["encoding"] == "pcm_s16le"


async def test_cartesia_stt_promotes_last_partial_when_no_final() -> None:
    # Socket emits only interims (no endpointed final). The adapter must
    # promote the last partial to is_final=True so the pipeline's
    # "wait for is_final" loop unblocks rather than hangs — same defensive
    # guarantee as the Deepgram adapter.
    ws = _FakeCartesiaSttWebsocket(
        [
            {"type": "transcript", "text": "hello", "is_final": False},
            {"type": "transcript", "text": "hello world", "is_final": False},
            {"type": "done"},
        ]
    )
    stt = CartesiaSTT(client=_FakeCartesiaClient(ws))
    chunks = [c async for c in stt.transcribe(_audio_stream(b"x"))]
    assert chunks[-1].is_final is True
    assert chunks[-1].text == "hello world"


async def test_cartesia_stt_coalesces_multiple_finals() -> None:
    # A long utterance can produce multiple silence-endpointed segments. The
    # single-turn pipeline expects ONE final containing the full utterance, so
    # the adapter coalesces them.
    ws = _FakeCartesiaSttWebsocket(
        [
            {"type": "transcript", "text": "first half.", "is_final": True},
            {"type": "transcript", "text": "second half.", "is_final": True},
            {"type": "done"},
        ]
    )
    stt = CartesiaSTT(client=_FakeCartesiaClient(ws))
    chunks = [c async for c in stt.transcribe(_audio_stream(b"x"))]
    # Both finals are yielded as they arrive, plus one coalesced terminal final
    # for the full utterance.
    assert chunks[-1].is_final is True
    assert chunks[-1].text == "first half. second half."


async def test_cartesia_stt_empty_stream_yields_empty_final() -> None:
    # No audio at all → one empty final chunk (never hang). Matches the
    # Deepgram + buffered OpenAI Whisper adapters' defensive guarantee.
    ws = _FakeCartesiaSttWebsocket([])
    stt = CartesiaSTT(client=_FakeCartesiaClient(ws))
    chunks = [c async for c in stt.transcribe(_audio_stream())]
    assert len(chunks) == 1
    assert chunks[0].is_final is True
    assert chunks[0].text == ""
    # No WebSocket was even opened for an empty stream.
    assert ws.sent == []


async def test_cartesia_stt_ignores_non_transcript_events() -> None:
    # ``flush_done`` / ``done`` are lifecycle events with no text — the adapter
    # must not surface them as TranscriptChunks.
    ws = _FakeCartesiaSttWebsocket(
        [
            {"type": "flush_done", "request_id": "r1"},
            {"type": "transcript", "text": "hi", "is_final": True},
            {"type": "done", "request_id": "r1"},
        ]
    )
    stt = CartesiaSTT(client=_FakeCartesiaClient(ws))
    chunks = [c async for c in stt.transcribe(_audio_stream(b"x"))]
    # Exactly the one transcript event → one final chunk; no extra coalesced
    # final because there was exactly one real final and no trailing partial.
    assert len(chunks) == 1
    assert chunks[0].text == "hi"
    assert chunks[0].is_final is True


async def test_cartesia_stt_error_bubbles_up() -> None:
    # A socket error surfaces as an exception, which the pipeline turns into a
    # stage="stt" error event (graceful degrade per ADR 048 — same as Deepgram).
    ws = _ErroringCartesiaSttWebsocket([])
    stt = CartesiaSTT(client=_FakeCartesiaClient(ws))
    with pytest.raises(RuntimeError, match="socket blew up"):
        _ = [c async for c in stt.transcribe(_audio_stream(b"x"))]
