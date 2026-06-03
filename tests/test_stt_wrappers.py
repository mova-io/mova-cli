"""Cost/quality STT wrappers: silence gating + confidence-gated escalation."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    ConfidenceGatedSTT,
    FakeSTT,
    SilenceGatedSTT,
    TranscriptChunk,
    frame_rms,
    is_silent,
)


def _pcm(amplitude: int, n: int = 160) -> bytes:
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _loud() -> AudioChunk:
    return AudioChunk(data=_pcm(8000))


def _quiet() -> AudioChunk:
    return AudioChunk(data=_pcm(0))


async def _audio(*chunks: AudioChunk) -> AsyncIterator[AudioChunk]:
    for c in chunks:
        yield c


# --- VAD primitives --------------------------------------------------------


def test_frame_rms_and_is_silent() -> None:
    assert frame_rms(_quiet()) == 0.0
    assert frame_rms(_loud()) > 1000.0
    assert is_silent(_quiet())
    assert not is_silent(_loud())


# --- SilenceGatedSTT -------------------------------------------------------


async def test_silence_gate_drops_long_silence() -> None:
    inner = FakeSTT("hi")
    gated = SilenceGatedSTT(inner, hangover_frames=2)
    # 2 loud + 6 silent → inner should receive the 2 loud + 2 hangover = 4.
    chunks = [_loud(), _loud(), _quiet(), _quiet(), _quiet(), _quiet(), _quiet(), _quiet()]
    out = [t async for t in gated.transcribe(_audio(*chunks))]
    assert any(t.is_final for t in out)
    assert len(inner.received) == 4  # 2 speech + 2 hangover frames


async def test_silence_gate_keeps_all_speech() -> None:
    inner = FakeSTT("hi")
    gated = SilenceGatedSTT(inner)
    chunks = [_loud(), _loud(), _loud()]
    _ = [t async for t in gated.transcribe(_audio(*chunks))]
    assert len(inner.received) == 3


# --- ConfidenceGatedSTT ----------------------------------------------------


class _ConfSTT:
    """STT yielding a fixed transcript + confidence; records call count."""

    def __init__(self, name: str, text: str, confidence: float | None) -> None:
        self.name = name
        self.version = "0.0.1"
        self._text = text
        self._confidence = confidence
        self.calls = 0

    async def transcribe(
        self, audio, *, language=None, api_key=None
    ) -> AsyncIterator[TranscriptChunk]:
        self.calls += 1
        async for _ in audio:
            pass
        yield TranscriptChunk(text=self._text, is_final=True, confidence=self._confidence)


async def _final(stt) -> str:
    out = [t async for t in stt.transcribe(_audio(_loud()))]
    return next(t.text for t in out if t.is_final)


async def test_low_confidence_escalates() -> None:
    primary = _ConfSTT("cheap", "garbled", confidence=0.3)
    escalation = _ConfSTT("premium", "the real transcript", confidence=0.95)
    gated = ConfidenceGatedSTT(primary, escalation, min_confidence=0.6)
    assert await _final(gated) == "the real transcript"
    assert primary.calls == 1
    assert escalation.calls == 1  # escalated


async def test_high_confidence_does_not_escalate() -> None:
    primary = _ConfSTT("cheap", "clear transcript", confidence=0.9)
    escalation = _ConfSTT("premium", "should not be used", confidence=1.0)
    gated = ConfidenceGatedSTT(primary, escalation, min_confidence=0.6)
    assert await _final(gated) == "clear transcript"
    assert escalation.calls == 0  # cheap result was confident enough


async def test_none_confidence_is_trusted() -> None:
    primary = _ConfSTT("cheap", "no score", confidence=None)
    escalation = _ConfSTT("premium", "unused", confidence=1.0)
    gated = ConfidenceGatedSTT(primary, escalation, min_confidence=0.6)
    assert await _final(gated) == "no score"
    assert escalation.calls == 0  # no signal → don't spend on escalation
