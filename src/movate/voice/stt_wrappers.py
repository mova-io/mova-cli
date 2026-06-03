"""Composable STT wrappers for cost + quality — both *are* SpeechToTextProviders.

Like the failover composites, these wrap an STT behind the same Protocol so they
drop into the pipeline (or inside a ``FailoverSTT`` chain) transparently:

* :class:`SilenceGatedSTT` — drop near-silent frames before they reach the
  provider (don't pay to transcribe dead air);
* :class:`ConfidenceGatedSTT` — transcribe with a *cheap* provider first and only
  escalate to a *premium* one when the result's confidence is low (pay for
  quality only when the cheap result is uncertain — the data is already in
  :class:`~movate.voice.base.TranscriptChunk.confidence`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from movate.voice.base import AudioChunk, SpeechToTextProvider, TranscriptChunk
from movate.voice.observer import NullObserver, VoiceObserver
from movate.voice.vad import DEFAULT_SILENCE_RMS, is_silent


def _engine_name(provider: Any) -> str:
    """The real serving engine — follows ``effective_provider`` through nested
    wrappers, falling back to ``name`` (so ops see the engine, not the wrapper)."""
    return getattr(provider, "effective_provider", "") or getattr(provider, "name", "stt")


class SilenceGatedSTT:
    """Drops silent frames before delegating to ``inner`` (cost; ADR 048 D8 edge).

    Keeps a short *hangover* of silence after speech so endpointing/word-final
    audio isn't clipped, then suppresses the long silent runs that would
    otherwise be billed. Coarse but free.
    """

    name = "silence_gated_stt"
    version = "1"

    def __init__(
        self,
        inner: SpeechToTextProvider,
        *,
        threshold: float = DEFAULT_SILENCE_RMS,
        hangover_frames: int = 3,
        observer: VoiceObserver | None = None,
    ) -> None:
        self._inner = inner
        self._threshold = threshold
        self._hangover = max(0, hangover_frames)
        self._observer = observer or NullObserver()

    def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        async def _gated() -> AsyncIterator[AudioChunk]:
            silence_run = 0
            dropped = 0
            kept = 0
            async for chunk in audio:
                if is_silent(chunk, self._threshold):
                    silence_run += 1
                    if silence_run <= self._hangover:
                        kept += 1
                        yield chunk  # brief hangover, then suppress
                    else:
                        dropped += 1
                else:
                    silence_run = 0
                    kept += 1
                    yield chunk
            # Quantify the savings so ops can see how much audio the gate trimmed.
            self._observer.on_event("audio_gated", dropped=dropped, kept=kept)

        return self._inner.transcribe(_gated(), language=language, api_key=api_key)


class ConfidenceGatedSTT:
    """Cheap-first STT that escalates to a premium provider only on low confidence.

    Runs ``primary`` (the cheap provider); if its final transcript's
    ``confidence`` is below ``min_confidence``, re-runs ``escalation`` on the same
    audio and uses *its* result instead. When the primary reports no confidence
    (``None``), it is trusted (no escalation) — escalation costs money, so we only
    spend it on a real low-confidence signal.
    """

    name = "confidence_gated_stt"
    version = "1"
    effective_provider: str = ""  # the engine that actually served the last turn

    def __init__(
        self,
        primary: SpeechToTextProvider,
        escalation: SpeechToTextProvider,
        *,
        min_confidence: float = 0.6,
        observer: VoiceObserver | None = None,
    ) -> None:
        self._primary = primary
        self._escalation = escalation
        self._min_confidence = min_confidence
        self._observer = observer or NullObserver()

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        # Buffer so the same audio can be replayed to the escalation provider.
        buffered: list[AudioChunk] = [chunk async for chunk in audio]

        async def _replay() -> AsyncIterator[AudioChunk]:
            for chunk in buffered:
                yield chunk

        primary_final: TranscriptChunk | None = None
        async for chunk in self._primary.transcribe(_replay(), language=language, api_key=api_key):
            if chunk.is_final:
                primary_final = chunk
            else:
                yield chunk  # partials pass through

        needs_escalation = primary_final is None or (
            primary_final.confidence is not None and primary_final.confidence < self._min_confidence
        )
        if needs_escalation:
            self._observer.on_event(
                "stt_escalated",
                confidence=None if primary_final is None else primary_final.confidence,
            )
            escalated_final: TranscriptChunk | None = None
            async for chunk in self._escalation.transcribe(
                _replay(), language=language, api_key=api_key
            ):
                if chunk.is_final:
                    escalated_final = chunk
                else:
                    yield chunk
            if escalated_final is not None:
                self.effective_provider = _engine_name(self._escalation)
                self._observer.on_event(
                    "stt_engine", provider=self.effective_provider, escalated=True
                )
                yield escalated_final
                return

        if primary_final is not None:
            self.effective_provider = _engine_name(self._primary)
            self._observer.on_event("stt_engine", provider=self.effective_provider, escalated=False)
            yield primary_final
