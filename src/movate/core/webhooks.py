"""Webhook subscriptions + signing (ADR 035 D2 — outbound deliveries).

D1 (ADR 035) recorded domain events to a durable outbox. D2 = the
**delivery consumer**: a background worker drains that outbox and POSTs
each event to subscribers configured via :class:`WebhookSubscription`.

This module is pure data + signing primitives — no HTTP, no storage, no
event-loop side effects. The storage Protocol persists the subscription;
the runtime worker (``movate.runtime.webhook_worker``) consumes events,
signs the body, and POSTs to ``url``. ``cli`` ⊥ ``runtime`` is preserved
because both layers depend ONLY on this core seam.

Signature scheme — Stripe's ``t=<ts>,v1=<hex>`` idiom:

* Pick a unix-second timestamp ``ts``.
* Build the canonical signed string ``"<ts>.<raw_body>"`` (UTF-8 bytes).
* Compute ``v1 = hex(HMAC-SHA256(secret, signed_string))``.
* Send header ``X-MDK-Signature: t=<ts>,v1=<v1>``.

Subscribers verify by recomputing the HMAC over the same canonical
string. Stripe-style binding-the-timestamp guards against replay (the
timestamp is part of what the HMAC covers); we surface this idiom so any
reviewer recognizes it without reading the source.

Boundary discipline:

* ``url`` must be HTTPS — validated at construction. HTTP would allow
  eavesdropping a webhook secret or tampering with the payload.
* ``secret`` is stored at rest (NOT just a hash): we must be able to
  RE-sign every outbound delivery with the same secret the subscriber
  configured. This is the deliberate trade-off vs API keys / triggers
  (which hash because the runtime only ever VERIFIES). The HTTP API
  echoes the secret EXACTLY ONCE on create; subsequent reads return a
  ``secret_hint`` (last-4 chars) only — operators copy the secret on
  creation, like a one-time-show password.
* No-auto-disable: a subscriber that keeps 5xx-ing increments
  ``failure_count`` (advisory) but stays enabled. ADR-016-style
  auto-promote/demote is for evals; webhooks are operator-controlled.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Header names — exported so the worker + tests + docs reference one truth.
# ---------------------------------------------------------------------------

EVENT_ID_HEADER = "X-MDK-Event-Id"
"""Header carrying the event's id (subscribers dedupe on this — at-least-once)."""

EVENT_KIND_HEADER = "X-MDK-Event-Kind"
"""Header carrying the event kind (e.g. ``run.completed``)."""

WEBHOOK_ID_HEADER = "X-MDK-Webhook-Id"
"""Header carrying the subscription id (subscribers can route by webhook)."""

SIGNATURE_HEADER = "X-MDK-Signature"
"""Stripe-style ``t=<ts>,v1=<hex>`` HMAC signature header."""

# Last-N tail to surface as a hint after the one-time create response.
# Same idea as bank/4-digits cards or GitHub PAT hints — enough to ID the
# secret without re-exposing it.
SECRET_HINT_TAIL = 4

# Wildcard sentinel for "subscribe to every event kind". Stored as a
# single-element list ``["*"]`` so the column schema stays uniform across
# backends (TEXT[] / JSONB / list[str]); the worker treats ``["*"]`` as
# "match all" without re-encoding.
WILDCARD_KIND = "*"


# ---------------------------------------------------------------------------
# Secret generation
# ---------------------------------------------------------------------------


def generate_secret() -> str:
    """Mint a fresh webhook signing secret.

    ~32 bytes of os-random entropy, base32-encoded (uppercase A-Z + 2-7,
    no padding). Base32 is copy/paste-safe (no slashes, no ``+``/``/``)
    and case-insensitive — the operator can paste it back from a
    yellow-on-yellow terminal without ambiguity. The HMAC key derived
    from this is the raw ASCII string, NOT decoded bytes; subscribers
    receive the same string and treat it as opaque.
    """
    raw = secrets.token_bytes(32)
    # b32encode → b'...=', strip padding for a cleaner one-line copy.
    return base64.b32encode(raw).rstrip(b"=").decode("ascii")


def secret_hint(secret: str) -> str:
    """Return a fixed-length last-N hint of ``secret`` for safe display.

    Pad with leading ``*`` so callers can recognize the shape; the tail
    of a real ~52-char b32 secret is enough to disambiguate two stored
    webhooks at-a-glance without re-revealing the full key.
    """
    if not secret:
        return ""
    tail = secret[-SECRET_HINT_TAIL:]
    return f"...{tail}"


# ---------------------------------------------------------------------------
# Signing (Stripe-style)
# ---------------------------------------------------------------------------


def sign_payload(*, secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Build the ``X-MDK-Signature`` header value for ``body``.

    Returns a header value ``"t=<ts>,v1=<hex>"`` where ``<hex>`` is
    ``HMAC-SHA256(secret, f"{ts}.{body_utf8}")`` and ``<ts>`` is the
    unix-second timestamp the signer used (epoch seconds, UTC).

    ``timestamp`` is exposed for test determinism; production callers
    pass ``None`` and we stamp ``time.time()``.

    The canonical signed string deliberately binds the timestamp to the
    payload so a subscriber can:

    * recompute the HMAC and confirm the body wasn't tampered with;
    * reject signatures whose ``t`` is too old (replay-window guard) —
      the timestamp is part of what's signed, so an attacker can't
      change it after the fact.
    """
    ts = int(timestamp if timestamp is not None else time.time())
    signed_string = f"{ts}.".encode() + body
    mac = hmac.new(secret.encode("utf-8"), signed_string, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_signature(*, secret: str, body: bytes, header_value: str) -> bool:
    """Verify a ``X-MDK-Signature`` header against ``body`` using ``secret``.

    Returns ``True`` iff the header parses cleanly AND the recomputed
    HMAC matches ``v1`` in constant time. Returns ``False`` for any
    malformed header (missing ``t=`` / ``v1=`` / non-hex) without
    raising — the caller treats unverifiable signatures uniformly. We
    do NOT enforce a freshness window here; subscribers do that on
    their end (the canonical string binds ``t`` so they can).

    This function exists for tests + future receiving code (e.g. a
    Mova iO server that ALSO accepts inbound webhooks). The delivery
    worker only signs — it never verifies its own deliveries.
    """
    parts = {}
    for raw_piece in header_value.split(","):
        piece = raw_piece.strip()
        if "=" not in piece:
            return False
        k, v = piece.split("=", 1)
        parts[k] = v
    ts_str = parts.get("t")
    v1 = parts.get("v1")
    if ts_str is None or v1 is None:
        return False
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    signed_string = f"{ts}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), signed_string, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _validate_https_url(value: str) -> str:
    """Reject non-HTTPS URLs at construction time.

    HTTP URLs would leak the webhook secret on the network (the secret
    isn't on the wire, but a tampered body + recomputed signature would
    work against a passive MITM); HTTPS is non-negotiable.
    """
    stripped = (value or "").strip()
    if not stripped.lower().startswith("https://"):
        raise ValueError("webhook url must use https:// — http and other schemes are rejected")
    return stripped


class WebhookSubscription(BaseModel):
    """One configured outbound webhook subscriber (ADR 035 D2).

    Persisted by the storage layer; consumed by the delivery worker.
    Tenant-scoped throughout — the worker only delivers events whose
    ``tenant_id`` matches the subscription's tenant.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    """uuid4 hex — stable, opaque, unguessable. Doubles as the public
    handle in CLI / API."""

    tenant_id: str
    """The tenant this subscription belongs to. **NOT NULL** in storage.
    The worker only matches events whose ``event.tenant_id == tenant_id``."""

    url: str
    """HTTPS target URL. Validated at construction; HTTP and other
    schemes raise. We never follow redirects on delivery (the worker
    POSTs and accepts the first response)."""

    kind_filter: list[str]
    """Event kinds this subscription wants. Either a list of exact kinds
    (e.g. ``["run.completed", "eval.failed"]``) or the wildcard sentinel
    ``["*"]`` to receive every kind. Empty list ⇒ never matches (the
    subscription is dormant by filter alone)."""

    secret: str = Field(default_factory=generate_secret)
    """HMAC signing secret — STORED (not hashed), because the worker
    must re-sign every outbound delivery with the same key the
    subscriber configured. Surfaced on the wire EXACTLY ONCE (the
    create-response); subsequent reads return ``secret_hint`` only."""

    enabled: bool = True
    """Soft disable — the worker skips disabled subscriptions on every
    pass. Operators flip this manually; the system never auto-disables
    (a failing subscriber keeps incrementing ``failure_count`` but
    stays enabled — auto-disable is for ops dashboards, not the
    worker)."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """UTC timestamp the subscription was created."""

    failure_count: int = 0
    """Advisory counter — incremented each time a delivery exhausts its
    retry budget. Drives the ops dashboard ("which webhook has been
    flaky?"). Reset is manual (no auto-reset; the counter is the
    historical record)."""

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _validate_https_url(value)

    @field_validator("kind_filter")
    @classmethod
    def _check_kind_filter(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("kind_filter must be a list of strings")
        return [str(k) for k in value]

    def matches(self, kind: str) -> bool:
        """Return ``True`` iff ``kind`` is in this subscription's filter.

        Wildcard handling: ``["*"]`` matches every kind. Mixed
        ``["*", "run.completed"]`` also matches everything (the
        wildcard wins), but the create path normalizes the list, so
        in practice the filter is either pure-wildcard or a concrete
        kind list.
        """
        if WILDCARD_KIND in self.kind_filter:
            return True
        return kind in self.kind_filter


class WebhookAttempt(BaseModel):
    """One delivery attempt — recorded in the ``webhook_attempts`` log.

    Persisted ONCE per ``(webhook_id, event_id, attempt_n)`` triple.
    Drives the ops view ``GET /api/v1/webhooks/{id}/attempts`` and the
    CLI ``mdk webhooks attempts``.

    ``error_kind`` is a small closed enum so the read path can render a
    badge without parsing free-form messages.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    """uuid4 hex — primary key in the log table."""

    webhook_id: str
    """The subscription this attempt targeted."""

    event_id: str
    """The event being delivered."""

    tenant_id: str
    """The tenant — denormalized onto the row so the per-tenant
    ``GET /api/v1/webhooks/{id}/attempts`` can scope cheaply with a
    single index on ``(tenant_id, attempted_at)``."""

    attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """When the worker dispatched the POST."""

    status_code: int | None = None
    """HTTP status the subscriber returned. ``None`` for timeout /
    connection errors (no response was received). 2xx is the only
    success class — 3xx isn't auto-followed."""

    response_excerpt: str | None = None
    """First ~512 chars of the response body. ``None`` for non-HTTP
    errors. Truncated server-side to keep the log row bounded — a
    misbehaving subscriber returning megabytes can't blow up storage."""

    error_kind: str
    """One of: ``"ok"`` (2xx response), ``"http_error"`` (non-2xx),
    ``"timeout"`` (read/connect timeout), ``"connection"`` (DNS / TLS /
    socket refused), or ``"max_retries"`` (terminal — every attempt
    failed). The summary view groups by this."""

    attempt_n: int
    """1-indexed retry number. Attempt 1 is the initial delivery;
    attempt 4 (with the default policy) is the final one before
    ``max_retries`` is recorded."""


# ---------------------------------------------------------------------------
# Wire shape — what the API returns / accepts.
# ---------------------------------------------------------------------------


class WebhookView(BaseModel):
    """Outbound shape for ``GET /api/v1/webhooks{,/{id}}``. NEVER carries
    the full secret — only a last-4 ``secret_hint``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    url: str
    kind_filter: list[str]
    enabled: bool
    failure_count: int
    secret_hint: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: WebhookSubscription) -> WebhookView:
        return cls(
            id=record.id,
            tenant_id=record.tenant_id,
            url=record.url,
            kind_filter=record.kind_filter,
            enabled=record.enabled,
            failure_count=record.failure_count,
            secret_hint=secret_hint(record.secret),
            created_at=record.created_at,
        )


class WebhookCreatedView(WebhookView):
    """Create-response shape — extends :class:`WebhookView` with the
    plaintext ``secret``. The only place the secret is ever transmitted.

    The matching CLI / front-end surface MUST surface this secret to the
    operator and warn that it will not be shown again.
    """

    model_config = ConfigDict(extra="forbid")

    secret: str

    @classmethod
    def from_record(cls, record: WebhookSubscription) -> WebhookCreatedView:
        return cls(
            id=record.id,
            tenant_id=record.tenant_id,
            url=record.url,
            kind_filter=record.kind_filter,
            enabled=record.enabled,
            failure_count=record.failure_count,
            secret_hint=secret_hint(record.secret),
            secret=record.secret,
            created_at=record.created_at,
        )


class WebhookListView(BaseModel):
    """``GET /api/v1/webhooks`` response shape."""

    model_config = ConfigDict(extra="forbid")

    webhooks: list[WebhookView]
    count: int


class WebhookCreateRequest(BaseModel):
    """Body of ``POST /api/v1/webhooks``."""

    model_config = ConfigDict(extra="forbid")

    url: str
    kind_filter: list[str] = Field(default_factory=lambda: [WILDCARD_KIND])
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _validate_https_url(value)


class WebhookUpdateRequest(BaseModel):
    """Body of ``PATCH /api/v1/webhooks/{id}`` — only ``enabled`` is
    mutable today; URL/kind_filter changes belong in delete+recreate so
    the audit log + secret are explicit about a subscriber rewire."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None


class WebhookAttemptView(BaseModel):
    """``GET /api/v1/webhooks/{id}/attempts`` row shape."""

    model_config = ConfigDict(extra="forbid")

    id: str
    webhook_id: str
    event_id: str
    attempted_at: datetime
    status_code: int | None
    response_excerpt: str | None
    error_kind: str
    attempt_n: int

    @classmethod
    def from_record(cls, record: WebhookAttempt) -> WebhookAttemptView:
        return cls(
            id=record.id,
            webhook_id=record.webhook_id,
            event_id=record.event_id,
            attempted_at=record.attempted_at,
            status_code=record.status_code,
            response_excerpt=record.response_excerpt,
            error_kind=record.error_kind,
            attempt_n=record.attempt_n,
        )


class WebhookAttemptListView(BaseModel):
    """``GET /api/v1/webhooks/{id}/attempts`` response shape."""

    model_config = ConfigDict(extra="forbid")

    attempts: list[WebhookAttemptView]
    count: int


# ---------------------------------------------------------------------------
# Payload shape — the JSON body the worker POSTs to a subscriber.
# ---------------------------------------------------------------------------


def build_payload(event: Any) -> dict[str, Any]:
    """Render the canonical wire payload for one event.

    Mirrors :class:`movate.core.events.EventView` field-for-field; we
    don't import ``Event`` here to avoid a hard-typing cycle (the
    storage Protocol pulls webhooks types, and the events module is
    already imported by the storage Protocol). ``event`` is duck-typed
    to expose ``id`` / ``kind`` / ``subject`` / ``data`` /
    ``tenant_id`` / ``created_at``.
    """
    created_at = event.created_at
    created_at_iso = created_at.isoformat() if isinstance(created_at, datetime) else str(created_at)
    return {
        "id": event.id,
        "kind": event.kind,
        "subject": event.subject,
        "data": event.data,
        "tenant_id": event.tenant_id,
        "created_at": created_at_iso,
    }


__all__ = [
    "EVENT_ID_HEADER",
    "EVENT_KIND_HEADER",
    "SECRET_HINT_TAIL",
    "SIGNATURE_HEADER",
    "WEBHOOK_ID_HEADER",
    "WILDCARD_KIND",
    "WebhookAttempt",
    "WebhookAttemptListView",
    "WebhookAttemptView",
    "WebhookCreateRequest",
    "WebhookCreatedView",
    "WebhookListView",
    "WebhookSubscription",
    "WebhookUpdateRequest",
    "WebhookView",
    "build_payload",
    "generate_secret",
    "secret_hint",
    "sign_payload",
    "verify_signature",
]
