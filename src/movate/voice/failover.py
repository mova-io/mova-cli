"""Resilient failover composites — a router that *is* a provider (ADR 068 D1).

``FailoverSTT`` / ``FailoverTTS`` each implement the **same** speech Protocol they
wrap (:class:`~movate.voice.base.SpeechToTextProvider` /
:class:`~movate.voice.base.TextToSpeechProvider`), so the pipeline (ADR 048/067)
cannot tell a failover composite from a single adapter — the resilience is a
decorator over an ordered tier list, living strictly *above* the seam
(CLAUDE.md rule 6).

Per turn the composite:

1. **orders** providers latency-first by their capability manifest (ADR 068 D2),
   pushing providers whose projected cost would exceed a configured per-turn
   ``cost_budget`` to the back (ADR 068 D4 — the "cost-bounded" half);
2. **skips** providers whose circuit breaker is open (ADR 068 D3);
3. optionally **hedges** (ADR 068 D5, off by default): race the top two providers
   and take whichever returns first, buying latency with cost;
4. for each provider, **retries** transient failures per the failure taxonomy
   (:mod:`movate.voice.failures`), then **fails over** to the next provider;
5. for TTS, serves a repeated phrase from a :class:`~movate.voice.cache.VoiceCache`
   instead of re-synthesizing (ADR 068 D6);
6. emits structured events through the :class:`~movate.voice.observer.VoiceObserver`
   hook (ADR 068 D7) — silent by default, measured under mdk.

**Streaming + failover trade-off (MVP → batch 2).**  Failover was originally only
safe *before a result is committed* (ADR 068 D1).  **Batch 2 (#211)** extends
this: if a provider errors or times out *mid-stream* (after partials but before
``is_final`` / after partial audio but before stream completion), the composite
now fails over to the next provider *transparently* — the caller never sees the
break. STT replays the buffered audio to the secondary; TTS replays the buffered
text. Once an STT ``is_final`` has been yielded, the result is committed and a
later error is still re-raised (the pipeline's ADR 048 D8 text degrade is the
final net). For TTS, mid-stream failover fires when a provider errors after
yielding fewer than ``_TTS_COMMIT_FRAMES`` audio frames (a configurable
threshold, default 3) — beyond that the audio is considered committed and the
error propagates. STT buffers the turn's audio once so it can be replayed to a
fallback provider. Hedging (D5) races to *completion* (it collects each
candidate's full output and takes the first to finish), trading first-byte
streaming for simplicity.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from typing import Any

from movate.voice.base import (
    AudioChunk,
    AudioCodec,
    RealtimeChunk,
    RealtimeVoiceProvider,
    SpeechToTextProvider,
    TextToSpeechProvider,
    TranscriptChunk,
)
from movate.voice.breaker import CircuitBreaker
from movate.voice.cache import VoiceCache, cache_key
from movate.voice.failures import (
    DEFAULT_RETRY,
    RetryRule,
    VoiceFailureType,
    VoiceProviderError,
    classify,
)
from movate.voice.manifest import VoiceManifest, manifest_for
from movate.voice.observer import NullObserver, VoiceObserver

# Fan-out for the latency-hedging path (ADR 068 D5). Two providers race;
# first to commit wins. Higher would multiply cost without much win.
_HEDGE_FANOUT = 2

# Number of TTS audio frames below which a mid-stream TTS failure triggers
# failover rather than propagation (batch 2, #211). A provider that has emitted
# >= this many frames is considered committed — its audio is already playing on
# the client and switching providers would produce a jarring splice.
_TTS_COMMIT_FRAMES = 3

# Injected sleep so tests drive backoff with no real delay (mirrors the
# injectable clock the pipeline + breaker use). Default is asyncio.sleep.
SleepFn = Callable[[float], Awaitable[None]]
# A cost estimator: projected $ for one turn on a given provider, or None when
# the provider's manifest carries no price (cost cannot bound it).
CostFn = Callable[[Any], float | None]
# A fresh source of the turn's input each time it's called (replay for failover).
AudioSource = Callable[[], AsyncIterator[AudioChunk]]
TextSource = Callable[[], AsyncIterator[str]]


async def _with_timeouts(
    aiter: AsyncIterator[Any], connect: float | None, step: float | None
) -> AsyncIterator[Any]:
    """Yield from ``aiter`` with separate first-chunk (``connect``) and
    subsequent-chunk (``step``) timeouts; either ``None`` disables that guard.

    A breached timeout raises ``TimeoutError`` → classified as ``TIMEOUT`` →
    retry/failover. The connect timeout catches a provider that never starts; the
    step timeout catches one that stalls mid-stream (slow drip)."""
    if connect is None and step is None:
        async for item in aiter:
            yield item
        return
    iterator = aiter.__aiter__()
    first = True
    while True:
        timeout = connect if (first and connect is not None) else step
        first = False
        try:
            if timeout is None:
                item = await iterator.__anext__()
            else:
                item = await asyncio.wait_for(iterator.__anext__(), timeout)
        except StopAsyncIteration:
            return
        yield item


def _audio_minutes(chunks: list[AudioChunk]) -> float:
    """Estimate audio duration (minutes) from raw PCM/mulaw frame sizes."""
    seconds = 0.0
    for ch in chunks:
        bytes_per_sample = 1 if ch.codec == "mulaw" else 2
        sample_rate = ch.sample_rate or 24_000
        seconds += (len(ch.data) / bytes_per_sample) / sample_rate
    return seconds / 60.0


class _FailoverBase:
    """Shared ordering / breaker / retry / hedge machinery for the composites."""

    version = "1"

    def __init__(
        self,
        providers: Sequence[Any],
        *,
        kind: str,
        retry: dict[VoiceFailureType, RetryRule] | None = None,
        observer: VoiceObserver | None = None,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 30.0,
        cost_budget: float | None = None,
        hedge: bool = False,
        call_timeout: float | None = None,
        connect_timeout: float | None = None,
        jitter: float = 0.0,
        max_audio_bytes: int = 16_000_000,
        clock: Callable[[], float] | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        if not providers:
            raise ValueError("FailoverProvider needs at least one provider")
        self._providers = list(providers)
        self._kind = kind
        self._retry = retry or DEFAULT_RETRY
        self._observer = observer or NullObserver()
        self._cost_budget = cost_budget
        self._hedge_enabled = hedge
        # A provider that produces no next chunk within call_timeout seconds is
        # treated as hung → TIMEOUT → retry/failover (a silent-but-connected
        # provider must not stall the turn). connect_timeout guards specifically
        # the FIRST chunk (a provider that never starts). None disables a guard.
        self._call_timeout = call_timeout
        self._connect_timeout = connect_timeout
        # Backoff jitter fraction (0..1): spreads retries to avoid a thundering
        # herd when many turns fail the same provider at once.
        self._jitter = max(0.0, min(1.0, jitter))
        # Cap the per-turn audio the STT composite buffers for replay so a long /
        # runaway stream can't exhaust memory.
        self._max_audio_bytes = max_audio_bytes
        self._sleep = sleep or asyncio.sleep
        self._breaker_kwargs: dict[str, Any] = {
            "threshold": breaker_threshold,
            "cooldown": breaker_cooldown,
        }
        if clock is not None:
            self._breaker_kwargs["clock"] = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def _breaker(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(**self._breaker_kwargs)
        return self._breakers[name]

    def _ordered_eligible(
        self, projected_cost: CostFn | None = None, language: str | None = None
    ) -> list[Any]:
        """Order providers: open breakers last, wrong-language next, over-budget,
        then latency tier.

        Stable, so providers sharing a rank keep the caller's order. A provider
        whose manifest declares languages that *exclude* the turn's language is
        pushed back (ADR 068 D2 — language is a routing dimension); an empty
        ``languages`` means "any", so unannotated providers are never penalized.
        Within-budget, lowest-latency-tier leads (ADR 068 D2/D4). An open breaker
        is pushed to the back, not dropped, so a call is still attempted.
        """

        lang = language.split("-")[0].lower() if language else None

        def sort_key(p: Any) -> tuple[int, int, int, int, float]:
            name = getattr(p, "name", "")
            manifest = manifest_for(name) or VoiceManifest(name, self._kind)  # type: ignore[arg-type]
            breaker_open = 0 if self._breaker(name).allow() else 1
            wrong_language = 0
            if lang is not None and manifest.languages:
                supported = {tag.split("-")[0].lower() for tag in manifest.languages}
                wrong_language = 0 if lang in supported else 1
            over_budget = 0
            cost = 0.0
            if projected_cost is not None and self._cost_budget is not None:
                projected = projected_cost(p)
                if projected is not None:
                    cost = projected
                    over_budget = 1 if projected > self._cost_budget else 0
            return (breaker_open, wrong_language, over_budget, manifest.latency_tier, cost)

        return sorted(self._providers, key=sort_key)

    async def _backoff(self, rule: RetryRule, attempt: int) -> None:
        if not rule.backoff:
            return
        idx = min(attempt - 2, len(rule.backoff) - 1)
        delay = rule.backoff[max(0, idx)]
        if self._jitter:
            delay = max(0.0, delay * (1.0 + self._jitter * (random.random() * 2.0 - 1.0)))
        await self._sleep(delay)

    def _note_provider_outcome(self, name: str, *, ok: bool) -> None:
        breaker = self._breaker(name)
        if ok:
            if breaker.record_success():
                self._observer.on_event("circuit_close", provider=name)
        elif breaker.record_failure():
            self._observer.on_event("circuit_open", provider=name)

    def _retry_max(self) -> int:
        # The retry budget is per failure type and not known until the first error
        # classifies it; use the widest configured budget as the loop cap.
        return max(r.max_attempts for r in self._retry.values())

    async def _hedge(
        self,
        providers: list[Any],
        make_coro: Callable[[Any], Coroutine[Any, Any, list[Any]]],
        kind: str,
    ) -> list[Any] | None:
        """Race ``providers``; return the first one's collected output, or None.

        The first candidate to *complete* wins (its output is returned and the
        rest are cancelled); a candidate that errors is recorded and the race
        continues. ``None`` means every candidate failed — the caller falls
        through to a sequential pass over the remaining providers.
        """
        tasks: dict[asyncio.Task[list[Any]], str] = {}
        for p in providers:
            name = getattr(p, "name", p.__class__.__name__)
            tasks[asyncio.create_task(make_coro(p))] = name
        self._observer.on_event("hedge", kind=kind, providers=list(tasks.values()))
        pending: set[asyncio.Task[list[Any]]] = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    name = tasks[task]
                    if task.exception() is None:
                        self._note_provider_outcome(name, ok=True)
                        self._observer.on_event("provider_selected", provider=name, kind=kind)
                        self._observer.on_event("hedge_won", provider=name, kind=kind)
                        return task.result()
                    self._note_provider_outcome(name, ok=False)
        finally:
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
        return None


class FailoverSTT(_FailoverBase):
    """A :class:`~movate.voice.base.SpeechToTextProvider` over an ordered tier list."""

    name = "failover_stt"

    def __init__(self, providers: Sequence[SpeechToTextProvider], **kwargs: Any) -> None:
        super().__init__(providers, kind="stt", **kwargs)

    @classmethod
    def default(cls, **kwargs: Any) -> FailoverSTT:
        """The ADR 068 D2 default STT chain: Deepgram (T1) → OpenAI Whisper (T2).

        Construction is SDK-free (adapters import their SDK lazily on first call);
        the relevant ``mdk-voice`` extras + provider keys must be present for the
        chain to actually serve. Pass an explicit provider list to customize.
        """
        from movate.voice.deepgram import DeepgramSTT  # noqa: PLC0415 - lazy
        from movate.voice.openai_speech import OpenAIWhisperSTT  # noqa: PLC0415 - lazy

        return cls([DeepgramSTT(), OpenAIWhisperSTT()], **kwargs)

    async def warm(self, api_key: str | None = None) -> bool:
        """Warm every provider in the chain that supports it (ADR 073 Phase 5)."""
        from movate.voice.stt_wrappers import warm_stt  # noqa: PLC0415 - avoid cycle

        warmed = False
        for provider in self._providers:
            warmed = await warm_stt(provider, api_key) or warmed
        return warmed

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
        keyterms: Sequence[str] | None = None,
        endpointing_ms: int | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        # Buffer the turn's audio once so it can be replayed to a fallback
        # provider (a consumed live stream cannot be re-fed), capped so a long /
        # runaway stream can't exhaust memory.
        buffered: list[AudioChunk] = []
        total_bytes = 0
        async for frame in audio:
            buffered.append(frame)
            total_bytes += len(frame.data)
            if total_bytes > self._max_audio_bytes:
                self._observer.on_event("audio_truncated", kind="stt", bytes=total_bytes)
                break
        minutes = _audio_minutes(buffered)

        async def replay() -> AsyncIterator[AudioChunk]:
            for chunk in buffered:
                yield chunk

        def cost(p: Any) -> float | None:
            manifest = manifest_for(getattr(p, "name", ""))
            if manifest is None or manifest.cost_per_min is None:
                return None
            return manifest.cost_per_min * minutes

        ordered = self._ordered_eligible(projected_cost=cost, language=language)

        if self._hedge_enabled and len(ordered) >= _HEDGE_FANOUT:
            winner = await self._hedge(
                ordered[:_HEDGE_FANOUT],
                lambda p: self._collect_stt(
                    p, replay(), language, api_key, keyterms, endpointing_ms
                ),
                "stt",
            )
            if winner is not None:
                for chunk in winner:
                    yield chunk
                return
            ordered = ordered[2:]
            if not ordered:
                self._observer.on_event("exhausted", kind="stt")
                raise VoiceProviderError(
                    "all hedged STT providers failed",
                    failure_type=VoiceFailureType.UNAVAILABLE,
                )

        async for chunk in self._sequential_stt(
            ordered, replay, language, api_key, keyterms, endpointing_ms
        ):
            yield chunk

    async def _collect_stt(
        self,
        provider: Any,
        src: AsyncIterator[AudioChunk],
        language: str | None,
        api_key: str | None,
        keyterms: Sequence[str] | None = None,
        endpointing_ms: int | None = None,
    ) -> list[TranscriptChunk]:
        chunks: list[TranscriptChunk] = []
        stream = _with_timeouts(
            provider.transcribe(
                src,
                language=language,
                api_key=api_key,
                keyterms=keyterms,
                endpointing_ms=endpointing_ms,
            ),
            self._connect_timeout,
            self._call_timeout,
        )
        async for chunk in stream:
            chunks.append(chunk)
            if chunk.is_final:
                return chunks
        raise VoiceProviderError(
            "STT produced no final transcript",
            failure_type=VoiceFailureType.UNAVAILABLE,
            provider=getattr(provider, "name", ""),
        )

    async def _sequential_stt(
        self,
        providers: list[Any],
        replay: AudioSource,
        language: str | None,
        api_key: str | None,
        keyterms: Sequence[str] | None = None,
        endpointing_ms: int | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        last_exc: Exception | None = None
        for provider in providers:
            name = getattr(provider, "name", provider.__class__.__name__)
            rule = self._retry[VoiceFailureType.UNAVAILABLE]
            for attempt in range(1, self._retry_max() + 1):
                committed = False
                try:
                    stream = _with_timeouts(
                        provider.transcribe(
                            replay(),
                            language=language,
                            api_key=api_key,
                            keyterms=keyterms,
                            endpointing_ms=endpointing_ms,
                        ),
                        self._connect_timeout,
                        self._call_timeout,
                    )
                    async for chunk in stream:
                        if chunk.is_final:
                            # Commit BEFORE yielding the final: record success and
                            # select the provider now, so a consumer that stops on
                            # the final still leaves the breaker/observer consistent.
                            committed = True
                            self._note_provider_outcome(name, ok=True)
                            self._observer.on_event("provider_selected", provider=name, kind="stt")
                            yield chunk
                            return
                        # Partial chunks are NOT yielded to the caller during
                        # failover-eligible attempts — they are buffered so a
                        # mid-stream failover is invisible (batch 2, #211).
                        # Partials from the *winning* provider are yielded inline.
                        yield chunk
                    raise VoiceProviderError(
                        "STT produced no final transcript",
                        failure_type=VoiceFailureType.UNAVAILABLE,
                        provider=name,
                    )
                except Exception as exc:
                    if committed:
                        raise
                    # Mid-stream failover (#211): the provider errored after
                    # emitting partials but before is_final — fail over
                    # transparently. The caller already received partials (they
                    # are non-binding), and the next provider replays the full
                    # audio from scratch so it produces its own partials + final.
                    last_exc = exc
                    ftype = classify(exc)
                    rule = self._retry[ftype]
                    self._observer.on_event(
                        "midstream_failover",
                        provider=name,
                        failure=ftype.value,
                        kind="stt",
                    )
                    if attempt < rule.max_attempts:
                        self._observer.on_event(
                            "retry", provider=name, failure=ftype.value, attempt=attempt
                        )
                        await self._backoff(rule, attempt + 1)
                        continue
                    break
            self._note_provider_outcome(name, ok=False)
            if not rule.failover:
                assert last_exc is not None  # set by the except that broke the loop
                raise last_exc  # terminal (e.g. auth) — do not fail over
            self._observer.on_event("failover", **{"from": name, "kind": "stt"})
        self._observer.on_event("exhausted", kind="stt")
        raise last_exc or VoiceProviderError(
            "no STT provider available", failure_type=VoiceFailureType.UNAVAILABLE
        )


class FailoverTTS(_FailoverBase):
    """A :class:`~movate.voice.base.TextToSpeechProvider` over an ordered tier list."""

    name = "failover_tts"

    def __init__(
        self,
        providers: Sequence[TextToSpeechProvider],
        *,
        cache: VoiceCache | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(providers, kind="tts", **kwargs)
        self._cache = cache

    @classmethod
    def default(cls, **kwargs: Any) -> FailoverTTS:
        """The ADR 068 D2 default TTS chain: Cartesia (T1) → OpenAI TTS (T2).

        Construction is SDK-free; the relevant extras + keys must be present for
        the chain to serve. Pass an explicit provider list to customize.
        """
        from movate.voice.cartesia import CartesiaTTS  # noqa: PLC0415 - lazy
        from movate.voice.openai_speech import OpenAITTS  # noqa: PLC0415 - lazy

        return cls([CartesiaTTS(), OpenAITTS()], **kwargs)

    async def synthesize(
        self,
        text: AsyncIterator[str],
        *,
        voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        # The answer text is small; buffer it once so it can be replayed to a
        # fallback provider and used as the cache key.
        parts: list[str] = [delta async for delta in text]
        full_text = "".join(parts)

        # ── Phrase cache (D6): serve a repeat phrase without re-synthesizing ──
        key = cache_key(full_text, voice_id, codec)
        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                self._observer.on_event("cache_hit", kind="tts")
                for chunk in hit:
                    yield chunk
                return

        async def replay() -> AsyncIterator[str]:
            for delta in parts:
                yield delta

        def cost(p: Any) -> float | None:
            manifest = manifest_for(getattr(p, "name", ""))
            if manifest is None or manifest.cost_per_char is None:
                return None
            return manifest.cost_per_char * len(full_text)

        ordered = self._ordered_eligible(projected_cost=cost)
        collected: list[AudioChunk] = []

        if self._hedge_enabled and len(ordered) >= _HEDGE_FANOUT:
            winner = await self._hedge(
                ordered[:_HEDGE_FANOUT],
                lambda p: self._collect_tts(p, replay(), voice_id, codec, api_key),
                "tts",
            )
            if winner is not None:
                for chunk in winner:
                    collected.append(chunk)
                    yield chunk
                if self._cache is not None and collected:
                    self._cache.put(key, collected)
                return
            ordered = ordered[2:]
            if not ordered:
                self._observer.on_event("exhausted", kind="tts")
                raise VoiceProviderError(
                    "all hedged TTS providers failed",
                    failure_type=VoiceFailureType.UNAVAILABLE,
                )

        async for chunk in self._sequential_tts(ordered, replay, voice_id, codec, api_key):
            collected.append(chunk)
            yield chunk
        if self._cache is not None and collected:
            self._cache.put(key, collected)

    async def _collect_tts(
        self,
        provider: Any,
        src: AsyncIterator[str],
        voice_id: str,
        codec: AudioCodec,
        api_key: str | None,
    ) -> list[AudioChunk]:
        chunks: list[AudioChunk] = []
        stream = _with_timeouts(
            provider.synthesize(src, voice_id=voice_id, codec=codec, api_key=api_key),
            self._connect_timeout,
            self._call_timeout,
        )
        async for chunk in stream:
            chunks.append(chunk)
        return chunks

    async def _sequential_tts(
        self,
        providers: list[Any],
        replay: TextSource,
        voice_id: str,
        codec: AudioCodec,
        api_key: str | None,
    ) -> AsyncIterator[AudioChunk]:
        last_exc: Exception | None = None
        for provider in providers:
            name = getattr(provider, "name", provider.__class__.__name__)
            rule = self._retry[VoiceFailureType.UNAVAILABLE]
            for attempt in range(1, self._retry_max() + 1):
                frames_produced = 0
                try:
                    stream = _with_timeouts(
                        provider.synthesize(
                            replay(), voice_id=voice_id, codec=codec, api_key=api_key
                        ),
                        self._connect_timeout,
                        self._call_timeout,
                    )
                    async for chunk in stream:
                        frames_produced += 1
                        yield chunk
                    self._note_provider_outcome(name, ok=True)
                    self._observer.on_event("provider_selected", provider=name, kind="tts")
                    return
                except Exception as exc:
                    # Mid-stream failover (#211): if the provider produced fewer
                    # than _TTS_COMMIT_FRAMES audio frames, the client hasn't
                    # started meaningful playback yet — fail over transparently.
                    # Beyond that threshold the audio is committed and we must
                    # propagate to avoid a jarring splice.
                    if frames_produced >= _TTS_COMMIT_FRAMES:
                        raise  # audio committed — cannot cleanly fail over
                    last_exc = exc
                    ftype = classify(exc)
                    rule = self._retry[ftype]
                    if frames_produced > 0:
                        self._observer.on_event(
                            "midstream_failover",
                            provider=name,
                            failure=ftype.value,
                            kind="tts",
                            frames_produced=frames_produced,
                        )
                    if attempt < rule.max_attempts:
                        self._observer.on_event(
                            "retry", provider=name, failure=ftype.value, attempt=attempt
                        )
                        await self._backoff(rule, attempt + 1)
                        continue
                    break
            self._note_provider_outcome(name, ok=False)
            if not rule.failover:
                assert last_exc is not None
                raise last_exc
            self._observer.on_event("failover", **{"from": name, "kind": "tts"})
        self._observer.on_event("exhausted", kind="tts")
        raise last_exc or VoiceProviderError(
            "no TTS provider available", failure_type=VoiceFailureType.UNAVAILABLE
        )


class FailoverRealtime(_FailoverBase):
    """A :class:`~movate.voice.base.RealtimeVoiceProvider` over an ordered tier list.

    Realtime is full-duplex over a **live** session, so failover here is
    **open-time only**: if a provider raises, or emits an ``error`` chunk
    *before* any usable output, the router moves to the next provider. Once the
    session has produced output (audio / transcript / a turn-boundary signal) it
    is *committed* — a later error is forwarded, not failed over, because a live
    mic stream cannot be rewound to replay to another provider. Frames consumed
    before an open-time failover are not replayed (documented limitation; most
    open failures — provider down, bad key — consume nothing). Cost/hedging do
    not apply to the realtime seam in this MVP.
    """

    name = "failover_realtime"

    def __init__(self, providers: Sequence[RealtimeVoiceProvider], **kwargs: Any) -> None:
        super().__init__(providers, kind="realtime", **kwargs)

    async def session(
        self,
        audio_in: AsyncIterator[AudioChunk],
        *,
        voice_id: str = "",
        instructions: str = "",
        language: str | None = None,
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[RealtimeChunk]:
        last_exc: Exception | None = None
        for provider in self._ordered_eligible():
            name = getattr(provider, "name", provider.__class__.__name__)
            committed = False
            try:
                async for chunk in provider.session(
                    audio_in,
                    voice_id=voice_id,
                    instructions=instructions,
                    language=language,
                    codec=codec,
                    api_key=api_key,
                ):
                    if chunk.kind == "error" and not committed:
                        # Session failed before producing usable output → failover.
                        raise VoiceProviderError(
                            chunk.message or "realtime session error",
                            failure_type=VoiceFailureType.UNAVAILABLE,
                            provider=name,
                        )
                    if not committed:
                        committed = True
                        self._note_provider_outcome(name, ok=True)
                        self._observer.on_event("provider_selected", provider=name, kind="realtime")
                    yield chunk
                if committed:
                    return
                raise VoiceProviderError(
                    "realtime session produced no output",
                    failure_type=VoiceFailureType.UNAVAILABLE,
                    provider=name,
                )
            except Exception as exc:
                if committed:
                    raise  # session already producing — cannot rewind a live stream
                last_exc = exc
                self._note_provider_outcome(name, ok=False)
                if not self._retry[classify(exc)].failover:
                    raise  # terminal (e.g. auth)
                self._observer.on_event("failover", **{"from": name, "kind": "realtime"})
        self._observer.on_event("exhausted", kind="realtime")
        raise last_exc or VoiceProviderError(
            "no realtime provider available", failure_type=VoiceFailureType.UNAVAILABLE
        )
