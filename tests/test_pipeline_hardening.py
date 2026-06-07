"""Agent-stage timeout (#1) and barge-in cancels the agent in streaming (#2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from movate.voice import AudioChunk, FakeSTT, FakeTTS, VoiceEvent, run_voice_pipeline
from movate.voice.agent_turn import AgentTurnResult


async def _audio(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


async def _collect(gen: AsyncIterator[VoiceEvent]) -> list[VoiceEvent]:
    return [e async for e in gen]


class _HangingAgent:
    """An agent whose run() never returns (until cancelled)."""

    name = "hang"
    version = "1"

    def __init__(self) -> None:
        self.cancelled = False

    async def run(self, text, *, on_token=None, language=None, session_id=None) -> AgentTurnResult:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return AgentTurnResult(answer_text="never", status="ok")  # pragma: no cover


# --- #1: agent-stage timeout ----------------------------------------------


async def test_agent_timeout_sequential() -> None:
    tts = FakeTTS()
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio(b"a"),
            stt=FakeSTT("hi"),
            tts=tts,
            agent=_HangingAgent(),
            agent_timeout=0.02,
        )
    )
    err = next(e for e in events if e.kind == "error")
    assert err.stage == "agent"
    assert [e for e in events if e.kind == "tts.audio"] == []  # nothing synthesized


async def test_agent_timeout_streaming() -> None:
    tts = FakeTTS()
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio(b"a"),
            stt=FakeSTT("hi"),
            tts=tts,
            agent=_HangingAgent(),
            agent_timeout=0.02,
            tts_streaming=True,
        )
    )
    err = next(e for e in events if e.kind == "error")
    assert err.stage == "agent"


# --- #2: barge-in cancels the agent (streaming) ----------------------------


async def test_bargein_cancels_running_agent_streaming() -> None:
    """A mid-turn barge-in must stop the agent, not let it finish off-mic."""
    cancel = asyncio.Event()
    agent = _HangingAgent()

    async def _drive() -> list[VoiceEvent]:
        return await _collect(
            run_voice_pipeline(
                audio_in=_audio(b"a"),
                stt=FakeSTT("hi"),
                tts=FakeTTS(),
                agent=agent,
                cancel=cancel,
                tts_streaming=True,
            )
        )

    task = asyncio.create_task(_drive())
    await asyncio.sleep(0.02)  # let the turn start, agent now "thinking"
    cancel.set()  # caller barges in
    events = await asyncio.wait_for(task, timeout=1.0)  # must finish promptly
    assert agent.cancelled is True  # the agent was actually cancelled
    assert events[-1].status == "interrupted"
