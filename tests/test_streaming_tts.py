"""Sentence chunking + streaming TTS (the time-to-first-audio win)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    FakeAgentTurn,
    FakeSTT,
    FakeTTS,
    SentenceChunker,
    VoiceEvent,
    run_voice_pipeline,
)
from movate.voice.agent_turn import AgentTurnError, AgentTurnResult

# --- SentenceChunker (pure) ------------------------------------------------


def test_chunker_emits_on_sentence_boundaries() -> None:
    c = SentenceChunker()
    assert c.feed("Hello there. ") == ["Hello there."]
    assert c.feed("How are ") == []
    assert c.feed("you? Fine.") == ["How are you?"]  # "Fine." has no trailing space yet
    assert c.flush() == "Fine."


def test_chunker_handles_multiple_sentences_in_one_delta() -> None:
    c = SentenceChunker()
    assert c.feed("One. Two! Three? ") == ["One.", "Two!", "Three?"]
    assert c.flush() == ""


def test_chunker_splits_on_newlines() -> None:
    c = SentenceChunker()
    assert c.feed("- item one\n- item two\n") == ["- item one", "- item two"]


def test_chunker_no_boundary_buffers_until_flush() -> None:
    c = SentenceChunker()
    assert c.feed("no terminator here") == []
    assert c.flush() == "no terminator here"


# --- streaming pipeline path -----------------------------------------------


async def _audio_in(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


async def _collect(gen: AsyncIterator[VoiceEvent]) -> list[VoiceEvent]:
    return [e async for e in gen]


async def test_streaming_round_trips_full_answer() -> None:
    """Streaming mode speaks the whole answer (across sentences) and ends in done."""
    stt = FakeSTT("hello")
    tts = FakeTTS()
    agent = FakeAgentTurn("First sentence. Second sentence.")
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"), stt=stt, tts=tts, agent=agent, tts_streaming=True
        )
    )
    kinds = [e.kind for e in events]
    assert "transcript.final" in kinds
    assert "agent.token" in kinds
    assert "tts.audio" in kinds
    assert kinds[-1] == "done"
    assert events[-1].status == "ok"
    # Every sentence reached TTS (two synthesize calls, one per sentence).
    assert tts.spoken == ["First sentence.", "Second sentence."]
    audio = b"".join(e.audio.data for e in events if e.kind == "tts.audio" and e.audio)
    assert audio.decode("utf-8") == "First sentence.Second sentence."


async def test_streaming_speaks_first_sentence_before_agent_finishes() -> None:
    """The overlap property: first audio is emitted before the LAST agent token.

    A slow agent streams two sentences with a gap; in streaming mode the first
    sentence's audio must arrive before the second sentence's tokens.
    """

    class _SlowAgent:
        name = "slow"
        version = "1"

        async def run(self, text, *, on_token=None, language=None, session_id=None):
            assert on_token is not None
            on_token("Ready. ")
            await asyncio.sleep(0.02)
            on_token("Now the rest.")
            return AgentTurnResult(answer_text="", status="ok")

    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=FakeTTS(),
            agent=_SlowAgent(),
            tts_streaming=True,
        )
    )
    kinds = [e.kind for e in events]
    first_audio = kinds.index("tts.audio")
    last_token = len(kinds) - 1 - kinds[::-1].index("agent.token")
    # First audio landed before the final agent token → synthesis overlapped gen.
    assert first_audio < last_token


async def test_streaming_non_streaming_agent_speaks_answer_text() -> None:
    """An agent that returns answer_text without streaming tokens is still spoken."""
    # FakeAgentTurn(stream=False) returns answer_text and emits no on_token deltas.
    agent = FakeAgentTurn("the whole answer", stream=False)
    tts = FakeTTS()
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=tts,
            agent=agent,
            tts_streaming=True,
        )
    )
    assert tts.spoken == ["the whole answer"]
    assert events[-1].kind == "done"


async def test_streaming_agent_error_surfaces_and_speaks_nothing() -> None:
    agent = FakeAgentTurn(error=AgentTurnError(message="boom", code="schema_error"))
    tts = FakeTTS()
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=tts,
            agent=agent,
            tts_streaming=True,
        )
    )
    err = next(e for e in events if e.kind == "error")
    assert err.stage == "agent"
    assert [e for e in events if e.kind == "tts.audio"] == []
    # No tokens streamed before the error → nothing synthesized.
    assert "done" not in [e.kind for e in events]


async def test_streaming_bargein_before_tts_skips_synthesis() -> None:
    cancel = asyncio.Event()
    cancel.set()
    tts = FakeTTS()
    events = await _collect(
        run_voice_pipeline(
            audio_in=_audio_in(b"a"),
            stt=FakeSTT("hi"),
            tts=tts,
            agent=FakeAgentTurn("answer."),
            cancel=cancel,
            tts_streaming=True,
        )
    )
    assert [e for e in events if e.kind == "tts.audio"] == []
    assert tts.spoken == []
    assert events[-1].status == "interrupted"
