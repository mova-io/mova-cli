"""Adaptive endpointing controller (ADR 073 Phase 3).

The controller moves the STT silence-hold within a session from the speculation
commit-ratio: clean turn-ends (high ratio) → shorten; speakers running past
their interims (low ratio) → lengthen. Bounded, single-step, sample-gated.
"""

from __future__ import annotations

from movate.voice import AdaptiveEndpointing
from movate.voice.observer import MetricsObserver


def _commit(n: int) -> dict:
    return {"started": n, "committed": n, "cancelled": 0}


def _cancel(n: int) -> dict:
    return {"started": n, "committed": 0, "cancelled": n}


def test_starts_at_base_and_waits_for_min_samples() -> None:
    a = AdaptiveEndpointing(base_ms=1500, min_samples=6, step_ms=150)
    assert a.current_ms == 1500
    # Under the sample floor, no movement even with a clear signal.
    for _ in range(5):
        assert a.record(_commit(1)) is None
    assert a.current_ms == 1500


def test_shortens_on_high_commit_ratio() -> None:
    a = AdaptiveEndpointing(base_ms=1500, min_samples=6, step_ms=150, shorten_above=0.7)
    moved = None
    for _ in range(6):
        moved = a.record(_commit(1)) or moved
    assert moved == 1350  # one step down once the floor is cleared
    assert a.current_ms == 1350


def test_lengthens_on_low_commit_ratio() -> None:
    a = AdaptiveEndpointing(base_ms=1500, min_samples=6, step_ms=150, lengthen_below=0.4)
    moved = None
    for _ in range(6):
        moved = a.record(_cancel(1)) or moved
    assert moved == 1650
    assert a.current_ms == 1650


def test_dead_band_leaves_hold_untouched() -> None:
    """A mid-range ratio (between the thresholds) doesn't move the hold."""
    a = AdaptiveEndpointing(base_ms=1500, min_samples=4, shorten_above=0.7, lengthen_below=0.4)
    # 50% commit-ratio → in the dead band.
    a.record({"started": 2, "committed": 1, "cancelled": 1})
    assert a.record({"started": 2, "committed": 1, "cancelled": 1}) is None
    assert a.current_ms == 1500


def test_clamps_to_min_and_max() -> None:
    a = AdaptiveEndpointing(
        base_ms=700, min_ms=600, max_ms=2500, step_ms=150, min_samples=1, shorten_above=0.7
    )
    a.record(_commit(10))  # 700 → 600 (clamped at min)
    assert a.current_ms == 600
    # Further high-ratio turns can't go below min → no movement reported.
    assert a.record(_commit(10)) is None
    assert a.current_ms == 600


def test_reads_full_snapshot_block() -> None:
    a = AdaptiveEndpointing(base_ms=1500, min_samples=2, step_ms=100, shorten_above=0.7)
    obs = MetricsObserver()
    obs.on_event("speculation_started")
    obs.on_event("speculation_committed", head_start_ms=500)
    a.record(obs.snapshot())
    assert a.record(obs.snapshot()) == 1400
