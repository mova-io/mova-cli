"""A per-provider circuit breaker (ADR 068 D3).

A provider that is consistently failing should be **skipped**, not retried on
every turn. The breaker tracks consecutive failures: after ``threshold`` of them
it **opens** (the router routes away from that provider); after ``cooldown``
seconds it goes **half-open** (one trial request is allowed); a success
**closes** it again, a failure re-opens it.

The clock is injected (``clock: () -> float``, default :func:`time.monotonic`) so
tests drive open/half-open/close transitions deterministically — there is no
wall-clock call inside, mirroring the no-``Date.now`` discipline the pipeline's
latency clock already uses.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class CircuitBreaker:
    """Three-state breaker (closed → open → half-open → closed)."""

    threshold: int = 3
    cooldown: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def allow(self) -> bool:
        """May a request be sent to this provider right now?

        ``True`` when closed, or when open but the cooldown has elapsed (a
        half-open trial). ``False`` while open and still cooling down.
        """
        if self._opened_at is None:
            return True
        return (self.clock() - self._opened_at) >= self.cooldown

    @property
    def is_open(self) -> bool:
        """Whether the breaker is currently tripped (cooldown not yet elapsed)."""
        return self._opened_at is not None and not self.allow()

    def record_success(self) -> bool:
        """Note a success. Returns ``True`` if this *closed* an open breaker."""
        was_open = self._opened_at is not None
        self._consecutive_failures = 0
        self._opened_at = None
        return was_open

    def record_failure(self) -> bool:
        """Note a failure. Returns ``True`` if this *opened* the breaker."""
        self._consecutive_failures += 1
        if self._opened_at is None and self._consecutive_failures >= self.threshold:
            self._opened_at = self.clock()
            return True
        # Re-open (refresh the cooldown) on a failed half-open trial.
        if self._opened_at is not None:
            self._opened_at = self.clock()
        return False
