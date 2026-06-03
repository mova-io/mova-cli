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
import time
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

    ``at_ms`` is a monotonic wall-clock offset (milliseconds since the turn
    began, measured by :func:`run_voice_pipeline`) stamped on every event. It
    is **additive and back-compat**: it defaults to ``0.0``, the transport does
    not serialize it onto the wire, and existing consumers ignore it. It exists
    so the demo's latency badge (and an optional voice-turn trace span, ADR 024)
    can read per-stage latencies — STT-final → agent-first-token → TTS-first-
    audio — straight off the event stream, no extra clock plumbing.
    """

    kind: VoiceEventKind
    text: str = ""
    audio: AudioChunk | None = None
    message: str = ""
    code: str = ""
    stage: str = ""
    run_id: str = ""
    status: str = ""
    at_ms: float = 0.0


@dataclass(frozen=True)
class VoiceTurnLatency:
    """Per-stage latencies for one voice turn (the demo's latency badge source).

    Computed from the ``at_ms`` offsets :func:`run_voice_pipeline` stamps on the
    event stream (or, equivalently, from a transport that records its own
    arrival clock). All fields are milliseconds-since-turn-start; ``None`` means
    that milestone was never reached (e.g. the turn errored at STT, so the agent
    never produced a first token).

    * :attr:`stt_final_ms` — when STT endpointed (the user's words were final).
    * :attr:`agent_first_token_ms` — when the agent emitted its first token.
    * :attr:`tts_first_audio_ms` — when the first synthesized audio frame was
      ready to play (the "responded" moment the badge headlines).

    :attr:`responded_in_ms` is the headline number — time from turn start to the
    first audio the user hears, falling back to the agent's first token when TTS
    produced no audio (a degraded text-only turn still has a meaningful "first
    response" latency).
    """

    stt_final_ms: float | None = None
    agent_first_token_ms: float | None = None
    tts_first_audio_ms: float | None = None

    @property
    def responded_in_ms(self) -> float | None:
        """Turn-start → first audible response (falls back to first token)."""
        if self.tts_first_audio_ms is not None:
            return self.tts_first_audio_ms
        return self.agent_first_token_ms

    @property
    def agent_think_ms(self) -> float | None:
        """STT-final → agent-first-token (how long the agent took to start)."""
        if self.stt_final_ms is None or self.agent_first_token_ms is None:
            return None
        return max(0.0, self.agent_first_token_ms - self.stt_final_ms)

    @property
    def tts_ms(self) -> float | None:
        """Agent-first-token → first-audio (how long synthesis took to start)."""
        if self.agent_first_token_ms is None or self.tts_first_audio_ms is None:
            return None
        return max(0.0, self.tts_first_audio_ms - self.agent_first_token_ms)


def compute_turn_latency(events: list[VoiceEvent]) -> VoiceTurnLatency:
    """Derive a :class:`VoiceTurnLatency` from a turn's event stream.

    Reads the ``at_ms`` offset off the FIRST event of each milestone kind —
    the first ``transcript.final``, the first ``agent.token``, the first
    ``tts.audio``. Events without a stamped offset (legacy producers, the
    realtime path) contribute ``None`` for that milestone, so the badge simply
    shows the stages it has data for rather than fabricating a number.

    Pure and side-effect-free: the same input always yields the same latency,
    which is what the badge test and the trace-span stamp both rely on.
    """

    def _first(kind: str) -> float | None:
        for ev in events:
            if ev.kind == kind:
                return ev.at_ms
        return None

    return VoiceTurnLatency(
        stt_final_ms=_first("transcript.final"),
        agent_first_token_ms=_first("agent.token"),
        tts_first_audio_ms=_first("tts.audio"),
    )


def format_latency_badge(latency: VoiceTurnLatency) -> str:
    """Render the human-facing "responded in {X}ms" badge for the demo UI.

    The headline is the first-audible-response latency; the per-stage breakdown
    (agent think / synthesis) is appended when available so a viewer can see
    *where* the time went. Returns an empty string when no milestone was
    reached (nothing useful to show — e.g. an STT-stage error).
    """
    headline = latency.responded_in_ms
    if headline is None:
        return ""
    parts = [f"⚡ responded in {round(headline)}ms"]
    breakdown: list[str] = []
    if latency.agent_think_ms is not None:
        breakdown.append(f"agent {round(latency.agent_think_ms)}ms")
    if latency.tts_ms is not None:
        breakdown.append(f"voice {round(latency.tts_ms)}ms")
    if breakdown:
        parts.append(f"({' · '.join(breakdown)})")
    return " ".join(parts)


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
    cancel: asyncio.Event | None = None,
    clock: Any = None,
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

    ``cancel`` is the **barge-in** signal (additive, opt-in): an
    :class:`asyncio.Event` the transport sets when the user starts speaking
    while the agent is still talking. The pipeline checks it before each TTS
    audio frame and **stops synthesizing the in-flight answer** the moment it is
    set — so the user isn't talked over. The TTS generator is closed cleanly and
    the turn ends with a normal ``done`` (status ``"interrupted"``) rather than
    dropping audio mid-buffer. ``None`` (the default) preserves today's
    uninterruptible behavior byte-for-byte. (Barge-in *before* TTS — during STT
    or the agent run — is the realtime path's job; this seam interrupts the part
    a pipeline turn can actually still cancel: the spoken answer.)

    ``clock`` is an injectable ``() -> float`` monotonic-seconds source (defaults
    to :func:`time.monotonic`) used to stamp each event's ``at_ms`` offset; a
    test passes a deterministic clock to assert exact latency numbers.
    """
    from movate.core.models import RunRequest  # noqa: PLC0415 - lazy: keep import light

    _now = clock if clock is not None else time.monotonic
    _t0 = _now()

    def _stamp(event: VoiceEvent) -> VoiceEvent:
        # Record ms-since-turn-start on every event so a consumer can derive
        # per-stage latencies (the badge) without its own clock. Frozen
        # dataclass → rebuild via replace-on-construct.
        return VoiceEvent(
            kind=event.kind,
            text=event.text,
            audio=event.audio,
            message=event.message,
            code=event.code,
            stage=event.stage,
            run_id=event.run_id,
            status=event.status,
            at_ms=(_now() - _t0) * 1000.0,
        )

    # ── Stage 1: STT (audio → text), streaming partials + a final transcript ──
    final_transcript: str | None = None
    try:
        async for tchunk in stt.transcribe(audio_in, language=language, api_key=stt_api_key):
            if tchunk.is_final:
                final_transcript = tchunk.text
                yield _stamp(VoiceEvent(kind="transcript.final", text=tchunk.text))
            else:
                yield _stamp(VoiceEvent(kind="transcript.partial", text=tchunk.text))
    except Exception as exc:  # provider down mid-stream → degrade (D8)
        yield _stamp(
            VoiceEvent(
                kind="error",
                message=str(exc) or exc.__class__.__name__,
                code="stt_error",
                stage="stt",
            )
        )
        return

    if final_transcript is None:
        # The provider streamed only partials and never endpointed — we have no
        # utterance to run the agent on. Surface it rather than hang.
        yield _stamp(
            VoiceEvent(
                kind="error",
                message="speech-to-text produced no final (endpointed) transcript",
                code="stt_no_final",
                stage="stt",
            )
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
                    yield _stamp(VoiceEvent(kind="agent.token", text=payload))
                continue
            if kind == "result":
                response = payload
                run_id = response.run_id
                status = response.status
                if response.status == "error":
                    err = response.error
                    agent_failed = True
                    yield _stamp(
                        VoiceEvent(
                            kind="error",
                            message=err.message if err is not None else "run failed",
                            code=err.type if err is not None else "agent_error",
                            stage="agent",
                        )
                    )
                else:
                    # Prefer the agent's human-readable answer for speech; fall
                    # back to the concatenated streamed tokens.
                    answer_text = response.human_readable or "".join(token_parts)
                break
            if kind == "exc":
                agent_exc = payload
                agent_failed = True
                yield _stamp(
                    VoiceEvent(
                        kind="error",
                        message=str(agent_exc) or agent_exc.__class__.__name__,
                        code="agent_error",
                        stage="agent",
                    )
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

    interrupted = False
    # Barge-in: if the user already started talking before we even begin
    # synthesizing, don't speak at all.
    if cancel is not None and cancel.is_set():
        interrupted = True
    else:
        audio_stream = tts.synthesize(
            _answer_text_stream(), voice_id=voice_id, codec=codec, api_key=tts_api_key
        )
        try:
            async for achunk in audio_stream:
                # Barge-in check BEFORE emitting each frame: the user started
                # speaking → stop talking over them. Close the TTS generator so
                # the adapter can release its connection, then end the turn.
                if cancel is not None and cancel.is_set():
                    interrupted = True
                    break
                yield _stamp(VoiceEvent(kind="tts.audio", audio=achunk))
        except Exception as exc:  # TTS down → caller already got the text answer (D8)
            yield _stamp(
                VoiceEvent(
                    kind="error",
                    message=str(exc) or exc.__class__.__name__,
                    code="tts_error",
                    stage="tts",
                )
            )
            # Not a hard fail — fall through to the terminal done event below.
        finally:
            # Eagerly close the synthesis generator (cancel path or normal end)
            # so an in-flight provider stream is torn down promptly.
            aclose = getattr(audio_stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

    # ``interrupted`` marks a barge-in: the turn completed normally as far as the
    # agent/run is concerned, but the spoken answer was cut short. Surface it as
    # the done status so the UI can label it ("interrupted") without treating it
    # as an error. A plain successful turn keeps its original run status.
    done_status = "interrupted" if interrupted else status
    yield _stamp(VoiceEvent(kind="done", run_id=run_id, status=done_status))
