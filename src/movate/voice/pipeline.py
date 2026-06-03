"""The voice pipeline driver — STT → an ``AgentTurn`` → TTS.

This is the modality-blind orchestration ADR 048 D1 describes, generalized per
ADR 067 D3:

    audio ──▶ STT ──▶ [ the agent stage — any ``AgentTurn`` ] ──▶ TTS ──▶ audio

It is the *logic* the transport (a WS ``/voice`` route, a Lyzr deployment, the
pipeline tests) wraps. The key architectural property is that the agent stage is
an **injected seam**, not a hard-coded engine: this module awaits
``agent.run(transcript, on_token=...)`` against the :class:`~movate.voice.agent_turn.AgentTurn`
Protocol. It does **not** import, subclass, or know about the mdk ``Executor``
(or Lyzr, or any framework); the agent never learns the text arrived as speech
(CLAUDE.md rule 6 / ADR 048 R2 / ADR 067). The mapping from transcript to a
concrete agent run lives in the ``AgentTurn`` implementation, not here.

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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from movate.voice.agent_turn import AgentTurn
from movate.voice.base import (
    AudioChunk,
    AudioCodec,
    SpeechToTextProvider,
    TextToSpeechProvider,
)
from movate.voice.chunking import SentenceChunker

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


# ── Speculative agent kickoff (ADR 070) ──────────────────────────────────────
# These helpers run the agent stage as a (token / result / exc) event queue so
# BOTH TTS paths (sequential + streaming) consume an agent run identically — and
# so a *speculative* run started early (on a stable interim) can be adopted by
# either path on commit, byte-for-byte the same as a fresh run.


def _norm_text(text: str) -> str:
    """Normalize for interim==final comparison (case/space/trailing punctuation)."""
    return " ".join(text.lower().split()).rstrip(".,!?;: ")


def _start_agent_run(
    agent: AgentTurn,
    transcript: str,
    *,
    language: str | None,
    session_id: str | None,
    agent_timeout: float | None,
) -> tuple[asyncio.Queue[tuple[str, Any]], asyncio.Task[None]]:
    """Start ``agent.run`` as a task feeding a (kind, payload) queue.

    The queue carries ``("token", delta)`` for each streamed delta, then exactly
    one terminal ``("result", AgentTurnResult)`` or ``("exc", Exception)``. This
    is the single shape both TTS paths drain — and the same shape a speculative
    run produces, so commit is just "adopt this queue/task instead of a new one."
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def _drive() -> None:
        try:
            run = agent.run(
                transcript,
                on_token=lambda delta: queue.put_nowait(("token", delta)),
                language=language,
                session_id=session_id,
            )
            result = await (asyncio.wait_for(run, agent_timeout) if agent_timeout else run)
            await queue.put(("result", result))
        except Exception as exc:  # surface ANY failure (incl. timeout) as an error
            await queue.put(("exc", exc))

    return queue, asyncio.create_task(_drive())


def _agent_is_speculatable(agent: AgentTurn) -> bool:
    """Whether ``agent`` opted into cancel-safe speculation (ADR 070 D3)."""
    return bool(getattr(agent, "speculatable", False))


class _Speculator:
    """Runs at most one speculative agent turn during the STT stage (ADR 070).

    Fed each interim transcript via :meth:`note_partial`; debounces by
    ``quiet_gap_s`` so a run fires only after the interim stops changing. On
    :meth:`resolve` (STT final), if the running speculation's text matches the
    final it is **committed** (its queue/task handed to the agent stage); else it
    is **cancelled** and discarded. A no-op unless the agent is speculatable.
    """

    def __init__(
        self,
        agent: AgentTurn,
        *,
        quiet_gap_s: float,
        language: str | None,
        session_id: str | None,
        agent_timeout: float | None,
        observer: Any,
    ) -> None:
        self._agent = agent
        self._quiet_gap_s = max(0.0, quiet_gap_s)
        self._language = language
        self._session_id = session_id
        self._agent_timeout = agent_timeout
        self._observer = observer
        self._loop = asyncio.get_event_loop()
        self._armed: asyncio.TimerHandle | None = None
        self._spec_text: str | None = None  # normalized text of the in-flight run
        self._queue: asyncio.Queue[tuple[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        # Tasks we cancelled (or that were superseded) — drained in aclose() so
        # asyncio never warns about an un-retrieved exception (cancel-safety).
        self._abandoned: list[asyncio.Task[None]] = []

    def _emit(self, event: str, **fields: Any) -> None:
        if self._observer is not None:
            with contextlib.suppress(Exception):
                self._observer.on_event(event, **fields)

    def note_partial(self, text: str) -> None:
        """A new interim transcript arrived — (re)arm/replace the speculation."""
        norm = _norm_text(text)
        if not norm or norm == self._spec_text:
            return  # empty, or already speculating on this exact text
        # The interim changed → drop any in-flight speculation and re-debounce.
        self._cancel_running()
        if self._armed is not None:
            self._armed.cancel()
        self._armed = self._loop.call_later(self._quiet_gap_s, self._fire, text)

    def _fire(self, text: str) -> None:
        self._armed = None
        self._spec_text = _norm_text(text)
        self._queue, self._task = _start_agent_run(
            self._agent,
            text,
            language=self._language,
            session_id=self._session_id,
            agent_timeout=self._agent_timeout,
        )
        self._emit("speculation_started", chars=len(text))

    def resolve(
        self, final_text: str
    ) -> tuple[asyncio.Queue[tuple[str, Any]], asyncio.Task[None]] | None:
        """STT endpointed — commit a matching speculation, else cancel it."""
        if self._armed is not None:
            self._armed.cancel()
            self._armed = None
        if (
            self._task is not None
            and self._queue is not None
            and self._spec_text == _norm_text(final_text)
        ):
            self._emit("speculation_committed")
            committed = (self._queue, self._task)
            self._queue = self._task = self._spec_text = None
            return committed
        if self._task is not None:
            self._emit("speculation_cancelled")
        self._cancel_running()
        return None

    def _cancel_running(self) -> None:
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            self._abandoned.append(self._task)
        self._task = self._queue = self._spec_text = None

    async def aclose(self) -> None:
        """Cancel + drain any abandoned speculative tasks (cancel-safety)."""
        if self._armed is not None:
            self._armed.cancel()
            self._armed = None
        self._cancel_running()
        for task in self._abandoned:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._abandoned.clear()


async def run_voice_pipeline(
    *,
    audio_in: AsyncIterator[AudioChunk],
    stt: SpeechToTextProvider,
    tts: TextToSpeechProvider,
    agent: AgentTurn,
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
    speculative: bool = False,
    speculation_quiet_gap_s: float = 0.3,
    observer: Any = None,
    clock: Any = None,
) -> AsyncIterator[VoiceEvent]:
    """Drive one voice turn: audio → STT → the agent stage → TTS → audio.

    Yields :class:`VoiceEvent`s in pipeline order. The agent stage is the
    injected :class:`~movate.voice.agent_turn.AgentTurn` seam (ADR 067 D3) — the
    pipeline awaits ``agent.run(transcript, on_token=...)`` and knows nothing
    about *which* framework runs the turn. The streamed ``on_token`` deltas
    become ``agent.token`` events, so the answer streams as it is produced.

    The transcript-to-agent-input binding (an mdk ``input_key``, a Lyzr message)
    is the ``AgentTurn`` implementation's concern, not the pipeline's.
    ``session_id`` is an optional pass-through for adapters that thread
    multi-turn state (mdk/Lyzr keep their own session/memory).

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

    ``tts_streaming`` (opt-in, default off) overlaps synthesis with generation:
    the agent token stream is split into sentences
    (:class:`~movate.voice.chunking.SentenceChunker`)
    and fed to TTS *as they complete*, so the first sentence is spoken while the
    agent is still producing the rest — the biggest time-to-first-audio win. It
    runs the agent and TTS concurrently, so ``agent.token`` and ``tts.audio``
    events interleave (consumers already key off ``kind``). The default
    (``False``) path is the strictly-sequential agent-then-TTS turn, byte-for-byte
    unchanged. In streaming mode an agent that errors *after* emitting tokens may
    have already spoken them (a live turn cannot be un-spoken); terminal errors
    (auth/schema) emit no tokens, so nothing is spoken before the error.

    ``pii_redactor`` (opt-in) masks PII in the **emitted** ``transcript.*`` events
    (captions / logs / observability) while the agent stage still runs on the raw
    transcript — compliance on the observability surface without starving the
    agent (e.g. :func:`~movate.voice.pii.redact_pii`).

    ``agent_timeout`` (opt-in) bounds the agent stage: if ``agent.run`` doesn't
    finish within it, the run is cancelled and a ``stage="agent"`` error is
    emitted (STT/TTS already have their own timeouts via the failover composites;
    this closes the one stage that otherwise could hang forever).

    ``text_filter`` (opt-in) reshapes the agent's text *just before* synthesis —
    e.g. :func:`~movate.voice.speakify.speakify` strips Markdown so a text agent's
    ``**bold**`` / bullet lists don't get read aloud literally. It is applied to
    the answer (sequential) or to each sentence (streaming); ``None`` leaves the
    text untouched.

    ``speculative`` (opt-in, default off — ADR 070) starts the agent stage early,
    on a **stable interim transcript**, before STT endpoints (``transcript.final``).
    The biggest fixed cost of a pipeline turn is the endpointing silence-wait
    (~1.5s, measured ~1.66s headroom in the bench); speculation runs the agent
    during that wait and, when the endpointed final matches the interim we
    speculated on, **commits** the in-flight run — recovering most of that gap.
    When the final differs (the caller kept talking), the speculative run is
    **cancelled** and discarded, and the agent runs fresh on the corrected
    transcript. Speculation only fires when ``getattr(agent, "speculatable",
    False)`` is true (the :class:`~movate.voice.agent_turn.AgentTurn`
    cancel-safety opt-in, ADR 070 D3): a cancelled speculative run must be safe
    to discard (no irreversible pre-first-token side effect). Speculative tokens
    are **buffered**, never emitted, until commit — a cancelled speculation
    reaches neither the wire nor TTS.

    ``speculation_quiet_gap_s`` (default 0.3s) is the debounce: a speculation
    fires only after the interim transcript has been stable for this long, so we
    don't start-and-cancel on every mid-utterance word (the knob that keeps the
    cancel/waste rate down — ADR 070 D1).

    ``observer`` (optional :class:`~movate.voice.observer.VoiceObserver`) receives
    ``speculation_started`` / ``speculation_committed`` / ``speculation_cancelled``
    events so the win and its cost (cancel ratio) are measurable live (ADR 070
    D5). ``None`` (default) drops them.

    ``clock`` is an injectable ``() -> float`` monotonic-seconds source (defaults
    to :func:`time.monotonic`) used to stamp each event's ``at_ms`` offset; a
    test passes a deterministic clock to assert exact latency numbers.
    """
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
    # ``pii_redactor`` masks the EMITTED transcript text (captions/logs/obs) while
    # the agent still runs on the RAW transcript — compliance on the observability
    # surface without crippling the agent (ADR: redact at the edge, not the input).
    def _shown(text: str) -> str:
        return pii_redactor(text) if pii_redactor is not None else text

    # Speculative agent kickoff (ADR 070): only when opted in AND the agent is
    # cancel-safe. The speculator watches interims and may start the agent early;
    # it is a no-op otherwise. ``committed`` (set after STT endpoints) is a
    # pre-started agent run the agent stage adopts instead of starting fresh.
    speculator = (
        _Speculator(
            agent,
            quiet_gap_s=speculation_quiet_gap_s,
            language=language,
            session_id=session_id,
            agent_timeout=agent_timeout,
            observer=observer,
        )
        if speculative and _agent_is_speculatable(agent)
        else None
    )
    committed: tuple[asyncio.Queue[tuple[str, Any]], asyncio.Task[None]] | None = None

    final_transcript: str | None = None
    try:
        async for tchunk in stt.transcribe(audio_in, language=language, api_key=stt_api_key):
            if tchunk.is_final:
                final_transcript = tchunk.text
                yield _stamp(VoiceEvent(kind="transcript.final", text=_shown(tchunk.text)))
            else:
                if speculator is not None:
                    speculator.note_partial(tchunk.text)
                yield _stamp(VoiceEvent(kind="transcript.partial", text=_shown(tchunk.text)))
    except Exception as exc:  # provider down mid-stream → degrade (D8)
        if speculator is not None:
            await speculator.aclose()
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
        if speculator is not None:
            await speculator.aclose()
        yield _stamp(
            VoiceEvent(
                kind="error",
                message="speech-to-text produced no final (endpointed) transcript",
                code="stt_no_final",
                stage="stt",
            )
        )
        return

    # STT endpointed: commit a matching speculation (adopt its in-flight run) or
    # cancel it. Either way the agent stage below sees one uniform run handle.
    # ``aclose`` then drains any *abandoned* (cancelled / superseded) speculative
    # tasks so asyncio never warns about an un-retrieved exception — it leaves the
    # committed run (if any) untouched for the agent stage to consume.
    if speculator is not None:
        committed = speculator.resolve(final_transcript)
        await speculator.aclose()

    # ── Stage 2+3 (streaming, opt-in): overlap synthesis with generation ──
    if tts_streaming:
        async for streamed_event in _run_streaming_turn(
            agent=agent,
            tts=tts,
            transcript=final_transcript,
            language=language,
            session_id=session_id,
            voice_id=voice_id,
            codec=codec,
            tts_api_key=tts_api_key,
            cancel=cancel,
            text_filter=text_filter,
            agent_timeout=agent_timeout,
            committed=committed,
            stamp=_stamp,
        ):
            yield streamed_event
        return

    # ── Stage 2: the agent stage (an AgentTurn), with token streaming (D11) ──
    # Decouple the agent's *sync* on_token callback from our *async* generator
    # with a queue — the agent streams deltas as it produces them, we forward.
    # A committed speculation (ADR 070) hands us an already-running queue/task on
    # the SAME shape, so adoption is a one-line swap; otherwise start fresh.
    if committed is not None:
        queue, task = committed
    else:
        queue, task = _start_agent_run(
            agent,
            final_transcript,
            language=language,
            session_id=session_id,
            agent_timeout=agent_timeout,
        )

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
                result = payload
                run_id = result.run_id
                status = result.status
                if result.error is not None:
                    err = result.error
                    agent_failed = True
                    yield _stamp(
                        VoiceEvent(
                            kind="error",
                            message=err.message or "agent run failed",
                            code=err.code or "agent_error",
                            stage="agent",
                        )
                    )
                else:
                    # Prefer the agent's answer text for speech; fall back to the
                    # concatenated streamed tokens.
                    answer_text = result.answer_text or "".join(token_parts)
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
    if text_filter is not None and answer_text:
        answer_text = text_filter(answer_text)

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


# Sentinel marking both concurrent phases finished (drained from the event queue).
_PHASES_DONE = object()


async def _run_streaming_turn(
    *,
    agent: AgentTurn,
    tts: TextToSpeechProvider,
    transcript: str,
    language: str | None,
    session_id: str | None,
    voice_id: str,
    codec: AudioCodec,
    tts_api_key: str | None,
    cancel: asyncio.Event | None,
    text_filter: Callable[[str], str] | None,
    agent_timeout: float | None,
    committed: tuple[asyncio.Queue[tuple[str, Any]], asyncio.Task[None]] | None = None,
    stamp: Any,
) -> AsyncIterator[VoiceEvent]:
    """Run the agent and TTS concurrently, streaming sentences as they complete.

    The agent phase forwards ``agent.token`` events and splits the token stream
    into sentences (:class:`~movate.voice.chunking.SentenceChunker`), pushing each to
    a sentence queue. The TTS phase synthesizes that sentence stream and forwards
    ``tts.audio`` events. Both funnel into one event queue so the caller yields
    them interleaved, in real-time order — the overlap that buys time-to-first-
    audio. A ``done`` is emitted at the end unless the agent failed (mirroring the
    sequential path, which ends on the agent error with no ``done``).
    """
    sentence_q: asyncio.Queue[str | None] = asyncio.Queue()
    event_q: asyncio.Queue[Any] = asyncio.Queue()
    state: dict[str, Any] = {
        "run_id": "",
        "status": "",
        "agent_failed": False,
        "interrupted": False,
    }

    async def _agent_phase() -> None:
        chunker = SentenceChunker()
        token_parts: list[str] = []

        # Adopt a committed speculation (ADR 070) — its queue already carries the
        # buffered early tokens; we drain it exactly like a fresh run. Otherwise
        # start the agent now on the endpointed transcript.
        if committed is not None:
            token_q, drive_task = committed
        else:
            token_q, drive_task = _start_agent_run(
                agent,
                transcript,
                language=language,
                session_id=session_id,
                agent_timeout=agent_timeout,
            )
        try:
            while True:
                kind, payload = await token_q.get()
                if kind == "token":
                    if payload:
                        token_parts.append(payload)
                        await event_q.put(stamp(VoiceEvent(kind="agent.token", text=payload)))
                        for sentence in chunker.feed(payload):
                            await sentence_q.put(sentence)
                    continue
                if kind == "result":
                    res = payload
                    state["run_id"] = res.run_id
                    state["status"] = res.status
                    if res.error is not None:
                        state["agent_failed"] = True
                        await event_q.put(
                            stamp(
                                VoiceEvent(
                                    kind="error",
                                    message=res.error.message or "agent run failed",
                                    code=res.error.code or "agent_error",
                                    stage="agent",
                                )
                            )
                        )
                    else:
                        tail = chunker.flush()
                        if tail:
                            await sentence_q.put(tail)
                        # A non-streaming agent (no on_token) → speak its answer_text.
                        if not token_parts and res.answer_text:
                            await sentence_q.put(res.answer_text)
                    break
                if kind == "exc":
                    state["agent_failed"] = True
                    await event_q.put(
                        stamp(
                            VoiceEvent(
                                kind="error",
                                message=str(payload) or payload.__class__.__name__,
                                code="agent_error",
                                stage="agent",
                            )
                        )
                    )
                    break
        finally:
            if not drive_task.done():
                drive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drive_task
            await sentence_q.put(None)  # close the sentence stream

    async def _tts_phase() -> None:
        # Barge-in before synthesis even starts → speak nothing (don't invoke TTS).
        if cancel is not None and cancel.is_set():
            state["interrupted"] = True
            return

        # Synthesize ONE sentence at a time: sentence N is spoken while the agent
        # is still generating sentence N+1. (A single synthesize() fed the whole
        # sentence stream would only overlap for streaming-native adapters; a
        # per-sentence call overlaps for buffered adapters too — the real win.)
        while True:
            sentence = await sentence_q.get()
            if sentence is None:
                return  # agent finished, no more sentences
            if cancel is not None and cancel.is_set():
                state["interrupted"] = True
                return

            # Voice-shape each sentence just before synthesis (strip Markdown,
            # etc.); a chunk that filters down to nothing is skipped.
            if text_filter is not None:
                sentence = text_filter(sentence)
                if not sentence:
                    continue

            async def _one(_text: str = sentence) -> AsyncIterator[str]:
                yield _text

            audio_stream = tts.synthesize(
                _one(), voice_id=voice_id, codec=codec, api_key=tts_api_key
            )
            try:
                async for achunk in audio_stream:
                    if cancel is not None and cancel.is_set():
                        state["interrupted"] = True
                        return
                    await event_q.put(stamp(VoiceEvent(kind="tts.audio", audio=achunk)))
            except Exception as exc:
                await event_q.put(
                    stamp(
                        VoiceEvent(
                            kind="error",
                            message=str(exc) or exc.__class__.__name__,
                            code="tts_error",
                            stage="tts",
                        )
                    )
                )
                return  # stop speaking further sentences after a TTS failure
            finally:
                aclose = getattr(audio_stream, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()

    async def _both() -> None:
        # Run the agent and TTS concurrently, but race them against the barge-in
        # signal so a cancel fired while a phase is *idle* (e.g. TTS waiting on
        # the next sentence while the agent is still thinking) still interrupts
        # promptly — and cancels the agent so we don't burn tokens generating an
        # answer the caller cut off (and ``done`` doesn't wait for it).
        agent_task = asyncio.create_task(_agent_phase())
        tts_task = asyncio.create_task(_tts_phase())
        phases: asyncio.Future[Any] = asyncio.gather(agent_task, tts_task)
        cancel_task: asyncio.Future[Any] | None = (
            asyncio.ensure_future(cancel.wait()) if cancel is not None else None
        )
        try:
            if cancel_task is None:
                await phases
            else:
                done, _pending = await asyncio.wait(
                    {phases, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_task in done:
                    state["interrupted"] = True
        except Exception:
            pass
        finally:
            for task in (agent_task, tts_task):
                if not task.done():
                    task.cancel()
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
            # Drain BOTH the inner tasks AND the outer `phases` Gather so
            # asyncio doesn't surface a "_GatheringFuture exception was never
            # retrieved" warning when barge-in cancels mid-flight.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(agent_task, tts_task, return_exceptions=True)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await phases
            if cancel_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await cancel_task
            await event_q.put(_PHASES_DONE)

    orchestrator = asyncio.create_task(_both())
    try:
        while True:
            item = await event_q.get()
            if item is _PHASES_DONE:
                break
            yield item
    finally:
        if not orchestrator.done():
            orchestrator.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await orchestrator

    if not state["agent_failed"]:
        done_status = "interrupted" if state["interrupted"] else state["status"]
        yield stamp(VoiceEvent(kind="done", run_id=state["run_id"], status=done_status))
