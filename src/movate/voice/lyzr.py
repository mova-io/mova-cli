"""Lyzr ADK voice binding (ADR 069) — voice-enable a Lyzr-native agent, no mdk.

With ADR 067's :class:`~movate.voice.agent_turn.AgentTurn` seam, voicing a Lyzr
agent is **one adapter, not an integration project**. Lyzr ADK's turn is
``agent.run(message) -> response.response`` (synchronous, text-in/text-out);
:class:`LyzrAgentTurn` wraps exactly that, and the *unchanged* pipeline + the
ADR-068 failover router do the rest.

This binding is **duck-typed**: it never imports the Lyzr SDK — it only calls
``agent.run`` on whatever agent object you pass. So this module has zero
third-party dependency; the ``mdk-voice[lyzr]`` extra is a convenience that
installs the Lyzr SDK *you* use to build the agent, not something we import.

This is the **reverse** of mdk's ``runtime: lyzr`` provider (which runs a Lyzr
agent *as an LLM inside mdk* and needs the mdk runtime): here we put voice
*around* a Lyzr-native agent with **no mdk present at all**.
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
# asyncio.to_thread; tests pass a direct shim for determinism.
ToThread = Callable[..., Awaitable[Any]]


def _extract_answer(resp: Any) -> str:
    """Pull the assistant text out of a Lyzr ``run`` response, tolerantly.

    Lyzr returns an object exposing ``.response`` (a str); some shapes return a
    plain dict or a bare string. We accept all three so a minor SDK shape change
    doesn't break the binding (ADR 069 — the ``getattr`` tolerance note).
    """
    text = getattr(resp, "response", None)
    if text is None and isinstance(resp, dict):
        text = resp.get("response")
    if text is None:
        text = resp
    return text if isinstance(text, str) else str(text)


class LyzrAgentTurn:
    """An :class:`~movate.voice.agent_turn.AgentTurn` over a Lyzr ADK agent.

    Wraps ``agent.run(text)`` — offloaded to a thread (it is synchronous) so the
    event loop stays free for concurrent STT/TTS streaming — and returns its
    ``response`` as the answer. Lyzr's turn is non-streaming, so the whole answer
    is emitted as a single ``on_token`` delta; the pipeline's buffered TTS path
    handles that exactly as it does the OpenAI TTS adapter. An exception from
    ``agent.run`` becomes a typed error result (the pipeline surfaces a
    ``stage="agent"`` error and degrades per ADR 048 D8) rather than propagating.
    """

    name = "lyzr-adk"
    version = "1"

    def __init__(self, agent: Any, *, to_thread: ToThread | None = None) -> None:
        self._agent = agent
        self._to_thread = to_thread or asyncio.to_thread

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurnResult:
        try:
            resp = await self._to_thread(self._agent.run, text)
        except Exception as exc:
            return AgentTurnResult(
                status="error",
                error=AgentTurnError(message=str(exc) or exc.__class__.__name__),
            )
        answer = _extract_answer(resp)
        if on_token is not None and answer:
            on_token(answer)
        return AgentTurnResult(answer_text=answer, status="ok")


def voice_agent(
    lyzr_agent: Any,
    *,
    audio_in: AsyncIterator[AudioChunk],
    stt: SpeechToTextProvider | None = None,
    tts: TextToSpeechProvider | None = None,
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
    """Voice a Lyzr agent in one call (ADR 069 D3).

    Wires :func:`~movate.voice.pipeline.run_voice_pipeline` with a
    :class:`LyzrAgentTurn` over ``lyzr_agent`` and, by default, the ADR-068
    resilient :class:`~movate.voice.failover.FailoverSTT` /
    :class:`~movate.voice.failover.FailoverTTS` chains — so a Lyzr deployment gets
    failover, circuit-breaking, and latency-first routing for free, with no mdk.
    Pass your own ``stt`` / ``tts`` to override (e.g. a specific provider, or a
    test double). Returns the pipeline's :class:`~movate.voice.pipeline.VoiceEvent`
    stream; the caller's transport serializes it.
    """
    return run_voice_pipeline(
        audio_in=audio_in,
        stt=stt if stt is not None else FailoverSTT.default(),
        tts=tts if tts is not None else FailoverTTS.default(),
        agent=LyzrAgentTurn(lyzr_agent),
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
