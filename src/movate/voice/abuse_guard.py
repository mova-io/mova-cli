"""Voice WS abuse / cost guards (#212).

Rate-limiting and resource-bounding for the hosted voice playground (ADR 053)
and any multi-tenant deployment.  Three guards, all configurable:

1. **Max session duration** — a hard wall-clock cap on a single WS session
   (default 30 minutes).  After the cap the session is closed with a clear
   ``"session_max_duration"`` message.

2. **Idle timeout** — if no audio frames arrive for a configurable window
   (default 2 minutes), the session is closed with ``"session_idle"``.

3. **Per-key concurrent-session limit** — at most N active voice WS
   connections per API key (default 3).  The N+1th connection is rejected
   with a 429-equivalent WS close (``4029``).

Environment variables:

* ``VOICE_MAX_SESSION_DURATION_S`` — max session seconds (default 1800 = 30 min)
* ``VOICE_IDLE_TIMEOUT_S`` — idle timeout seconds (default 120 = 2 min)
* ``VOICE_MAX_CONCURRENT_SESSIONS`` — per-key concurrency cap (default 3)

Design (CLAUDE.md rule 6 — transport edge, never execution logic):

* Guards live at the WS route, not inside the pipeline or adapters.
* The concurrent-session tracker is an in-process ``dict`` keyed by API key.
  Multi-worker deployments share the limit at the load-balancer or via a
  shared counter (Redis, etc.) — this module provides the single-worker
  primitive.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ── Configurable defaults ────────────────────────────────────────────────
_DEFAULT_MAX_DURATION_S: float = float(os.environ.get("VOICE_MAX_SESSION_DURATION_S", "1800"))
_DEFAULT_IDLE_TIMEOUT_S: float = float(os.environ.get("VOICE_IDLE_TIMEOUT_S", "120"))
_DEFAULT_MAX_CONCURRENT: int = int(os.environ.get("VOICE_MAX_CONCURRENT_SESSIONS", "3"))

# WS close code for "too many sessions" — mirrors HTTP 429.  4000-4999 is the
# application-defined range per RFC 6455 §7.4.2.
WS_CLOSE_TOO_MANY_SESSIONS = 4029
WS_CLOSE_MAX_DURATION = 4001
WS_CLOSE_IDLE = 4002


@dataclass(frozen=True)
class SessionGuardConfig:
    """Configuration for the per-session abuse guards."""

    max_duration_s: float = _DEFAULT_MAX_DURATION_S
    idle_timeout_s: float = _DEFAULT_IDLE_TIMEOUT_S
    max_concurrent_sessions: int = _DEFAULT_MAX_CONCURRENT

    @classmethod
    def from_env(cls) -> SessionGuardConfig:
        """Build from environment variables, falling back to defaults."""
        return cls(
            max_duration_s=float(
                os.environ.get("VOICE_MAX_SESSION_DURATION_S", str(_DEFAULT_MAX_DURATION_S))
            ),
            idle_timeout_s=float(
                os.environ.get("VOICE_IDLE_TIMEOUT_S", str(_DEFAULT_IDLE_TIMEOUT_S))
            ),
            max_concurrent_sessions=int(
                os.environ.get("VOICE_MAX_CONCURRENT_SESSIONS", str(_DEFAULT_MAX_CONCURRENT))
            ),
        )


class SessionDurationGuard:
    """Enforces a maximum session duration, closing the WS when exceeded.

    Call :meth:`start` when the WS is accepted; it schedules a task that fires
    ``close_fn`` after ``max_duration_s`` seconds.  Call :meth:`stop` on normal
    session teardown to cancel the timer.
    """

    def __init__(
        self,
        close_fn: Any,
        max_duration_s: float = _DEFAULT_MAX_DURATION_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._close_fn = close_fn
        self._max_duration_s = max(0.01, max_duration_s)
        self._clock = clock
        self._started_at: float = clock()
        self._task: asyncio.Task[None] | None = None
        self._expired = False

    def start(self) -> None:
        """Start the duration timer."""
        self._started_at = self._clock()
        self._task = asyncio.create_task(self._watch())

    def stop(self) -> None:
        """Cancel the duration timer (idempotent)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def expired(self) -> bool:
        return self._expired

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._started_at

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._max_duration_s - self.elapsed_s)

    async def _watch(self) -> None:
        try:
            await asyncio.sleep(self._max_duration_s)
            self._expired = True
            with contextlib.suppress(Exception):
                await self._close_fn(
                    WS_CLOSE_MAX_DURATION,
                    f"Session exceeded maximum duration of {int(self._max_duration_s)}s",
                )
        except asyncio.CancelledError:
            return


class IdleTimeoutGuard:
    """Closes the WS after a period of no audio frames.

    Call :meth:`note_activity` on every inbound audio frame.  If no activity
    arrives within ``idle_timeout_s``, ``close_fn`` is called.
    """

    def __init__(
        self,
        close_fn: Any,
        idle_timeout_s: float = _DEFAULT_IDLE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._close_fn = close_fn
        self._idle_timeout_s = max(0.01, idle_timeout_s)
        self._clock = clock
        self._last_activity: float = clock()
        self._task: asyncio.Task[None] | None = None
        self._expired = False

    def start(self) -> None:
        """Start the idle watcher."""
        self._last_activity = self._clock()
        self._task = asyncio.create_task(self._watch())

    def stop(self) -> None:
        """Cancel the idle watcher (idempotent)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def note_activity(self) -> None:
        """Record that an audio frame arrived — reset the idle clock."""
        self._last_activity = self._clock()

    @property
    def expired(self) -> bool:
        return self._expired

    async def _watch(self) -> None:
        try:
            while True:
                elapsed = self._clock() - self._last_activity
                remaining = self._idle_timeout_s - elapsed
                if remaining <= 0:
                    self._expired = True
                    with contextlib.suppress(Exception):
                        await self._close_fn(
                            WS_CLOSE_IDLE,
                            "Session closed due to inactivity",
                        )
                    return
                await asyncio.sleep(min(remaining, 5.0))  # check at least every 5s
        except asyncio.CancelledError:
            return


class ConcurrentSessionTracker:
    """Tracks active voice sessions per API key and enforces a concurrency cap.

    Call :meth:`try_acquire` when a WS is accepted — returns ``True`` if the
    session is allowed (under the cap), ``False`` if it should be rejected (the
    route sends a 4029 close).  Call :meth:`release` when the WS closes.

    The tracker is process-scoped (one per worker).  Multi-worker deployments
    should layer a shared counter (Redis / DB) above this.
    """

    def __init__(self, max_concurrent: int = _DEFAULT_MAX_CONCURRENT) -> None:
        self._max = max(1, max_concurrent)
        self._sessions: dict[str, int] = defaultdict(int)

    def try_acquire(self, api_key: str) -> bool:
        """Attempt to open a session.  Returns False if at the cap."""
        if self._sessions[api_key] >= self._max:
            return False
        self._sessions[api_key] += 1
        return True

    def release(self, api_key: str) -> None:
        """Release a session slot."""
        if self._sessions[api_key] > 0:
            self._sessions[api_key] -= 1
        if self._sessions[api_key] == 0:
            self._sessions.pop(api_key, None)

    def active_count(self, api_key: str) -> int:
        """How many sessions this key currently holds."""
        return self._sessions.get(api_key, 0)

    @property
    def max_concurrent(self) -> int:
        return self._max
