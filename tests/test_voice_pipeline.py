"""Executor ⇄ voice integration via ``ExecutorAgentTurn`` (ADR 067 D4).

Post-extraction (ADR 067), the voice *pipeline mechanics* — partials, latency
badge, barge-in, STT/TTS failure degrade — live in the standalone ``mdk-voice``
package and are tested there. What stays mdk's responsibility is the **seam
binding**: that :class:`movate.voice.ExecutorAgentTurn` runs the *unchanged*
Executor behind ``mdk-voice``'s ``AgentTurn`` so an existing text agent is
voice-capable with zero changes and the run persists exactly like a non-voice
run (the load-bearing ADR 048 R2 property).

These tests drive the real ``run_voice_pipeline`` (from ``mdk-voice`` via the
``movate.voice`` shim) with a real ``Executor`` + ``MockProvider`` in the middle
and fake STT/TTS at the edges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from movate.core.loader import load_agent
from movate.testing.scaffold import build_test_executor, scaffold_agent
from movate.voice import (
    AudioChunk,
    ExecutorAgentTurn,
    FakeSTT,
    FakeTTS,
    VoiceEvent,
    run_voice_pipeline,
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
# The unchanged Executor is reused behind the AgentTurn seam
# ---------------------------------------------------------------------------


async def test_executor_agent_turn_drives_pipeline_and_persists_run(tmp_path: Path) -> None:
    """audio → STT → ExecutorAgentTurn(real Executor) → TTS → audio, and the run
    is persisted exactly like a non-voice run (proof the Executor ran as-is)."""
    bundle = _scaffold_bundle(tmp_path)
    executor, _provider, storage, _tracer = build_test_executor(
        response='{"message": "spoken answer"}', tenant_id="t-voice"
    )

    stt = FakeSTT("turn the lights on")
    tts = FakeTTS()
    agent = ExecutorAgentTurn(executor, bundle, tenant_id="t-voice")

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

    # The Executor was REUSED UNCHANGED: it persisted a RunRecord for this tenant,
    # identical to a non-voice run, bound to the transcript STT produced.
    assert len(storage.runs) == 1
    record = storage.runs[0]
    assert record.agent == "voice-demo"
    assert record.tenant_id == "t-voice"
    assert record.input == {"text": "turn the lights on"}

    done = events[-1]
    assert done.status == "success"
    assert done.run_id == record.run_id

    # The agent's answer round-tripped through TTS into audio bytes.
    audio = b"".join(e.audio.data for e in events if e.kind == "tts.audio" and e.audio)
    assert audio.decode("utf-8") == "spoken answer"
    assert tts.spoken == ["spoken answer"]


async def test_executor_agent_turn_honors_input_key(tmp_path: Path) -> None:
    """The transcript is bound to ``input_key`` (the one zero-change knob).

    Default key → success whose persisted run input is ``{"text": ...}``; a
    mismatched key binds under the wrong field, fails input-schema validation,
    and surfaces an agent-stage error (only possible if the key is honored)."""
    bundle = _scaffold_bundle(tmp_path)

    executor, _p, storage, _t = build_test_executor(
        response='{"message": "ok"}', tenant_id="t-voice"
    )
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=FakeTTS(),
            agent=ExecutorAgentTurn(executor, bundle, tenant_id="t-voice"),
        )
    )
    assert events[-1].kind == "done"
    assert storage.runs[0].input == {"text": "hi"}

    executor2, _p2, _s2, _t2 = build_test_executor(
        response='{"message": "ok"}', tenant_id="t-voice"
    )
    events2 = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=FakeTTS(),
            agent=ExecutorAgentTurn(executor2, bundle, tenant_id="t-voice", input_key="utterance"),
        )
    )
    # A mismatched input_key fails input-schema validation → a stage="agent"
    # error with no audio synthesized (the ADR 048 D8 degrade through the seam).
    err = next(e for e in events2 if e.kind == "error")
    assert err.stage == "agent"
