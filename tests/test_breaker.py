"""The per-provider circuit breaker (ADR 068 D3)."""

from __future__ import annotations

from movate.voice.breaker import CircuitBreaker


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_breaker_starts_closed() -> None:
    cb = CircuitBreaker(threshold=3)
    assert cb.allow() is True
    assert cb.is_open is False


def test_breaker_opens_after_threshold_failures() -> None:
    clock = _Clock()
    cb = CircuitBreaker(threshold=3, cooldown=30.0, clock=clock)
    assert cb.record_failure() is False  # 1
    assert cb.record_failure() is False  # 2
    assert cb.record_failure() is True  # 3 → opens
    assert cb.is_open is True
    assert cb.allow() is False


def test_breaker_half_opens_after_cooldown() -> None:
    clock = _Clock()
    cb = CircuitBreaker(threshold=1, cooldown=30.0, clock=clock)
    cb.record_failure()  # opens at t=0
    assert cb.allow() is False
    clock.t = 29.0
    assert cb.allow() is False
    clock.t = 30.0
    assert cb.allow() is True  # half-open trial allowed
    assert cb.is_open is False


def test_breaker_success_closes_it() -> None:
    cb = CircuitBreaker(threshold=1)
    cb.record_failure()  # open
    assert cb.record_success() is True  # closing an open breaker
    assert cb.is_open is False
    assert cb.allow() is True
    # A success on an already-closed breaker is not a "close" transition.
    assert cb.record_success() is False


def test_breaker_reopens_on_failed_half_open_trial() -> None:
    clock = _Clock()
    cb = CircuitBreaker(threshold=1, cooldown=10.0, clock=clock)
    cb.record_failure()  # open at t=0
    clock.t = 10.0
    assert cb.allow() is True  # half-open
    clock.t = 11.0
    cb.record_failure()  # trial failed → re-open, refresh cooldown
    assert cb.allow() is False
    clock.t = 20.0
    assert cb.allow() is False  # cooldown measured from the re-open at t=11
    clock.t = 21.0
    assert cb.allow() is True
