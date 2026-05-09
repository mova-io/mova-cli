"""Async retry executor honoring the typed failure taxonomy.

The provider layer raises typed :class:`MovateError` subclasses. Each
failure type has its own :class:`RetryRule` (see ``DEFAULT_RETRY``).
After exhaustion the rule's ``fallback_on_exhaust`` flag tells the executor
whether to walk the model fallback chain.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from movate.core.failures import DEFAULT_RETRY, FailureType, MovateError, RateLimitError, RetryRule

T = TypeVar("T")
RetryPolicy = dict[FailureType, RetryRule]


class RetryExhaustedError(Exception):
    """Raised when all retries for a failure type have been consumed."""

    def __init__(self, last_error: MovateError, attempts: int) -> None:
        super().__init__(f"retries exhausted after {attempts} attempt(s): {last_error}")
        self.last_error = last_error
        self.attempts = attempts


async def run_with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
) -> T:
    """Invoke ``fn`` with retry semantics keyed on the raised ``MovateError`` type."""
    rules: RetryPolicy = policy or DEFAULT_RETRY
    last_error: MovateError | None = None
    attempt_counts: dict[FailureType, int] = {}

    overall_cap = max((r.max_attempts for r in rules.values()), default=1)

    for _ in range(overall_cap):
        try:
            return await fn()
        except MovateError as err:
            last_error = err
            ftype = err.failure_type
            rule: RetryRule = rules.get(ftype, RetryRule(1, (), False))
            attempt_counts[ftype] = attempt_counts.get(ftype, 0) + 1

            if attempt_counts[ftype] >= rule.max_attempts:
                raise RetryExhaustedError(err, attempt_counts[ftype]) from err

            backoff_idx = attempt_counts[ftype] - 1
            if isinstance(err, RateLimitError) and err.retry_after is not None:
                delay = err.retry_after
            elif backoff_idx < len(rule.backoff_seconds):
                delay = rule.backoff_seconds[backoff_idx]
            else:
                delay = rule.backoff_seconds[-1] if rule.backoff_seconds else 0.0

            if delay > 0:
                await asyncio.sleep(delay)

    assert last_error is not None
    raise RetryExhaustedError(last_error, overall_cap)
