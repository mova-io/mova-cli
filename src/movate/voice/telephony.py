"""Telephony audio helpers — G.711 μ-law ↔ PCM16 and rate conversion.

Phone channels (Twilio/SIP) speak **8 kHz μ-law**, while STT/TTS adapters work
in PCM16 (typically 16/24 kHz). ADR 048 D8 keeps codec transcoding at the *edge*
(out of the agent and adapters), so these are the edge primitives a telephony
transport uses to bridge: decode inbound μ-law → PCM16 for STT, and encode TTS
PCM16 → μ-law outbound, resampling between 8 kHz and the adapter's rate.

Pure, dependency-free, and 3.11-3.13 safe — implemented directly rather than via
the (removed-in-3.13) ``audioop``. The resampler is simple linear interpolation
(no anti-alias filter); it's the practical baseline for narrowband telephony, not
a mastering-grade SRC.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from collections.abc import AsyncIterator

from movate.voice.base import AudioChunk

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


def _linear_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    sample = min(sample, _ULAW_CLIP)
    sample += _ULAW_BIAS
    idx = (sample >> 7) & 0xFF
    exponent = idx.bit_length() - 1 if idx else 0
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _ulaw_to_linear(byte: int) -> int:
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + _ULAW_BIAS) << exponent
    sample -= _ULAW_BIAS
    return -sample if sign else sample


def pcm16_to_wav(pcm: bytes, sample_rate: int, *, channels: int = 1) -> bytes:
    """Wrap raw little-endian 16-bit PCM in a WAV (RIFF) container.

    APIs that take a "file" and sniff the format (e.g. OpenAI Whisper) reject
    header-less PCM — they need a real container. This is the cheap, dependency-
    free way to give them one.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def pcm16_to_mulaw(data: bytes) -> bytes:
    """Encode little-endian 16-bit PCM to 8-bit G.711 μ-law (1 byte/sample)."""
    count = len(data) // 2
    if count == 0:
        return b""
    samples = struct.unpack(f"<{count}h", data[: count * 2])
    return bytes(_linear_to_ulaw(s) for s in samples)


def mulaw_to_pcm16(data: bytes) -> bytes:
    """Decode 8-bit G.711 μ-law to little-endian 16-bit PCM (2 bytes/sample)."""
    if not data:
        return b""
    samples = [_ulaw_to_linear(b) for b in data]
    return struct.pack(f"<{len(samples)}h", *samples)


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample little-endian 16-bit PCM from ``src_rate`` to ``dst_rate`` (linear).

    Used at the edge to bridge an 8 kHz telephony stream and an adapter's
    16/24 kHz rate. Returns ``data`` unchanged when the rates match.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    if src_rate == dst_rate:
        return data
    count = len(data) // 2
    if count == 0:
        return b""
    samples: list[float] = list(struct.unpack(f"<{count}h", data[: count * 2]))
    # Anti-alias before decimation: low-pass at just under the destination
    # Nyquist so high frequencies don't fold back as buzz (a plain linear resample
    # aliases badly on downsample, e.g. 24 kHz → 8 kHz telephony).
    if dst_rate < src_rate:
        samples = _lowpass_biquad(samples, src_rate, cutoff=0.45 * dst_rate)
    out_count = max(1, round(count * dst_rate / src_rate))
    ratio = (count - 1) / (out_count - 1) if out_count > 1 else 0.0
    out: list[int] = []
    for i in range(out_count):
        pos = i * ratio
        lo = int(pos)
        hi = min(lo + 1, count - 1)
        frac = pos - lo
        value = samples[lo] * (1.0 - frac) + samples[hi] * frac
        out.append(max(-32768, min(32767, round(value))))
    return struct.pack(f"<{out_count}h", *out)


def _lowpass_biquad(samples: list[float], fs: int, cutoff: float) -> list[float]:
    """2nd-order Butterworth low-pass (RBJ biquad) — pure-Python anti-alias filter."""
    w0 = 2.0 * math.pi * cutoff / fs
    cos_w0 = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * 0.7071067811865476)  # Q = 1/sqrt(2)
    b0 = (1.0 - cos_w0) / 2.0
    b1 = 1.0 - cos_w0
    b2 = (1.0 - cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0
    x1 = x2 = y1 = y2 = 0.0
    out: list[float] = []
    for x0 in samples:
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out.append(y0)
        x2, x1 = x1, x0
        y2, y1 = y1, y0
    return out


# Telephony default: phone media is 8 kHz; the pipeline/STT typically want 16 kHz.
TELEPHONY_RATE = 8_000
PIPELINE_RATE = 16_000


async def telephony_inbound(
    frames: AsyncIterator[bytes],
    *,
    src_rate: int = TELEPHONY_RATE,
    pipeline_rate: int = PIPELINE_RATE,
) -> AsyncIterator[AudioChunk]:
    """Bridge an inbound μ-law phone stream into the pipeline's PCM16 ``AudioChunk``s.

    Each raw 8 kHz μ-law frame (e.g. a Twilio Media-Streams payload) is decoded to
    PCM16 and resampled to ``pipeline_rate`` so it can feed ``run_voice_pipeline``
    (``audio_in=telephony_inbound(frames)``). Edge transcoding only — the agent
    never sees codecs (ADR 048 D8).
    """
    async for frame in frames:
        pcm = mulaw_to_pcm16(frame)
        if pipeline_rate != src_rate:
            pcm = resample_pcm16(pcm, src_rate, pipeline_rate)
        yield AudioChunk(data=pcm, codec="pcm16", sample_rate=pipeline_rate)


async def telephony_outbound(
    audio: AsyncIterator[AudioChunk],
    *,
    dst_rate: int = TELEPHONY_RATE,
    frame_bytes: int = 160,
) -> AsyncIterator[bytes]:
    """Bridge outbound TTS ``AudioChunk``s back to μ-law phone frames.

    Each synthesized PCM16 chunk is resampled to ``dst_rate`` and μ-law encoded,
    then re-chunked into fixed ``frame_bytes`` frames — Twilio Media Streams
    expects **160-byte (20 ms @ 8 kHz) μ-law frames**, not arbitrary blobs. Set
    ``frame_bytes<=0`` to emit one frame per input chunk (no re-chunking). The
    trailing partial frame is flushed as-is at end of stream.
    """
    buffer = bytearray()
    async for chunk in audio:
        pcm = chunk.data
        if chunk.sample_rate != dst_rate:
            pcm = resample_pcm16(pcm, chunk.sample_rate, dst_rate)
        encoded = pcm16_to_mulaw(pcm)
        if frame_bytes <= 0:
            yield encoded
            continue
        buffer.extend(encoded)
        while len(buffer) >= frame_bytes:
            yield bytes(buffer[:frame_bytes])
            del buffer[:frame_bytes]
    if buffer:
        yield bytes(buffer)  # trailing partial frame
