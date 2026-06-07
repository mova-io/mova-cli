"""A reproducible STT + TTS bench — turn provider quality into numbers (ADR 049 D5).

Provider choice should be *measured*, not assumed. This is the standalone core of
``mdk voice bench``: run a corpus of ``(audio, reference-transcript)`` pairs
through any :class:`~movate.voice.base.SpeechToTextProvider` and report **word error
rate** and **latency** — apples-to-apples, on *your* audio. For TTS, measure
**first-byte latency** and **total latency** through any
:class:`~movate.voice.base.TextToSpeechProvider`.

The full bench produces a :class:`BenchReport` with per-provider STT and TTS
results. :func:`compare_to_baseline` detects regressions against a saved
baseline (WER increase >5%, latency increase >20%).

Dependency-free and clock-injectable so it's deterministic in tests; feed it real
adapters + real audio to get a real verdict.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from movate.voice.base import AudioChunk, SpeechToTextProvider, TextToSpeechProvider

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


@dataclass(frozen=True)
class TTSBenchItem:
    """One TTS probe's result."""

    phrase: str
    first_byte_ms: float
    total_ms: float
    audio_bytes: int


@dataclass(frozen=True)
class TTSBenchReport:
    """Aggregate TTS verdict over probe phrases."""

    provider: str
    items: list[TTSBenchItem]

    @property
    def mean_first_byte_ms(self) -> float:
        return sum(i.first_byte_ms for i in self.items) / len(self.items) if self.items else 0.0

    @property
    def mean_total_ms(self) -> float:
        return sum(i.total_ms for i in self.items) / len(self.items) if self.items else 0.0

    def format(self) -> str:
        return (
            f"{self.provider}: first-byte {round(self.mean_first_byte_ms)}ms · "
            f"total {round(self.mean_total_ms)}ms · n={len(self.items)}"
        )


@dataclass
class BenchReport:
    """Full bench report across all providers (STT + TTS)."""

    stt_reports: list[STTBenchReport] = field(default_factory=list)
    tts_reports: list[TTSBenchReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict for ``--output json`` / baseline."""
        return {
            "stt": [
                {
                    "provider": r.provider,
                    "mean_wer": round(r.mean_wer, 4),
                    "p50_latency_ms": round(r.p50_latency_ms, 2),
                    "p95_latency_ms": round(r.p95_latency_ms, 2),
                    "items": [asdict(i) for i in r.items],
                }
                for r in self.stt_reports
            ],
            "tts": [
                {
                    "provider": r.provider,
                    "mean_first_byte_ms": round(r.mean_first_byte_ms, 2),
                    "mean_total_ms": round(r.mean_total_ms, 2),
                    "items": [asdict(i) for i in r.items],
                }
                for r in self.tts_reports
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Regression / baseline comparison
# ---------------------------------------------------------------------------

_DEFAULT_WER_THRESHOLD = 0.05  # 5% absolute WER increase
_DEFAULT_LATENCY_THRESHOLD = 0.20  # 20% relative latency increase


@dataclass(frozen=True)
class RegressionResult:
    """One detected regression."""

    provider: str
    metric: str
    baseline_value: float
    current_value: float
    threshold: float
    message: str


def compare_to_baseline(
    report: BenchReport,
    baseline: dict[str, Any],
    *,
    wer_threshold: float = _DEFAULT_WER_THRESHOLD,
    latency_threshold: float = _DEFAULT_LATENCY_THRESHOLD,
) -> list[RegressionResult]:
    """Compare a bench report against a saved baseline. Return regressions.

    A regression is detected when:
    - STT WER increases by more than ``wer_threshold`` (absolute, default 5%).
    - STT p50 latency increases by more than ``latency_threshold`` (relative, default 20%).
    - TTS first-byte latency increases by more than ``latency_threshold`` (relative).
    - TTS total latency increases by more than ``latency_threshold`` (relative).
    """
    regressions: list[RegressionResult] = []
    baseline_stt = {s["provider"]: s for s in baseline.get("stt", [])}
    baseline_tts = {t["provider"]: t for t in baseline.get("tts", [])}

    for stt_report in report.stt_reports:
        bl = baseline_stt.get(stt_report.provider)
        if bl is None:
            continue
        # WER regression (absolute increase)
        bl_wer = bl.get("mean_wer", 0.0)
        if stt_report.mean_wer - bl_wer > wer_threshold:
            regressions.append(
                RegressionResult(
                    provider=stt_report.provider,
                    metric="wer",
                    baseline_value=bl_wer,
                    current_value=round(stt_report.mean_wer, 4),
                    threshold=wer_threshold,
                    message=(
                        f"STT WER regressed: {bl_wer:.1%} -> {stt_report.mean_wer:.1%} "
                        f"(>{wer_threshold:.0%} threshold)"
                    ),
                )
            )
        # Latency regression (relative increase)
        bl_lat = bl.get("p50_latency_ms", 0.0)
        if bl_lat > 0 and (stt_report.p50_latency_ms - bl_lat) / bl_lat > latency_threshold:
            regressions.append(
                RegressionResult(
                    provider=stt_report.provider,
                    metric="stt_latency",
                    baseline_value=bl_lat,
                    current_value=round(stt_report.p50_latency_ms, 2),
                    threshold=latency_threshold,
                    message=(
                        f"STT p50 latency regressed: {bl_lat:.0f}ms -> "
                        f"{stt_report.p50_latency_ms:.0f}ms "
                        f"(>{latency_threshold:.0%} threshold)"
                    ),
                )
            )

    for tts_report in report.tts_reports:
        bl = baseline_tts.get(tts_report.provider)
        if bl is None:
            continue
        bl_fb = bl.get("mean_first_byte_ms", 0.0)
        if bl_fb > 0 and (tts_report.mean_first_byte_ms - bl_fb) / bl_fb > latency_threshold:
            regressions.append(
                RegressionResult(
                    provider=tts_report.provider,
                    metric="tts_first_byte",
                    baseline_value=bl_fb,
                    current_value=round(tts_report.mean_first_byte_ms, 2),
                    threshold=latency_threshold,
                    message=(
                        f"TTS first-byte latency regressed: {bl_fb:.0f}ms -> "
                        f"{tts_report.mean_first_byte_ms:.0f}ms "
                        f"(>{latency_threshold:.0%} threshold)"
                    ),
                )
            )
        bl_total = bl.get("mean_total_ms", 0.0)
        if bl_total > 0 and (tts_report.mean_total_ms - bl_total) / bl_total > latency_threshold:
            regressions.append(
                RegressionResult(
                    provider=tts_report.provider,
                    metric="tts_total",
                    baseline_value=bl_total,
                    current_value=round(tts_report.mean_total_ms, 2),
                    threshold=latency_threshold,
                    message=(
                        f"TTS total latency regressed: {bl_total:.0f}ms -> "
                        f"{tts_report.mean_total_ms:.0f}ms "
                        f"(>{latency_threshold:.0%} threshold)"
                    ),
                )
            )

    return regressions


def save_baseline(report: BenchReport, path: Path) -> None:
    """Write a bench report as a baseline JSON file."""
    path.write_text(report.to_json() + "\n", encoding="utf-8")


def load_baseline(path: Path) -> dict[str, Any]:
    """Load a baseline JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusItem:
    """One item from the golden audio corpus manifest."""

    id: str
    filename: str
    expected_transcript: str
    language: str
    duration_s: float
    speaker_type: str
    description: str = ""


def load_corpus(corpus_dir: Path) -> list[CorpusItem]:
    """Load corpus items from a manifest.json in the given directory."""
    manifest_path = corpus_dir / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        CorpusItem(
            id=item["id"],
            filename=item["filename"],
            expected_transcript=item["expected_transcript"],
            language=item.get("language", "en-US"),
            duration_s=item.get("duration_s", 0.0),
            speaker_type=item.get("speaker_type", "unknown"),
            description=item.get("description", ""),
        )
        for item in data["items"]
    ]


def load_audio_chunks(corpus_dir: Path, filename: str) -> list[AudioChunk]:
    """Load a WAV file as a list of AudioChunk (raw PCM16 data, skipping header)."""
    wav_path = corpus_dir / filename
    raw = wav_path.read_bytes()
    # Skip standard PCM WAV header (44 bytes) to get raw sample data.
    wav_header_size = 44
    pcm_data = raw[wav_header_size:] if len(raw) > wav_header_size else raw
    # Split into ~100ms chunks at 16kHz 16-bit mono = 3200 bytes per chunk
    chunk_size = 3200
    chunks: list[AudioChunk] = []
    for i in range(0, len(pcm_data), chunk_size):
        chunks.append(
            AudioChunk(data=pcm_data[i : i + chunk_size], codec="pcm16", sample_rate=16_000)
        )
    return chunks


# ---------------------------------------------------------------------------
# Bench runners
# ---------------------------------------------------------------------------


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


async def bench_tts(
    provider: TextToSpeechProvider,
    phrases: Sequence[str],
    *,
    voice_id: str = "",
    api_key: str | None = None,
    clock: Callable[[], float] | None = None,
) -> TTSBenchReport:
    """Run ``phrases`` through a TTS ``provider``, measuring latency.

    For each phrase, measures first-byte latency (time to first audio chunk) and
    total latency (time to last audio chunk). ``clock`` is an injectable
    monotonic-seconds source for deterministic tests.
    """
    now = clock if clock is not None else time.monotonic
    items: list[TTSBenchItem] = []
    for phrase in phrases:

        async def _text_src(text: str = phrase) -> AsyncIterator[str]:
            yield text

        start = now()
        first_byte_ms: float | None = None
        total_bytes = 0
        async for audio_chunk in provider.synthesize(
            _text_src(), voice_id=voice_id, api_key=api_key
        ):
            if first_byte_ms is None:
                first_byte_ms = (now() - start) * 1000.0
            total_bytes += len(audio_chunk.data)
        total_ms = (now() - start) * 1000.0
        items.append(
            TTSBenchItem(
                phrase=phrase,
                first_byte_ms=first_byte_ms if first_byte_ms is not None else total_ms,
                total_ms=total_ms,
                audio_bytes=total_bytes,
            )
        )
    return TTSBenchReport(provider=getattr(provider, "name", "tts"), items=items)
