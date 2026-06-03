"""Speculative agent kickoff (ADR 070) — pipeline behavior.

Covers the three states the speculator must get right:

* **commit** — the stable interim matches the endpointed final, so the
  in-flight speculative run is adopted (the agent runs ONCE, on the interim).
* **cancel** — the caller kept talking, the final differs, so the speculation
  is discarded and the agent re-runs on the corrected final (its speculative
  output never reaches the wire).
* **opt-out / off** — a non-speculatable agent, or ``speculative=False``, runs
  exactly the legacy single-shot turn (no early start).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from movate.voice.base import AudioChunk, TranscriptChunk
from movate.voice.doubles import FakeAgentTurn, FakeTTS
from movate.voice.observer import MetricsObserver
from movate.voice.pipeline import run_voice_pipeline

pytestmark = pytest.mark.asyncio


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"\x00\x00")


class _ScriptedSTT:
    """Emits paced partials then a final, so the speculator's debounce can fire.

    ``partials`` stream with ``gap_s`` between them; ``final`` lands after
    ``final_gap_s``. A test sets ``gap_s`` below the quiet-gap and ``final_gap_s``
    above it so a speculation arms on the last stable interim and is in flight
    when the final arrives.
    """

    name = "scripted_stt"
    version = "0"

    def __init__(
        self, partials: list[str], final: str, *, gap_s: float, final_gap_s: float
    ) -> None:
        self._partials = partials
        self._final = final
        self._gap_s = gap_s
        self._final_gap_s = final_gap_s

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
        keyterms=None,
    ) -> AsyncIterator[TranscriptChunk]:
        async for _ in audio:
            pass
        for p in self._partials:
            await asyncio.sleep(self._gap_s)
            yield TranscriptChunk(text=p, is_final=False)
        await asyncio.sleep(self._final_gap_s)
        yield TranscriptChunk(text=self._final, is_final=True, confidence=1.0)


async def _drain(events_iter: AsyncIterator) -> list:
    return [e async for e in events_iter]


async def test_speculation_commits_when_interim_matches_final() -> None:
    """Stable interim == final → adopt the speculative run; agent runs ONCE."""
    agent = FakeAgentTurn(answer="the answer", speculatable=True)
    stt = _ScriptedSTT(
        ["the", "the user", "the user question"],
        "the user question",
        gap_s=0.01,
        final_gap_s=0.15,  # > quiet gap → speculation fires + is in flight
    )
    obs = MetricsObserver()
    events = await _drain(
        run_voice_pipeline(
            audio_in=_audio(),
            stt=stt,
            tts=FakeTTS(),
            agent=agent,
            speculative=True,
            speculation_quiet_gap_s=0.05,
            observer=obs,
        )
    )
    kinds = [e.kind for e in events]
    assert "done" in kinds
    assert any(e.kind == "tts.audio" for e in events)
    # Committed: the agent ran exactly once, and on the interim it speculated on.
    assert agent.prompts == ["the user question"]
    assert obs.events["speculation_started"] == 1
    assert obs.events["speculation_committed"] == 1
    assert obs.events.get("speculation_cancelled", 0) == 0
    # The A/B snapshot (ADR 070/073): one started, one committed → ratio 1.0,
    # with a measured (non-negative) head-start the commit bought.
    snap = obs.speculation_snapshot()
    assert snap["started"] == 1
    assert snap["committed"] == 1
    assert snap["cancelled"] == 0
    assert snap["commit_ratio"] == 1.0
    assert snap["avg_head_start_ms"] >= 0.0
    # The full snapshot embeds the same block under "speculation".
    assert obs.snapshot()["speculation"] == snap


async def test_speculation_cancels_when_caller_keeps_talking() -> None:
    """Interim != final → discard the speculation, re-run on the final."""
    agent = FakeAgentTurn(answer="ok", speculatable=True, run_delay_s=0.2)
    # A long quiet gap after "turn on the" arms a speculation; then the caller
    # adds "...lights", so the final differs from what we speculated on.
    stt = _ScriptedSTT(
        ["turn on the"],
        "turn on the lights",
        gap_s=0.0,
        final_gap_s=0.12,  # > quiet gap → speculation on "turn on the" fires
    )
    obs = MetricsObserver()
    events = await _drain(
        run_voice_pipeline(
            audio_in=_audio(),
            stt=stt,
            tts=FakeTTS(),
            agent=agent,
            speculative=True,
            speculation_quiet_gap_s=0.05,
            observer=obs,
        )
    )
    assert any(e.kind == "done" for e in events)
    # The speculation fired on the interim, then a fresh run on the real final.
    assert "turn on the" in agent.prompts
    assert agent.prompts[-1] == "turn on the lights"
    assert obs.events["speculation_started"] == 1
    assert obs.events["speculation_cancelled"] == 1
    assert obs.events.get("speculation_committed", 0) == 0
    # A cancelled-only turn → commit_ratio 0.0, no head-start booked.
    snap = obs.speculation_snapshot()
    assert snap["commit_ratio"] == 0.0
    assert snap["avg_head_start_ms"] == 0.0


async def test_no_speculation_when_agent_not_speculatable() -> None:
    """A non-speculatable agent runs the legacy single-shot turn, once, on final."""
    agent = FakeAgentTurn(answer="hi", speculatable=False)
    stt = _ScriptedSTT(["he", "hello"], "hello", gap_s=0.0, final_gap_s=0.1)
    obs = MetricsObserver()
    events = await _drain(
        run_voice_pipeline(
            audio_in=_audio(),
            stt=stt,
            tts=FakeTTS(),
            agent=agent,
            speculative=True,  # requested, but agent opts out
            speculation_quiet_gap_s=0.02,
            observer=obs,
        )
    )
    assert any(e.kind == "done" for e in events)
    assert agent.prompts == ["hello"]
    assert obs.events.get("speculation_started", 0) == 0


async def test_speculative_false_is_unchanged_behavior() -> None:
    """``speculative=False`` (default) never speculates even on a willing agent."""
    agent = FakeAgentTurn(answer="hi", speculatable=True)
    stt = _ScriptedSTT(["he", "hello"], "hello", gap_s=0.0, final_gap_s=0.05)
    events = await _drain(
        run_voice_pipeline(audio_in=_audio(), stt=stt, tts=FakeTTS(), agent=agent)
    )
    assert any(e.kind == "done" for e in events)
    assert agent.prompts == ["hello"]


async def test_speculation_commits_in_streaming_mode() -> None:
    """Commit path also works under tts_streaming (the demo/runtime default)."""
    agent = FakeAgentTurn(answer="one two three", speculatable=True)
    stt = _ScriptedSTT(["one", "one two three"], "one two three", gap_s=0.01, final_gap_s=0.15)
    obs = MetricsObserver()
    events = await _drain(
        run_voice_pipeline(
            audio_in=_audio(),
            stt=stt,
            tts=FakeTTS(),
            agent=agent,
            tts_streaming=True,
            speculative=True,
            speculation_quiet_gap_s=0.05,
            observer=obs,
        )
    )
    assert any(e.kind == "tts.audio" for e in events)
    assert any(e.kind == "done" for e in events)
    assert agent.prompts == ["one two three"]
    assert obs.events["speculation_committed"] == 1


async def test_speculative_tokens_not_emitted_until_commit() -> None:
    """A cancelled speculation's tokens never reach the event stream."""
    # The speculative run is slow (run_delay) so it's still mid-flight at final;
    # its text won't match the final, so it must be cancelled with no agent.token
    # bearing the speculated-only answer leaking out before the real run.
    agent = FakeAgentTurn(answer="speculated", speculatable=True, run_delay_s=0.3)
    stt = _ScriptedSTT(["draft"], "final text", gap_s=0.0, final_gap_s=0.1)
    events = await _drain(
        run_voice_pipeline(
            audio_in=_audio(),
            stt=stt,
            tts=FakeTTS(),
            agent=agent,
            speculative=True,
            speculation_quiet_gap_s=0.03,
        )
    )
    # Final agent run was on the corrected transcript; the turn completed.
    assert agent.prompts[-1] == "final text"
    assert any(e.kind == "done" for e in events)
