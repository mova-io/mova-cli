"""Deepgram Aura TTS adapter — T1 streaming text-to-speech (parity with Lyzr).

Mirrors the Cartesia/ElevenLabs slice of ``tests/test_voice_deepgram_cartesia.py``
for the Deepgram-side TTS, behind the ADR 048 D3
:class:`~movate.voice.base.TextToSpeechProvider` seam. Pins:

* runtime-checkable conformance against the Protocol (an injected fake satisfies
  it without ``deepgram-sdk`` installed — the lazy-import guarantee);
* streaming behavior: token deltas are buffered into ONE synthesis call, each
  emitted ``httpx.Response``-shaped frame becomes one :class:`AudioChunk`;
* default-voice fallback when ``voice_id=""``;
* empty/whitespace text → no synthesis call (and no audio);
* error propagation through the streaming iterator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from movate.voice import DeepgramAuraTTS, TextToSpeechProvider


async def _text_stream(*parts: str) -> AsyncIterator[str]:
    for p in parts:
        yield p


# ---------------------------------------------------------------------------
# Fakes — model the ``client.speak.asyncrest.v("1").stream_raw(...)`` shape
# the adapter calls, and the ``httpx.Response``-ish aiter_bytes() it iterates.
# ---------------------------------------------------------------------------


class _FakeAuraResponse:
    """Stand-in for ``httpx.Response`` — exposes ``aiter_bytes()`` + ``aclose()``."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.closed = False

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        frames = self._frames

        async def _gen() -> AsyncIterator[bytes]:
            for f in frames:
                yield f

        return _gen()

    async def aclose(self) -> None:
        self.closed = True


class _ErroringAuraResponse:
    """Like ``_FakeAuraResponse`` but the byte iterator raises mid-stream."""

    def __init__(self, message: str) -> None:
        self._message = message
        self.closed = False

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        message = self._message

        async def _gen() -> AsyncIterator[bytes]:
            raise RuntimeError(message)
            yield b""  # pragma: no cover - unreachable; satisfies async-gen type

        return _gen()

    async def aclose(self) -> None:
        self.closed = True


class _FakeAsyncSpeakRESTClient:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def stream_raw(self, *, source: Any, options: Any) -> Any:
        self.calls.append({"source": source, "options": options})
        return self._response


class _FakeAsyncRestVersion:
    def __init__(self, rest_client: _FakeAsyncSpeakRESTClient) -> None:
        self._rest_client = rest_client

    def v(self, _version: str) -> _FakeAsyncSpeakRESTClient:
        return self._rest_client


class _FakeSpeak:
    def __init__(self, rest_client: _FakeAsyncSpeakRESTClient) -> None:
        self.asyncrest = _FakeAsyncRestVersion(rest_client)


class _FakeDeepgramClient:
    def __init__(self, rest_client: _FakeAsyncSpeakRESTClient) -> None:
        self.speak = _FakeSpeak(rest_client)


def _make_tts(
    response: Any, *, default_voice: str | None = None
) -> tuple[DeepgramAuraTTS, _FakeAsyncSpeakRESTClient]:
    rest_client = _FakeAsyncSpeakRESTClient(response)
    client = _FakeDeepgramClient(rest_client)
    kwargs: dict[str, Any] = {"client": client}
    if default_voice is not None:
        kwargs["default_voice"] = default_voice
    return DeepgramAuraTTS(**kwargs), rest_client


# ---------------------------------------------------------------------------
# Protocol conformance + lazy SDK import
# ---------------------------------------------------------------------------


def test_aura_satisfies_tts_protocol_without_sdk() -> None:
    # Construction with an injected fake must NOT import ``deepgram`` — the
    # whole suite runs without ``mdk[voice]`` installed.
    assert isinstance(DeepgramAuraTTS(), TextToSpeechProvider)


def test_aura_advertises_name_and_version() -> None:
    """The registry / parity check key off ``.name`` — pin it as part of the contract."""
    assert DeepgramAuraTTS.name == "deepgram_aura"
    assert DeepgramAuraTTS.version == "1"


# ---------------------------------------------------------------------------
# Streaming behavior
# ---------------------------------------------------------------------------


async def test_aura_buffers_text_and_streams_frames() -> None:
    response = _FakeAuraResponse([b"frame1", b"frame2", b"frame3"])
    tts, rest_client = _make_tts(response)
    audio = [
        c async for c in tts.synthesize(_text_stream("hello ", "there"), voice_id="aura-2-luna-en")
    ]
    # Each emitted frame becomes one AudioChunk (streamed, not re-sliced).
    assert [c.data for c in audio] == [b"frame1", b"frame2", b"frame3"]
    assert all(c.codec == "pcm16" for c in audio)
    assert all(c.sample_rate == 24_000 for c in audio)
    # The token stream was buffered into ONE synthesis call.
    assert len(rest_client.calls) == 1
    call = rest_client.calls[0]
    # The full utterance reached Aura as the JSON text source body.
    assert call["source"] == {"text": "hello there"}
    # Raw PCM output format so bytes map onto pcm16 with no container.
    opts = call["options"]
    assert opts["encoding"] == "linear16"
    assert opts["container"] == "none"
    assert opts["sample_rate"] == 24_000
    # Aura selects voice via the model id; caller-supplied id passed through.
    assert opts["model"] == "aura-2-luna-en"
    # The streamed response was closed (no httpx connection leak).
    assert response.closed is True


async def test_aura_default_voice_when_unset() -> None:
    response = _FakeAuraResponse([b"frame"])
    tts, rest_client = _make_tts(response)
    _ = [c async for c in tts.synthesize(_text_stream("hi"))]
    # voice_id="" → the adapter's configured default Aura voice, not an empty id.
    model = rest_client.calls[0]["options"]["model"]
    assert model
    assert model == "aura-2-thalia-en"  # the documented module default


async def test_aura_respects_constructor_default_voice() -> None:
    response = _FakeAuraResponse([b"frame"])
    tts, rest_client = _make_tts(response, default_voice="aura-2-arcas-en")
    _ = [c async for c in tts.synthesize(_text_stream("hi"))]
    assert rest_client.calls[0]["options"]["model"] == "aura-2-arcas-en"


async def test_aura_blank_text_makes_no_call() -> None:
    response = _FakeAuraResponse([b"unused"])
    tts, rest_client = _make_tts(response)
    audio = [c async for c in tts.synthesize(_text_stream("   "))]
    assert audio == []
    assert rest_client.calls == []
    # And we never opened the response, so nothing to close.
    assert response.closed is False


async def test_aura_empty_stream_safe() -> None:
    """An empty text iterator must not crash and must not call the API."""
    response = _FakeAuraResponse([b"unused"])
    tts, rest_client = _make_tts(response)
    audio = [c async for c in tts.synthesize(_text_stream())]
    assert audio == []
    assert rest_client.calls == []


async def test_aura_error_in_stream_propagates() -> None:
    """A mid-stream provider error surfaces as an exception, which the pipeline
    turns into a stage="tts" error event (graceful degrade, ADR 048 D8)."""
    response = _ErroringAuraResponse("aura blew up")
    tts, _ = _make_tts(response)
    with pytest.raises(RuntimeError, match="aura blew up"):
        _ = [c async for c in tts.synthesize(_text_stream("hello"))]
