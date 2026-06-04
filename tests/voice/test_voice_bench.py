"""Tests for the voice eval/regression harness (ADR 049 D5).

Validates the bench machinery end-to-end using fake providers and the golden
audio corpus — no real STT/TTS keys needed. The ``voice_bench`` marker lets CI
include or exclude these tests independently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.voice.base import AudioChunk
from movate.voice.bench import (
    BenchReport,
    TTSBenchItem,
    TTSBenchReport,
    bench_stt,
    bench_tts,
    compare_to_baseline,
    load_audio_chunks,
    load_corpus,
    save_baseline,
    word_error_rate,
)
from movate.voice.doubles import FakeSTT, FakeTTS

CORPUS_DIR = Path(__file__).parent / "corpus"


# ---------------------------------------------------------------------------
# WER unit tests
# ---------------------------------------------------------------------------


class TestWordErrorRate:
    def test_perfect(self) -> None:
        assert word_error_rate("hello world", "hello world") == 0.0

    def test_case_insensitive(self) -> None:
        assert word_error_rate("Hello World", "hello world") == 0.0

    def test_punctuation_ignored(self) -> None:
        assert word_error_rate("hello, world!", "hello world") == 0.0

    def test_substitution(self) -> None:
        wer = word_error_rate("hello world", "hello earth")
        assert wer == pytest.approx(0.5)

    def test_insertion(self) -> None:
        wer = word_error_rate("hello", "hello world")
        assert wer == pytest.approx(1.0)

    def test_deletion(self) -> None:
        wer = word_error_rate("hello world", "hello")
        assert wer == pytest.approx(0.5)

    def test_empty_reference(self) -> None:
        assert word_error_rate("", "") == 0.0
        assert word_error_rate("", "hello") == 1.0

    def test_complete_miss(self) -> None:
        wer = word_error_rate("alpha beta gamma", "one two three")
        assert wer == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


class TestCorpusLoading:
    def test_load_corpus_from_manifest(self) -> None:
        items = load_corpus(CORPUS_DIR)
        assert len(items) >= 4
        assert items[0].id == "greeting-en"
        assert items[0].expected_transcript == "hello how are you today"
        assert items[0].language == "en-US"
        assert items[0].duration_s > 0

    def test_load_audio_chunks(self) -> None:
        chunks = load_audio_chunks(CORPUS_DIR, "greeting-en.wav")
        assert len(chunks) > 0
        assert all(isinstance(c, AudioChunk) for c in chunks)
        assert all(c.codec == "pcm16" for c in chunks)
        assert all(c.sample_rate == 16_000 for c in chunks)


# ---------------------------------------------------------------------------
# STT bench with fakes
# ---------------------------------------------------------------------------


@pytest.mark.voice_bench
class TestSTTBench:
    async def test_bench_stt_basic(self) -> None:
        """The harness runs, computes WER, and reports latency."""
        stt = FakeSTT(transcript="hello how are you today")
        items = load_corpus(CORPUS_DIR)
        corpus = [(load_audio_chunks(CORPUS_DIR, items[0].filename), items[0].expected_transcript)]

        # Use a deterministic clock: starts at 0, advances 10ms per call
        tick = _make_tick(step_s=0.01)
        report = await bench_stt(stt, corpus, clock=tick)

        assert report.provider == "fake_stt"
        assert len(report.items) == 1
        assert report.items[0].wer == 0.0  # FakeSTT returns the expected transcript
        assert report.items[0].latency_ms > 0

    async def test_bench_stt_wer_nonzero(self) -> None:
        """When the provider mis-transcribes, WER is > 0."""
        stt = FakeSTT(transcript="goodbye cruel world")
        items = load_corpus(CORPUS_DIR)
        corpus = [(load_audio_chunks(CORPUS_DIR, items[0].filename), items[0].expected_transcript)]

        report = await bench_stt(stt, corpus)
        assert report.items[0].wer > 0.0

    async def test_bench_stt_full_corpus(self) -> None:
        """Run the full corpus through a fake provider."""
        stt = FakeSTT(transcript="hello")
        items = load_corpus(CORPUS_DIR)
        corpus = [
            (load_audio_chunks(CORPUS_DIR, it.filename), it.expected_transcript) for it in items
        ]

        report = await bench_stt(stt, corpus)
        assert len(report.items) == len(items)
        assert report.mean_wer >= 0.0
        assert report.p50_latency_ms >= 0.0


# ---------------------------------------------------------------------------
# TTS bench with fakes
# ---------------------------------------------------------------------------


@pytest.mark.voice_bench
class TestTTSBench:
    async def test_bench_tts_basic(self) -> None:
        """The TTS harness runs and reports latencies."""
        tts = FakeTTS()
        phrases = ["hello how are you today", "one two three"]

        tick = _make_tick(step_s=0.005)
        report = await bench_tts(tts, phrases, clock=tick)

        assert report.provider == "fake_tts"
        assert len(report.items) == 2
        for item in report.items:
            assert item.first_byte_ms > 0
            assert item.total_ms >= item.first_byte_ms
            assert item.audio_bytes > 0

    async def test_bench_tts_multi_frame(self) -> None:
        """Multi-frame TTS still records correct total bytes."""
        tts = FakeTTS(frames=3)
        report = await bench_tts(tts, ["hello world"])
        assert report.items[0].audio_bytes == len(b"hello world")


# ---------------------------------------------------------------------------
# BenchReport serialization
# ---------------------------------------------------------------------------


class TestBenchReport:
    def test_to_dict_round_trip(self) -> None:
        report = BenchReport(
            stt_reports=[],
            tts_reports=[
                TTSBenchReport(
                    provider="fake_tts",
                    items=[
                        TTSBenchItem(
                            phrase="hi",
                            first_byte_ms=5.0,
                            total_ms=10.0,
                            audio_bytes=2,
                        )
                    ],
                )
            ],
        )
        d = report.to_dict()
        assert d["tts"][0]["provider"] == "fake_tts"
        assert d["tts"][0]["mean_first_byte_ms"] == 5.0

    def test_to_json(self) -> None:
        report = BenchReport()
        j = report.to_json()
        assert '"stt"' in j
        assert '"tts"' in j


# ---------------------------------------------------------------------------
# Baseline / regression
# ---------------------------------------------------------------------------


@pytest.mark.voice_bench
class TestRegression:
    def test_no_regression_same_results(self) -> None:
        report = _make_sample_report(
            wer=0.05,
            stt_latency=100.0,
            tts_fb=50.0,
            tts_total=80.0,
        )
        baseline = report.to_dict()
        regressions = compare_to_baseline(report, baseline)
        assert regressions == []

    def test_wer_regression_detected(self) -> None:
        baseline_report = _make_sample_report(
            wer=0.05,
            stt_latency=100.0,
            tts_fb=50.0,
            tts_total=80.0,
        )
        baseline = baseline_report.to_dict()
        current = _make_sample_report(
            wer=0.15,
            stt_latency=100.0,
            tts_fb=50.0,
            tts_total=80.0,
        )
        regressions = compare_to_baseline(current, baseline)
        assert len(regressions) >= 1
        assert any(r.metric == "wer" for r in regressions)

    def test_latency_regression_detected(self) -> None:
        baseline_report = _make_sample_report(
            wer=0.05,
            stt_latency=100.0,
            tts_fb=50.0,
            tts_total=80.0,
        )
        baseline = baseline_report.to_dict()
        current = _make_sample_report(
            wer=0.05,
            stt_latency=200.0,
            tts_fb=50.0,
            tts_total=80.0,
        )
        regressions = compare_to_baseline(current, baseline)
        assert any(r.metric == "stt_latency" for r in regressions)

    def test_tts_regression_detected(self) -> None:
        baseline_report = _make_sample_report(
            wer=0.05,
            stt_latency=100.0,
            tts_fb=50.0,
            tts_total=80.0,
        )
        baseline = baseline_report.to_dict()
        current = _make_sample_report(
            wer=0.05,
            stt_latency=100.0,
            tts_fb=100.0,
            tts_total=160.0,
        )
        regressions = compare_to_baseline(current, baseline)
        assert any(r.metric == "tts_first_byte" for r in regressions)
        assert any(r.metric == "tts_total" for r in regressions)

    def test_save_and_load_baseline(self, tmp_path: Path) -> None:
        from movate.voice.bench import load_baseline  # noqa: PLC0415

        report = _make_sample_report(wer=0.05, stt_latency=100.0, tts_fb=50.0, tts_total=80.0)
        bl_path = tmp_path / "baseline.json"
        save_baseline(report, bl_path)
        loaded = load_baseline(bl_path)
        assert loaded["stt"][0]["provider"] == "fake_stt"
        assert loaded["tts"][0]["provider"] == "fake_tts"

    def test_unknown_provider_skipped(self) -> None:
        """Providers not in the baseline are silently skipped (not a regression)."""
        baseline: dict[str, list[dict[str, object]]] = {"stt": [], "tts": []}
        report = _make_sample_report(wer=0.5, stt_latency=999.0, tts_fb=999.0, tts_total=999.0)
        regressions = compare_to_baseline(report, baseline)
        assert regressions == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tick(step_s: float = 0.01) -> object:
    """Return a callable clock that advances by ``step_s`` per call."""
    state = [0.0]

    def tick() -> float:
        val = state[0]
        state[0] += step_s
        return val

    return tick


def _make_sample_report(
    *,
    wer: float,
    stt_latency: float,
    tts_fb: float,
    tts_total: float,
) -> BenchReport:
    from movate.voice.bench import STTBenchItem, STTBenchReport  # noqa: PLC0415

    return BenchReport(
        stt_reports=[
            STTBenchReport(
                provider="fake_stt",
                items=[
                    STTBenchItem(
                        reference="hello",
                        hypothesis="hello" if wer == 0.0 else "goodbye",
                        wer=wer,
                        latency_ms=stt_latency,
                    )
                ],
            )
        ],
        tts_reports=[
            TTSBenchReport(
                provider="fake_tts",
                items=[
                    TTSBenchItem(
                        phrase="hello",
                        first_byte_ms=tts_fb,
                        total_ms=tts_total,
                        audio_bytes=5,
                    )
                ],
            )
        ],
    )
