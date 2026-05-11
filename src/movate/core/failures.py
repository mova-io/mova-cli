"""Typed failure taxonomy + default retry policy.

Mirrors the locked decisions: rate_limit / timeout / tool_error retry; schema /
context_length / hallucination do not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureType(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TOOL_ERROR = "tool_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    SCHEMA_ERROR = "schema_error"
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    AUTH_ERROR = "auth_error"
    CONTENT_FILTER = "content_filter"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    POLICY_VIOLATION = "policy_violation"
    TENANT_BUDGET_EXCEEDED = "tenant_budget_exceeded"


@dataclass(frozen=True)
class RetryRule:
    max_attempts: int  # total attempts including the first try; 1 = no retry
    backoff_seconds: tuple[float, ...]
    fallback_on_exhaust: bool


DEFAULT_RETRY: dict[FailureType, RetryRule] = {
    FailureType.TIMEOUT: RetryRule(2, (0.0,), fallback_on_exhaust=False),
    FailureType.RATE_LIMIT: RetryRule(4, (1.0, 4.0, 16.0), fallback_on_exhaust=False),
    FailureType.TOOL_ERROR: RetryRule(2, (0.5,), fallback_on_exhaust=False),
    FailureType.MODEL_UNAVAILABLE: RetryRule(3, (1.0, 4.0), fallback_on_exhaust=True),
    FailureType.SCHEMA_ERROR: RetryRule(1, (), fallback_on_exhaust=False),
    FailureType.CONTEXT_LENGTH_EXCEEDED: RetryRule(1, (), fallback_on_exhaust=False),
    FailureType.AUTH_ERROR: RetryRule(1, (), fallback_on_exhaust=False),
    FailureType.CONTENT_FILTER: RetryRule(1, (), fallback_on_exhaust=False),
    FailureType.COST_BUDGET_EXCEEDED: RetryRule(1, (), fallback_on_exhaust=False),
    # Policy violations are deterministic — a model that's denied stays
    # denied. No retry, no fallback (the fallback chain itself is
    # policy-checked, so falling back to another denied model would
    # just hit the same wall).
    FailureType.POLICY_VIOLATION: RetryRule(1, (), fallback_on_exhaust=False),
    # Tenant-budget breaches don't recover by retrying or falling back
    # to a cheaper model — the tenant's monthly cap is the cap. The
    # operator un-pauses by raising the budget or waiting for month
    # rollover. No retry, no fallback.
    FailureType.TENANT_BUDGET_EXCEEDED: RetryRule(1, (), fallback_on_exhaust=False),
}


class MovateError(Exception):
    """Base class for typed runtime failures."""

    failure_type: FailureType
    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class MovateTimeoutError(MovateError):
    failure_type = FailureType.TIMEOUT
    retryable = True


class RateLimitError(MovateError):
    failure_type = FailureType.RATE_LIMIT
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ToolError(MovateError):
    failure_type = FailureType.TOOL_ERROR
    retryable = True


class ModelUnavailableError(MovateError):
    failure_type = FailureType.MODEL_UNAVAILABLE
    retryable = True


class SchemaError(MovateError):
    failure_type = FailureType.SCHEMA_ERROR
    retryable = False


class ContextLengthError(MovateError):
    failure_type = FailureType.CONTEXT_LENGTH_EXCEEDED
    retryable = False


class AuthError(MovateError):
    failure_type = FailureType.AUTH_ERROR
    retryable = False


class ContentFilterError(MovateError):
    failure_type = FailureType.CONTENT_FILTER
    retryable = False


class BudgetExceededError(MovateError):
    failure_type = FailureType.COST_BUDGET_EXCEEDED
    retryable = False


class PolicyViolationError(MovateError):
    """An ``agent.yaml`` or runtime model choice violates the project's
    ``policy:`` block (e.g. provider not in ``allowed_providers``, model
    in ``deny_models``, budget above ``max_cost_per_run_usd`` ceiling).

    Caught at two layers:

    * ``movate validate <agent>`` — static check; fails fast at parse time
      with the operator pointer ``See movate.yaml: policy``.
    * ``Executor.execute()`` entry — runtime check; catches a runtime-loaded
      bundle (e.g. one served via ``movate serve``) that skipped validate.
    """

    failure_type = FailureType.POLICY_VIOLATION
    retryable = False


class TenantBudgetExceededError(MovateError):
    """A tenant has spent past its monthly budget; runs are paused.

    Raised at :meth:`Executor.execute` entry when the tenant's
    current-month cost (summed across all ``RunRecord.metrics.cost_usd``)
    meets or exceeds its ``TenantBudget.monthly_usd_limit``.

    Operator un-pauses by either:

    * Raising the budget — ``movate tenants set-budget <id>
      --monthly-usd <new>``.
    * Waiting for the next calendar month — the next request after
      00:00 UTC on the 1st sees fresh spend = 0.
    * Clearing the budget — ``movate tenants clear-budget <id>``
      removes the limit (effectively "unlimited").
    """

    failure_type = FailureType.TENANT_BUDGET_EXCEEDED
    retryable = False
