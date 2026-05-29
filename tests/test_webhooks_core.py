"""Core webhooks primitives — HMAC, payload shape, filter matching
(ADR 035 D2).

Covers the pure-Python seam (no HTTP, no storage, no event loop):

* HMAC signing correctness — header format, deterministic output, replay-
  binding via the timestamp.
* Payload shape — what the worker POSTs matches the events outbox shape.
* Kind-filter matching — exact / wildcard / empty.
* URL validation — HTTPS-only at construction.
* Secret handling — generated entropy, ``secret_hint`` doesn't leak.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

import pytest

from movate.core.events import Event, EventKind
from movate.core.webhooks import (
    SECRET_HINT_TAIL,
    SIGNATURE_HEADER,
    WILDCARD_KIND,
    WebhookSubscription,
    build_payload,
    generate_secret,
    secret_hint,
    sign_payload,
    verify_signature,
)

# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_sign_payload_format_is_stripe_style() -> None:
    """Signature has the canonical ``t=<ts>,v1=<hex>`` shape."""
    header = sign_payload(secret="abc", body=b"hello", timestamp=1234567890)
    assert header.startswith("t=1234567890,v1=")
    # v1 portion is 64 hex chars (SHA-256 hex).
    v1_part = header.split("v1=")[1]
    assert len(v1_part) == 64
    assert all(c in "0123456789abcdef" for c in v1_part)


def test_sign_payload_is_deterministic_for_same_ts() -> None:
    """Same secret + body + timestamp → same signature (no entropy)."""
    h1 = sign_payload(secret="abc", body=b"hello", timestamp=42)
    h2 = sign_payload(secret="abc", body=b"hello", timestamp=42)
    assert h1 == h2


def test_sign_payload_binds_timestamp() -> None:
    """The timestamp is part of the signed string — different ts → different v1."""
    h1 = sign_payload(secret="abc", body=b"hello", timestamp=1)
    h2 = sign_payload(secret="abc", body=b"hello", timestamp=2)
    assert h1 != h2


def test_sign_payload_hmac_matches_canonical_string() -> None:
    """The v1 value is HMAC-SHA256 of ``f"{ts}.{body}"`` keyed by secret.

    This is the canonical signed string a subscriber recomputes —
    explicit check so the contract can't drift silently.
    """
    secret = "test-secret"
    body = b'{"id":"e1","kind":"run.completed"}'
    ts = 1700000000
    header = sign_payload(secret=secret, body=body, timestamp=ts)
    v1 = header.split("v1=")[1]
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    assert v1 == expected


def test_verify_signature_accepts_signed_header() -> None:
    """``verify_signature`` round-trips ``sign_payload`` output."""
    secret = "rotate-me"
    body = b'{"x": 1}'
    header = sign_payload(secret=secret, body=body, timestamp=42)
    assert verify_signature(secret=secret, body=body, header_value=header)


def test_verify_signature_rejects_tampered_body() -> None:
    """Changing the body invalidates the signature (HMAC integrity)."""
    secret = "k"
    header = sign_payload(secret=secret, body=b"hello", timestamp=1)
    assert not verify_signature(secret=secret, body=b"world", header_value=header)


def test_verify_signature_rejects_wrong_secret() -> None:
    """A different secret can't forge a valid signature."""
    header = sign_payload(secret="a", body=b"hi", timestamp=1)
    assert not verify_signature(secret="b", body=b"hi", header_value=header)


def test_verify_signature_handles_malformed_header() -> None:
    """Garbage header → False, never raises."""
    assert not verify_signature(secret="k", body=b"x", header_value="garbage")
    assert not verify_signature(secret="k", body=b"x", header_value="v1=abc")
    assert not verify_signature(secret="k", body=b"x", header_value="t=abc,v1=def")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_generate_secret_unique() -> None:
    """Two consecutive calls produce different secrets (32 bytes of entropy)."""
    s1 = generate_secret()
    s2 = generate_secret()
    assert s1 != s2
    # ~52 chars of base32 (32 bytes → 52 chars before padding strip).
    assert len(s1) > 40


def test_secret_hint_only_tail() -> None:
    """``secret_hint`` returns last-N chars prefixed with ``...``."""
    secret = "ABCDEFGHIJKLMNOP"
    hint = secret_hint(secret)
    assert hint == f"...{secret[-SECRET_HINT_TAIL:]}"
    assert secret[:-SECRET_HINT_TAIL] not in hint


# ---------------------------------------------------------------------------
# WebhookSubscription / URL validation
# ---------------------------------------------------------------------------


def test_subscription_requires_https_url() -> None:
    """HTTP URLs are rejected at construction — non-negotiable security."""
    with pytest.raises(ValueError, match="https"):
        WebhookSubscription(
            tenant_id="t1",
            url="http://example.com/hook",
            kind_filter=[WILDCARD_KIND],
        )
    with pytest.raises(ValueError, match="https"):
        WebhookSubscription(
            tenant_id="t1",
            url="ftp://example.com/hook",
            kind_filter=[WILDCARD_KIND],
        )


def test_subscription_accepts_https_url() -> None:
    """HTTPS URLs pass validation."""
    sub = WebhookSubscription(
        tenant_id="t1",
        url="https://example.com/hook",
        kind_filter=[WILDCARD_KIND],
    )
    assert sub.url == "https://example.com/hook"


# ---------------------------------------------------------------------------
# Kind filter matching
# ---------------------------------------------------------------------------


def _sub(kinds: list[str]) -> WebhookSubscription:
    return WebhookSubscription(tenant_id="t1", url="https://example.com/h", kind_filter=kinds)


def test_matches_exact_kind() -> None:
    sub = _sub(["run.completed", "eval.failed"])
    assert sub.matches("run.completed")
    assert sub.matches("eval.failed")
    assert not sub.matches("agent.published")


def test_matches_wildcard() -> None:
    sub = _sub([WILDCARD_KIND])
    assert sub.matches("run.completed")
    assert sub.matches("anything.at.all")


def test_empty_filter_matches_nothing() -> None:
    sub = _sub([])
    assert not sub.matches("run.completed")


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


def test_build_payload_mirrors_event_view() -> None:
    """The wire payload exposes ``id/kind/subject/data/tenant_id/created_at``."""
    e = Event(
        tenant_id="t-1",
        kind=EventKind.RUN_COMPLETED.value,
        subject="faq-agent",
        data={"job_id": "j1", "duration_ms": 12},
    )
    payload = build_payload(e)
    assert payload["id"] == e.id
    assert payload["kind"] == "run.completed"
    assert payload["subject"] == "faq-agent"
    assert payload["data"] == {"job_id": "j1", "duration_ms": 12}
    assert payload["tenant_id"] == "t-1"
    assert isinstance(payload["created_at"], str)
    # ISO-8601 string round-trips through datetime.fromisoformat.
    assert datetime.fromisoformat(payload["created_at"]) == e.created_at


def test_signature_header_constant_is_stable() -> None:
    """The header name is part of the public contract — fix at one place."""
    assert SIGNATURE_HEADER == "X-MDK-Signature"
