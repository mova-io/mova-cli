"""Package-local voice failure taxonomy + retry rules (ADR 068 D3).

The resilient router (:mod:`movate.voice.failover`) needs to decide, on a provider
error, whether to **retry** the same provider and whether to **fail over** to the
next one. That decision keys off a small failure taxonomy.

This deliberately **mirrors the shape** of mdk's ``core/failures.py`` /
``core/retry.py`` but imports **nothing** from mdk — re-coupling to mdk would
break the standalone promise (ADR 067). A contract test in mdk can assert the
two classifiers agree on the same provider errors.

Four types, because that is all the router needs to distinguish:

* ``TIMEOUT`` / ``UNAVAILABLE`` — transient; retry a little, then fail over.
* ``RATE_LIMIT`` — transient but back off harder before retrying; then fail over.
* ``AUTH`` — **terminal**: a credentials bug. Never retry, and never fail over to
  a *paid* tier on a bad key (ADR 068 D3) — surface it so it gets fixed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum


class VoiceFailureType(StrEnum):
    """How a provider failed, to the precision the router acts on."""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    AUTH = "auth"


class VoiceProviderError(Exception):
    """A provider failure an adapter can raise to classify itself explicitly.

    Adapters are not required to raise this — :func:`classify` maps arbitrary
    exceptions heuristically — but raising it removes the guesswork (e.g. an
    adapter that knows it got an HTTP 429 can raise
    ``VoiceProviderError("...", failure_type=RATE_LIMIT)``).
    """

    def __init__(
        self,
        message: str,
        *,
        failure_type: VoiceFailureType = VoiceFailureType.UNAVAILABLE,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.provider = provider


@dataclass(frozen=True)
class RetryRule:
    """How to handle one failure type within a single provider.

    * ``max_attempts`` — total tries against the provider (1 = no retry).
    * ``backoff`` — seconds to wait before attempt *n* (index ``n-2``); shorter
      than ``max_attempts-1`` reuses the last value.
    * ``failover`` — after attempts are exhausted, may the router try the **next**
      provider? ``False`` makes the failure terminal (``AUTH``).
    """

    max_attempts: int
    backoff: tuple[float, ...]
    failover: bool


# Latency-first/cost-bounded posture (ADR 068 D2): transient errors retry briefly
# then fail over to the next tier; an auth error is terminal.
DEFAULT_RETRY: dict[VoiceFailureType, RetryRule] = {
    VoiceFailureType.TIMEOUT: RetryRule(2, (0.0,), failover=True),
    VoiceFailureType.RATE_LIMIT: RetryRule(3, (0.2, 0.8), failover=True),
    VoiceFailureType.UNAVAILABLE: RetryRule(2, (0.1,), failover=True),
    VoiceFailureType.AUTH: RetryRule(1, (), failover=False),
}


def classify(exc: Exception) -> VoiceFailureType:
    """Map any exception to a :class:`VoiceFailureType` (heuristic, conservative).

    An explicit :class:`VoiceProviderError` wins; otherwise timeouts are detected
    by type and rate-limit / auth by the usual markers in the class name or
    message. Anything unrecognized is ``UNAVAILABLE`` — transient and
    fail-over-able, the safe default (we'd rather try another provider than
    wrongly treat a blip as terminal).
    """
    if isinstance(exc, VoiceProviderError):
        return exc.failure_type
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return VoiceFailureType.TIMEOUT
    name = exc.__class__.__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name or "rate limit" in msg or "429" in msg or "quota" in msg:
        return VoiceFailureType.RATE_LIMIT
    if (
        "auth" in name
        or "unauthorized" in name
        or "unauthorized" in msg
        or "api key" in msg
        or "api_key" in msg
        or "401" in msg
        or "403" in msg
    ):
        return VoiceFailureType.AUTH
    return VoiceFailureType.UNAVAILABLE
