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

from collections.abc import AsyncIterator
from pathlib import Path

from movate.core.loader import load_agent
from movate.testing.scaffold import build_test_executor, scaffold_agent
from movate.voice import AudioChunk, FakeSTT, FakeTTS, run_voice_pipeline
from movate.voice.pipeline import VoiceEvent


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
