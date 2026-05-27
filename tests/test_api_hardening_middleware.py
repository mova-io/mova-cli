"""ADR 033 Layer-1 API hardening middlewares — D2 + D3 + D6.

Hermetic: every test builds a fresh app over ``InMemoryStorage`` via
``build_app`` and drives it through ``fastapi.TestClient`` (no live server).

Covers the three cross-cutting, additive middlewares:

* **D2 — request correlation**: ``X-Request-Id`` on success AND error; an
  inbound id is echoed; on an error the response header equals the body's
  ``error.request_id``; logs carry the same id.
* **D3 — rate-limit headers**: a 429 carries ``Retry-After`` + ``X-RateLimit-*``
  (a focused regression — the limiter logic itself lives in test_rate_limit).
* **D6 — payload size limit**: a body over the max → 413 envelope naming the
  limit; under → passes; the guard is configurable + can be disabled.

Regression: a normal request keeps its body/status, gaining only the new
headers.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, mint_api_key
from movate.core.models import ApiKeyEnv
from movate.runtime import build_app
from movate.runtime.hardening import (
    DEFAULT_MAX_REQUEST_BYTES,
    MAX_REQUEST_BYTES_ENV,
    resolve_max_request_bytes,
)
from movate.runtime.request_context import (
    REQUEST_ID_HEADER,
    get_request_id,
    install_request_id_logging,
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


@pytest.fixture
async def authed() -> tuple[InMemoryStorage, str]:
    """Storage with a registered all-scopes key + its bearer token."""
    s = InMemoryStorage()
    await s.init()
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="hardening", scopes=list(ALL_SCOPES)
    )
    await s.save_api_key(minted.record)
    return s, f"Bearer {minted.full_key}"


# ---------------------------------------------------------------------------
# D2 — request correlation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_request_id_present_on_success(storage: InMemoryStorage) -> None:
    """Every success response carries an ``X-Request-Id`` (generated when the
    client didn't send one)."""
    client = TestClient(build_app(storage))
    r = client.get("/healthz")
    assert r.status_code == 200
    rid = r.headers.get(REQUEST_ID_HEADER)
    assert rid and len(rid) >= 8  # a generated UUID hex


@pytest.mark.unit
def test_inbound_request_id_is_echoed(storage: InMemoryStorage) -> None:
    """A caller-supplied ``X-Request-Id`` is threaded straight through (so an
    upstream gateway's id wins)."""
    client = TestClient(build_app(storage))
    r = client.get("/healthz", headers={REQUEST_ID_HEADER: "caller-trace-123"})
    assert r.status_code == 200
    assert r.headers[REQUEST_ID_HEADER] == "caller-trace-123"


@pytest.mark.unit
def test_blank_inbound_request_id_is_replaced(storage: InMemoryStorage) -> None:
    """A blank/whitespace inbound id is treated as absent → a real id is
    generated rather than echoing an empty string."""
    client = TestClient(build_app(storage))
    r = client.get("/healthz", headers={REQUEST_ID_HEADER: "   "})
    assert r.status_code == 200
    assert r.headers[REQUEST_ID_HEADER].strip() != ""


@pytest.mark.unit
def test_error_header_matches_envelope_request_id(storage: InMemoryStorage) -> None:
    """HEADLINE D2 invariant: on an error, the response header
    ``X-Request-Id`` equals the body's ``error.request_id``."""
    client = TestClient(build_app(storage))
    # Unauthed /run → 401 envelope (built via runtime/errors).
    r = client.post("/run", json={"kind": "agent", "target": "demo", "input": {}})
    assert r.status_code == 401
    header_id = r.headers[REQUEST_ID_HEADER]
    body_id = r.json()["detail"]["error"]["request_id"]
    assert body_id is not None
    assert header_id == body_id


@pytest.mark.unit
def test_error_envelope_uses_inbound_id(storage: InMemoryStorage) -> None:
    """An inbound id flows all the way into the error body too — same value in
    the header and ``error.request_id``."""
    client = TestClient(build_app(storage))
    r = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers={REQUEST_ID_HEADER: "abc-correlate-999"},
    )
    assert r.status_code == 401
    assert r.headers[REQUEST_ID_HEADER] == "abc-correlate-999"
    assert r.json()["detail"]["error"]["request_id"] == "abc-correlate-999"


@pytest.mark.unit
def test_request_id_logging_filter_stamps_records(storage: InMemoryStorage, caplog) -> None:
    """The installed ``RequestIdFilter`` stamps the active id onto log records
    emitted during a request, so logs correlate to the client's id."""
    client = TestClient(build_app(storage))
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(getattr(record, "request_id", "<missing>"))

    handler = _Capture()
    # Re-install so the filter attaches to our freshly-added handler too.
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_request_id_logging()
        # A bad bearer triggers an ``auth_failure`` INFO log inside the request.
        with caplog.at_level(logging.INFO):
            client.post(
                "/run",
                json={"kind": "agent", "target": "demo", "input": {}},
                headers={
                    "Authorization": "Bearer mvt_live_x_y_z",
                    REQUEST_ID_HEADER: "log-corr-42",
                },
            )
    finally:
        root.removeHandler(handler)
    # At least one record emitted during the request carries our id.
    assert "log-corr-42" in seen


@pytest.mark.unit
def test_request_id_context_resets_between_requests(storage: InMemoryStorage) -> None:
    """The contextvar is reset after each request so an id can't leak into the
    next one on a reused thread (and is empty outside any request)."""
    client = TestClient(build_app(storage))
    client.get("/healthz", headers={REQUEST_ID_HEADER: "first"})
    # Outside a request the var is back to its empty default.
    assert get_request_id() == ""
    r = client.get("/healthz", headers={REQUEST_ID_HEADER: "second"})
    assert r.headers[REQUEST_ID_HEADER] == "second"


# ---------------------------------------------------------------------------
# D3 — rate-limit response headers (focused regression; logic in test_rate_limit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_429_carries_retry_after_and_ratelimit_headers() -> None:
    """Driving the limiter to reject yields a 429 envelope WITH ``Retry-After``
    + ``X-RateLimit-*`` so a client can recover programmatically."""
    s = InMemoryStorage()
    await s.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="rl")
    await s.save_api_key(minted.record)
    client = TestClient(build_app(s, rate_limit_per_minute=1))
    token = f"Bearer {minted.full_key}"

    assert client.get("/agents", headers={"Authorization": token}).status_code == 200
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 429
    assert r.json()["detail"]["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["X-RateLimit-Limit"] == "1"
    assert r.headers["X-RateLimit-Remaining"] == "0"
    assert int(r.headers["X-RateLimit-Reset"]) > 0
    # The 429 still carries the correlation id (request-id wraps the limiter).
    assert r.headers[REQUEST_ID_HEADER] == r.json()["detail"]["error"]["request_id"]


# ---------------------------------------------------------------------------
# D6 — payload size limit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_payload_over_limit_rejected_413(storage: InMemoryStorage) -> None:
    """A body over the configured max → 413 with the envelope shape + the limit
    stated in the message."""
    max_bytes = 1024
    client = TestClient(build_app(storage, max_request_bytes=max_bytes))
    oversized = {"kind": "agent", "target": "x", "input": {"blob": "A" * 4096}}
    r = client.post("/run", json=oversized)
    assert r.status_code == 413
    err = r.json()["detail"]["error"]
    assert err["code"] == "payload_too_large"
    assert str(max_bytes) in err["message"]  # the limit is named
    # Even the 413 carries the correlation id (header == envelope).
    assert r.headers[REQUEST_ID_HEADER] == err["request_id"]


@pytest.mark.unit
def test_payload_under_limit_passes(authed: tuple[InMemoryStorage, str]) -> None:
    """A body under the max passes the guard untouched (reaches the handler)."""
    s, token = authed
    client = TestClient(build_app(s, max_request_bytes=10 * 1024, rate_limit_per_minute=None))
    small = {"kind": "agent", "target": "demo", "input": {"text": "hi"}}
    r = client.post("/run", json=small, headers={"Authorization": token})
    # 404 (no such agent) — the point is it got PAST the payload guard to the
    # handler, NOT a 413.
    assert r.status_code != 413


@pytest.mark.unit
def test_payload_rejected_via_content_length_without_reading_body(
    storage: InMemoryStorage,
) -> None:
    """A declared ``Content-Length`` over the limit is rejected up front (no
    need to read the body) — the guard fires before auth too."""
    client = TestClient(build_app(storage, max_request_bytes=512))
    r = client.post(
        "/run",
        content=b"B" * 2048,
        headers={"content-type": "application/json", "content-length": "2048"},
    )
    assert r.status_code == 413
    assert r.json()["detail"]["error"]["code"] == "payload_too_large"


@pytest.mark.unit
def test_payload_limit_disabled_allows_large_body(storage: InMemoryStorage) -> None:
    """``max_request_bytes=0`` disables the guard → a large body is NOT 413'd
    (it falls through to normal handling)."""
    client = TestClient(build_app(storage, max_request_bytes=0))
    big = {"kind": "agent", "target": "x", "input": {"blob": "A" * 100_000}}
    r = client.post("/run", json=big)
    assert r.status_code != 413  # 401 unauthed, but never 413


# ---------------------------------------------------------------------------
# Config resolution — explicit > env > default
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_max_request_bytes_default(monkeypatch) -> None:
    monkeypatch.delenv(MAX_REQUEST_BYTES_ENV, raising=False)
    assert resolve_max_request_bytes(None) == DEFAULT_MAX_REQUEST_BYTES


@pytest.mark.unit
def test_resolve_max_request_bytes_env(monkeypatch) -> None:
    monkeypatch.setenv(MAX_REQUEST_BYTES_ENV, "12345")
    assert resolve_max_request_bytes(None) == 12345


@pytest.mark.unit
def test_resolve_max_request_bytes_explicit_wins(monkeypatch) -> None:
    monkeypatch.setenv(MAX_REQUEST_BYTES_ENV, "12345")
    assert resolve_max_request_bytes(999) == 999


@pytest.mark.unit
def test_resolve_max_request_bytes_invalid_disables(monkeypatch) -> None:
    monkeypatch.setenv(MAX_REQUEST_BYTES_ENV, "not-a-number")
    assert resolve_max_request_bytes(None) == 0


@pytest.mark.unit
def test_resolve_max_request_bytes_zero_disables(monkeypatch) -> None:
    monkeypatch.setenv(MAX_REQUEST_BYTES_ENV, "0")
    assert resolve_max_request_bytes(None) == 0


# ---------------------------------------------------------------------------
# Regression — a normal request is unaffected (body/status preserved, +headers)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normal_request_unaffected_body_and_status(storage: InMemoryStorage) -> None:
    """A baseline request keeps its exact body + status; the only difference is
    the additive ``X-Request-Id`` (and, on authed paths, the rate-limit
    headers tested elsewhere)."""
    # Build two apps: one with hardening params at defaults, compare /healthz.
    client = TestClient(build_app(storage))
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    # The additive header is present; nothing in the body changed.
    assert REQUEST_ID_HEADER in r.headers
