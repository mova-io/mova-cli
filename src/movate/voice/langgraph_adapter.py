"""LangGraph voice binding — voice-enable a compiled LangGraph, no mdk.

Same shape as :mod:`movate.voice.lyzr` (ADR 069): the ADR-067
:class:`~movate.voice.agent_turn.AgentTurn` seam means a new framework is **one
adapter, not an integration project**. LangGraph's turn is a *compiled* graph
exposing ``await graph.ainvoke(state) -> state-out`` (or sync ``invoke``);
:class:`LangGraphAgentTurn` wraps exactly that, builds the input state from the
user transcript, and pulls the final answer text out of the result.

This binding is **duck-typed**: it never imports the ``langgraph`` SDK — it
only calls ``.ainvoke`` (or ``.invoke``) on whatever compiled-graph object you
pass. So the module has zero third-party dependency; the
``mdk-voice[langgraph]`` extra is a convenience that installs the ``langgraph``
package *you* use to compose the graph, not something we import. (Module is
named ``langgraph_adapter`` to avoid colliding with the installed ``langgraph``
package name on import.)

Companion to :mod:`movate.voice.lyzr` — together they prove the AgentTurn seam is
truly framework-neutral.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from movate.voice.agent_turn import AgentTurnError, AgentTurnResult
from movate.voice.base import AudioChunk, AudioCodec, SpeechToTextProvider, TextToSpeechProvider
from movate.voice.failover import FailoverSTT, FailoverTTS
from movate.voice.pipeline import VoiceEvent, run_voice_pipeline

# An injectable "run this sync callable off the event loop" — defaults to
# asyncio.to_thread; tests pass a direct shim for determinism (mirrors Lyzr).
ToThread = Callable[..., Awaitable[Any]]

# A pluggable extractor: graph-output → final answer text.
OutputExtractor = Callable[[Any], str]
# A pluggable state-builder: (user_text, session_id) → input state for the graph.
StateBuilder = Callable[[str, str | None], dict[str, Any]]


def _default_extract(result: Any) -> str:
    """Tolerantly pull the final answer text from a LangGraph result.

    LangGraph results vary by graph shape: the ubiquitous "messages" pattern
    yields ``{"messages": [..., AIMessage(content="...")]}``; a custom graph
    might surface ``{"answer": "..."}`` or ``{"output": "..."}``; a trivial
    graph might return a bare string. We accept all of those so a minor schema
    change doesn't break the binding — same posture as the Lyzr extractor.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            content = getattr(last, "content", None)
            if isinstance(content, str):
                return content
            if isinstance(last, dict):
                c = last.get("content")
                if isinstance(c, str):
                    return c
        for key in ("answer", "output"):
            val = result.get(key)
            if isinstance(val, str):
                return val
    # Last resort: stringify whatever came back.
    return result if isinstance(result, str) else str(result)


def _looks_messages_shaped(key: str) -> bool:
    # Single heuristic: only the canonical "messages" key gets the chat-list
    # shape; anything else (input/question/prompt/...) gets the bare-text shape.
    return key == "messages"


def _default_state_builder(input_key: str) -> StateBuilder:
    def _build(text: str, _session_id: str | None) -> dict[str, Any]:
        if _looks_messages_shaped(input_key):
            return {input_key: [{"role": "user", "content": text}]}
        return {input_key: text}

    return _build


class LangGraphAgentTurn:
    """An :class:`~movate.voice.agent_turn.AgentTurn` over a compiled LangGraph.

    Wraps a compiled graph's ``ainvoke`` (preferred) or ``invoke`` (run in a
    thread, to keep the event loop free for concurrent STT/TTS streaming) and
    returns the extracted answer text. Like Lyzr, LangGraph's turn is treated
    as non-streaming here: the final answer is emitted as a single ``on_token``
    delta and the buffered TTS path handles it exactly as it does for OpenAI
    TTS. An exception from the graph becomes a typed error result (the pipeline
    surfaces a ``stage="agent"`` error and degrades per ADR 048 D8) instead of
    propagating.

    The two seams that vary per-graph — the *input shape* and the *output
    shape* — are injectable: ``state_builder`` decides how the user transcript
    becomes graph input (default: messages list for ``input_key="messages"``,
    bare text otherwise), and ``output_extractor`` pulls the final answer out
    of the result (default: tolerant of messages / answer / output / str).
    """

    name = "langgraph"
    version = "1"
    # ADR 070 D3: NOT speculatable by default — a LangGraph graph may run tools
    # or write checkpoints inside the turn, which a discarded speculation would
    # leave behind. A graph that is provably side-effect-free before its first
    # token can override this to True per deployment.
    speculatable = False

    def __init__(
        self,
        graph: Any,
        *,
        input_key: str = "messages",
        output_extractor: OutputExtractor | None = None,
        state_builder: StateBuilder | None = None,
        to_thread: ToThread | None = None,
    ) -> None:
        self._graph = graph
        self._input_key = input_key
        self._extract = output_extractor or _default_extract
        self._build_state = state_builder or _default_state_builder(input_key)
        self._to_thread = to_thread or asyncio.to_thread

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurnResult:
        state = self._build_state(text, session_id)
        try:
            ainvoke = getattr(self._graph, "ainvoke", None)
            if ainvoke is not None:
                result = await ainvoke(state)
            else:
                # No async entrypoint — run the sync invoke in a thread so the
                # event loop stays free for concurrent STT/TTS streaming.
                result = await self._to_thread(self._graph.invoke, state)
        except Exception as exc:
            return AgentTurnResult(
                status="error",
                error=AgentTurnError(message=str(exc) or exc.__class__.__name__),
            )
        answer = self._extract(result)
        if not isinstance(answer, str):
            answer = str(answer)
        if on_token is not None and answer:
            on_token(answer)
        return AgentTurnResult(answer_text=answer, status="ok")


def voice_agent_langgraph(
    graph: Any,
    *,
    audio_in: AsyncIterator[AudioChunk],
    stt: SpeechToTextProvider | None = None,
    tts: TextToSpeechProvider | None = None,
    input_key: str = "messages",
    output_extractor: OutputExtractor | None = None,
    state_builder: StateBuilder | None = None,
    language: str | None = None,
    voice_id: str = "",
    codec: AudioCodec = "pcm16",
    stt_api_key: str | None = None,
    tts_api_key: str | None = None,
    session_id: str | None = None,
    cancel: asyncio.Event | None = None,
    tts_streaming: bool = False,
    text_filter: Callable[[str], str] | None = None,
    pii_redactor: Callable[[str], str] | None = None,
    agent_timeout: float | None = None,
) -> AsyncIterator[VoiceEvent]:
    """Voice a compiled LangGraph in one call (companion to :func:`voice_agent`).

    Wires :func:`~movate.voice.pipeline.run_voice_pipeline` with a
    :class:`LangGraphAgentTurn` over ``graph`` and, by default, the ADR-068
    resilient :class:`~movate.voice.failover.FailoverSTT` /
    :class:`~movate.voice.failover.FailoverTTS` chains — so a LangGraph deployment
    gets failover, circuit-breaking, and latency-first routing for free, with no
    mdk. Pass your own ``stt`` / ``tts`` to override. ``input_key`` /
    ``output_extractor`` / ``state_builder`` are passed straight through to
    :class:`LangGraphAgentTurn` so a non-messages-shaped graph (e.g. one keyed
    on ``"question"``) wires the same way.
    """
    return run_voice_pipeline(
        audio_in=audio_in,
        stt=stt if stt is not None else FailoverSTT.default(),
        tts=tts if tts is not None else FailoverTTS.default(),
        agent=LangGraphAgentTurn(
            graph,
            input_key=input_key,
            output_extractor=output_extractor,
            state_builder=state_builder,
        ),
        language=language,
        voice_id=voice_id,
        codec=codec,
        stt_api_key=stt_api_key,
        tts_api_key=tts_api_key,
        session_id=session_id,
        cancel=cancel,
        tts_streaming=tts_streaming,
        text_filter=text_filter,
        pii_redactor=pii_redactor,
        agent_timeout=agent_timeout,
    )
