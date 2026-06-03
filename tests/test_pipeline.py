"""The voice pipeline driver, behind the ``AgentTurn`` seam (ADR 067).

This is the standalone analog of mdk's load-bearing pipeline test. Where mdk
asserts the *Executor* is reused unchanged, here we assert the pipeline drives
**any** :class:`~movate.voice.agent_turn.AgentTurn` — proven with the framework-free
:class:`~movate.voice.doubles.FakeAgentTurn`:

* a turn of audio drives the agent and round-trips its answer through TTS;
* the event protocol comes out in pipeline order
  (transcript.final → agent.token* → tts.audio* → done);
* the transcript STT produced is exactly what the agent ran on;
* failure modes degrade per ADR 048 D8 (STT error stops; agent error stops
  before TTS; TTS error still yields the answer text + a done frame);
* the latency badge + barge-in behave as in the original pipeline.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    FakeAgentTurn,
    FakeSTT,
    FakeTTS,
    VoiceEvent,
    VoiceTurnLatency,
    compute_turn_latency,
    format_latency_badge,
    run_voice_pipeline,
)
from movate.voice.agent_turn import AgentTurnError
from movate.voice.base import TranscriptChunk


async def _audio_in(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


async def _collect(gen: AsyncIterator[VoiceEvent]) -> list[VoiceEvent]:
    return [e async for e in gen]


# ---------------------------------------------------------------------------
# Happy path — the pipeline drives an AgentTurn
# ---------------------------------------------------------------------------


async def test_pipeline_drives_agent_turn_and_round_trips_answer() -> None:
    """audio → STT → AgentTurn → TTS → audio, in pipeline order."""
    stt = FakeSTT("turn the lights on")
    tts = FakeTTS()
    agent = FakeAgentTurn("spoken answer")

    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"frame-1", b"frame-2"), stt=stt, tts=tts, agent=agent
        )
    )

    kinds = [e.kind for e in events]
    assert "transcript.final" in kinds
    assert "agent.token" in kinds
    assert "tts.audio" in kinds
    assert kinds[-1] == "done"
    assert "error" not in kinds

    # The transcript STT produced is exactly what the agent ran on.
    final = next(e for e in events if e.kind == "transcript.final")
    assert final.text == "turn the lights on"
    assert stt.received == [b"frame-1", b"frame-2"]
    assert agent.prompts == ["turn the lights on"]

    # The done frame carries the agent's run id + status.
    done = events[-1]
    assert done.status == "ok"
    assert done.run_id == "run-fake"

    # The agent's answer round-tripped through TTS into audio bytes.
    audio = b"".join(e.audio.data for e in events if e.kind == "tts.audio" and e.audio)
    assert audio.decode("utf-8") == "spoken answer"
    assert tts.spoken == ["spoken answer"]


async def test_pipeline_streams_partials_before_final() -> None:
    """Partials stream first; the agent runs only on the final transcript."""
    stt = FakeSTT("hello world", partials=["hello", "hello wor"])
    agent = FakeAgentTurn("ok")

    events = await _collect(
        run_voice_pipeline(audio_in=_audio_in(b"a"), stt=stt, tts=FakeTTS(), agent=agent)
    )
    partials = [e.text for e in events if e.kind == "transcript.partial"]
    assert partials == ["hello", "hello wor"]
    assert agent.prompts == ["hello world"]


async def test_pipeline_passes_language_and_session_through() -> None:
    """``language`` / ``session_id`` are threaded to the AgentTurn unchanged."""
    agent = FakeAgentTurn("ok")
    await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=FakeTTS(),
            agent=agent,
            language="en-US",
            session_id="sess-7",
        )
    )
    assert agent.languages == ["en-US"]
    assert agent.session_ids == ["sess-7"]


async def test_pipeline_falls_back_to_streamed_tokens_when_result_answer_empty() -> None:
    """When the result carries no answer_text, the pipeline speaks the joined
    streamed tokens instead."""
    agent = FakeAgentTurn("token one two", answer_in_result=False)
    tts = FakeTTS()
    await _collect(
        run_voice_pipeline(audio_in=_audio_in(b"a"), stt=FakeSTT("hi"), tts=tts, agent=agent)
    )
    assert tts.spoken == ["token one two"]


# ---------------------------------------------------------------------------
# Failure modes (ADR 048 D8)
# ---------------------------------------------------------------------------


class _BoomSTT:
    name = "boom_stt"
    version = "0.0.1"

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None, endpointing_ms=None
    ):
        async for _ in audio:
            pass
        raise RuntimeError("stt provider down")
        yield  # pragma: no cover - unreachable, makes this an async generator


class _NoFinalSTT:
    name = "no_final_stt"
    version = "0.0.1"

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None, endpointing_ms=None
    ):
        async for _ in audio:
            pass
        yield TranscriptChunk(text="partial only", is_final=False)


class _BoomTTS:
    name = "boom_tts"
    version = "0.0.1"

    async def synthesize(
        self, text, *, voice_id="", codec="pcm16", api_key=None, keyterms=None, endpointing_ms=None
    ):
        async for _ in text:
            pass
        raise RuntimeError("tts provider down")
        yield  # pragma: no cover - unreachable


async def test_pipeline_stt_failure_emits_error_and_stops() -> None:
    """STT down mid-stream → an error (stage=stt); the agent never runs."""
    agent = FakeAgentTurn("ok")
    events = await _collect(
        run_voice_pipeline(audio_in=_audio_in(b"a"), stt=_BoomSTT(), tts=FakeTTS(), agent=agent)
    )
    assert [e.kind for e in events] == ["error"]
    assert events[0].stage == "stt"
    assert events[0].code == "stt_error"
    assert agent.prompts == []


async def test_pipeline_stt_no_final_emits_error() -> None:
    """STT streamed only partials, never endpointed → a clear error, not a hang."""
    agent = FakeAgentTurn("ok")
    events = await _collect(
        run_voice_pipeline(audio_in=_audio_in(b"a"), stt=_NoFinalSTT(), tts=FakeTTS(), agent=agent)
    )
    error = next(e for e in events if e.kind == "error")
    assert error.stage == "stt"
    assert error.code == "stt_no_final"
    assert agent.prompts == []


async def test_pipeline_agent_error_stops_before_tts() -> None:
    """An AgentTurn that returns an error result → stage=agent error, no audio."""
    agent = FakeAgentTurn(error=AgentTurnError(message="run failed", code="schema_error"))
    tts = FakeTTS()
    events = await _collect(
        run_voice_pipeline(audio_in=_audio_in(b"a"), stt=FakeSTT("hi"), tts=tts, agent=agent)
    )
    err = next(e for e in events if e.kind == "error")
    assert err.stage == "agent"
    assert err.code == "schema_error"
    assert err.message == "run failed"
    # No audio was synthesized.
    assert [e for e in events if e.kind == "tts.audio"] == []
    assert tts.spoken == []


async def test_pipeline_tts_failure_still_yields_answer_and_done() -> None:
    """TTS down → caller already received the answer as agent.token text; the
    turn degrades to a tts error + a terminal done (D8)."""
    agent = FakeAgentTurn("you still get this")
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"), stt=FakeSTT("anything"), tts=_BoomTTS(), agent=agent
        )
    )
    kinds = [e.kind for e in events]
    assert "agent.token" in kinds
    tts_err = next(e for e in events if e.kind == "error")
    assert tts_err.stage == "tts"
    assert kinds[-1] == "done"


# ---------------------------------------------------------------------------
# Latency badge — per-stage timings off the event stream
# ---------------------------------------------------------------------------


def test_compute_turn_latency_reads_milestone_offsets() -> None:
    events = [
        VoiceEvent(kind="transcript.partial", text="he", at_ms=50.0),
        VoiceEvent(kind="transcript.final", text="hello", at_ms=120.0),
        VoiceEvent(kind="agent.token", text="hi", at_ms=300.0),
        VoiceEvent(kind="agent.token", text=" there", at_ms=350.0),
        VoiceEvent(kind="tts.audio", audio=AudioChunk(data=b"x"), at_ms=480.0),
        VoiceEvent(kind="tts.audio", audio=AudioChunk(data=b"y"), at_ms=520.0),
        VoiceEvent(kind="done", run_id="r1", status="ok", at_ms=600.0),
    ]
    lat = compute_turn_latency(events)
    assert lat.stt_final_ms == 120.0
    assert lat.agent_first_token_ms == 300.0
    assert lat.tts_first_audio_ms == 480.0
    assert lat.responded_in_ms == 480.0
    assert lat.agent_think_ms == 180.0
    assert lat.tts_ms == 180.0


def test_compute_turn_latency_falls_back_to_first_token_without_audio() -> None:
    events = [
        VoiceEvent(kind="transcript.final", text="hi", at_ms=100.0),
        VoiceEvent(kind="agent.token", text="answer", at_ms=250.0),
        VoiceEvent(kind="error", code="tts_error", stage="tts", at_ms=260.0),
        VoiceEvent(kind="done", run_id="r", status="ok", at_ms=270.0),
    ]
    lat = compute_turn_latency(events)
    assert lat.tts_first_audio_ms is None
    assert lat.responded_in_ms == 250.0
    assert lat.tts_ms is None


def test_format_latency_badge_renders_headline_and_breakdown() -> None:
    lat = VoiceTurnLatency(stt_final_ms=100.0, agent_first_token_ms=280.0, tts_first_audio_ms=460.0)
    badge = format_latency_badge(lat)
    assert badge.startswith("⚡ responded in 460ms")
    assert "agent 180ms" in badge
    assert "voice 180ms" in badge
    assert format_latency_badge(VoiceTurnLatency()) == ""


async def test_pipeline_stamps_at_ms_on_every_event() -> None:
    ticks = iter([10.0, 10.12, 10.30, 10.48, 10.60, 10.7, 10.8])

    def _clock() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 11.0

    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hello"),
            tts=FakeTTS(),
            agent=FakeAgentTurn("spoken"),
            clock=_clock,
        )
    )
    offsets = [e.at_ms for e in events]
    assert all(o >= 0 for o in offsets)
    assert offsets == sorted(offsets)
    lat = compute_turn_latency(events)
    assert lat.responded_in_ms is not None
    assert format_latency_badge(lat).startswith("⚡ responded in")


# ---------------------------------------------------------------------------
# Barge-in — a mid-answer interrupt cancels the in-flight TTS
# ---------------------------------------------------------------------------


class _ControllableTTS:
    name = "controllable_tts"
    version = "0.0.1"

    def __init__(self, cancel: asyncio.Event, *, total_frames: int = 5) -> None:
        self._cancel = cancel
        self._total = total_frames
        self.emitted = 0
        self.closed = False

    async def synthesize(
        self, text: AsyncIterator[str], *, voice_id: str = "", codec: str = "pcm16", api_key=None
    ) -> AsyncIterator[AudioChunk]:
        async for _ in text:
            pass
        try:
            for i in range(self._total):
                yield AudioChunk(data=f"frame-{i}".encode(), codec=codec)
                self.emitted += 1
                if i == 0:
                    self._cancel.set()
                await asyncio.sleep(0)
        finally:
            self.closed = True


async def test_pipeline_bargein_cancels_inflight_tts() -> None:
    cancel = asyncio.Event()
    tts = _ControllableTTS(cancel, total_frames=5)
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hello"),
            tts=tts,
            agent=FakeAgentTurn("a very long spoken answer"),
            cancel=cancel,
        )
    )
    audio_events = [e for e in events if e.kind == "tts.audio"]
    assert len(audio_events) == 1
    assert tts.emitted < 5
    assert tts.closed is True
    done = events[-1]
    assert done.kind == "done"
    assert done.status == "interrupted"
    assert "error" not in [e.kind for e in events]


async def test_pipeline_bargein_before_tts_skips_synthesis() -> None:
    cancel = asyncio.Event()
    cancel.set()
    tts = FakeTTS()
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hello"),
            tts=tts,
            agent=FakeAgentTurn("answer"),
            cancel=cancel,
        )
    )
    assert [e for e in events if e.kind == "tts.audio"] == []
    assert tts.spoken == []
    assert events[-1].status == "interrupted"


async def test_pipeline_no_cancel_is_unchanged() -> None:
    tts = FakeTTS(frames=3)
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=tts,
            agent=FakeAgentTurn("full answer"),
        )
    )
    audio_events = [e for e in events if e.kind == "tts.audio"]
    assert audio_events
    answer_bytes = b"".join(e.audio.data for e in audio_events if e.audio)
    assert answer_bytes.decode("utf-8") == "full answer"
    assert tts.spoken == ["full answer"]
    assert events[-1].status == "ok"
