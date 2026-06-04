"""Voice WebSocket resilience — reconnect/resume + audio ring buffer (#209).

On WS disconnect mid-conversation the server keeps a bounded ring buffer of
recent audio frames so a reconnecting client (same ``session_id``) can resume
mid-turn instead of starting from silence.  The buffer is capped at
``max_seconds`` / ``max_bytes`` (whichever is reached first) and flushed after
``reconnect_timeout_s`` if no reconnect arrives.

Design constraints (CLAUDE.md rule 6 — transport, not execution logic):

* The buffer lives at the **transport edge** (the WS route) — the pipeline
  (:mod:`movate.voice.pipeline`) is unaware of it.
* A resumed turn feeds the buffered audio as a normal ``audio_in`` iterator to
  ``run_voice_pipeline`` — no new pipeline parameter.
* Memory-bounded: ``max_bytes`` hard-caps the buffer regardless of frame count
  (default 160 KB ≈ 10 s of 16 kHz PCM-16), and ``max_seconds`` soft-caps it
  by estimated duration (default 5 s, configurable up to 10 s).
"""

from __future__ import annotations

import asyncio
import collections
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Env-configurable defaults.
_DEFAULT_MAX_SECONDS: float = float(os.environ.get("VOICE_RECONNECT_BUFFER_S", "5"))
_MAX_SECONDS_CAP: float = 10.0
_DEFAULT_MAX_BYTES: int = int(os.environ.get("VOICE_RECONNECT_BUFFER_BYTES", "163840"))  # 160 KB
_DEFAULT_RECONNECT_TIMEOUT: float = float(os.environ.get("VOICE_RECONNECT_TIMEOUT_S", "30"))


def _estimate_duration_s(
    data: bytes, sample_rate: int = 16_000, bytes_per_sample: int = 2
) -> float:
    """Rough audio-duration estimate from raw PCM frame size."""
    return len(data) / (sample_rate * bytes_per_sample)


@dataclass
class AudioRingBuffer:
    """A bounded ring buffer of raw audio frames for WS reconnect/resume.

    Frames are ``bytes`` (the raw binary payloads the WS transport receives).
    The buffer maintains *both* a time-bound (estimated audio seconds) and a
    hard byte-bound so memory stays predictable.

    Thread-safety: not needed — each WS session owns one buffer, and the
    event loop is single-threaded.  The ``asyncio.Event`` is the coordination
    primitive between the disconnect handler and the reconnect handler.
    """

    max_seconds: float = _DEFAULT_MAX_SECONDS
    max_bytes: int = _DEFAULT_MAX_BYTES
    reconnect_timeout_s: float = _DEFAULT_RECONNECT_TIMEOUT
    clock: Callable[[], float] = field(default=time.monotonic)

    # Internal state
    _frames: collections.deque[bytes] = field(default_factory=collections.deque, init=False)
    _total_bytes: int = field(default=0, init=False)
    _disconnected_at: float | None = field(default=None, init=False)
    _reconnect_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _flushed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # Clamp max_seconds to the hard cap.
        self.max_seconds = min(max(0.1, self.max_seconds), _MAX_SECONDS_CAP)

    def push(self, frame: bytes) -> None:
        """Append a frame, evicting oldest frames to stay within bounds."""
        self._frames.append(frame)
        self._total_bytes += len(frame)
        self._evict()

    def _evict(self) -> None:
        """Remove oldest frames until both size bounds are satisfied."""
        # Hard byte cap first.
        while self._total_bytes > self.max_bytes and self._frames:
            evicted = self._frames.popleft()
            self._total_bytes -= len(evicted)
        # Soft duration cap.
        total_secs = self._estimated_seconds()
        while total_secs > self.max_seconds and self._frames:
            evicted = self._frames.popleft()
            self._total_bytes -= len(evicted)
            total_secs = self._estimated_seconds()

    def _estimated_seconds(self) -> float:
        return _estimate_duration_s(b"x" * self._total_bytes)

    def mark_disconnected(self) -> None:
        """Record that the WS dropped — start the reconnect window."""
        self._disconnected_at = self.clock()
        self._reconnect_event.clear()

    def mark_reconnected(self) -> None:
        """Signal that a client reconnected — unblock any waiter."""
        self._disconnected_at = None
        self._reconnect_event.set()

    @property
    def is_disconnected(self) -> bool:
        return self._disconnected_at is not None

    async def wait_for_reconnect(self) -> bool:
        """Block until reconnect or timeout.  Returns True if reconnected."""
        if self._disconnected_at is None:
            return True
        try:
            await asyncio.wait_for(self._reconnect_event.wait(), self.reconnect_timeout_s)
            return True
        except TimeoutError:
            self.flush()
            return False

    def drain(self) -> list[bytes]:
        """Return all buffered frames and clear the buffer."""
        frames = list(self._frames)
        self._frames.clear()
        self._total_bytes = 0
        return frames

    def flush(self) -> None:
        """Discard all buffered frames (timeout / session end)."""
        self._frames.clear()
        self._total_bytes = 0
        self._flushed = True

    @property
    def flushed(self) -> bool:
        return self._flushed

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def estimated_seconds(self) -> float:
        return self._estimated_seconds()


@dataclass
class VoiceSessionResumeState:
    """Per-session state that survives a WS disconnect/reconnect (#209).

    Held by the voice route keyed on ``session_id``.  Carries the audio ring
    buffer plus any config the reconnecting client should inherit from the
    prior connection (voice_id, language, etc.).  Flushed and removed when the
    reconnect window expires or the session ends normally.
    """

    session_id: str
    buffer: AudioRingBuffer = field(default_factory=AudioRingBuffer)
    # Snapshot of the config at disconnect time so the resumed turn inherits it.
    config_snapshot: dict[str, object] = field(default_factory=dict)
    # Whether a pipeline turn was in progress at disconnect.
    turn_in_progress: bool = False
