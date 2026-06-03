"""A reproducible STT bench — turn provider quality into numbers (ADR 049 D5).

Provider choice should be *measured*, not assumed. This is the standalone core of
``mdk voice bench``: run a corpus of ``(audio, reference-transcript)`` pairs
through any :class:`~movate.voice.base.SpeechToTextProvider` and report **word error
rate** and **latency** — apples-to-apples, on *your* audio. (TTS naturalness/MOS
needs human or model judging and a real corpus, so it stays out of this pure
core; latency/$ for TTS read off the manifest + the observer.)

Dependency-free and clock-injectable so it's deterministic in tests; feed it real
adapters + real audio to get a real verdict.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from movate.voice.base import AudioChunk, SpeechToTextProvider

# Strip everything that isn't a letter / digit / apostrophe / whitespace before
# tokenizing — so "hello." vs "Hello" doesn't count as a substitution. (TTS-then-
# STT round-trips routinely add punctuation/capitalization; that's stylistic, not
# a word error.)
_NON_WORD = re.compile(r"[^\w'\s]+")


def _normalize(text: str) -> list[str]:
    return _NON_WORD.sub(" ", text.lower()).split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word error rate (Levenshtein over whitespace tokens).

    Normalizes case + punctuation before comparison — only *real* word-level
    edits (substitutions / insertions / deletions) count, so a trailing period
    or capitalization isn't scored as an error. ``0.0`` is perfect; ``1.0``
    means the reference is empty but the hypothesis is not.
    """
    ref = _normalize(reference)
    hyp = _normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    dist = list(range(len(hyp) + 1))
    for i, r_word in enumerate(ref, 1):
        prev = dist[0]
        dist[0] = i
        for j, h_word in enumerate(hyp, 1):
            cur = dist[j]
            dist[j] = min(dist[j] + 1, dist[j - 1] + 1, prev + (0 if r_word == h_word else 1))
            prev = cur
    return dist[len(hyp)] / len(ref)


@dataclass(frozen=True)
class STTBenchItem:
    """One corpus item's result."""

    reference: str
    hypothesis: str
    wer: float
    latency_ms: float


@dataclass(frozen=True)
class STTBenchReport:
    """Aggregate verdict over a corpus run."""

    provider: str
    items: list[STTBenchItem]

    @property
    def mean_wer(self) -> float:
        return sum(i.wer for i in self.items) / len(self.items) if self.items else 0.0

    @property
    def p50_latency_ms(self) -> float:
        return _percentile([i.latency_ms for i in self.items], 50)

    @property
    def p95_latency_ms(self) -> float:
        return _percentile([i.latency_ms for i in self.items], 95)

    def format(self) -> str:
        return (
            f"{self.provider}: WER {self.mean_wer:.1%} · "
            f"p50 {round(self.p50_latency_ms)}ms · p95 {round(self.p95_latency_ms)}ms · "
            f"n={len(self.items)}"
        )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def bench_stt(
    provider: SpeechToTextProvider,
    corpus: Sequence[tuple[Sequence[AudioChunk], str]],
    *,
    language: str | None = None,
    api_key: str | None = None,
    clock: Callable[[], float] | None = None,
) -> STTBenchReport:
    """Run ``corpus`` (``(audio_chunks, reference)`` pairs) through ``provider``.

    Records the final transcript's WER vs. the reference and the latency to that
    final. ``clock`` is an injectable monotonic-seconds source for deterministic
    tests (defaults to :func:`time.monotonic`).
    """
    now = clock if clock is not None else time.monotonic
    items: list[STTBenchItem] = []
    for audio_chunks, reference in corpus:

        async def _src(chunks: Sequence[AudioChunk] = audio_chunks) -> AsyncIterator[AudioChunk]:
            for chunk in chunks:
                yield chunk

        start = now()
        hypothesis = ""
        latency_ms: float | None = None
        async for chunk in provider.transcribe(_src(), language=language, api_key=api_key):
            if chunk.is_final:
                hypothesis = chunk.text
                if latency_ms is None:
                    latency_ms = (now() - start) * 1000.0
        items.append(
            STTBenchItem(
                reference=reference,
                hypothesis=hypothesis,
                wer=word_error_rate(reference, hypothesis),
                latency_ms=latency_ms if latency_ms is not None else (now() - start) * 1000.0,
            )
        )
    return STTBenchReport(provider=getattr(provider, "name", "stt"), items=items)
