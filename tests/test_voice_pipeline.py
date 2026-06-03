"""End-to-end voice pipeline (ADR 048 D1/D7, Phase 1).

The load-bearing test for the ADR: audio → STT → **the UNCHANGED text
Executor** → TTS → audio, asserting the agent stage is the existing run path
reused as-is (no Executor edit). With a real scaffolded agent + a real
``Executor`` + ``MockProvider`` in the middle and fake STT/TTS at the edges:

* a turn of audio drives a real agent run that **persists a RunRecord**
  exactly like a non-voice run (the proof the Executor was reused unchanged);
* the agent's answer round-trips back through TTS as audio;
* the event protocol comes out in pipeline order
  (transcript.final → agent.token* → tts.audio* → done);
* the transcript STT produced is what the agent ran on (bound to ``input.text``);
* failure modes degrade per ADR 048 D8 (STT error stops; TTS error still
  yields the answer text + a done frame).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from movate.core.loader import load_agent
from movate.testing.scaffold import build_test_executor, scaffold_agent
from movate.voice import AudioChunk, FakeSTT, FakeTTS, run_voice_pipeline
from movate.voice.pipeline import (
    VoiceEvent,
    VoiceTurnLatency,
    compute_turn_latency,
    format_latency_badge,
)


async def _audio_in(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


def _scaffold_bundle(tmp_path: Path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    scaffold_agent(agents_dir / "voice-demo", name="voice-demo")
    return load_agent(agents_dir / "voice-demo")


async def _collect(gen: AsyncIterator[VoiceEvent]) -> list[VoiceEvent]:
    return [e async for e in gen]


# ---------------------------------------------------------------------------
# Happy path — the unchanged Executor is reused
# ---------------------------------------------------------------------------


async def test_pipeline_runs_unchanged_executor_and_persists_run(tmp_path: Path) -> None:
    """audio → STT → real Executor (MockProvider) → TTS → audio, and the run
    is persisted exactly like a non-voice run (proof the Executor ran as-is)."""
    bundle = _scaffold_bundle(tmp_path)
    # Mock answer drives both the agent.token stream AND (via the `message`
    # key → human_readable) the text TTS speaks.
    executor, _provider, storage, _tracer = build_test_executor(
        response='{"message": "spoken answer"}', tenant_id="t-voice"
    )

    stt = FakeSTT("turn the lights on")
    tts = FakeTTS()

    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"frame-1", b"frame-2"),
            stt=stt,
            tts=tts,
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
        )
    )

    kinds = [e.kind for e in events]
    # Pipeline order: a final transcript, ≥1 agent token, ≥1 audio frame, done.
    assert "transcript.final" in kinds
    assert "agent.token" in kinds
    assert "tts.audio" in kinds
    assert kinds[-1] == "done"
    # No errors on the happy path.
    assert "error" not in kinds

    # The transcript STT produced is what the agent ran on.
    final = next(e for e in events if e.kind == "transcript.final")
    assert final.text == "turn the lights on"
    assert stt.received == [b"frame-1", b"frame-2"]

    # The Executor was REUSED UNCHANGED: it persisted a RunRecord for this
    # tenant, identical to a non-voice run.
    assert len(storage.runs) == 1
    record = storage.runs[0]
    assert record.agent == "voice-demo"
    assert record.tenant_id == "t-voice"
    assert record.input == {"text": "turn the lights on"}

    # The done frame carries that run's id + success status.
    done = events[-1]
    assert done.status == "success"
    assert done.run_id == record.run_id

    # The agent's answer round-tripped through TTS into audio bytes.
    audio = b"".join(e.audio.data for e in events if e.kind == "tts.audio" and e.audio)
    assert audio.decode("utf-8") == "spoken answer"
    assert tts.spoken == ["spoken answer"]


async def test_pipeline_streams_partials_before_final(tmp_path: Path) -> None:
    """A streaming-endpointing STT emits partials first; the agent only runs
    on the final (endpointed) transcript."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, storage, _t = build_test_executor(
        response='{"message": "ok"}', tenant_id="t-voice"
    )
    stt = FakeSTT("hello world", partials=["hello", "hello wor"])

    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=stt,
            tts=FakeTTS(),
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
        )
    )
    partials = [e.text for e in events if e.kind == "transcript.partial"]
    assert partials == ["hello", "hello wor"]
    # The agent ran exactly once, on the FINAL transcript.
    assert len(storage.runs) == 1
    assert storage.runs[0].input == {"text": "hello world"}


async def test_pipeline_input_key_is_honored(tmp_path: Path) -> None:
    """The transcript is bound to ``input_key`` (the one zero-change knob).

    Proven two ways against the default template (whose input schema REQUIRES
    ``text``): the default key produces a clean success whose persisted run
    input is ``{"text": ...}``, while a mismatched key is actually used — it
    binds under the wrong field, fails input-schema validation, and surfaces
    an agent-stage error (which it could only do if the key were honored)."""
    bundle = _scaffold_bundle(tmp_path)

    # Default key → success, and the persisted run carries the bound input.
    executor, _p, storage, _t = build_test_executor(
        response='{"message": "ok"}', tenant_id="t-voice"
    )
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=FakeTTS(),
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
        )
    )
    assert events[-1].kind == "done"
    assert storage.runs[0].input == {"text": "hi"}

    # Mismatched key → bound under the wrong field → input-schema failure
    # surfaced as an agent-stage error (the key is genuinely plumbed through).
    executor2, _p2, _s2, _t2 = build_test_executor(
        response='{"message": "ok"}', tenant_id="t-voice"
    )
    events2 = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=FakeTTS(),
            executor=executor2,
            bundle=bundle,
            tenant_id="t-voice",
            input_key="utterance",
        )
    )
    err = next(e for e in events2 if e.kind == "error")
    assert err.stage == "agent"


# ---------------------------------------------------------------------------
# Failure modes (ADR 048 D8)
# ---------------------------------------------------------------------------


class _BoomSTT:
    name = "boom_stt"
    version = "0.0.1"

    async def transcribe(self, audio, *, language=None, api_key=None):
        async for _ in audio:
            pass
        raise RuntimeError("stt provider down")
        yield  # pragma: no cover - unreachable, makes this an async generator


class _NoFinalSTT:
    """STT that streams only partials and never endpoints (no is_final)."""

    name = "no_final_stt"
    version = "0.0.1"

    async def transcribe(self, audio, *, language=None, api_key=None):
        async for _ in audio:
            pass
        from movate.voice.base import TranscriptChunk  # noqa: PLC0415

        yield TranscriptChunk(text="partial only", is_final=False)


class _BoomTTS:
    name = "boom_tts"
    version = "0.0.1"

    async def synthesize(self, text, *, voice_id="", codec="pcm16", api_key=None):
        async for _ in text:
            pass
        raise RuntimeError("tts provider down")
        yield  # pragma: no cover - unreachable


async def test_pipeline_stt_failure_emits_error_and_stops(tmp_path: Path) -> None:
    """STT down mid-stream → an ``error`` (stage=stt); the agent never runs."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, storage, _t = build_test_executor(
        response='{"message": "ok"}', tenant_id="t-voice"
    )
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=_BoomSTT(),
            tts=FakeTTS(),
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
        )
    )
    assert [e.kind for e in events] == ["error"]
    assert events[0].stage == "stt"
    assert events[0].code == "stt_error"
    # No agent run happened.
    assert storage.runs == []


async def test_pipeline_stt_no_final_emits_error(tmp_path: Path) -> None:
    """STT streamed only partials and never endpointed → a clear error rather
    than a hang."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, storage, _t = build_test_executor(
        response='{"message": "ok"}', tenant_id="t-voice"
    )
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=_NoFinalSTT(),
            tts=FakeTTS(),
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
        )
    )
    error = next(e for e in events if e.kind == "error")
    assert error.stage == "stt"
    assert error.code == "stt_no_final"
    assert storage.runs == []


async def test_pipeline_tts_failure_still_yields_answer_and_done(tmp_path: Path) -> None:
    """TTS down → caller already received the answer as agent.token text; the
    turn degrades to a tts error + a terminal done, not a dropped turn (D8)."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, storage, _t = build_test_executor(
        response='{"message": "you still get this"}', tenant_id="t-voice"
    )
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("anything"),
            tts=_BoomTTS(),
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
        )
    )
    kinds = [e.kind for e in events]
    # The answer was streamed as text BEFORE the TTS failure.
    assert "agent.token" in kinds
    # The TTS error is surfaced, and the turn still terminates with done.
    tts_err = next(e for e in events if e.kind == "error")
    assert tts_err.stage == "tts"
    assert kinds[-1] == "done"
    # The run still succeeded (it's the audio output that failed, not the run).
    assert len(storage.runs) == 1


# ---------------------------------------------------------------------------
# Latency badge (demo polish) — per-stage timings off the event stream
# ---------------------------------------------------------------------------


def test_compute_turn_latency_reads_milestone_offsets() -> None:
    """compute_turn_latency picks the FIRST event of each milestone kind and
    reports STT-final / agent-first-token / TTS-first-audio offsets + derived
    spans."""
    events = [
        VoiceEvent(kind="transcript.partial", text="he", at_ms=50.0),
        VoiceEvent(kind="transcript.final", text="hello", at_ms=120.0),
        VoiceEvent(kind="agent.token", text="hi", at_ms=300.0),
        VoiceEvent(kind="agent.token", text=" there", at_ms=350.0),  # later token ignored
        VoiceEvent(kind="tts.audio", audio=AudioChunk(data=b"x"), at_ms=480.0),
        VoiceEvent(kind="tts.audio", audio=AudioChunk(data=b"y"), at_ms=520.0),
        VoiceEvent(kind="done", run_id="r1", status="success", at_ms=600.0),
    ]
    lat = compute_turn_latency(events)
    assert lat.stt_final_ms == 120.0
    assert lat.agent_first_token_ms == 300.0  # first token, not the second
    assert lat.tts_first_audio_ms == 480.0  # first audio frame
    # responded_in_ms headlines the first audible response.
    assert lat.responded_in_ms == 480.0
    # Derived spans: agent think = final→first-token, voice = first-token→audio.
    assert lat.agent_think_ms == 180.0
    assert lat.tts_ms == 180.0


def test_compute_turn_latency_falls_back_to_first_token_without_audio() -> None:
    """A degraded text-only turn (TTS produced no audio) still has a meaningful
    'responded' latency: the agent's first token."""
    events = [
        VoiceEvent(kind="transcript.final", text="hi", at_ms=100.0),
        VoiceEvent(kind="agent.token", text="answer", at_ms=250.0),
        VoiceEvent(kind="error", code="tts_error", stage="tts", at_ms=260.0),
        VoiceEvent(kind="done", run_id="r", status="success", at_ms=270.0),
    ]
    lat = compute_turn_latency(events)
    assert lat.tts_first_audio_ms is None
    assert lat.responded_in_ms == 250.0  # falls back to first token
    assert lat.tts_ms is None  # no audio → no synthesis span


def test_format_latency_badge_renders_headline_and_breakdown() -> None:
    """The badge headlines 'responded in {X}ms' and appends the per-stage split
    when available; an empty latency renders nothing."""
    lat = VoiceTurnLatency(stt_final_ms=100.0, agent_first_token_ms=280.0, tts_first_audio_ms=460.0)
    badge = format_latency_badge(lat)
    assert badge.startswith("⚡ responded in 460ms")
    assert "agent 180ms" in badge
    assert "voice 180ms" in badge
    # Nothing reached → empty string (nothing useful to show).
    assert format_latency_badge(VoiceTurnLatency()) == ""


async def test_pipeline_stamps_at_ms_on_every_event(tmp_path: Path) -> None:
    """run_voice_pipeline stamps a monotonic at_ms offset on every event, in
    non-decreasing order, so the badge can be computed off the stream."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, _s, _t = build_test_executor(
        response='{"message": "spoken"}', tenant_id="t-voice"
    )

    # A deterministic clock: a list of monotonic-seconds the pipeline pops in
    # order, so the stamped offsets are exactly assertable.
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
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
            clock=_clock,
        )
    )
    offsets = [e.at_ms for e in events]
    # Every event carries an offset; offsets are non-decreasing (time only moves
    # forward) and start at ~0 (the first tick is the turn-start baseline).
    assert all(o >= 0 for o in offsets)
    assert offsets == sorted(offsets)
    lat = compute_turn_latency(events)
    assert lat.responded_in_ms is not None
    assert format_latency_badge(lat).startswith("⚡ responded in")


# ---------------------------------------------------------------------------
# Barge-in — a mid-answer interrupt cancels the in-flight TTS (ADR 048 D2b)
# ---------------------------------------------------------------------------


class _ControllableTTS:
    """A TTS double that yields several frames and lets the test drive barge-in.

    It sets ``cancel`` itself right after emitting its FIRST audio frame
    (simulating the user starting to speak mid-answer), then keeps trying to
    emit more frames. It records whether its generator was closed
    (``aclose``-d) so a test can assert the pipeline tore the in-flight
    synthesis down rather than draining it.
    """

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
                    # The user just started talking → barge in.
                    self._cancel.set()
                await asyncio.sleep(0)  # yield control so cancel is observed
        finally:
            self.closed = True


async def test_pipeline_bargein_cancels_inflight_tts(tmp_path: Path) -> None:
    """When ``cancel`` is set mid-answer, the pipeline stops emitting TTS audio,
    closes the synthesis generator, and ends with a ``done`` status of
    ``interrupted`` — proof the in-flight TTS was actually cancelled."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, _s, _t = build_test_executor(
        response='{"message": "a very long spoken answer"}', tenant_id="t-voice"
    )
    cancel = asyncio.Event()
    tts = _ControllableTTS(cancel, total_frames=5)

    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hello"),
            tts=tts,
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
            cancel=cancel,
        )
    )

    audio_events = [e for e in events if e.kind == "tts.audio"]
    # Only the first frame made it out before the barge-in stopped synthesis —
    # the remaining frames were NOT forwarded to the client.
    assert len(audio_events) == 1
    assert tts.emitted < 5  # the TTS did not run to completion
    # The synthesis generator was closed (the in-flight stream was torn down).
    assert tts.closed is True
    # The turn ends cleanly, flagged as interrupted (a barge-in, not an error).
    done = events[-1]
    assert done.kind == "done"
    assert done.status == "interrupted"
    assert "error" not in [e.kind for e in events]


async def test_pipeline_bargein_before_tts_skips_synthesis(tmp_path: Path) -> None:
    """If the user is already speaking when the answer is ready (cancel set
    before TTS begins), no audio is synthesized at all."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, _s, _t = build_test_executor(
        response='{"message": "answer"}', tenant_id="t-voice"
    )
    cancel = asyncio.Event()
    cancel.set()  # already barging in
    tts = FakeTTS()

    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hello"),
            tts=tts,
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
            cancel=cancel,
        )
    )
    assert [e for e in events if e.kind == "tts.audio"] == []
    # TTS was never invoked (no synthesis attempted).
    assert tts.spoken == []
    assert events[-1].status == "interrupted"


async def test_pipeline_no_cancel_is_unchanged(tmp_path: Path) -> None:
    """Back-compat: with no ``cancel`` event, the turn behaves exactly as before
    (full audio, status reflects the run, not 'interrupted')."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _p, _s, _t = build_test_executor(
        response='{"message": "full answer"}', tenant_id="t-voice"
    )
    tts = FakeTTS(frames=3)
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=tts,
            executor=executor,
            bundle=bundle,
            tenant_id="t-voice",
        )
    )
    audio_events = [e for e in events if e.kind == "tts.audio"]
    # The full answer round-tripped through TTS (every frame forwarded).
    assert audio_events
    answer_bytes = b"".join(e.audio.data for e in audio_events if e.audio)
    assert answer_bytes.decode("utf-8") == "full answer"
    assert tts.spoken == ["full answer"]
    # Not interrupted — the done status reflects the run.
    assert events[-1].status == "success"
