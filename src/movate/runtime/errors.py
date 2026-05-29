"""Centralized error response shape + exception → HTTP mapping.

One JSON shape for every error so consumers don't have to special-case
auth vs validation vs not-found:

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Codes are stable enums (``AUTH_REQUIRED``, ``NOT_FOUND``, etc.) — the
``message`` is human-readable and may change between releases, but
the ``code`` is contract.

Auth failures intentionally return a single ``AUTH_REQUIRED`` regardless
of why (missing header, malformed token, revoked, wrong tenant). Leaking
the discriminator to the caller would create a timing-attack oracle.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "auth_required"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    CONFLICT = "conflict"
    INTERNAL = "internal"
    RATE_LIMITED = "rate_limited"
    # ADR 033 D6 — request body exceeded the configured size limit (413).
    # Additive: a NEW stable code, distinct from BAD_REQUEST so a client can
    # special-case "shrink the payload" without string-matching the message.
    PAYLOAD_TOO_LARGE = "payload_too_large"
    # ADR 036 D2 — per-tenant aggregate quota (tokens / requests / cost over
    # a billing window) was hit; the request is refused with 429. Distinct
    # from ``RATE_LIMITED`` (burst requests/sec) so a client can special-case
    # "you've used your daily / monthly budget" vs "slow down".
    QUOTA_EXCEEDED = "quota_exceeded"


class ErrorBody(BaseModel):
    """Inner payload of every error response.

    ``request_id`` (ADR 033 D2) is the per-request correlation id, set to the
    same value as the ``X-Request-Id`` response header so a caller can quote
    one id when reporting a 4xx/5xx. It's ``None`` only when an error is built
    entirely outside a request scope (e.g. a unit test calling a helper
    directly); inside the runtime the request-id middleware always has a value
    bound. Additive + optional → existing consumers that read only ``code`` /
    ``message`` are unaffected."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """Outer envelope. The single field makes future expansion (e.g.
    ``request_id`` for tracing) non-breaking on the wire."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


def http_error(
    code: ErrorCode,
    *,
    status_code: int,
    message: str | None = None,
) -> HTTPException:
    """Build an ``HTTPException`` whose ``detail`` matches our envelope.

    Default ``message`` is the code's human form; pass ``message`` to
    override (e.g. ``message="job 'xyz' not found"``). Auth-related
    callers should NEVER pass a discriminating message.

    ``error.request_id`` (ADR 033 D2) is stamped from the active request-id
    context (set by ``RequestIdMiddleware``), so the body's id matches the
    ``X-Request-Id`` response header for the same request. Outside a request
    scope the context default is ``""`` → serialized as ``None`` so the field
    stays cleanly absent of a fake id.
    """
    # Read lazily to avoid a hard import cycle at module import time and to
    # keep this stdlib-only module decoupled from the contextvar machinery's
    # eager init.
    from movate.runtime.request_context import get_request_id  # noqa: PLC0415

    request_id = get_request_id() or None
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message or code.value.replace("_", " "),
            request_id=request_id,
        )
    )
    return HTTPException(
        status_code=status_code,
        detail=body.model_dump(mode="json"),
    )


def auth_required() -> HTTPException:
    """Single-shape 401 for every auth failure mode."""
    return http_error(
        ErrorCode.AUTH_REQUIRED,
        status_code=status.HTTP_401_UNAUTHORIZED,
        message="authentication required",
    )


def forbidden(message: str = "admin scope required") -> HTTPException:
    """403 for callers that are authenticated but lack the required scope."""
    return http_error(
        ErrorCode.FORBIDDEN,
        status_code=status.HTTP_403_FORBIDDEN,
        message=message,
    )


def not_found(resource: str, identifier: str) -> HTTPException:
    """404 with a narrowly-scoped message — safe to include the id since
    the caller already knew it."""
    return http_error(
        ErrorCode.NOT_FOUND,
        status_code=status.HTTP_404_NOT_FOUND,
        message=f"{resource} {identifier!r} not found",
    )


def conflict(message: str = "version conflict") -> HTTPException:
    """409 for an optimistic-concurrency mismatch (ADR 014 D3).

    Raised by ``PUT /api/v1/agents/{name}`` when the caller sends an
    ``If-Match`` precondition (the version or content_hash it believes
    is current) that no longer matches the registry's latest version —
    someone else published in between. The message is safe to be
    specific: the caller is authenticated and already knows the agent
    name, so naming the stale-vs-current versions helps the client
    re-fetch and retry rather than silently clobbering a teammate's
    write. Absent ``If-Match`` this is never raised (last-write-wins
    back-compat).
    """
    return http_error(
        ErrorCode.CONFLICT,
        status_code=status.HTTP_409_CONFLICT,
        message=message,
    )


def rate_limited(
    *,
    retry_after_seconds: int,
    limit: int,
    reset_at_unix: int,
    tenant_headers: dict[str, str] | None = None,
) -> HTTPException:
    """429 with ``Retry-After`` + the same ``X-RateLimit-*`` headers as
    successful responses, so the client can recover programmatically.

    RFC 7231 §7.1.3: ``Retry-After`` is either a delta-seconds or a
    HTTP-date. We send the delta-seconds form because it's friendlier
    to clients that don't keep accurate wall clocks.

    ``limit`` / ``reset_at_unix`` / ``X-RateLimit-Remaining: 0`` describe
    the **per-API-key** ceiling (unchanged contract). ``retry_after_seconds``
    is the wait the caller should honor — when the per-tenant aggregate cap
    (item 25) is also in play it's the *max* of the per-key and per-tenant
    retry-afters, so a single back-off clears whichever ceiling is binding.

    ``tenant_headers`` (additive, default ``None`` → today's exact header
    set) carries the tenant-scoped budget snapshot
    (``X-RateLimit-Tenant-Limit`` / ``-Remaining`` / ``-Reset``) so a
    client can tell *which* ceiling it hit. The per-key header names and
    semantics are never mutated.
    """
    exc = http_error(
        ErrorCode.RATE_LIMITED,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        message=f"rate limit exceeded (limit={limit}/min); retry after {retry_after_seconds}s",
    )
    # FastAPI's HTTPException exposes ``headers`` as a mutable dict-like
    # we can stamp before raising. The middleware attaches the same
    # rate-limit headers to the 200 path; doing it here for 429 keeps
    # the client-side handling symmetric.
    headers = {
        "Retry-After": str(retry_after_seconds),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(reset_at_unix),
    }
    if tenant_headers:
        headers.update(tenant_headers)
    exc.headers = headers
    return exc


__all__ = [
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "auth_required",
    "conflict",
    "forbidden",
    "http_error",
    "not_found",
    "rate_limited",
]
