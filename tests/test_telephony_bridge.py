"""The telephony streaming bridge: μ-law 8 kHz ⇄ pipeline PCM16."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    telephony_inbound,
    telephony_outbound,
)


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


async def _frames(*items: bytes) -> AsyncIterator[bytes]:
    for it in items:
        yield it


async def _chunks(*items: AudioChunk) -> AsyncIterator[AudioChunk]:
    for it in items:
        yield it


async def test_inbound_decodes_and_resamples_to_pipeline_rate() -> None:
    # One 8 kHz μ-law frame (encode 80 samples = 10 ms) → PCM16 @ 16 kHz.
    mulaw = pcm16_to_mulaw(_pcm([1000] * 80))
    out = [c async for c in telephony_inbound(_frames(mulaw))]
    assert len(out) == 1
    chunk = out[0]
    assert chunk.codec == "pcm16"
    assert chunk.sample_rate == 16_000
    # 80 samples @8k upsampled to 16k → ~160 samples (320 bytes).
    assert len(chunk.data) // 2 == 160


async def test_outbound_resamples_and_mulaw_encodes() -> None:
    # A 24 kHz TTS chunk (240 samples = 10 ms) → 8 kHz μ-law (80 bytes < 1 frame).
    chunk = AudioChunk(data=_pcm([2000] * 240), codec="pcm16", sample_rate=24_000)
    out = [f async for f in telephony_outbound(_chunks(chunk))]
    assert len(out) == 1
    assert len(out[0]) == 80  # one trailing partial frame (< 160-byte frame)


async def test_outbound_rechunks_into_twilio_20ms_frames() -> None:
    # 800 samples @ 8 kHz → 800 μ-law bytes → five full 160-byte (20 ms) frames.
    chunk = AudioChunk(data=_pcm([1000] * 800), codec="pcm16", sample_rate=8_000)
    out = [f async for f in telephony_outbound(_chunks(chunk))]
    assert [len(f) for f in out] == [160, 160, 160, 160, 160]


async def test_outbound_frame_chunking_can_be_disabled() -> None:
    chunk = AudioChunk(data=_pcm([1000] * 800), codec="pcm16", sample_rate=8_000)
    out = [f async for f in telephony_outbound(_chunks(chunk), frame_bytes=0)]
    assert len(out) == 1 and len(out[0]) == 800  # one blob per input chunk


async def test_bridge_round_trip_preserves_signal_shape() -> None:
    # μ-law in → pipeline PCM → μ-law out at the same rate should be close.
    original = pcm16_to_mulaw(_pcm([4000, -4000, 4000, -4000] * 40))
    inbound = [c async for c in telephony_inbound(_frames(original), pipeline_rate=8_000)]
    outbound = [f async for f in telephony_outbound(_chunks(*inbound), dst_rate=8_000)]
    # Same rate in/out → same frame count; decode both and compare loosely.
    a = struct.unpack(f"<{len(mulaw_to_pcm16(original)) // 2}h", mulaw_to_pcm16(original))
    b = struct.unpack(f"<{len(mulaw_to_pcm16(outbound[0])) // 2}h", mulaw_to_pcm16(outbound[0]))
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        assert abs(x - y) <= max(256, abs(x) * 0.2)


async def test_empty_streams() -> None:
    assert [c async for c in telephony_inbound(_frames())] == []
    assert [f async for f in telephony_outbound(_chunks())] == []
