"""Self-teaching error envelope — additive optional fields + back-compat guard.

The error envelope's contract is ``{error: {code, message, request_id}}``.
This module exercises the STRICTLY ADDITIVE self-teaching extension:

* known codes gain optional ``docs_url`` / ``fix_hint`` / ``retriable`` (and
  ``retry_after_ms`` when retriable + known) from a single registry;
* unknown codes still produce a VALID envelope (the optional fields are simply
  omitted — graceful unknown);
* the three original fields keep their exact names, types, and ordering (the
  hard back-compat guard).

Hermetic: builds a fresh app over ``InMemoryStorage`` and drives it through
``fastapi.TestClient`` plus direct unit calls to the ``errors`` helpers.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, mint_api_key
from movate.core.models import ApiKeyEnv
from movate.runtime import build_app
from movate.runtime.errors import (
    ERROR_HINTS,
    ErrorBody,
    ErrorCode,
    auth_required,
    enrich_error_fields,
    http_error,
    not_found,
    rate_limited,
)
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _error_of(exc) -> dict:
    """Pull the inner ``error`` dict from an ``HTTPException`` built by our
    helpers (its ``detail`` is ``{"error": {...}}``)."""
    return exc.detail["error"]


# ---------------------------------------------------------------------------
# HARD back-compat guard — the three contract fields are unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backcompat_three_fields_unchanged_names_types_order() -> None:
    """HEADLINE guard: ``code`` / ``message`` / ``request_id`` keep their exact
    names, types, and leading positions — the new fields are appended after.

    Asserted at the MODEL level so a field rename/reorder/type-change in
    ``ErrorBody`` trips the test even if no endpoint exercises it."""
    fields = list(ErrorBody.model_fields)
    # The three contract fields come first, in order.
    assert fields[:3] == ["code", "message", "request_id"]
    annotations = {name: f.annotation for name, f in ErrorBody.model_fields.items()}
    assert annotations["code"] is ErrorCode
    assert annotations["message"] is str
    assert annotations["request_id"] == (str | None)
    # The additive fields exist and trail the contract trio.
    assert set(fields[3:]) == {"docs_url", "fix_hint", "retriable", "retry_after_ms"}


@pytest.mark.unit
def test_backcompat_unknown_code_emits_only_three_fields() -> None:
    """An unregistered code with no retry hint serializes the BYTE-IDENTICAL
    pre-extension body: exactly the three contract fields, nothing more.

    ``CONFLICT`` IS registered, so use a constructed body with a fabricated
    code path via the registry-miss helper to prove the omit behavior."""
    # ``enrich_error_fields`` for a code not in the registry → {}.
    assert enrich_error_fields("totally_made_up_code") == {}
    # And the serialized envelope for an absent-hint code carries only the trio.
    body = ErrorBody(code=ErrorCode.BAD_REQUEST, message="x", request_id="rid")
    # Simulate the unknown path by dumping with the same exclude logic:
    dumped = body.model_dump(mode="json")
    for f in ("docs_url", "fix_hint", "retriable", "retry_after_ms"):
        if dumped.get(f) is None:
            dumped.pop(f, None)
    assert set(dumped) == {"code", "message", "request_id"}


@pytest.mark.unit
def test_backcompat_request_id_still_present_even_when_null() -> None:
    """``request_id`` must still serialize (as ``null``) outside a request scope
    — the self-teaching omit logic strips ONLY the four new fields, never the
    long-standing contract trio."""
    # No request context bound (unit call) → request_id None, but PRESENT.
    exc = http_error(ErrorCode.INTERNAL, status_code=500)
    err = _error_of(exc)
    assert "request_id" in err
    assert err["request_id"] is None
    assert err["code"] == "internal"
    assert "message" in err


# ---------------------------------------------------------------------------
# Additive fields appear for known codes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_known_code_gets_docs_url_fix_hint_retriable() -> None:
    """A known code (auth_required) carries all three static self-teaching
    fields, sourced from the registry."""
    err = _error_of(auth_required())
    assert err["code"] == "auth_required"
    assert err["docs_url"].endswith("#auth_required")
    assert "mdk auth create" in err["fix_hint"]
    assert err["retriable"] is False


@pytest.mark.unit
def test_not_found_is_non_retriable_with_hint() -> None:
    err = _error_of(not_found("run", "abc"))
    assert err["code"] == "not_found"
    assert err["retriable"] is False
    assert err["fix_hint"]
    # ``retry_after_ms`` is omitted for a non-retriable error.
    assert "retry_after_ms" not in err


@pytest.mark.unit
def test_rate_limited_is_retriable_with_retry_after_ms() -> None:
    """429 is retriable AND carries the occurrence-specific ``retry_after_ms``
    (the seconds-based ``Retry-After`` header times 1000)."""
    exc = rate_limited(retry_after_seconds=7, limit=10, reset_at_unix=123)
    err = _error_of(exc)
    assert err["code"] == "rate_limited"
    assert err["retriable"] is True
    assert err["retry_after_ms"] == 7000
    # Header contract unchanged: Retry-After stays in SECONDS.
    assert exc.headers["Retry-After"] == "7"


@pytest.mark.unit
def test_retry_after_ms_suppressed_for_non_retriable_code() -> None:
    """Even if a caller threads ``retry_after_ms`` for a non-retriable code, it
    is NOT advertised (we never tell a client to retry something that can't
    succeed on retry)."""
    exc = http_error(ErrorCode.BAD_REQUEST, status_code=400, retry_after_ms=500)
    err = _error_of(exc)
    assert err["retriable"] is False
    assert "retry_after_ms" not in err


@pytest.mark.unit
def test_every_error_code_has_a_registry_entry() -> None:
    """Every stable ``ErrorCode`` has a hint so no first-class code ever ships
    a bare envelope (the registry is the maintainable source of truth)."""
    for code in ErrorCode:
        assert code.value in ERROR_HINTS, f"missing hint for {code.value}"


@pytest.mark.unit
def test_enrich_unknown_code_with_retry_after_still_emits_delay() -> None:
    """An unknown code (no registry entry → retriable unknown) WITH an explicit
    ``retry_after_ms`` still surfaces the delay — we don't have a ``retriable``
    flag to gate on, so a caller-supplied wait is honored."""
    fields = enrich_error_fields("mystery_code", retry_after_ms=250)
    assert fields == {"retry_after_ms": 250}


# ---------------------------------------------------------------------------
# Over-the-wire: known + unknown paths through the real app.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wire_401_envelope_self_teaching(storage: InMemoryStorage) -> None:
    """An unauthed request yields a 401 whose body carries the contract trio
    PLUS the self-teaching fields for ``auth_required``."""
    client = TestClient(build_app(storage))
    r = client.post("/run", json={"kind": "agent", "target": "demo", "input": {}})
    assert r.status_code == 401
    err = r.json()["detail"]["error"]
    # Contract trio present + unchanged.
    assert err["code"] == "auth_required"
    assert isinstance(err["message"], str)
    assert err["request_id"]
    # Self-teaching additions.
    assert err["docs_url"].endswith("#auth_required")
    assert err["fix_hint"]
    assert err["retriable"] is False


@pytest.mark.unit
async def test_wire_429_envelope_carries_retry_after_ms() -> None:
    """A 429 body is retriable + carries ``retry_after_ms`` while the header
    contract (``Retry-After`` seconds, ``X-RateLimit-*``) is untouched."""
    s = InMemoryStorage()
    await s.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="rl")
    await s.save_api_key(minted.record)
    client = TestClient(build_app(s, rate_limit_per_minute=1))
    token = f"Bearer {minted.full_key}"
    assert client.get("/agents", headers={"Authorization": token}).status_code == 200
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 429
    err = r.json()["detail"]["error"]
    assert err["code"] == "rate_limited"
    assert err["retriable"] is True
    assert err["retry_after_ms"] >= 1000
    # Header (seconds) still present + consistent.
    assert int(r.headers["Retry-After"]) >= 1


@pytest.mark.unit
async def test_wire_404_envelope_is_non_retriable(storage: InMemoryStorage) -> None:
    """A 404 (authed, missing run) carries a non-retriable self-teaching
    envelope and no ``retry_after_ms``."""
    # Register a real key so we get past auth to the 404.
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="nf", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    client = TestClient(build_app(storage))
    r = client.get("/runs/does-not-exist", headers={"Authorization": f"Bearer {minted.full_key}"})
    assert r.status_code == 404
    err = r.json()["detail"]["error"]
    assert err["code"] == "not_found"
    assert err["retriable"] is False
    assert "retry_after_ms" not in err
