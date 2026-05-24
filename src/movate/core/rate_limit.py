"""Per-API-key rate limiting — token bucket, in-process state.

Architecture
------------

Token bucket (not leaky bucket) because it tolerates bursts: a client
that's been quiet for a minute can spend its full budget in one
go, which matches realistic API usage (humans cluster their requests).
Steady-state still averages to the configured limit.

State is **in-process** in v1.x. ACA may run multiple replicas of the
API container, and each has its own ``InProcessRateLimiter`` — so the
effective limit per key is ``limit * replica_count``. Acceptable for
v1.x (replicas are typically 1-2 in dev/staging, 2-4 in prod);
Redis-backed shared state lands in v1.2 if a single-pod limit is
actually needed.

The :class:`RateLimiter` Protocol is the seam: the middleware calls
``check(key)`` and gets a :class:`RateLimitDecision` back. A future
``RedisRateLimiter`` implements the same interface; the middleware
doesn't change.

The same Protocol is reused for the **per-tenant aggregate** cap (item
25): the runtime builds a SECOND :class:`InProcessRateLimiter` keyed by
``tenant:<tenant_id>`` instead of by ``key_id``, so a tenant can't
sidestep the per-key limit by minting more keys. No new algorithm — just
a second instance, with the same in-process / per-replica caveat below
(effective tenant limit ≈ ``limit * replica_count`` in v1.x; the
``RedisRateLimiter`` seam is the shared-state future for both).

Algorithm
---------

Bucket per key with two fields: ``tokens`` (float; allow fractional
refill so the math is monotonic) and ``last_refill_at``. On each
``check``:

1. Elapsed since ``last_refill_at`` → refill_rate * elapsed tokens
   added, capped at ``capacity``.
2. If ``tokens >= 1``: decrement by 1, allowed=True.
3. Else: allowed=False, ``retry_after = (1 - tokens) / refill_rate``.

Bucket state is never persisted; on process restart, every client
gets a fresh full bucket (the operator's grace gift).

Headers (RFC-ish)
-----------------

Every authenticated response carries:

* ``X-RateLimit-Limit`` — bucket capacity
* ``X-RateLimit-Remaining`` — tokens left (integer floor)
* ``X-RateLimit-Reset`` — Unix timestamp when bucket will be full

429 responses additionally carry:

* ``Retry-After`` — seconds until the next request would be allowed
  (integer ceiling). RFC 7231-compliant; most HTTP clients honor it
  for automatic retry.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of a ``RateLimiter.check`` call.

    Used by middleware to decide whether to allow the request (and
    what headers to attach) or short-circuit with a 429.
    """

    allowed: bool
    limit: int
    """Bucket capacity. Goes on ``X-RateLimit-Limit``."""
    remaining: int
    """Tokens left after this check (integer floor of the float
    state). Goes on ``X-RateLimit-Remaining``. Always >= 0."""
    reset_at_unix: int
    """Unix timestamp when the bucket will be full again. Goes on
    ``X-RateLimit-Reset``. Stable across "allowed" and "denied"
    decisions so clients can plan."""
    retry_after_seconds: int | None = None
    """``None`` when allowed=True; integer ceiling of the wait
    until the next request would succeed when allowed=False.
    Goes on ``Retry-After`` for 429 responses."""


@dataclass
class _BucketState:
    """Internal per-key state. Mutated under the assumption that
    ``check`` doesn't await between read and write (single-threaded
    event loop semantics in uvicorn)."""

    tokens: float
    last_refill_at: float  # monotonic seconds; not wall-clock

    @classmethod
    def fresh(cls, *, capacity: int, now: float) -> _BucketState:
        return cls(tokens=float(capacity), last_refill_at=now)


class RateLimiter(Protocol):
    """Anything that can answer "is this key allowed to make a request?"

    Implementations:

    * :class:`InProcessRateLimiter` — token bucket, dict-backed,
      single-process. Default for v1.x.
    * Future ``RedisRateLimiter`` — same interface, INCR + EXPIRE
      atomics in Redis. Lands when multi-replica state-sharing
      becomes load-bearing.
    """

    async def check(self, key: str) -> RateLimitDecision:
        """Atomically: refill the bucket, attempt to spend a token,
        return the decision. Never raises — a malfunctioning rate
        limiter must fail-open (allow the request) rather than 503
        every endpoint."""


class InProcessRateLimiter:
    """Token bucket per key, dict-backed.

    ``limit_per_minute`` is the bucket capacity (and the steady-state
    request rate, since refill_rate is ``limit / 60``). Default 60 →
    60 req/min steady; up to 60 req in a burst after idle.

    Memory grows linearly with the number of distinct keys; for
    production, plug a Redis backend (post-v1.x). For dev / staging /
    single-tenant prod, in-process is fine — a few thousand keys is
    tens of KB of state.
    """

    name = "in-process"

    def __init__(self, *, limit_per_minute: int = 60) -> None:
        if limit_per_minute < 1:
            raise ValueError(
                f"limit_per_minute must be >= 1, got {limit_per_minute}. "
                "Use a high value like 100000 to effectively disable rate limiting."
            )
        self._capacity = limit_per_minute
        # tokens per second. 60/min → 1/s; 600/min → 10/s.
        self._refill_rate = limit_per_minute / 60.0
        self._buckets: dict[str, _BucketState] = {}

    async def check(self, key: str) -> RateLimitDecision:
        # ``time.monotonic`` because rate-limit windows shouldn't
        # break on NTP corrections / system clock jumps.
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _BucketState.fresh(capacity=self._capacity, now=now)
            self._buckets[key] = bucket

        # Refill: add (now - last_refill) * refill_rate tokens.
        elapsed = max(0.0, now - bucket.last_refill_at)
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_rate)
        bucket.last_refill_at = now

        # ``reset_at`` = when the bucket will be full again. Wall-clock
        # timestamp for the header (clients expect a real timestamp,
        # not a monotonic value).
        seconds_until_full = (
            (self._capacity - bucket.tokens) / self._refill_rate if self._refill_rate > 0 else 0
        )
        reset_at_unix = int(time.time() + seconds_until_full)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return RateLimitDecision(
                allowed=True,
                limit=self._capacity,
                remaining=int(bucket.tokens),
                reset_at_unix=reset_at_unix,
            )

        # Denied. ``retry_after`` is the wait until tokens >= 1.
        retry_after = (1.0 - bucket.tokens) / self._refill_rate
        return RateLimitDecision(
            allowed=False,
            limit=self._capacity,
            remaining=0,
            reset_at_unix=reset_at_unix,
            # Integer ceiling — RFC 7231 says Retry-After is in seconds.
            # Ceiling so a sub-second wait still tells the client to
            # back off at least 1s.
            retry_after_seconds=max(1, math.ceil(retry_after)),
        )


@dataclass
class NoOpRateLimiter:
    """Always-allow rate limiter. Used when rate limiting is disabled
    (operator sets ``limit_per_minute=0`` or skips wiring entirely).
    The middleware path still calls ``check``; this just always
    returns ``allowed=True`` with a sentinel limit value so headers
    stay coherent."""

    name: str = "noop"
    _sentinel_limit: int = field(default=0, init=False)

    async def check(self, key: str) -> RateLimitDecision:
        return RateLimitDecision(
            allowed=True,
            limit=self._sentinel_limit,
            remaining=self._sentinel_limit,
            reset_at_unix=int(time.time()),
        )


__all__ = [
    "InProcessRateLimiter",
    "NoOpRateLimiter",
    "RateLimitDecision",
    "RateLimiter",
]
