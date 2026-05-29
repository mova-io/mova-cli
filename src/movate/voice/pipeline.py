"""The voice pipeline driver — STT → unchanged Executor → TTS.

This is the modality-blind orchestration ADR 048 D1 describes:

    audio ──▶ STT ──▶ [ the existing text agent, run by the UNCHANGED Executor ] ──▶ TTS ──▶ audio

It is the *logic* the WS ``/voice`` route (and the pipeline tests) wrap. The
key architectural property — and the thing the tests assert — is that the
agent stage is the **existing run path, untouched**: this module calls
``executor.execute(bundle, run_request, on_token=...)`` exactly as
``_sse_run_stream`` does in ``runtime/app.py``. It does **not** import,
subclass, or modify ``core/executor.py``; the Executor never learns the text
arrived as speech (CLAUDE.md rule 6 / ADR 048 R2).

The driver is transport-agnostic: it consumes an async stream of inbound
:class:`~movate.voice.base.AudioChunk` and emits a stream of
:class:`VoiceEvent` envelopes (partial-transcript / final-transcript /
agent-token / tts-audio / error / done). The WS route serializes those events
onto the socket; a test consumes them directly. Keeping the protocol in
typed Python events (not raw WS frames) is what lets the end-to-end pipeline
test run with no socket at all.

Failure modes (ADR 048 D8), handled here so the transport stays thin:

* **STT raises / yields no final** → an ``error`` event with stage ``stt``;
  the driver stops (the transport offers a text fallback at the edge).
* **the agent run errors** → an ``error`` event with stage ``agent`` carrying
  the executor's error message/code (no audio is synthesized).
* **TTS raises** → an ``error`` event with stage ``tts`` AFTER the
  ``agent.token`` events already streamed the answer as text, so the caller
  still receives the answer (reads instead of hears) — a graceful degrade,
  not a dropped turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from movate.voice.base import (
    AudioChunk,
    AudioCodec,
    SpeechToTextProvider,
    TextToSpeechProvider,
)

if TYPE_CHECKING:
    from movate.core.loader import AgentBundle

# Event "kinds" the driver emits — the wire protocol's server→client events
# (ADR 048 D4), expressed as a typed envelope so the transport is a thin
# serializer and tests can assert on structured events.
VoiceEventKind = Literal[
    "transcript.partial",
    "transcript.final",
    "agent.token",
    "tts.audio",
    "error",
    "done",
]


@dataclass(frozen=True)
class VoiceEvent:
    """One server→client event from the pipeline (ADR 048 D4).

    Exactly one of the payload fields is meaningful per ``kind``:

    * ``transcript.partial`` / ``transcript.final`` → ``text``
    * ``agent.token`` → ``text`` (a streamed agent output delta, ADR 045 D11)
    * ``tts.audio`` → ``audio`` (a synthesized :class:`AudioChunk`)
    * ``error`` → ``message`` + ``code`` + ``stage`` (the degrade taken, D8)
    * ``done`` → ``run_id`` + ``status`` (terminal; mirrors the SSE ``done``)
    """

    kind: VoiceEventKind
    text: str = ""
    audio: AudioChunk | None = None
    message: str = ""
    code: str = ""
    stage: str = ""
    run_id: str = ""
    status: str = ""


@dataclass
class VoicePipelineResult:
    """What :func:`run_voice_pipeline` produces, beside the event stream.

    Populated as the events flow so a caller (and the WS route's ``usage``
    frame) can read the turn's outcome without re-parsing events.
    """

    transcript: str = ""
    answer_text: str = ""
    run_id: str = ""
    status: str = ""
    events: list[VoiceEvent] = field(default_factory=list)


async def run_voice_pipeline(
    *,
    audio_in: AsyncIterator[AudioChunk],
    stt: SpeechToTextProvider,
    tts: TextToSpeechProvider,
    executor: Any,
    bundle: AgentBundle,
    tenant_id: str,
    input_key: str = "text",
    language: str | None = None,
    voice_id: str = "",
    codec: AudioCodec = "pcm16",
    stt_api_key: str | None = None,
    tts_api_key: str | None = None,
) -> AsyncIterator[VoiceEvent]:
    """Drive one voice turn: audio → STT → the unchanged agent → TTS → audio.

    Yields :class:`VoiceEvent`s in pipeline order. The agent stage reuses the
    Executor exactly (``execute(..., on_token=...)``) — the same call the SSE
    streaming route makes — so the zero-change-to-existing-agents promise
    holds (ADR 048 R1/R2).

    ``input_key`` is the agent-input field the transcript is bound to
    (default ``"text"`` — the common single-text-field convention). It is the
    one knob the transport may pass through from the connect handshake so a
    differently-shaped agent still works without editing its ``agent.yaml``.
    """
    from movate.core.models import RunRequest  # noqa: PLC0415 - lazy: keep import light

    # ── Stage 1: STT (audio → text), streaming partials + a final transcript ──
    final_transcript: str | None = None
    try:
        async for tchunk in stt.transcribe(audio_in, language=language, api_key=stt_api_key):
            if tchunk.is_final:
                final_transcript = tchunk.text
                yield VoiceEvent(kind="transcript.final", text=tchunk.text)
            else:
                yield VoiceEvent(kind="transcript.partial", text=tchunk.text)
    except Exception as exc:  # provider down mid-stream → degrade (D8)
        yield VoiceEvent(
            kind="error",
            message=str(exc) or exc.__class__.__name__,
            code="stt_error",
            stage="stt",
        )
        return

    if final_transcript is None:
        # The provider streamed only partials and never endpointed — we have no
        # utterance to run the agent on. Surface it rather than hang.
        yield VoiceEvent(
            kind="error",
            message="speech-to-text produced no final (endpointed) transcript",
            code="stt_no_final",
            stage="stt",
        )
        return

    # ── Stage 2: the UNCHANGED text Executor, with token streaming (D11) ──
    # Decouple the executor's *sync* on_token callback from our *async*
    # generator with a queue — the exact pattern ``_sse_run_stream`` uses.
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    run_request = RunRequest(agent=bundle.spec.name, input={input_key: final_transcript})

    async def _drive() -> None:
        try:
            response = await executor.execute(
                bundle,
                run_request,
                on_token=lambda delta: queue.put_nowait(("token", delta)),
                tenant_id_override=tenant_id,
            )
            await queue.put(("result", response))
        except Exception as exc:  # surface ANY failure as an error event
            await queue.put(("exc", exc))

    task = asyncio.create_task(_drive())
    token_parts: list[str] = []
    answer_text = ""
    run_id = ""
    status = ""
    agent_failed = False
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "token":
                if payload:
                    token_parts.append(payload)
                    yield VoiceEvent(kind="agent.token", text=payload)
                continue
            if kind == "result":
                response = payload
                run_id = response.run_id
                status = response.status
                if response.status == "error":
                    err = response.error
                    agent_failed = True
                    yield VoiceEvent(
                        kind="error",
                        message=err.message if err is not None else "run failed",
                        code=err.type if err is not None else "agent_error",
                        stage="agent",
                    )
                else:
                    # Prefer the agent's human-readable answer for speech; fall
                    # back to the concatenated streamed tokens.
                    answer_text = response.human_readable or "".join(token_parts)
                break
            if kind == "exc":
                agent_exc = payload
                agent_failed = True
                yield VoiceEvent(
                    kind="error",
                    message=str(agent_exc) or agent_exc.__class__.__name__,
                    code="agent_error",
                    stage="agent",
                )
                break
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    if agent_failed:
        return

    # ── Stage 3: TTS (text → audio), streaming the answer back as audio ──
    async def _answer_text_stream() -> AsyncIterator[str]:
        # Feed the whole answer as one delta. (A streaming-native TTS would
        # benefit from the per-token stream; the buffered OpenAI adapter
        # joins it anyway, so one delta is simplest and lossless here.)
        if answer_text:
            yield answer_text

    try:
        async for achunk in tts.synthesize(
            _answer_text_stream(), voice_id=voice_id, codec=codec, api_key=tts_api_key
        ):
            yield VoiceEvent(kind="tts.audio", audio=achunk)
    except Exception as exc:  # TTS down → caller already got the text answer (D8)
        yield VoiceEvent(
            kind="error",
            message=str(exc) or exc.__class__.__name__,
            code="tts_error",
            stage="tts",
        )
        # Not a hard fail — fall through to the terminal done event below.

    yield VoiceEvent(kind="done", run_id=run_id, status=status)
