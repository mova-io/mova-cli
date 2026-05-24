"""Job-level retry policy — re-queue transient failures, dead-letter the rest.

Distinct from :mod:`movate.core.retry` (which is the **in-process**
retry policy for a single provider call). This module decides what
happens to a JOB after it lands in a terminal-but-retryable state —
re-queue with backoff, or dead-letter if the retry budget is
exhausted.

Decision tree (input: outcome's ``error.retryable`` flag +
``job.attempt_count``):

* ``retryable=False`` → terminal (``ERROR`` or ``SAFETY_BLOCKED``,
  whatever the dispatch outcome already said). No retry.
* ``retryable=True`` and ``attempt_count + 1 < max_attempts`` →
  re-queue with ``next_retry_at = now + backoff(attempt_count + 1)``.
* ``retryable=True`` and ``attempt_count + 1 >= max_attempts`` →
  ``DEAD_LETTER``. Operators triage with
  ``movate jobs list --status dead_letter``.

The retryable flag itself comes from the failure taxonomy in
:mod:`movate.core.failures` and is set per-call-site in
``runtime/dispatch.py`` (e.g. unknown agent → not retryable;
unhandled executor exception → retryable). The Executor also
stamps it on every ``ErrorInfo`` it produces from a typed
``MovateError``.

Backoff: exponential with jitter, capped at 5 minutes. Attempt 1
waits ~5s, attempt 2 ~15s, attempt 3 ~45s, attempt 4 ~135s, attempt
5+ capped at 300s. Jitter is ±25% to avoid thundering-herd retries
across many parallel transient failures.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class JobRetryPolicy:
    """How the worker decides retry vs dead-letter.

    Defaults are conservative — 3 attempts for transient failures is
    enough to ride out a brief provider blip without retrying
    forever on a persistent issue.
    """

    max_attempts: int = 3
    """Total attempts including the first dispatch. ``max_attempts=3``
    means initial + 2 retries. Set to 1 to disable retries entirely
    (every retryable error goes straight to ``DEAD_LETTER``)."""

    base_seconds: float = 5.0
    """First retry delay before exponential growth. Subsequent
    retries multiply by ``factor``."""

    factor: float = 3.0
    """Exponential growth between retries. With base=5 and factor=3:
    5s, 15s, 45s, 135s, ... up to the cap."""

    cap_seconds: float = 300.0
    """Maximum delay between retries (5 minutes). After this, every
    retry waits exactly the cap. Prevents pathological "retry in 2
    hours" scenarios on very high attempt counts."""

    jitter: float = 0.25
    """Multiplicative jitter ± this fraction. Default 25% spreads
    re-queue times so 100 concurrent transient failures don't all
    re-attempt at the same instant."""


DEFAULT_POLICY = JobRetryPolicy()
"""Module-level default. Override at the worker layer if needed."""


DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 900.0
"""Default visibility timeout for the stale-job reaper (15 minutes).

A job that's been ``RUNNING`` with ``claimed_at`` older than this is
treated as orphaned (worker OOM/SIGKILL/node loss) and reclaimed. This
MUST be generously larger than the longest expected job: a value smaller
than a still-running job's runtime would reclaim a healthy in-flight job
and cause at-least-once double-execution. The worker loop and the
scheduler-tick backstop both default to this single source of truth."""


@dataclass(frozen=True)
class ReclaimResult:
    """Outcome of one :meth:`StorageProvider.reclaim_stale_jobs` sweep.

    ``requeued`` is the count of orphaned ``RUNNING`` jobs flipped back
    to ``QUEUED`` (retry budget remained); ``dead_lettered`` is the count
    that exhausted their budget and landed in ``DEAD_LETTER`` instead.
    Both are zero on an idle sweep (nothing stale)."""

    requeued: int
    dead_lettered: int


def should_retry(
    *, retryable: bool, attempt_count: int, policy: JobRetryPolicy = DEFAULT_POLICY
) -> bool:
    """Decide whether a failed job should re-queue or land terminal.

    ``attempt_count`` is the count BEFORE this attempt (i.e. the
    value stored on the JobRecord at claim time). After the dispatch
    fails, we increment by 1 to get "attempts so far"; if that's
    still under ``max_attempts``, we re-queue.

    Returns True for re-queue, False for terminal. ``False`` covers
    both the "not retryable" and "exhausted" cases; the worker
    distinguishes them by the original outcome status (``ERROR``
    stays ``ERROR``; retryable-exhausted lands in ``DEAD_LETTER``).
    """
    if not retryable:
        return False
    return (attempt_count + 1) < policy.max_attempts


def compute_next_retry_at(
    *,
    attempt_count: int,
    policy: JobRetryPolicy = DEFAULT_POLICY,
    now: datetime | None = None,
) -> datetime:
    """When should the next attempt fire?

    ``attempt_count`` is the count AFTER this failure (i.e. we just
    incremented it). attempt_count=1 → first retry delay
    (``base_seconds``); attempt_count=2 → factor * base, etc.

    The ``now`` parameter is for testability — pass a fixed datetime
    to assert exact retry times in tests.
    """
    base_now = now if now is not None else datetime.now(UTC)
    # attempt_count starts at 1 for the first retry; n=0 is "no retry needed."
    # The exponent is attempt_count - 1 so the first retry is exactly
    # base_seconds (no exponential factor applied yet).
    delay_seconds = policy.base_seconds * (policy.factor ** (attempt_count - 1))
    delay_seconds = min(delay_seconds, policy.cap_seconds)

    # Apply jitter. random.uniform is fine for spread — we don't need
    # crypto randomness here.
    if policy.jitter > 0:
        jitter_amount = delay_seconds * policy.jitter
        delay_seconds += random.uniform(-jitter_amount, jitter_amount)
        # Floor at 0 — a negative jitter on a tiny base shouldn't put
        # us in the past, which would defeat the purpose.
        delay_seconds = max(0.0, delay_seconds)

    return base_now + timedelta(seconds=delay_seconds)


def is_exhausted(*, attempt_count: int, policy: JobRetryPolicy = DEFAULT_POLICY) -> bool:
    """``True`` if this job has hit its retry budget — caller should
    transition to ``DEAD_LETTER`` instead of re-queueing.

    ``attempt_count`` is the BEFORE-increment value (matches what's
    on the JobRecord at claim time). Used together with
    ``should_retry`` to distinguish "retryable but exhausted" from
    "not retryable at all" — both produce False from ``should_retry``,
    but only the former lands in ``DEAD_LETTER``.
    """
    return (attempt_count + 1) >= policy.max_attempts


__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_VISIBILITY_TIMEOUT_SECONDS",
    "JobRetryPolicy",
    "ReclaimResult",
    "compute_next_retry_at",
    "is_exhausted",
    "should_retry",
]
