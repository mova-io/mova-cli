"""Deepgram reference STT adapter — the T1 low-latency speech-to-text.

The streaming-native counterpart to the OpenAI Whisper reference in
:mod:`movate.voice.openai_speech`, behind the same ADR 048 D3 seam
(:class:`movate.voice.base.SpeechToTextProvider`). Deepgram is ADR 048/049's
**T1 "wow" tier**: a true streaming transcription socket that emits *partial*
hypotheses as the caller speaks and an *endpointed* final per utterance — the
latency story OpenAI's buffered transcription can't tell (see the shape note in
``openai_speech.py``).

The ``deepgram`` SDK import is **lazy + guarded** exactly like
:mod:`movate.providers.openai_native` / the OpenAI voice adapters: nothing here
imports ``deepgram`` at module scope, so a runtime/CLI installed without
``mdk[voice]`` is wholly unaffected (ADR 048 D9). The SDK client is constructed
on first use from the BYOK key; tests inject a fake via the ``client=`` kwarg.

Shape notes (re-confirmed at build time, per ADR 048's caveat that the provider
landscape moves fast):

* **Streaming socket** — Deepgram's live transcription is a bidirectional
  WebSocket: the caller ``send()``s audio frames and receives transcript
  events. The adapter drives both halves concurrently — a sender task pumps the
  inbound :class:`AudioChunk` stream into the socket, while the main coroutine
  consumes transcript events and yields :class:`TranscriptChunk` slices. An
  event with ``speech_final``/``is_final`` true (Deepgram's endpointing) yields
  a ``TranscriptChunk(is_final=True)``; interim results yield partials.
* **Endpointing** — Deepgram marks an endpointed utterance via ``speech_final``
  (VAD decided the turn ended) or, failing that, ``is_final`` (the final
  hypothesis for a segment). The adapter treats either as the Protocol's
  ``is_final`` so the pipeline knows when to run the agent. If the socket closes
  having only emitted interims, the adapter promotes the last interim to a final
  so the pipeline's "wait for is_final" loop unblocks rather than hangs (the
  same defensive guarantee the OpenAI adapter gives for empty audio).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from movate.voice.base import AudioChunk, TranscriptChunk

if TYPE_CHECKING:
    import deepgram

# Deepgram's live model + defaults. ``nova-3`` is Deepgram's current most
# accurate streaming model (succeeds nova-2 at the same price tier) and is the
# one that supports **keyterm prompting** — boosting domain vocabulary at
# recognition time. ``smart_format`` gives punctuated, readable transcripts. The
# encoding/sample-rate hint matches our ``pcm16`` ``AudioChunk`` default (raw
# signed 16-bit LE) so Deepgram decodes the raw frames the transport forwards
# without a container.
_DEEPGRAM_MODEL = "nova-3"
_DEEPGRAM_ENCODING = "linear16"
_DEEPGRAM_SAMPLE_RATE = 24_000

# Models that take the newer ``keyterm`` prompting param (nova-3 family). Older
# models (nova-2 and earlier) use the legacy ``keywords`` boosting param
# instead. We pick the right option key from the configured model so callers
# pass one ``keyterms`` list and the adapter wires it correctly either way.
_KEYTERM_MODEL_PREFIXES = ("nova-3",)

# Upper bound on the per-key client cache (ADR 073 Phase 5). One process rarely
# serves more than a handful of distinct BYOK keys at once; this just stops a
# pathological churn of keys from growing the cache without bound.
_CLIENT_CACHE_MAX = 32


def _require_deepgram() -> Any:
    """Import the ``deepgram`` SDK lazily, with a clear install hint.

    Mirrors :class:`movate.voice.openai_speech.OpenAIWhisperSTT` — the import
    lives inside the call so importing this module (e.g. for the Protocol type)
    never requires the optional dep.
    """
    try:
        import deepgram as _deepgram  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'deepgram-sdk' package is required for the Deepgram voice adapter. "
            "Install with: pip install 'mdk-voice[deepgram]'"
        ) from exc
    return _deepgram


def _read(payload: Any, name: str) -> Any:
    """Attribute-or-key read, so SDK objects and dict fakes both work."""
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def _chunk_is_final(payload: Any) -> bool:
    """Whether Deepgram has *committed* the text in this payload (no revisions).

    Deepgram's ``is_final=True`` says "this segment's transcript won't be
    rewritten" — it fires MID-STREAM during continuous speech every time
    Deepgram commits a chunk (a clause, a sentence). It is NOT a turn-end
    signal. Use :func:`_chunk_is_speech_final` for that.
    """
    return bool(_read(payload, "is_final"))


def _chunk_is_speech_final(payload: Any) -> bool:
    """Whether Deepgram's VAD says the speaker has finished a turn.

    ``speech_final=True`` only fires after the configured ``endpointing_ms``
    of trailing silence. This is the **real** end-of-turn signal — the only
    one the single-turn voice pipeline should run the agent on. A mid-sentence
    ``is_final=True`` commits the partial text without ending the turn (the
    caller is still mid-thought).
    """
    return bool(_read(payload, "speech_final"))


def _transcript_text(payload: Any) -> str:
    """Pull the best-alternative transcript text off a Deepgram event.

    Deepgram nests the transcript at ``channel.alternatives[0].transcript``.
    Walk it defensively (attr or key at each hop) and fall back to a top-level
    ``transcript`` field a simpler fake might expose. Returns ``""`` when the
    event carries no text (e.g. a keep-alive / metadata frame), which the caller
    filters out.
    """
    channel = _read(payload, "channel")
    if channel is not None:
        alternatives = _read(channel, "alternatives")
        if alternatives:
            text = _read(alternatives[0], "transcript")
            if isinstance(text, str):
                return text
    direct = _read(payload, "transcript")
    return direct if isinstance(direct, str) else ""


def _confidence(payload: Any) -> float | None:
    """Optional provider confidence in ``[0, 1]`` from the best alternative."""
    channel = _read(payload, "channel")
    if channel is not None:
        alternatives = _read(channel, "alternatives")
        if alternatives:
            conf = _read(alternatives[0], "confidence")
            if isinstance(conf, (int, float)):
                return float(conf)
    return None


class DeepgramSTT:
    """Deepgram :class:`~movate.voice.base.SpeechToTextProvider` (T1 streaming).

    Streams the inbound audio into Deepgram's live transcription socket and
    yields partial :class:`~movate.voice.base.TranscriptChunk` slices as the
    caller speaks, then an ``is_final=True`` chunk per endpointed utterance.
    """

    name = "deepgram"
    version = "0.0.1"

    def __init__(
        self,
        *,
        model: str = _DEEPGRAM_MODEL,
        finish_grace_seconds: float = 0.25,
        endpointing_ms: int = 1500,
        utterance_end_ms: int | None = 2500,
        keyterms: Sequence[str] | None = None,
        client: deepgram.DeepgramClient | None = None,
        reuse_client: bool = True,
    ) -> None:
        """``client`` is for tests — pass a fake exposing the live-transcription
        connection shape (``listen.asyncwebsocket.v("1")`` →
        ``start``/``send``/``finish`` + an ``on(...)`` event hook). Production
        leaves it ``None`` and the SDK client is constructed from the BYOK key
        on first use.

        ``finish_grace_seconds`` is a brief wait between the last sent audio
        frame and ``connection.finish()`` so Deepgram can flush the trailing
        audio's transcript (without it, the last ~200ms often gets dropped on
        real-time-paced streams). Pass ``0`` to disable (tests do).

        ``endpointing_ms`` is how much **silence** Deepgram waits before
        declaring an utterance complete (``speech_final``). Deepgram's default
        is ~10 ms which is brutally aggressive for thoughtful speech — a brief
        mid-sentence pause to think ("um, I was wondering if…") gets treated as
        end-of-turn and the agent jumps in. **1500 ms** is the default here:
        long enough that natural pauses don't trigger a turn-end, short enough
        that the caller doesn't feel ignored after they truly finish. Drop to
        300-500 for snappy back-and-forth, raise to 2500+ for deliberate
        speakers / IVR menus.

        ``utterance_end_ms`` (a separate Deepgram knob) forces an
        ``utterance_end`` event after this much continuous silence as a
        safety backstop, even when no speech-final has fired. Set ``None`` to
        disable. We default to **2000 ms** — slightly above ``endpointing_ms``
        so it acts as the ceiling, not the primary trigger.

        ``keyterms`` is an optional list of domain terms to **boost** at
        recognition time — names, acronyms, and jargon a general model
        otherwise mis-hears (``["VPN", "VIP", "Okta", "Mova-iO"]``). On a
        nova-3 model these become Deepgram ``keyterm`` prompts; on nova-2 and
        earlier they fall back to the legacy ``keywords`` param. ``None`` (the
        default) sends no boosting and is byte-for-byte the prior behavior.
        This is the cheapest accuracy win for enterprise vocabularies.

        ``reuse_client`` (default True, ADR 073 Phase 5) caches the constructed
        ``DeepgramClient`` per resolved key and reuses it across turns, instead
        of building a fresh one (and its connector / DNS / TLS setup) on every
        ``transcribe`` call. The SDK client is a connection-pooling factory — one
        per session, opening a new socket per turn, is the intended usage — so
        this removes per-turn client cold-start with no behavior change. Call
        :meth:`warm` once at session start to also skip it on the *first* turn.
        Set ``False`` to restore the old construct-per-call behavior.
        """
        self._model = model
        self._finish_grace_seconds = max(0.0, finish_grace_seconds)
        self._endpointing_ms = max(0, int(endpointing_ms))
        self._utterance_end_ms = (
            max(0, int(utterance_end_ms)) if utterance_end_ms is not None else None
        )
        self._keyterms = [t for t in (keyterms or []) if t and t.strip()]
        self._client = client
        # ADR 073 Phase 5 — per-key client cache so turns 2..N skip client
        # construction (connector/DNS/TLS setup). Bounded so a churn of BYOK keys
        # can't grow it unbounded. An injected ``client`` bypasses it entirely.
        self._reuse_client = reuse_client
        self._client_cache: dict[str, Any] = {}

    def _uses_keyterm_prompting(self) -> bool:
        """Whether the configured model takes ``keyterm`` (vs legacy ``keywords``)."""
        return any(self._model.startswith(p) for p in _KEYTERM_MODEL_PREFIXES)

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        import os  # noqa: PLC0415

        deepgram_mod = _require_deepgram()
        # Per-call client when a tenant BYOK key is supplied (ADR 018); else fall
        # back to the SDK's own env (DEEPGRAM_API_KEY) for the local/dev path.
        key = api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        if not self._reuse_client:
            return deepgram_mod.DeepgramClient(key)
        # ADR 073 Phase 5: reuse the client (and its connector) across turns.
        cached = self._client_cache.get(key)
        if cached is None:
            cached = deepgram_mod.DeepgramClient(key)
            if len(self._client_cache) >= _CLIENT_CACHE_MAX:
                self._client_cache.clear()  # crude bound — keys are few per process
            self._client_cache[key] = cached
        return cached

    async def warm(self, api_key: str | None = None) -> bool:
        """Pre-construct + cache the Deepgram client (ADR 073 Phase 5).

        Call once at session start (while the user is being greeted) so the
        *first* turn doesn't pay client cold-start. Best-effort: returns True if
        it warmed a reusable client, False if warming doesn't apply (an injected
        client, or ``reuse_client=False``) or the construction failed — warming
        is an optimization and must never break the session.

        Note: this warms the *client/connector*, not a live recognition socket.
        Deepgram binds per-utterance options (sample rate, language, keyterms,
        endpointing) at ``connection.start()`` and closes idle sockets, so a
        pre-opened socket can't be reused for the next turn — pooling live
        sockets is deliberately out of scope.
        """
        if self._client is not None or not self._reuse_client:
            return False
        try:
            self._resolve_client(api_key)
            return True
        except Exception:
            return False

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
        keyterms: Sequence[str] | None = None,
        endpointing_ms: int | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        import asyncio  # noqa: PLC0415
        import contextlib  # noqa: PLC0415

        # ADR 071 D4: per-call keyterms (e.g. an agent's domain vocab) merge with
        # the constructor list — union, de-duped, order-preserving — so a tenant
        # default + a per-agent set both apply.
        call_keyterms = [t for t in (keyterms or []) if t and t.strip()]
        effective_keyterms = list(dict.fromkeys([*self._keyterms, *call_keyterms]))

        # ADR 073 D3: a per-call endpointing override (an agent's silence-hold)
        # wins over the constructor default for this turn; None keeps the default.
        effective_endpointing = (
            max(0, int(endpointing_ms)) if endpointing_ms is not None else self._endpointing_ms
        )

        # PEEK the first audio chunk so we can declare the correct sample rate
        # to Deepgram (mismatch yields empty transcripts — verified live with
        # the browser demo at 16kHz vs the previous hardcoded 24kHz default).
        first_chunk: AudioChunk | None = None
        sample_rate = _DEEPGRAM_SAMPLE_RATE
        audio_iter = audio.__aiter__()
        try:
            first_chunk = await audio_iter.__anext__()
            sample_rate = first_chunk.sample_rate or _DEEPGRAM_SAMPLE_RATE
        except StopAsyncIteration:
            # No audio at all → emit empty final, same defensive guarantee as
            # the buffered OpenAI Whisper adapter.
            yield TranscriptChunk(text="", is_final=True)
            return

        async def _audio_with_replay() -> AsyncIterator[AudioChunk]:
            if first_chunk is not None:
                yield first_chunk
            async for c in audio_iter:
                yield c

        client = self._resolve_client(api_key)
        # The live socket: client.listen.asyncwebsocket.v("1"). The SDK exposes
        # an event-callback API (``connection.on(event, handler)``); we bridge
        # those callbacks onto an asyncio.Queue so we can yield from a plain
        # async generator (the same decouple-callback-from-generator pattern the
        # voice pipeline uses for the executor's sync on_token).
        connection = client.listen.asyncwebsocket.v("1")
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _on_transcript(_self: Any = None, result: Any = None, **_: Any) -> None:
            queue.put_nowait(("transcript", result))

        async def _on_close(_self: Any = None, **_: Any) -> None:
            queue.put_nowait(("close", None))

        async def _on_error(_self: Any = None, error: Any = None, **_: Any) -> None:
            queue.put_nowait(("error", error))

        # Register handlers. Event names live on the SDK's LiveTranscriptionEvents
        # enum; fall back to the canonical string names if the enum isn't present
        # on a fake. Registration is best-effort — a minimal fake may no-op here.
        events = (
            getattr(_require_deepgram(), "LiveTranscriptionEvents", None)
            if (self._client is None)
            else None
        )
        with contextlib.suppress(Exception):
            connection.on(getattr(events, "Transcript", "Results"), _on_transcript)
            connection.on(getattr(events, "Close", "Close"), _on_close)
            connection.on(getattr(events, "Error", "Error"), _on_error)

        await connection.start(
            self._build_options(
                language, sample_rate, effective_keyterms, endpointing_ms=effective_endpointing
            )
        )

        async def _pump() -> None:
            # Rate-limit OUTBOUND to real-time: Deepgram needs at least
            # real-time pacing or it returns empty (verified live — when
            # FailoverSTT replays a buffered utterance to us in a burst,
            # Deepgram closes before transcribing). If the inbound audio
            # iterator is already paced (live mic), our sleep is a no-op
            # because elapsed will already exceed frame_seconds.
            loop = asyncio.get_event_loop()
            last_send: float | None = None
            try:
                async for chunk in _audio_with_replay():
                    if sample_rate > 0 and last_send is not None:
                        frame_seconds = (len(chunk.data) / 2) / sample_rate
                        elapsed = loop.time() - last_send
                        if elapsed < frame_seconds:
                            await asyncio.sleep(frame_seconds - elapsed)
                    await connection.send(chunk.data)
                    last_send = loop.time()
            finally:
                # Grace before finish(): without it, the socket closes the
                # instant the last frame is sent and Deepgram can drop the
                # trailing ~200ms (e.g. "today?" missing on a real-time-paced
                # demo). 250ms is enough to recover the tail without adding
                # meaningful latency to a turn.
                with contextlib.suppress(Exception):
                    await asyncio.sleep(self._finish_grace_seconds)
                with contextlib.suppress(Exception):
                    await connection.finish()

        sender = asyncio.get_event_loop().create_task(_pump())

        # Deepgram emits TWO different "final" signals on a continuous stream:
        #   - ``is_final=True``       per-segment commit (fires MID-STREAM
        #                             every time Deepgram is sure of a chunk).
        #   - ``speech_final=True``   end-of-turn (VAD decided silence ≥
        #                             endpointing_ms after the user stopped).
        # The single-turn voice pipeline runs the agent on the FIRST
        # ``TranscriptChunk(is_final=True)`` we yield — so we MUST only mark
        # speech_final events as final. Mid-stream commits become partials
        # carrying the cumulative committed text. Bug if we don't: long
        # multi-clause prompts ("One of the VIP user reported that his VPN
        # connects but no network access. help me resolve this") get chopped
        # at the first comma-pause where Deepgram commits a segment.
        committed: list[str] = []  # final-form segments so far
        last_partial: str | None = None  # latest still-evolving interim text
        emitted_final = False  # did we yield our is_final=True yet?
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "error":
                    raise RuntimeError(str(payload) or "deepgram transcription error")
                if kind == "close":
                    break
                if kind != "transcript":
                    continue
                text = _transcript_text(payload)
                if not text:
                    continue  # keep-alive / empty interim — nothing to surface
                segment_committed = _chunk_is_final(payload)
                speech_final = _chunk_is_speech_final(payload)
                if segment_committed:
                    committed.append(text)
                    last_partial = None
                else:
                    last_partial = text
                # Build the cumulative text for the chunk we surface: committed
                # segments so far + the live partial (if any). This is what the
                # caller wants to see — the full utterance up to this moment.
                running = " ".join(committed + ([last_partial] if last_partial else []))
                if speech_final:
                    # Real end-of-turn — emit ONE final with the full utterance
                    # and stop forwarding partials (the pipeline will stop
                    # caring; downstream is now agent + TTS).
                    yield TranscriptChunk(
                        text=running, is_final=True, confidence=_confidence(payload)
                    )
                    emitted_final = True
                    break
                # Otherwise: still a partial (even if Deepgram committed this
                # particular segment — caller hasn't finished talking yet).
                yield TranscriptChunk(text=running, is_final=False, confidence=_confidence(payload))
        finally:
            if not sender.done():
                sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

        # If the socket closed before speech_final fired (e.g. caller hung up,
        # transport endpointed the audio_in stream, or Deepgram dropped the
        # connection), emit a backstop final with everything we have. Without
        # this the pipeline's "wait for is_final" loop hangs.
        if not emitted_final:
            tail = " ".join(committed + ([last_partial] if last_partial else []))
            yield TranscriptChunk(text=tail, is_final=True)

    def _build_options(
        self,
        language: str | None,
        sample_rate: int,
        keyterms: Sequence[str] | None = None,
        *,
        endpointing_ms: int | None = None,
    ) -> Any:
        """Build Deepgram ``LiveOptions`` for the socket.

        ``sample_rate`` MUST match the rate of the bytes being sent — Deepgram
        plays back at the declared rate, so a mismatch (e.g. browser captures
        16 kHz, we declare 24 kHz) makes the audio 1.5x too fast and the
        transcript comes back empty. We always pass through the actual rate
        from the first audio chunk; the module-level ``_DEEPGRAM_SAMPLE_RATE``
        is only the default before any audio has arrived.

        Falls back to a plain dict when the SDK type isn't importable (the test
        path with an injected fake), so a fake never needs the real SDK.
        """
        opts: dict[str, Any] = {
            "model": self._model,
            "encoding": _DEEPGRAM_ENCODING,
            "sample_rate": sample_rate,
            "smart_format": True,
            "interim_results": True,
            # See ``__init__`` docstring — 1500 ms keeps the agent from jumping
            # in on a mid-sentence pause to think (Deepgram's default ~10 ms
            # is way too aggressive for voice-agent UX). A per-call override
            # (ADR 073 D3, an agent's tuned silence-hold) wins when supplied.
            "endpointing": (
                self._endpointing_ms if endpointing_ms is None else max(0, int(endpointing_ms))
            ),
        }
        if self._utterance_end_ms is not None:
            opts["utterance_end_ms"] = self._utterance_end_ms
        if language:
            opts["language"] = language
        # Effective keyterms = constructor + per-call (resolved in transcribe);
        # fall back to the constructor list when called directly (e.g. tests).
        effective = list(keyterms) if keyterms is not None else list(self._keyterms)
        if effective:
            # nova-3 → keyterm prompting; nova-2/earlier → legacy keyword boost.
            # Deepgram accepts a list for both; the SDK serializes each as a
            # repeated query param.
            opts["keyterm" if self._uses_keyterm_prompting() else "keywords"] = effective
        if self._client is not None:
            # Test path: hand the fake the raw option dict.
            return opts
        live_options = getattr(_require_deepgram(), "LiveOptions", None)
        if live_options is None:  # pragma: no cover - SDK always ships it
            return opts
        return live_options(**opts)
