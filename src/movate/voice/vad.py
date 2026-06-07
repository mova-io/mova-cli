"""Energy-based voice activity detection — stop paying to transcribe silence.

STT is billed per audio-*minute*, but real calls are full of pauses, hold time,
and dead air. A cheap RMS energy gate drops near-silent frames before they reach
the provider, cutting billed seconds with no new dependency. This is a coarse
baseline (no spectral analysis); swap in a model VAD (e.g. Silero) behind the
same idea when you need better speech/non-speech discrimination.
"""

from __future__ import annotations

import struct

from movate.voice.base import AudioChunk

# RMS below this (16-bit linear scale, 0..32768) counts as silence. ~500 is a
# conservative floor — clearly-spoken speech sits well above it.
DEFAULT_SILENCE_RMS = 500.0


def frame_rms(chunk: AudioChunk) -> float:
    """Root-mean-square amplitude of a PCM16 frame (0.0 for empty/odd data)."""
    count = len(chunk.data) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", chunk.data[: count * 2])
    mean_square = sum(s * s for s in samples) / count
    return float(mean_square**0.5)


def is_silent(chunk: AudioChunk, threshold: float = DEFAULT_SILENCE_RMS) -> bool:
    """Whether a frame's energy is below the silence threshold."""
    return frame_rms(chunk) < threshold
