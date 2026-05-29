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

Self-teaching envelope (additive)
---------------------------------

On top of the three contract fields (``code`` / ``message`` /
``request_id``), the body MAY carry OPTIONAL self-teaching fields so a
caller (or an LLM agent driving the API) can recover without grepping the
docs:

* ``docs_url`` — a stable link to the relevant runbook/doc for the code.
* ``fix_hint`` — a one-line, actionable remediation.
* ``retriable`` — whether retrying the *same* request can succeed.
* ``retry_after_ms`` — how long to wait before retrying, when known.

These are populated from :data:`ERROR_HINTS`, a single registry keyed by
the string code so the hints stay consistent + maintainable rather than
scattered across call sites. They are **strictly additive**: the three
original fields keep their exact names, types, and positions, and an
unknown code simply omits the optional fields (the envelope stays valid).
Nothing sensitive is ever placed here — hints are generic remediation
text, never keys/tokens/PII.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple, TypedDict

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
    ``message`` are unaffected.

    The trailing four fields (``docs_url`` / ``fix_hint`` / ``retriable`` /
    ``retry_after_ms``) are the **self-teaching** additions. All OPTIONAL with
    a ``None`` default and serialized only when set, so the wire shape for an
    existing consumer is byte-identical when a code has no registered hint. The
    three contract fields above keep their exact names, types, and ordering.
    """

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    request_id: str | None = None
    # --- Self-teaching additions (all OPTIONAL, default-None, additive) -----
    docs_url: str | None = None
    """Stable link to the runbook/doc for this ``code``, when one exists."""
    fix_hint: str | None = None
    """One-line, actionable remediation (never sensitive)."""
    retriable: bool | None = None
    """Whether retrying the SAME request can plausibly succeed. ``None`` =
    unknown (no registered hint) — distinct from ``False`` (= definitively
    not worth retrying)."""
    retry_after_ms: int | None = None
    """Suggested wait before retrying, in milliseconds — set only when the
    error is retriable AND the delay is known (e.g. a 429's ``Retry-After``).
    Omitted otherwise."""


class ErrorResponse(BaseModel):
    """Outer envelope. The single field makes future expansion (e.g.
    ``request_id`` for tracing) non-breaking on the wire."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


# ----------------------------------------------------------------------
# Self-teaching hint registry (code → docs_url / fix_hint / retriable).
#
# A SINGLE source of truth so the optional self-teaching fields are
# consistent + maintainable rather than scattered across call sites. Keyed
# by the *string* code (``ErrorCode.value`` for enum errors; the bare
# string for the few custom-handler codes — ``already_exists`` etc.) so one
# registry covers every error path. A code absent from the table simply
# yields no optional fields → the envelope stays valid (graceful unknown).
#
# ``retry_after_ms`` is NOT in the registry: it's per-occurrence (e.g. a
# 429's actual ``Retry-After``), so callers thread it in explicitly.
# ----------------------------------------------------------------------

# Doc anchor base. Stable, version-free path so a link doesn't rot across
# CalVer releases; the fragment is the code so it deep-links to the section.
_DOCS_BASE = "https://movate.dev/docs/errors"


class ErrorHint(NamedTuple):
    """Static remediation metadata for one error ``code``."""

    fix_hint: str
    retriable: bool
    docs_url: str


class _SelfTeachingFields(TypedDict, total=False):
    """The OPTIONAL self-teaching fields, all absence-allowed (``total=False``).

    Typed so ``**enrich_error_fields(...)`` unpacks cleanly into
    :class:`ErrorBody` without a blanket ``dict[str, object]`` widening that
    mypy can't reconcile with the model's per-field types."""

    docs_url: str
    fix_hint: str
    retriable: bool
    retry_after_ms: int


def _hint(code: str, fix_hint: str, *, retriable: bool) -> tuple[str, ErrorHint]:
    return code, ErrorHint(fix_hint=fix_hint, retriable=retriable, docs_url=f"{_DOCS_BASE}#{code}")


ERROR_HINTS: dict[str, ErrorHint] = dict(
    [
        _hint(
            ErrorCode.AUTH_REQUIRED.value,
            "Send a valid bearer token: `Authorization: Bearer <key>`. "
            "Mint one with `mdk auth create`.",
            retriable=False,
        ),
        _hint(
            ErrorCode.FORBIDDEN.value,
            "Your key is missing a required scope; mint a key with it via "
            "`mdk auth create --scope <scope>`.",
            retriable=False,
        ),
        _hint(
            ErrorCode.NOT_FOUND.value,
            "Check the id/name and that it belongs to your tenant; "
            "list resources with the corresponding `mdk ... list`.",
            retriable=False,
        ),
        _hint(
            ErrorCode.BAD_REQUEST.value,
            "The request was malformed; fix the body/params per the message and resend.",
            retriable=False,
        ),
        _hint(
            ErrorCode.CONFLICT.value,
            "Another write landed first; re-fetch the resource (read its `ETag`) "
            "and retry with an updated `If-Match`.",
            retriable=True,
        ),
        _hint(
            ErrorCode.INTERNAL.value,
            "Transient server error; retry with backoff. If it persists, quote the "
            "`request_id` when reporting it.",
            retriable=True,
        ),
        _hint(
            ErrorCode.RATE_LIMITED.value,
            "You hit a rate limit; wait for `Retry-After` seconds before retrying, "
            "or request a higher quota.",
            retriable=True,
        ),
        _hint(
            ErrorCode.QUOTA_EXCEEDED.value,
            "Your tenant's usage quota for this window is exhausted; wait for the "
            "window to reset (see `Retry-After`) or request a higher quota.",
            retriable=True,
        ),
        _hint(
            ErrorCode.PAYLOAD_TOO_LARGE.value,
            "Shrink the request body below the limit named in the message "
            "(or raise `MDK_MAX_REQUEST_BYTES` on the runtime).",
            retriable=False,
        ),
        # Custom-handler codes (agent/skill creation) — string codes that are
        # NOT in ``ErrorCode`` but flow through the same registry.
        _hint(
            "already_exists",
            "A resource with that name already exists; pick a new name or "
            "use the replace/PUT path to overwrite.",
            retriable=False,
        ),
        _hint(
            "invalid_bundle",
            "The uploaded bundle failed validation; fix the issue named in the "
            "message and re-upload.",
            retriable=False,
        ),
        _hint(
            "upstream_unavailable",
            "An upstream dependency was unavailable; retry with backoff.",
            retriable=True,
        ),
        _hint(
            "agent_persistence_unavailable",
            "The agent registry/storage was unavailable; retry shortly.",
            retriable=True,
        ),
    ]
)


def enrich_error_fields(
    code: str,
    *,
    retry_after_ms: int | None = None,
) -> _SelfTeachingFields:
    """Resolve the OPTIONAL self-teaching fields for a string ``code``.

    Returns a mapping of ONLY the fields that are known — so an unknown code
    (no registry entry, no ``retry_after_ms``) yields ``{}`` and the envelope
    is byte-identical to the pre-self-teaching shape (graceful-unknown
    contract).

    ``retry_after_ms`` is threaded in per-occurrence (e.g. a 429's real wait);
    it's emitted only when both provided AND the code is retriable, so we never
    advertise a retry delay for a non-retriable error.
    """
    fields: _SelfTeachingFields = {}
    hint = ERROR_HINTS.get(code)
    if hint is not None:
        fields["docs_url"] = hint.docs_url
        fields["fix_hint"] = hint.fix_hint
        fields["retriable"] = hint.retriable
    if retry_after_ms is not None and (hint is None or hint.retriable):
        fields["retry_after_ms"] = retry_after_ms
    return fields


def http_error(
    code: ErrorCode,
    *,
    status_code: int,
    message: str | None = None,
    retry_after_ms: int | None = None,
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

    The OPTIONAL self-teaching fields (``docs_url`` / ``fix_hint`` /
    ``retriable`` / ``retry_after_ms``) are resolved from :data:`ERROR_HINTS`
    via :func:`enrich_error_fields` and emitted only when known — additive, so
    a code without a registered hint produces today's exact three-field body.
    ``retry_after_ms`` lets a caller (e.g. ``rate_limited``) pass the
    occurrence-specific wait.
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
            **enrich_error_fields(code.value, retry_after_ms=retry_after_ms),
        )
    )
    return HTTPException(
        status_code=status_code,
        detail=_serialize(body),
    )


# The exact set of fields the self-teaching extension added. We drop ONLY
# these when unset so a code without a registered hint produces the
# byte-identical pre-extension body — while the three contract fields
# (``code`` / ``message`` / ``request_id``) are ALWAYS present, including
# ``request_id: null`` outside a request scope (its historical behavior).
_SELF_TEACHING_FIELDS = ("docs_url", "fix_hint", "retriable", "retry_after_ms")


def _serialize(body: ErrorResponse) -> dict[str, object]:
    """Dump the envelope, omitting only the NEW optional fields when unset.

    We can't blanket ``exclude_none`` — that would also drop ``request_id``
    when it's ``None``, regressing its long-standing always-present (possibly
    ``null``) wire behavior. So we dump fully, then strip just the
    self-teaching keys that are ``None``."""
    dumped = body.model_dump(mode="json")
    inner = dumped["error"]
    for field in _SELF_TEACHING_FIELDS:
        if inner.get(field) is None:
            inner.pop(field, None)
    return dumped


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
        # Self-teaching: thread the occurrence-specific wait into the envelope
        # (milliseconds) so a programmatic client can back off off the BODY,
        # mirroring the ``Retry-After`` header (seconds) below.
        retry_after_ms=retry_after_seconds * 1000,
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
    "ERROR_HINTS",
    "ErrorBody",
    "ErrorCode",
    "ErrorHint",
    "ErrorResponse",
    "auth_required",
    "conflict",
    "enrich_error_fields",
    "forbidden",
    "http_error",
    "not_found",
    "rate_limited",
]
