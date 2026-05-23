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


class ErrorBody(BaseModel):
    """Inner payload of every error response."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str


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
    """
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message or code.value.replace("_", " "),
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
) -> HTTPException:
    """429 with ``Retry-After`` + the same ``X-RateLimit-*`` headers as
    successful responses, so the client can recover programmatically.

    RFC 7231 §7.1.3: ``Retry-After`` is either a delta-seconds or a
    HTTP-date. We send the delta-seconds form because it's friendlier
    to clients that don't keep accurate wall clocks.
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
    exc.headers = {
        "Retry-After": str(retry_after_seconds),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(reset_at_unix),
    }
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
