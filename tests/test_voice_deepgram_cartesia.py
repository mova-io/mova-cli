"""T1 low-latency voice pair — Deepgram STT + Cartesia TTS (ADR 048/049).

Mirrors ``tests/test_voice_protocols.py`` for the streaming-native pair behind
the ADR 048 D3 seams (``SpeechToTextProvider`` / ``TextToSpeechProvider``).
These tests pin:

* runtime-checkable conformance of both adapters against the Protocols (so a
  future provider can be checked the same way ``isinstance(p, BaseLLMProvider)``
  works);
* the adapters' lazy SDK import — constructing them with an injected fake client
  does NOT require the ``deepgram`` / ``cartesia`` packages (so the whole suite
  runs without ``mdk[voice]`` installed);
* Deepgram's streaming behavior: interim partials then an endpointed
  ``is_final=True`` chunk, the inbound audio drained into the socket, and the
  defensive "promote the last partial to a final if the socket only emitted
  interims" guarantee;
* Cartesia's streaming behavior: the buffered text → one synthesis call with
  raw-PCM output format + the resolved voice id, frames streamed straight
  through as ``AudioChunk``s, and blank text → no synthesis call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from movate.voice import (
    AudioChunk,
    CartesiaTTS,
    DeepgramSTT,
    SpeechToTextProvider,
    TextToSpeechProvider,
)


async def _audio_stream(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


async def _text_stream(*parts: str) -> AsyncIterator[str]:
    for p in parts:
        yield p


# ---------------------------------------------------------------------------
# Protocol conformance (runtime_checkable) + lazy import
# ---------------------------------------------------------------------------


def test_t1_adapters_satisfy_protocols() -> None:
    # Constructing with a None client must NOT import the provider SDK (lazy) —
    # this whole test module runs without deepgram/cartesia installed.
    assert isinstance(DeepgramSTT(), SpeechToTextProvider)
    assert isinstance(CartesiaTTS(), TextToSpeechProvider)


# ---------------------------------------------------------------------------
# Deepgram STT — with an injected fake live-transcription socket
# ---------------------------------------------------------------------------


class _FakeDeepgramConnection:
    """Fake of Deepgram's async live-transcription socket.

    Captures registered handlers via ``on(event, handler)`` and the audio sent
    via ``send(...)``. On ``finish()`` it replays a scripted sequence of
    transcript events (then a close) through the registered transcript/close
    handlers — the same callback shape the SDK drives.
    """

    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = scripted
        self._handlers: dict[str, Any] = {}
        self.sent: list[bytes] = []
        self.started_with: Any = None

    def on(self, event: Any, handler: Any) -> None:
        # The adapter passes string event names when no SDK enum is present
        # (our fake path). Normalize to a lowercase tag.
        name = str(event).lower()
        if "result" in name or "transcript" in name:
            self._handlers["transcript"] = handler
        elif "close" in name:
            self._handlers["close"] = handler
        elif "error" in name:
            self._handlers["error"] = handler

    async def start(self, options: Any) -> None:
        self.started_with = options

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def finish(self) -> None:
        transcript = self._handlers.get("transcript")
        if transcript is not None:
            for event in self._scripted:
                await transcript(result=event)
        close = self._handlers.get("close")
        if close is not None:
            await close()


class _FakeListenV:
    def __init__(self, connection: _FakeDeepgramConnection) -> None:
        self._connection = connection

    def v(self, _version: str) -> _FakeDeepgramConnection:
        return self._connection


class _FakeListen:
    def __init__(self, connection: _FakeDeepgramConnection) -> None:
        self.asyncwebsocket = _FakeListenV(connection)


class _FakeDeepgramClient:
    def __init__(self, connection: _FakeDeepgramConnection) -> None:
        self.listen = _FakeListen(connection)


def _dg_event(
    transcript: str,
    *,
    is_final: bool,
    speech_final: bool | None = None,
    confidence: float | None = None,
) -> dict:
    """Build a Deepgram-shaped transcript event (channel.alternatives[0]).

    Deepgram emits TWO independent flags:
      - ``is_final``: this segment's text won't be revised (mid-stream commits).
      - ``speech_final``: VAD decided the speaker finished the turn.

    Default ``speech_final = is_final`` keeps existing tests' semantics — the
    short-utterance case where the two coincide. Pass speech_final=False with
    is_final=True to model a mid-stream commit during continuous speech.
    """
    alt: dict[str, Any] = {"transcript": transcript}
    if confidence is not None:
        alt["confidence"] = confidence
    return {
        "is_final": is_final,
        "speech_final": is_final if speech_final is None else speech_final,
        "channel": {"alternatives": [alt]},
    }


async def test_deepgram_streams_partials_then_final_and_sends_audio() -> None:
    conn = _FakeDeepgramConnection(
        [
            _dg_event("the", is_final=False),
            _dg_event("the full", is_final=False),
            _dg_event("the full utterance", is_final=True, confidence=0.97),
        ]
    )
    stt = DeepgramSTT(finish_grace_seconds=0, client=_FakeDeepgramClient(conn))
    chunks = [
        c async for c in stt.transcribe(_audio_stream(b"aa", b"bb"), language="en-US", api_key="k")
    ]
    assert [c.text for c in chunks] == ["the", "the full", "the full utterance"]
    assert [c.is_final for c in chunks] == [False, False, True]
    # Confidence rides on the final chunk from the best alternative.
    assert chunks[-1].confidence == pytest.approx(0.97)
    # The whole inbound audio stream was pumped into the socket.
    assert conn.sent == [b"aa", b"bb"]
    # The language hint reached the socket options.
    assert conn.started_with["language"] == "en-US"
    # Endpointing defaults (regression — Deepgram's stock ~10 ms cuts speech
    # off on mid-sentence pauses; we ship 1500 + utterance_end backstop 2500
    # which tolerates natural pauses without adding multi-sec turn-end lag).
    assert conn.started_with["endpointing"] == 1500
    assert conn.started_with["utterance_end_ms"] == 2500


async def test_deepgram_mid_stream_is_final_does_not_end_turn() -> None:
    """Regression: continuous speech with Deepgram per-segment commits.

    User speaks a long multi-clause prompt with no real pause. Deepgram
    commits the first clause (``is_final=True, speech_final=False``) while
    the user is still talking, then continues with partials, then the full
    end-of-turn (``speech_final=True``).

    Before the fix, the adapter yielded ``is_final=True`` on the first
    commit and the single-turn voice pipeline ran the agent on that
    partial — chopping the prompt at a comma. After the fix, the only
    ``is_final=True`` we yield is the speech_final at the end, with the
    full accumulated transcript.
    """
    conn = _FakeDeepgramConnection(
        [
            _dg_event("One of the VIP users", is_final=False),
            # Mid-stream commit — Deepgram is sure of these words, but the
            # user is STILL TALKING (no silence → no speech_final).
            _dg_event(
                "One of the VIP users reported that his VPN connects",
                is_final=True,
                speech_final=False,
            ),
            _dg_event(
                "One of the VIP users reported that his VPN connects but no",
                is_final=False,
            ),
            _dg_event(
                "but no network access help me resolve this",
                is_final=True,
                speech_final=True,
                confidence=0.96,
            ),
        ]
    )
    stt = DeepgramSTT(finish_grace_seconds=0, client=_FakeDeepgramClient(conn))
    chunks = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k")]
    # Exactly ONE is_final=True yielded — the speech_final one. Mid-stream
    # commit must NOT have been forwarded as final.
    finals = [c for c in chunks if c.is_final]
    assert len(finals) == 1, f"expected 1 final, got {len(finals)}: {[c.text for c in finals]}"
    # And it carries the FULL accumulated utterance, not just the last segment.
    assert "VIP users" in finals[0].text
    assert "network access" in finals[0].text
    assert "help me resolve this" in finals[0].text


async def test_deepgram_custom_endpointing_kwargs_reach_socket() -> None:
    """A caller wanting snappy back-and-forth can dial endpointing down."""
    conn = _FakeDeepgramConnection([_dg_event("hi", is_final=True)])
    stt = DeepgramSTT(
        finish_grace_seconds=0,
        endpointing_ms=400,
        utterance_end_ms=None,  # disable backstop
        client=_FakeDeepgramClient(conn),
    )
    _ = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k")]
    assert conn.started_with["endpointing"] == 400
    assert "utterance_end_ms" not in conn.started_with


async def test_deepgram_per_call_endpointing_overrides_constructor() -> None:
    """ADR 073 D3: a per-call ``endpointing_ms`` wins over the constructor value
    for that turn (a deliberate-speaker agent holds longer)."""
    conn = _FakeDeepgramConnection([_dg_event("hi", is_final=True)])
    stt = DeepgramSTT(
        finish_grace_seconds=0,
        endpointing_ms=1500,  # session/adapter default
        client=_FakeDeepgramClient(conn),
    )
    _ = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k", endpointing_ms=800)]
    assert conn.started_with["endpointing"] == 800


async def test_deepgram_per_call_endpointing_none_keeps_constructor() -> None:
    """``endpointing_ms=None`` (the default) keeps the adapter value — byte-for-
    byte the prior behavior."""
    conn = _FakeDeepgramConnection([_dg_event("hi", is_final=True)])
    stt = DeepgramSTT(
        finish_grace_seconds=0,
        endpointing_ms=1500,
        client=_FakeDeepgramClient(conn),
    )
    _ = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k")]
    assert conn.started_with["endpointing"] == 1500


async def test_deepgram_keyterms_reach_socket_as_keyterm_on_nova3() -> None:
    """Domain vocab is boosted via nova-3 ``keyterm`` prompting (accuracy win)."""
    conn = _FakeDeepgramConnection([_dg_event("VPN for the VIP", is_final=True)])
    stt = DeepgramSTT(
        finish_grace_seconds=0,
        keyterms=["VPN", "VIP", "Mova-iO"],
        client=_FakeDeepgramClient(conn),
    )
    _ = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k")]
    assert conn.started_with["keyterm"] == ["VPN", "VIP", "Mova-iO"]
    assert "keywords" not in conn.started_with


async def test_deepgram_keyterms_fall_back_to_keywords_on_nova2() -> None:
    """On nova-2 the same list rides the legacy ``keywords`` boosting param."""
    conn = _FakeDeepgramConnection([_dg_event("hi", is_final=True)])
    stt = DeepgramSTT(
        finish_grace_seconds=0,
        model="nova-2",
        keyterms=["Okta", "SSO"],
        client=_FakeDeepgramClient(conn),
    )
    _ = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k")]
    assert conn.started_with["keywords"] == ["Okta", "SSO"]
    assert "keyterm" not in conn.started_with


async def test_deepgram_no_keyterms_sends_neither_param() -> None:
    """Default (no keyterms) is byte-for-byte the prior behavior."""
    conn = _FakeDeepgramConnection([_dg_event("hi", is_final=True)])
    stt = DeepgramSTT(finish_grace_seconds=0, client=_FakeDeepgramClient(conn))
    _ = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k")]
    assert "keyterm" not in conn.started_with
    assert "keywords" not in conn.started_with


async def test_deepgram_per_call_keyterms_merge_with_constructor() -> None:
    """ADR 071 D4: per-call keyterms union with the constructor list, de-duped."""
    conn = _FakeDeepgramConnection([_dg_event("hi", is_final=True)])
    stt = DeepgramSTT(
        finish_grace_seconds=0,
        keyterms=["VPN", "Okta"],  # tenant/default level
        client=_FakeDeepgramClient(conn),
    )
    _ = [
        c
        async for c in stt.transcribe(
            _audio_stream(b"a"),
            api_key="k",
            keyterms=["Okta", "Mova-iO"],  # per-agent
        )
    ]
    # Union, order-preserving, de-duped (Okta appears once).
    assert conn.started_with["keyterm"] == ["VPN", "Okta", "Mova-iO"]


async def test_deepgram_per_call_keyterms_only() -> None:
    """Per-call keyterms work with no constructor list (the runtime's path)."""
    conn = _FakeDeepgramConnection([_dg_event("hi", is_final=True)])
    stt = DeepgramSTT(finish_grace_seconds=0, client=_FakeDeepgramClient(conn))
    _ = [c async for c in stt.transcribe(_audio_stream(b"a"), api_key="k", keyterms=["SSO"])]
    assert conn.started_with["keyterm"] == ["SSO"]


async def test_deepgram_promotes_last_partial_when_no_final() -> None:
    # Socket emits only interims (no endpointed final). The adapter must
    # promote the last partial to is_final=True so the pipeline's
    # "wait for is_final" loop unblocks rather than hangs.
    conn = _FakeDeepgramConnection(
        [
            _dg_event("hello", is_final=False),
            _dg_event("hello world", is_final=False),
        ]
    )
    stt = DeepgramSTT(finish_grace_seconds=0, client=_FakeDeepgramClient(conn))
    chunks = [c async for c in stt.transcribe(_audio_stream(b"x"))]
    assert chunks[-1].is_final is True
    assert chunks[-1].text == "hello world"


async def test_deepgram_empty_stream_yields_empty_final() -> None:
    # No transcript events at all → one empty final chunk (never hang).
    conn = _FakeDeepgramConnection([])
    stt = DeepgramSTT(finish_grace_seconds=0, client=_FakeDeepgramClient(conn))
    chunks = [c async for c in stt.transcribe(_audio_stream())]
    assert len(chunks) == 1
    assert chunks[0].is_final is True
    assert chunks[0].text == ""


async def test_deepgram_error_event_raises() -> None:
    # An error event from the socket surfaces as an exception, which the
    # pipeline turns into a stage="stt" error event (graceful degrade, D8).
    class _ErroringConnection(_FakeDeepgramConnection):
        async def finish(self) -> None:
            handler = self._handlers.get("error")
            if handler is not None:
                await handler(error="socket blew up")

    conn = _ErroringConnection([])
    stt = DeepgramSTT(finish_grace_seconds=0, client=_FakeDeepgramClient(conn))
    with pytest.raises(RuntimeError, match="socket blew up"):
        _ = [c async for c in stt.transcribe(_audio_stream(b"x"))]


# ---------------------------------------------------------------------------
# Cartesia TTS — with an injected fake streaming client
# ---------------------------------------------------------------------------


class _FakeCartesiaTTS:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.calls: list[dict] = []

    def bytes(self, **kwargs: Any) -> AsyncIterator[bytes]:
        self.calls.append(kwargs)
        frames = self._frames

        async def _gen() -> AsyncIterator[bytes]:
            for f in frames:
                yield f

        return _gen()


class _FakeCartesiaClient:
    def __init__(self, tts: _FakeCartesiaTTS) -> None:
        self.tts = tts


async def test_cartesia_buffers_text_and_streams_frames() -> None:
    fake_tts = _FakeCartesiaTTS([b"frame1", b"frame2", b"frame3"])
    tts = CartesiaTTS(client=_FakeCartesiaClient(fake_tts))
    audio = [c async for c in tts.synthesize(_text_stream("hello ", "there"), voice_id="voice-xyz")]
    # Each emitted frame becomes one AudioChunk (streamed, not re-sliced).
    assert [c.data for c in audio] == [b"frame1", b"frame2", b"frame3"]
    assert all(c.codec == "pcm16" for c in audio)
    # The token stream was buffered into ONE synthesis call.
    assert len(fake_tts.calls) == 1
    call = fake_tts.calls[0]
    assert call["transcript"] == "hello there"
    # Raw PCM output format so bytes map onto pcm16 with no container.
    assert call["output_format"]["encoding"] == "pcm_s16le"
    assert call["output_format"]["container"] == "raw"
    # The caller-supplied voice id was passed through.
    assert call["voice"]["id"] == "voice-xyz"


async def test_cartesia_default_voice_when_unset() -> None:
    fake_tts = _FakeCartesiaTTS([b"frame"])
    tts = CartesiaTTS(client=_FakeCartesiaClient(fake_tts))
    _ = [c async for c in tts.synthesize(_text_stream("hi"))]
    # voice_id="" → the adapter's configured default, not an empty id.
    assert fake_tts.calls[0]["voice"]["id"]


async def test_cartesia_blank_text_makes_no_call() -> None:
    fake_tts = _FakeCartesiaTTS([b"unused"])
    tts = CartesiaTTS(client=_FakeCartesiaClient(fake_tts))
    audio = [c async for c in tts.synthesize(_text_stream("   "))]
    assert audio == []
    assert fake_tts.calls == []
