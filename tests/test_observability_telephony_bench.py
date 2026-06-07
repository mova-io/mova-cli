"""MetricsObserver, telephony codecs, and the STT bench harness."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    FailoverSTT,
    FakeSTT,
    MetricsObserver,
    STTBenchItem,
    STTBenchReport,
    TranscriptChunk,
    bench_stt,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_pcm16,
    word_error_rate,
)


async def _no_sleep(_s: float) -> None:
    return None


# --- MetricsObserver -------------------------------------------------------


class _FailingSTT:
    name = "bad"
    version = "0"

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None, endpointing_ms=None
    ) -> AsyncIterator[TranscriptChunk]:
        async for _ in audio:
            pass
        raise RuntimeError("down")
        yield  # pragma: no cover


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"x")


async def test_metrics_observer_counts_router_events() -> None:
    metrics = MetricsObserver()
    fo = FailoverSTT([_FailingSTT(), FakeSTT("ok")], observer=metrics, sleep=_no_sleep)
    async for _ in fo.transcribe(_audio()):
        pass
    snap = metrics.snapshot()
    assert snap["provider_selected"] == {"fake_stt": 1}
    # The failover retry logic (#694) retries the failing provider before
    # giving up — each retry emits a midstream_failover event, then the
    # final give-up emits a failover event.  MetricsObserver counts both
    # event types in the failovers bucket, so the total is 3 (2 retries + 1).
    assert snap["failovers"]["bad"] == 3
    assert snap["events"]["failover"] == 1
    assert snap["events"]["provider_selected"] == 1


# --- telephony codecs ------------------------------------------------------


def test_mulaw_silence_round_trips_exactly() -> None:
    silence = struct.pack("<4h", 0, 0, 0, 0)
    encoded = pcm16_to_mulaw(silence)
    assert len(encoded) == 4
    assert mulaw_to_pcm16(encoded) == silence


def test_mulaw_round_trip_is_close() -> None:
    samples = [0, 1000, -1000, 8000, -8000, 32000, -32000]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    out = struct.unpack(f"<{len(samples)}h", mulaw_to_pcm16(pcm16_to_mulaw(pcm)))
    for original, decoded in zip(samples, out, strict=True):
        # μ-law is lossy but monotonic; the error is small relative to amplitude.
        assert abs(original - decoded) <= max(64, abs(original) * 0.1)


def test_mulaw_byte_lengths() -> None:
    pcm = struct.pack("<10h", *range(10))
    assert len(pcm16_to_mulaw(pcm)) == 10  # 2 bytes/sample → 1 byte/sample
    assert len(mulaw_to_pcm16(b"\x00" * 10)) == 20  # 1 → 2


def test_resample_changes_length_and_is_noop_when_equal() -> None:
    pcm = struct.pack("<8h", *[100, 200, 300, 400, 500, 600, 700, 800])
    assert resample_pcm16(pcm, 16000, 16000) == pcm
    up = resample_pcm16(pcm, 8000, 16000)
    assert len(up) // 2 == 16  # 8 samples @8k → ~16 @16k
    down = resample_pcm16(pcm, 16000, 8000)
    assert len(down) // 2 == 4
    assert resample_pcm16(b"", 8000, 16000) == b""


# --- WER + bench -----------------------------------------------------------


def test_word_error_rate() -> None:
    assert word_error_rate("hello world", "hello world") == 0.0
    assert word_error_rate("hello world", "Hello World") == 0.0  # case-insensitive
    assert word_error_rate("the cat sat", "the dog sat") == 1 / 3  # one substitution
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "x") == 1.0


def test_word_error_rate_ignores_punctuation_and_capitalization() -> None:
    # TTS-then-STT round-trips routinely add punctuation/caps — that's stylistic,
    # not a word error. Caught a real false positive in the live smoke (11%→0%).
    assert (
        word_error_rate(
            "the quick brown fox jumped over the lazy dog",
            "The quick brown fox jumped over the lazy dog.",
        )
        == 0.0
    )
    assert word_error_rate("hello world", "Hello, world!") == 0.0


async def test_bench_stt_reports_wer_and_latency() -> None:
    corpus = [
        ([AudioChunk(data=b"a")], "turn the lights on"),
        ([AudioChunk(data=b"b")], "what time is it"),
    ]
    # FakeSTT echoes a fixed transcript → first item perfect, second wrong.
    report = await bench_stt(FakeSTT("turn the lights on"), corpus)
    assert report.items[0].wer == 0.0
    assert report.items[1].wer > 0.0
    assert 0.0 <= report.mean_wer <= 1.0
    assert report.p50_latency_ms >= 0.0
    # p95 is also defined and >= p50 (percentile is monotonic) — the regression
    # gate reads both, so pin the aggregate that was previously uncovered.
    assert report.p95_latency_ms >= report.p50_latency_ms
    assert "WER" in report.format()


def test_bench_percentile_interpolates_across_items() -> None:
    """STTBenchReport percentiles interpolate over the per-item latencies
    (the eval's regression-gate numbers), not just echo a single value."""
    items = [
        STTBenchItem(reference="x", hypothesis="x", wer=0.0, latency_ms=float(ms))
        for ms in (10, 20, 30, 40, 50)
    ]
    report = STTBenchReport(provider="fake", items=items)
    assert report.p50_latency_ms == 30.0  # median of 10..50
    # 95th percentile sits between 40 and 50 (linear interp), strictly above p50.
    assert 40.0 < report.p95_latency_ms <= 50.0
    assert report.mean_wer == 0.0
