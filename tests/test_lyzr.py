"""The Lyzr ADK voice binding (ADR 069).

The binding is duck-typed (it only calls ``agent.run``), so these tests use a
fake Lyzr agent — no Lyzr SDK, no mdk.
"""

from __future__ import annotations

import types
from collections.abc import AsyncIterator

import pytest

from movate.voice import (
    AudioChunk,
    FakeSTT,
    FakeTTS,
    LyzrAgentTurn,
    voice_agent,
)
from movate.voice.failover import FailoverSTT, FailoverTTS


class _FakeLyzrAgent:
    """Mimics a Lyzr ADK agent: sync ``run(message) -> obj.response``."""

    def __init__(self, answer: str = "hello from lyzr", *, raises: Exception | None = None) -> None:
        self._answer = answer
        self._raises = raises
        self.received: list[str] = []

    def run(self, message: str) -> object:
        self.received.append(message)
        if self._raises is not None:
            raise self._raises
        return types.SimpleNamespace(response=self._answer)


async def _audio(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


# --- LyzrAgentTurn ---------------------------------------------------------


async def test_lyzr_agent_turn_returns_response_text() -> None:
    agent = _FakeLyzrAgent("the answer")
    tokens: list[str] = []
    result = await LyzrAgentTurn(agent).run("a question", on_token=tokens.append)
    assert result.status == "ok"
    assert result.answer_text == "the answer"
    assert result.error is None
    assert agent.received == ["a question"]
    # Non-streaming agent → the whole answer arrives as one token delta.
    assert tokens == ["the answer"]


async def test_lyzr_agent_turn_extracts_from_dict_response() -> None:
    class _DictAgent:
        def run(self, message: str) -> dict:
            return {"response": "dict answer"}

    result = await LyzrAgentTurn(_DictAgent()).run("hi")
    assert result.answer_text == "dict answer"


async def test_lyzr_agent_turn_extracts_from_bare_string() -> None:
    class _StrAgent:
        def run(self, message: str) -> str:
            return "bare answer"

    result = await LyzrAgentTurn(_StrAgent()).run("hi")
    assert result.answer_text == "bare answer"


async def test_lyzr_agent_turn_maps_error_to_result() -> None:
    agent = _FakeLyzrAgent(raises=RuntimeError("lyzr exploded"))
    result = await LyzrAgentTurn(agent).run("hi")
    assert result.status == "error"
    assert result.error is not None
    assert "lyzr exploded" in result.error.message


# --- voice_agent convenience ----------------------------------------------


async def _collect(gen) -> list:
    return [e async for e in gen]


async def test_voice_agent_end_to_end_with_doubles() -> None:
    """A Lyzr agent voiced through the pipeline with explicit STT/TTS doubles."""
    agent = _FakeLyzrAgent("spoken from lyzr")
    stt = FakeSTT("what is the status")
    tts = FakeTTS()

    events = await _collect(voice_agent(agent, audio_in=_audio(b"frame"), stt=stt, tts=tts))

    kinds = [e.kind for e in events]
    assert "transcript.final" in kinds
    assert "agent.token" in kinds
    assert kinds[-1] == "done"
    assert "error" not in kinds
    # The Lyzr agent ran on the transcript STT produced.
    assert agent.received == ["what is the status"]
    # Its answer round-tripped through TTS.
    audio = b"".join(e.audio.data for e in events if e.kind == "tts.audio" and e.audio)
    assert audio.decode("utf-8") == "spoken from lyzr"


async def test_voice_agent_passes_failover_chains_through() -> None:
    """Explicit Failover* chains drive the turn (resilience wraps the Lyzr agent)."""
    agent = _FakeLyzrAgent("resilient answer")
    events = await _collect(
        voice_agent(
            agent,
            audio_in=_audio(b"f"),
            stt=FailoverSTT([FakeSTT("hello")]),
            tts=FailoverTTS([FakeTTS()]),
        )
    )
    assert events[-1].kind == "done"
    assert agent.received == ["hello"]


def test_voice_agent_defaults_build_failover_chains() -> None:
    """With no stt/tts, voice_agent constructs the ADR-068 default chains
    (construction is SDK-free; we don't run them here)."""
    assert isinstance(FailoverSTT.default(), FailoverSTT)
    assert isinstance(FailoverTTS.default(), FailoverTTS)


async def test_lyzr_agent_turn_error_degrades_in_pipeline() -> None:
    """A Lyzr failure surfaces as a stage=agent error and no audio (ADR 048 D8)."""
    agent = _FakeLyzrAgent(raises=RuntimeError("down"))
    tts = FakeTTS()
    events = await _collect(voice_agent(agent, audio_in=_audio(b"f"), stt=FakeSTT("hi"), tts=tts))
    err = next(e for e in events if e.kind == "error")
    assert err.stage == "agent"
    assert tts.spoken == []
    assert [e for e in events if e.kind == "tts.audio"] == []


@pytest.mark.parametrize("answer", ["", "single", "two words here"])
async def test_lyzr_agent_turn_token_emission(answer: str) -> None:
    agent = _FakeLyzrAgent(answer)
    tokens: list[str] = []
    result = await LyzrAgentTurn(agent).run("q", on_token=tokens.append)
    assert result.answer_text == answer
    # Empty answer → no token; otherwise the whole answer as one delta.
    assert tokens == ([] if not answer else [answer])
