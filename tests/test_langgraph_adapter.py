"""The LangGraph voice binding (D3 — mirrors ADR 069 for LangGraph).

The binding is duck-typed (it only calls ``.ainvoke`` or ``.invoke`` on the
compiled graph), so these tests use a fake graph — no ``langgraph`` SDK, no
mdk. Mirrors :mod:`tests.test_lyzr`.
"""

from __future__ import annotations

import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

from movate.voice import (
    AgentTurn,
    AudioChunk,
    FakeSTT,
    FakeTTS,
    LangGraphAgentTurn,
    voice_agent_langgraph,
)
from movate.voice.failover import FailoverSTT, FailoverTTS


class _FakeAsyncGraph:
    """Mimics a compiled LangGraph with an async ``ainvoke``."""

    def __init__(
        self,
        result: Any | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._result = (
            result
            if result is not None
            else {"messages": [types.SimpleNamespace(content="hello from langgraph")]}
        )
        self._raises = raises
        self.received: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any]) -> Any:
        self.received.append(state)
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeSyncOnlyGraph:
    """A compiled graph that only exposes the sync ``invoke`` (no ``ainvoke``)."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.received: list[dict[str, Any]] = []

    def invoke(self, state: dict[str, Any]) -> Any:
        self.received.append(state)
        return self._result


async def _audio(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


async def _collect(gen: AsyncIterator[Any]) -> list[Any]:
    return [e async for e in gen]


# --- Protocol conformance --------------------------------------------------


def test_langgraph_agent_turn_satisfies_agentturn_protocol() -> None:
    graph = _FakeAsyncGraph()
    assert isinstance(LangGraphAgentTurn(graph), AgentTurn)


# --- Happy path (ainvoke + messages-shaped output) -------------------------


async def test_ainvoke_messages_shaped_dict_returns_last_message_content() -> None:
    graph = _FakeAsyncGraph(
        result={
            "messages": [
                types.SimpleNamespace(content="user said hi"),
                types.SimpleNamespace(content="the final answer"),
            ]
        }
    )
    tokens: list[str] = []
    result = await LangGraphAgentTurn(graph).run("a question", on_token=tokens.append)

    assert result.status == "ok"
    assert result.answer_text == "the final answer"
    assert result.error is None
    # Default state-builder produced a messages-shaped input on the "messages" key.
    assert graph.received == [{"messages": [{"role": "user", "content": "a question"}]}]
    # Non-streaming → the whole answer arrives as one token delta.
    assert tokens == ["the final answer"]


# --- ainvoke-vs-invoke detection (sync fallback through to_thread) ---------


async def test_falls_back_to_sync_invoke_via_to_thread_when_no_ainvoke() -> None:
    graph = _FakeSyncOnlyGraph(result={"answer": "from sync invoke"})
    called: list[tuple[Any, ...]] = []

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        called.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    turn = LangGraphAgentTurn(graph, to_thread=fake_to_thread)
    result = await turn.run("hi")

    assert result.status == "ok"
    assert result.answer_text == "from sync invoke"
    # to_thread received graph.invoke (the sync entrypoint). Bound methods
    # compare by (__self__, __func__), not identity — so == not `is`.
    assert len(called) == 1
    assert called[0][0] == graph.invoke
    # And the graph saw exactly the state we built.
    assert graph.received == [{"messages": [{"role": "user", "content": "hi"}]}]


# --- Default output extractor (tolerates multiple result shapes) -----------


async def test_default_extractor_handles_messages_list_with_dict_messages() -> None:
    graph = _FakeAsyncGraph(
        result={"messages": [{"role": "assistant", "content": "dict-msg answer"}]}
    )
    result = await LangGraphAgentTurn(graph).run("hi")
    assert result.answer_text == "dict-msg answer"


async def test_default_extractor_handles_answer_key() -> None:
    graph = _FakeAsyncGraph(result={"answer": "answer-keyed"})
    result = await LangGraphAgentTurn(graph).run("hi")
    assert result.answer_text == "answer-keyed"


async def test_default_extractor_handles_output_key() -> None:
    graph = _FakeAsyncGraph(result={"output": "output-keyed"})
    result = await LangGraphAgentTurn(graph).run("hi")
    assert result.answer_text == "output-keyed"


async def test_default_extractor_handles_plain_string_result() -> None:
    graph = _FakeAsyncGraph(result="bare string answer")
    result = await LangGraphAgentTurn(graph).run("hi")
    assert result.answer_text == "bare string answer"


async def test_default_extractor_falls_back_to_str_for_unknown_shape() -> None:
    graph = _FakeAsyncGraph(result={"unexpected": 42})
    result = await LangGraphAgentTurn(graph).run("hi")
    # Last-resort stringification — proves we never raise on a strange shape.
    assert result.answer_text == str({"unexpected": 42})


# --- Custom hooks win ------------------------------------------------------


async def test_custom_output_extractor_wins_over_default() -> None:
    graph = _FakeAsyncGraph(result={"messages": [types.SimpleNamespace(content="DEFAULT")]})

    def extract(_r: Any) -> str:
        return "CUSTOM"

    result = await LangGraphAgentTurn(graph, output_extractor=extract).run("hi")
    assert result.answer_text == "CUSTOM"


async def test_custom_state_builder_is_honored() -> None:
    graph = _FakeAsyncGraph(result={"answer": "ok"})

    def build(text: str, session_id: str | None) -> dict[str, Any]:
        return {"question": text, "thread_id": session_id or "anon"}

    await LangGraphAgentTurn(graph, state_builder=build).run("real q", session_id="s1")
    assert graph.received == [{"question": "real q", "thread_id": "s1"}]


async def test_non_messages_input_key_uses_bare_text_default() -> None:
    """A graph keyed on ``"question"`` should receive a bare-text input, not a list."""
    graph = _FakeAsyncGraph(result={"answer": "yep"})
    await LangGraphAgentTurn(graph, input_key="question").run("what is it")
    assert graph.received == [{"question": "what is it"}]


# --- Error path ------------------------------------------------------------


async def test_graph_exception_becomes_typed_error_result() -> None:
    graph = _FakeAsyncGraph(raises=RuntimeError("graph blew up"))
    result = await LangGraphAgentTurn(graph).run("hi")
    assert result.status == "error"
    assert result.error is not None
    assert "graph blew up" in result.error.message


# --- on_token streaming hook (non-streaming agent) -------------------------


@pytest.mark.parametrize("answer", ["", "single", "two words here"])
async def test_on_token_receives_one_delta_for_non_empty_answer(answer: str) -> None:
    graph = _FakeAsyncGraph(result={"answer": answer})
    tokens: list[str] = []
    result = await LangGraphAgentTurn(graph).run("q", on_token=tokens.append)
    assert result.answer_text == answer
    # Empty answer → no token; otherwise the whole answer as one delta (Lyzr-parity).
    assert tokens == ([] if not answer else [answer])


# --- voice_agent_langgraph convenience ------------------------------------


async def test_voice_agent_langgraph_end_to_end_with_doubles() -> None:
    """A LangGraph voiced through the pipeline with explicit STT/TTS doubles."""
    graph = _FakeAsyncGraph(
        result={"messages": [types.SimpleNamespace(content="spoken from langgraph")]}
    )
    stt = FakeSTT("what is the status")
    tts = FakeTTS()

    events = await _collect(
        voice_agent_langgraph(graph, audio_in=_audio(b"frame"), stt=stt, tts=tts)
    )

    kinds = [e.kind for e in events]
    assert "transcript.final" in kinds
    assert "agent.token" in kinds
    assert kinds[-1] == "done"
    assert "error" not in kinds
    # The graph ran on the STT transcript.
    assert graph.received == [{"messages": [{"role": "user", "content": "what is the status"}]}]
    # Its answer round-tripped through TTS.
    audio = b"".join(e.audio.data for e in events if e.kind == "tts.audio" and e.audio)
    assert audio.decode("utf-8") == "spoken from langgraph"


async def test_voice_agent_langgraph_passes_failover_chains_through() -> None:
    """Explicit Failover* chains drive the turn (resilience wraps the graph)."""
    graph = _FakeAsyncGraph(result={"answer": "resilient answer"})
    events = await _collect(
        voice_agent_langgraph(
            graph,
            audio_in=_audio(b"f"),
            stt=FailoverSTT([FakeSTT("hello")]),
            tts=FailoverTTS([FakeTTS()]),
            input_key="question",
        )
    )
    assert events[-1].kind == "done"
    assert graph.received == [{"question": "hello"}]


def test_voice_agent_langgraph_defaults_build_failover_chains() -> None:
    """With no stt/tts, voice_agent_langgraph constructs the ADR-068 default chains
    (construction is SDK-free; we don't run them here)."""
    assert isinstance(FailoverSTT.default(), FailoverSTT)
    assert isinstance(FailoverTTS.default(), FailoverTTS)


async def test_langgraph_agent_turn_error_degrades_in_pipeline() -> None:
    """A graph failure surfaces as a stage=agent error and no audio (ADR 048 D8)."""
    graph = _FakeAsyncGraph(raises=RuntimeError("down"))
    tts = FakeTTS()
    events = await _collect(
        voice_agent_langgraph(graph, audio_in=_audio(b"f"), stt=FakeSTT("hi"), tts=tts)
    )
    err = next(e for e in events if e.kind == "error")
    assert err.stage == "agent"
    assert tts.spoken == []
    assert [e for e in events if e.kind == "tts.audio"] == []
