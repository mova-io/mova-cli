"""Semantic turn-detection seam (ADR 072) — the heuristic reference detector
and its wiring as the speculator's trigger.

The trained classifier ADR 072 designs stays gated; this covers the seam + the
dependency-free :class:`HeuristicTurnDetector`, and proves the pipeline fires a
speculation *immediately* on a semantically-complete interim (skipping the
quiet-gap) — the ADR 072 ↔ ADR 070 composition.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from movate.voice import HeuristicTurnDetector, NullTurnDetector, TurnDetector
from movate.voice.base import AudioChunk, TranscriptChunk
from movate.voice.doubles import FakeAgentTurn, FakeTTS
from movate.voice.observer import MetricsObserver
from movate.voice.pipeline import run_voice_pipeline


def test_protocol_conformance() -> None:
    assert isinstance(NullTurnDetector(), TurnDetector)
    assert isinstance(HeuristicTurnDetector(), TurnDetector)


def test_null_detector_never_complete() -> None:
    d = NullTurnDetector()
    assert d.is_complete("anything at all, even this.") is False


def test_heuristic_complete_on_terminal_punctuation() -> None:
    d = HeuristicTurnDetector()
    assert d.is_complete("turn on the lights.") is True
    assert d.is_complete("are you there?") is True
    assert d.is_complete("stop!") is True


def test_heuristic_incomplete_on_trailing_continuation_word() -> None:
    d = HeuristicTurnDetector()
    # Trails off mid-thought → not done.
    assert d.is_complete("turn on the") is False
    assert d.is_complete("reset my password and") is False
    assert d.is_complete("I want to") is False
    assert d.is_complete("um") is False


def test_heuristic_complete_on_full_clause_without_punct() -> None:
    d = HeuristicTurnDetector()
    assert d.is_complete("reset my password") is True
    assert d.is_complete("what is the status") is True


def test_heuristic_too_short_is_incomplete() -> None:
    d = HeuristicTurnDetector(min_words=3)
    assert d.is_complete("hi") is False
    assert d.is_complete("") is False
    assert d.is_complete("   ") is False


# ---------------------------------------------------------------------------
# Pipeline integration — the detector fires the speculation early (ADR 072↔070).
# ---------------------------------------------------------------------------


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"\x00\x00")


class _SlowFinalSTT:
    """Emits one complete interim, then waits a long time before the final.

    With a long ``speculation_quiet_gap_s`` AND a long pre-final wait, the only
    way a speculation can be in flight when the final lands is if the turn
    detector fired it immediately (skipping the quiet-gap). So this isolates the
    detector's early-trigger behavior.
    """

    name = "slow_final_stt"
    version = "0"

    def __init__(self, interim: str, final: str) -> None:
        self._interim = interim
        self._final = final

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
        keyterms=None,
        endpointing_ms=None,
    ) -> AsyncIterator[TranscriptChunk]:
        import asyncio  # noqa: PLC0415

        async for _ in audio:
            pass
        yield TranscriptChunk(text=self._interim, is_final=False)
        await asyncio.sleep(0.2)  # >> would exceed a normal quiet-gap debounce
        yield TranscriptChunk(text=self._final, is_final=True, confidence=1.0)


@pytest.mark.asyncio
async def test_detector_fires_speculation_before_quiet_gap() -> None:
    """A complete interim + detector → speculation fires now, commits on the
    matching final, even though the quiet-gap is set huge."""
    agent = FakeAgentTurn(answer="ok", speculatable=True)
    stt = _SlowFinalSTT("reset my password", "reset my password")
    obs = MetricsObserver()
    events = [
        e
        async for e in run_voice_pipeline(
            audio_in=_audio(),
            stt=stt,
            tts=FakeTTS(),
            agent=agent,
            speculative=True,
            speculation_quiet_gap_s=10.0,  # huge → debounce alone could NOT fire
            turn_detector=HeuristicTurnDetector(),
            observer=obs,
        )
    ]
    assert any(e.kind == "done" for e in events)
    # The detector fired it; it committed on the matching final.
    assert obs.events.get("turn_detected", 0) >= 1
    assert obs.events.get("speculation_started", 0) >= 1
    assert obs.events.get("speculation_committed", 0) == 1
    assert agent.prompts == ["reset my password"]


@pytest.mark.asyncio
async def test_no_detector_means_quiet_gap_alone() -> None:
    """Without a detector, a huge quiet-gap means no speculation fires in time
    (the legacy debounce-only behavior is unchanged)."""
    agent = FakeAgentTurn(answer="ok", speculatable=True)
    stt = _SlowFinalSTT("reset my password", "reset my password")
    obs = MetricsObserver()
    events = [
        e
        async for e in run_voice_pipeline(
            audio_in=_audio(),
            stt=stt,
            tts=FakeTTS(),
            agent=agent,
            speculative=True,
            speculation_quiet_gap_s=10.0,
            observer=obs,  # no turn_detector
        )
    ]
    assert any(e.kind == "done" for e in events)
    assert obs.events.get("turn_detected", 0) == 0
    assert obs.events.get("speculation_committed", 0) == 0
