"""Adaptive endpointing — tune the STT silence-hold from observed turn cadence.

ADR 073 Phase 3. ADR 071 D3 / ADR 073 D3 made ``endpointing_ms`` a *static*
per-agent knob: one number per agent, picked by hand. But a single number is
still wrong for some speakers within that agent — too short and the turn ends
mid-pause, too long and a finished speaker waits. This controller closes the
last gap by *moving* the hold within a session from a measured signal.

The signal it uses is the **speculation commit-ratio** (ADR 070), which is a
direct read on turn-end cleanliness:

* a high commit-ratio means the stable interim *was* the end of the turn (the
  speculation matched the final) → the speaker finishes cleanly, so we can
  **shorten** the hold and shave latency;
* a low commit-ratio means speakers keep going past their interims (the
  speculation cancelled) → ending sooner would barge in, so **lengthen** the
  hold.

It is deliberately conservative: it nudges by one ``step_ms`` at a time, stays
within ``[min_ms, max_ms]``, and waits for ``min_samples`` speculations before
moving at all. Opt-in (the caller decides whether to use ``current_ms``); a
session that doesn't enable it, or doesn't speculate, never adapts.
"""

from __future__ import annotations

from typing import Any

# Tuning defaults. The band brackets the 1500 ms default; the step is small so a
# session converges gently rather than oscillating, and the thresholds leave a
# dead-band in the middle (0.4-0.7) where the hold is left alone.
_BASE_MS = 1500
_MIN_MS = 600
_MAX_MS = 2500
_STEP_MS = 150
_MIN_SAMPLES = 6
_SHORTEN_ABOVE = 0.7  # commit-ratio above this → shorten the hold
_LENGTHEN_BELOW = 0.4  # commit-ratio below this → lengthen the hold


class AdaptiveEndpointing:
    """Session-scoped controller for the STT silence-hold (ADR 073 Phase 3).

    Seed it with the agent's static ``endpointing_ms`` as the base; read
    :attr:`current_ms` for each turn's ``endpointing_ms``; feed each turn's
    speculation snapshot to :meth:`record`, which returns the new value when it
    moves (else ``None``). Bounded, single-step, sample-gated — safe to leave on.
    """

    def __init__(
        self,
        *,
        base_ms: int = _BASE_MS,
        min_ms: int = _MIN_MS,
        max_ms: int = _MAX_MS,
        step_ms: int = _STEP_MS,
        min_samples: int = _MIN_SAMPLES,
        shorten_above: float = _SHORTEN_ABOVE,
        lengthen_below: float = _LENGTHEN_BELOW,
    ) -> None:
        self._min_ms = max(0, min_ms)
        self._max_ms = max(self._min_ms, max_ms)
        self._current = max(self._min_ms, min(self._max_ms, int(base_ms)))
        self._step_ms = max(1, step_ms)
        self._min_samples = max(1, min_samples)
        self._shorten_above = shorten_above
        self._lengthen_below = lengthen_below
        self._started = 0
        self._committed = 0

    @property
    def current_ms(self) -> int:
        """The endpointing hold (ms) to use for the next turn."""
        return self._current

    @property
    def commit_ratio(self) -> float:
        return (self._committed / self._started) if self._started else 0.0

    def record(self, snapshot: dict[str, Any]) -> int | None:
        """Absorb one turn's speculation snapshot; return the new hold if it moved.

        Accumulates the running commit-ratio and, once ``min_samples``
        speculations have been seen, nudges the hold by one ``step_ms`` toward
        shorter (clean turn-ends) or longer (speakers running past interims),
        clamped to ``[min_ms, max_ms]``. Returns the new ``current_ms`` only when
        it actually changes, so the caller can signal the adjustment once.
        """
        spec = snapshot.get("speculation", snapshot)
        self._started += int(spec.get("started", 0))
        self._committed += int(spec.get("committed", 0))
        if self._started < self._min_samples:
            return None

        ratio = self.commit_ratio
        before = self._current
        if ratio >= self._shorten_above:
            self._current = max(self._min_ms, self._current - self._step_ms)
        elif ratio <= self._lengthen_below:
            self._current = min(self._max_ms, self._current + self._step_ms)
        return self._current if self._current != before else None
