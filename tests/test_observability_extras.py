"""Observability through composites: real serving engine + silence savings."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    ConfidenceGatedSTT,
    FakeSTT,
    MetricsObserver,
    SilenceGatedSTT,
    TranscriptChunk,
)


def _pcm(amp: int, n: int = 160) -> bytes:
    return struct.pack(f"<{n}h", *([amp] * n))


async def _audio(*chunks: AudioChunk) -> AsyncIterator[AudioChunk]:
    for c in chunks:
        yield c


class _ConfSTT:
    def __init__(self, name: str, text: str, conf: float | None) -> None:
        self.name = name
        self.version = "0"
        self._t = text
        self._c = conf

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None, endpointing_ms=None
    ) -> AsyncIterator[TranscriptChunk]:
        async for _ in audio:
            pass
        yield TranscriptChunk(text=self._t, is_final=True, confidence=self._c)


async def _drain(stt) -> None:
    async for _ in stt.transcribe(_audio(AudioChunk(data=_pcm(8000)))):
        pass


async def test_confidence_gate_reports_real_engine_on_accept() -> None:
    m = MetricsObserver()
    gate = ConfidenceGatedSTT(
        _ConfSTT("cheap", "ok", 0.9), _ConfSTT("premium", "x", 1.0), observer=m
    )
    await _drain(gate)
    snap = m.snapshot()
    assert snap["engines"] == {"cheap": 1}  # the REAL engine, not "confidence_gated_stt"
    assert snap["escalations"] == 0
    assert gate.effective_provider == "cheap"


async def test_confidence_gate_reports_real_engine_on_escalation() -> None:
    m = MetricsObserver()
    gate = ConfidenceGatedSTT(
        _ConfSTT("cheap", "ok", 0.2), _ConfSTT("premium", "x", 0.95), observer=m
    )
    await _drain(gate)
    snap = m.snapshot()
    assert snap["engines"] == {"premium": 1}
    assert snap["escalations"] == 1
    assert gate.effective_provider == "premium"


async def test_silence_gate_reports_savings() -> None:
    m = MetricsObserver()
    gate = SilenceGatedSTT(FakeSTT("hi"), hangover_frames=1, observer=m)
    loud = AudioChunk(data=_pcm(8000))
    quiet = AudioChunk(data=_pcm(0))
    async for _ in gate.transcribe(_audio(loud, loud, quiet, quiet, quiet, quiet)):
        pass
    snap = m.snapshot()
    assert snap["silence_frames_dropped"] == 3  # 4 silent - 1 hangover
    assert snap["silence_frames_kept"] == 3  # 2 speech + 1 hangover


def test_metrics_snapshot_exposes_new_fields() -> None:
    snap = MetricsObserver().snapshot()
    for key in ("engines", "escalations", "silence_frames_dropped", "silence_frames_kept"):
        assert key in snap
