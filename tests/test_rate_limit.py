"""Per-API-key rate limiting — token-bucket math + middleware integration.

Two layers:

1. **Pure-Python token-bucket math** — ``InProcessRateLimiter.check``
   tested directly with mocked time via ``time.monotonic`` /
   ``time.time`` monkeypatches. Asserts capacity, refill, burst,
   denied path with ``retry_after``.
2. **Middleware integration** — full FastAPI app with a low-capacity
   limiter; assert the Nth request gets 429 with the right headers,
   ``/healthz`` and ``/ready`` are NOT rate-limited (they're
   unauthed; ACA probes them every 10s).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import mint_api_key
from movate.core.models import ApiKeyEnv
from movate.core.rate_limit import (
    InProcessRateLimiter,
    NoOpRateLimiter,
    RateLimitDecision,
)
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# 1. Pure-Python token-bucket math
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_clock(monkeypatch) -> Iterator[list[float]]:
    """Pin both ``time.monotonic`` and ``time.time`` to a mutable
    holder so tests can advance the clock deterministically.

    ``holder[0]`` is the current "time" in seconds. Tests mutate it
    to fast-forward (e.g. ``holder[0] += 30``) and observe behavior
    at the new clock position.
    """
    holder = [1000.0]  # monotonic must be a positive float
    monkeypatch.setattr("movate.core.rate_limit.time.monotonic", lambda: holder[0])
    monkeypatch.setattr("movate.core.rate_limit.time.time", lambda: holder[0])
    yield holder


@pytest.mark.unit
async def test_bucket_starts_full(fixed_clock) -> None:
    """First request gets allowed=True with ``remaining = capacity - 1``."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    decision = await limiter.check("key-A")
    assert decision.allowed is True
    assert decision.limit == 60
    assert decision.remaining == 59  # one consumed
    assert decision.retry_after_seconds is None


@pytest.mark.unit
async def test_bucket_drains_on_repeated_calls(fixed_clock) -> None:
    """N requests in a row drain the bucket to N-capacity. With no
    elapsed time, no refill happens between calls."""
    limiter = InProcessRateLimiter(limit_per_minute=5)
    decisions = [await limiter.check("key-A") for _ in range(5)]

    # All 5 allowed; remaining decrements each time.
    assert all(d.allowed for d in decisions)
    assert [d.remaining for d in decisions] == [4, 3, 2, 1, 0]

    # 6th request → denied.
    denied = await limiter.check("key-A")
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after_seconds is not None
    assert denied.retry_after_seconds >= 1


@pytest.mark.unit
async def test_bucket_refills_with_elapsed_time(fixed_clock) -> None:
    """After enough time passes, the bucket refills and the next
    request succeeds. 60 req/min = 1 req/sec refill rate."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    # Drain the bucket completely.
    for _ in range(60):
        decision = await limiter.check("key-A")
        assert decision.allowed
    # Next one denied.
    assert (await limiter.check("key-A")).allowed is False

    # Advance 5 seconds → 5 tokens refill (1/sec rate).
    fixed_clock[0] += 5
    for _ in range(5):
        decision = await limiter.check("key-A")
        assert decision.allowed, "should allow 5 requests after 5s refill"
    # 6th denied again.
    assert (await limiter.check("key-A")).allowed is False


@pytest.mark.unit
async def test_bucket_capacity_caps_refill(fixed_clock) -> None:
    """After a long idle period, the bucket is FULL (capacity) — never
    over. A 1-hour idle on a 60/min limit doesn't give you 3600
    requests, just 60."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    # First request consumes 1 → 59 left.
    await limiter.check("key-A")
    # Sleep 1 hour worth of refill (3600 tokens worth).
    fixed_clock[0] += 3600
    decision = await limiter.check("key-A")
    # Should have refilled to capacity (60), then consumed 1.
    assert decision.remaining == 59


@pytest.mark.unit
async def test_per_key_isolation(fixed_clock) -> None:
    """Two keys are independent. Draining A's bucket doesn't affect B."""
    limiter = InProcessRateLimiter(limit_per_minute=3)
    # Drain A.
    for _ in range(3):
        await limiter.check("key-A")
    assert (await limiter.check("key-A")).allowed is False

    # B's bucket is still full — first check consumes 1, leaving 2.
    b_first = await limiter.check("key-B")
    assert b_first.allowed is True
    assert b_first.remaining == 2


@pytest.mark.unit
async def test_retry_after_decreases_as_time_passes(fixed_clock) -> None:
    """``retry_after`` shrinks as elapsed time accumulates — at t=0
    we wait the full 1s, at t=0.5s we wait 0.5s (rounded up to 1)."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    # Drain.
    for _ in range(60):
        await limiter.check("key-A")
    first = await limiter.check("key-A")
    assert first.retry_after_seconds == 1  # 1s to refill 1 token at 1/s

    # Halfway through the refill — still rounds up to 1.
    fixed_clock[0] += 0.4
    second = await limiter.check("key-A")
    assert second.allowed is False
    assert second.retry_after_seconds == 1  # ceil(0.6) = 1


@pytest.mark.unit
async def test_limit_below_one_raises() -> None:
    """``limit_per_minute < 1`` is operator error — fail loud at
    construction. Use the explicit NoOp limiter to disable."""
    with pytest.raises(ValueError, match="limit_per_minute"):
        InProcessRateLimiter(limit_per_minute=0)
    with pytest.raises(ValueError):
        InProcessRateLimiter(limit_per_minute=-1)


@pytest.mark.unit
async def test_noop_always_allows() -> None:
    """``NoOpRateLimiter`` always allows; sentinel limit=0 is the
    operator signal "rate limiting is disabled."""
    limiter = NoOpRateLimiter()
    for _ in range(1000):
        d: RateLimitDecision = await limiter.check("any-key")
        assert d.allowed is True
        assert d.limit == 0


# ---------------------------------------------------------------------------
# 2. Middleware integration — full HTTP path with low capacity
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_app() -> tuple[TestClient, str]:
    """Build an app with a tight 3 req/min limit + a registered API key.

    Returns (client, bearer_token). Each test starts with a full
    bucket since the limiter is fresh per app.
    """
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="rl-test")
    await storage.save_api_key(minted.record)
    app = build_app(storage, rate_limit_per_minute=3)
    return TestClient(app), f"Bearer {minted.full_key}"


@pytest.mark.unit
async def test_authenticated_request_carries_rate_limit_headers(auth_app) -> None:
    """Every successful auth'd response includes ``X-RateLimit-*``
    headers so clients can budget proactively."""
    client, token = auth_app
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 200
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert int(r.headers["X-RateLimit-Remaining"]) >= 0
    assert int(r.headers["X-RateLimit-Reset"]) > 0


@pytest.mark.unit
async def test_burst_exhausts_then_429_with_retry_after(auth_app) -> None:
    """Drain the bucket; next request is 429 with ``Retry-After`` +
    the standard rate-limit headers (so clients can handle it
    programmatically)."""
    client, token = auth_app
    # 3 allowed.
    for _ in range(3):
        r = client.get("/agents", headers={"Authorization": token})
        assert r.status_code == 200

    # 4th denied.
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 429
    body = r.json()
    assert body["detail"]["error"]["code"] == "rate_limited"
    # Retry-After is the standard RFC 7231 header.
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert r.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.unit
async def test_unauthenticated_request_not_rate_limited(auth_app) -> None:
    """Auth fails BEFORE the rate-limit check, so a flood of bad-key
    requests gets 401 (not 429) and doesn't drain anyone's bucket."""
    client, _ = auth_app
    bad = "Bearer mvt_live_deadbeef_00000000_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    for _ in range(20):
        r = client.get("/agents", headers={"Authorization": bad})
        assert r.status_code == 401


@pytest.mark.unit
async def test_healthz_not_rate_limited(auth_app) -> None:
    """``/healthz`` is unauthed → never hits the rate-limit code path.
    Floods of probes from ACA mustn't be capped."""
    client, _ = auth_app
    for _ in range(100):
        r = client.get("/healthz")
        assert r.status_code == 200
    # No rate-limit headers on unauthed endpoints — they don't have an
    # api_key_id to attribute against.
    assert "X-RateLimit-Limit" not in r.headers


@pytest.mark.unit
async def test_ready_not_rate_limited(auth_app) -> None:
    """``/ready`` likewise; ACA hits this every 10s, mustn't burn a budget."""
    client, _ = auth_app
    for _ in range(100):
        r = client.get("/ready")
        assert r.status_code == 200


@pytest.mark.unit
async def test_per_key_isolation_at_http_layer() -> None:
    """Two API keys → two independent buckets. Draining key A doesn't
    affect key B's budget."""
    storage = InMemoryStorage()
    await storage.init()
    a = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="a")
    b = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="b")
    await storage.save_api_key(a.record)
    await storage.save_api_key(b.record)
    client = TestClient(build_app(storage, rate_limit_per_minute=2))

    tok_a = f"Bearer {a.full_key}"
    tok_b = f"Bearer {b.full_key}"

    # Drain A.
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 429

    # B still has a full bucket.
    assert client.get("/agents", headers={"Authorization": tok_b}).status_code == 200


@pytest.mark.unit
async def test_disabled_rate_limit_serves_zero_limit_header() -> None:
    """``rate_limit_per_minute=0`` → NoOpRateLimiter. Every request
    allowed; headers show the sentinel ``Limit: 0`` so operators can
    spot "rate limiting is OFF" without grepping config."""
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    client = TestClient(build_app(storage, rate_limit_per_minute=0))
    token = f"Bearer {minted.full_key}"

    for _ in range(50):
        r = client.get("/agents", headers={"Authorization": token})
        assert r.status_code == 200
    assert r.headers["X-RateLimit-Limit"] == "0"


@pytest.mark.unit
async def test_token_refill_lets_blocked_client_recover(monkeypatch) -> None:
    """End-to-end recovery: get 429, wait the Retry-After window
    (simulated by advancing the limiter's clock), succeed.

    Patches the limiter's clock so we don't actually sleep — same
    pattern as the pure-math tests, applied to the middleware path.
    """
    holder = [1000.0]
    monkeypatch.setattr("movate.core.rate_limit.time.monotonic", lambda: holder[0])
    monkeypatch.setattr("movate.core.rate_limit.time.time", lambda: holder[0])

    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    client = TestClient(build_app(storage, rate_limit_per_minute=2))
    token = f"Bearer {minted.full_key}"

    # Drain.
    client.get("/agents", headers={"Authorization": token})
    client.get("/agents", headers={"Authorization": token})
    denied = client.get("/agents", headers={"Authorization": token})
    assert denied.status_code == 429
    retry_after = int(denied.headers["Retry-After"])

    # Fast-forward past the retry window.
    holder[0] += retry_after + 1

    # Now allowed.
    recovered = client.get("/agents", headers={"Authorization": token})
    assert recovered.status_code == 200


# ---------------------------------------------------------------------------
# 3. Per-tenant aggregate cap (item 25) — a SECOND bucket keyed by tenant_id,
#    capping total throughput across ALL of a tenant's keys so minting more
#    keys can't sidestep the per-key limit.
# ---------------------------------------------------------------------------


async def _two_keys_same_tenant() -> tuple[InMemoryStorage, str, str]:
    """Mint two DIFFERENT api keys for the SAME tenant.

    ``mint_api_key`` with the same ``tenant_id`` yields two distinct
    ``key_id``s (so they land in distinct per-key buckets) but the same
    ``record.tenant_id`` (so they share one per-tenant bucket). Returns
    (storage, bearer_a, bearer_b).
    """
    storage = InMemoryStorage()
    await storage.init()
    tenant_id = uuid4().hex
    a = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="a")
    b = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="b")
    assert a.record.key_id != b.record.key_id  # distinct keys
    assert a.record.tenant_id == b.record.tenant_id  # same tenant
    await storage.save_api_key(a.record)
    await storage.save_api_key(b.record)
    return storage, f"Bearer {a.full_key}", f"Bearer {b.full_key}"


@pytest.mark.unit
async def test_tenant_cap_applies_across_two_keys() -> None:
    """HEADLINE: a tenant can't sidestep the cap by minting more keys.

    Two DIFFERENT keys for the SAME tenant, per-key limit HIGH (100) and
    tenant cap LOW (2). The 3rd request — spread across the two keys so
    NEITHER key hit its own per-key limit — gets 429 because the shared
    per-tenant bucket is exhausted.
    """
    storage, tok_a, tok_b = await _two_keys_same_tenant()
    client = TestClient(
        build_app(storage, rate_limit_per_minute=100, tenant_rate_limit_per_minute=2)
    )

    # Request 1 on key A, request 2 on key B — both allowed (each key's
    # own bucket has 99 left; the tenant bucket now has 0).
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_b}).status_code == 200

    # Request 3 on key A again: key A's per-key bucket still has plenty,
    # but the tenant aggregate is drained → 429.
    r = client.get("/agents", headers={"Authorization": tok_a})
    assert r.status_code == 429
    body = r.json()
    assert body["detail"]["error"]["code"] == "rate_limited"
    # The tenant ceiling is what was hit — its headers reflect it.
    assert r.headers["X-RateLimit-Tenant-Limit"] == "2"
    assert r.headers["X-RateLimit-Tenant-Remaining"] == "0"
    assert int(r.headers["Retry-After"]) >= 1


@pytest.mark.unit
async def test_per_key_limit_still_independent_with_high_tenant_cap() -> None:
    """Per-key limiting keeps working on its own. Per-key LOW (2), tenant
    cap HIGH (1000): the 3rd request on a SINGLE key 429s on the per-key
    bucket even though the tenant aggregate is nowhere near its cap."""
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="pk")
    await storage.save_api_key(minted.record)
    client = TestClient(
        build_app(storage, rate_limit_per_minute=2, tenant_rate_limit_per_minute=1000)
    )
    token = f"Bearer {minted.full_key}"

    assert client.get("/agents", headers={"Authorization": token}).status_code == 200
    assert client.get("/agents", headers={"Authorization": token}).status_code == 200
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 429
    # The per-key ceiling is what was hit.
    assert r.headers["X-RateLimit-Limit"] == "2"
    assert r.headers["X-RateLimit-Remaining"] == "0"
    # Tenant bucket far from exhausted — plenty of remaining.
    assert int(r.headers["X-RateLimit-Tenant-Remaining"]) > 0


@pytest.mark.unit
async def test_tenant_limit_default_off_is_byte_for_byte_today() -> None:
    """BACK-COMPAT GUARD: ``tenant_rate_limit_per_minute=None`` (the
    default) → NoOp tenant limiter that NEVER 429s. Behavior is exactly
    today's per-key-only path: with a generous per-key limit, a flood of
    requests across many keys for one tenant is never tenant-capped.

    Also asserts the additive tenant headers carry the inert sentinel
    (Limit: 0) rather than appearing/disappearing — mirrors the per-key
    OFF signal."""
    storage, tok_a, tok_b = await _two_keys_same_tenant()
    # tenant_rate_limit_per_minute omitted → default None → OFF.
    client = TestClient(build_app(storage, rate_limit_per_minute=100))

    # 50 requests spread across both keys for the one tenant — far more
    # than any tenant cap would allow, yet never 429 (no tenant cap).
    last = None
    for i in range(50):
        tok = tok_a if i % 2 == 0 else tok_b
        last = client.get("/agents", headers={"Authorization": tok})
        assert last.status_code == 200
    assert last is not None
    # Tenant headers present but inert (sentinel NoOp limit = 0).
    assert last.headers["X-RateLimit-Tenant-Limit"] == "0"
    # Per-key headers unchanged — the real per-key bucket is reported.
    assert last.headers["X-RateLimit-Limit"] == "100"


@pytest.mark.unit
async def test_tenant_isolation_a_does_not_429_b() -> None:
    """Tenant A exhausting its aggregate bucket must NOT 429 tenant B.
    Two SEPARATE tenants, low tenant cap (2) + high per-key (100)."""
    storage = InMemoryStorage()
    await storage.init()
    a = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="ta")
    b = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="tb")
    await storage.save_api_key(a.record)
    await storage.save_api_key(b.record)
    client = TestClient(
        build_app(storage, rate_limit_per_minute=100, tenant_rate_limit_per_minute=2)
    )
    tok_a = f"Bearer {a.full_key}"
    tok_b = f"Bearer {b.full_key}"

    # Drain tenant A's aggregate bucket (cap 2).
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 429

    # Tenant B's aggregate bucket is independent — still full.
    assert client.get("/agents", headers={"Authorization": tok_b}).status_code == 200


@pytest.mark.unit
async def test_tenant_headers_present_on_allowed_responses() -> None:
    """Both per-key and per-tenant headers ride every allowed response so
    a client can budget against either ceiling proactively."""
    storage, tok_a, _ = await _two_keys_same_tenant()
    client = TestClient(
        build_app(storage, rate_limit_per_minute=100, tenant_rate_limit_per_minute=10)
    )
    r = client.get("/agents", headers={"Authorization": tok_a})
    assert r.status_code == 200
    # Per-key headers (unchanged).
    assert r.headers["X-RateLimit-Limit"] == "100"
    assert int(r.headers["X-RateLimit-Remaining"]) >= 0
    assert int(r.headers["X-RateLimit-Reset"]) > 0
    # Tenant headers (additive).
    assert r.headers["X-RateLimit-Tenant-Limit"] == "10"
    assert r.headers["X-RateLimit-Tenant-Remaining"] == "9"  # one consumed
    assert int(r.headers["X-RateLimit-Tenant-Reset"]) > 0


@pytest.mark.unit
async def test_retry_after_is_max_of_both_buckets(monkeypatch) -> None:
    """When both ceilings are binding, ``Retry-After`` is the MAX of the
    per-key and per-tenant waits, so a single back-off clears both.

    Per-key cap 60 (1 token/sec refill → ~1s wait once empty); tenant cap
    6 (0.1 token/sec refill → ~10s wait once empty). Drain BOTH on one
    key; the denied request's Retry-After should equal the tenant's
    longer wait (~10s), not the per-key 1s."""
    holder = [1000.0]
    monkeypatch.setattr("movate.core.rate_limit.time.monotonic", lambda: holder[0])
    monkeypatch.setattr("movate.core.rate_limit.time.time", lambda: holder[0])

    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="m")
    await storage.save_api_key(minted.record)
    client = TestClient(
        build_app(storage, rate_limit_per_minute=60, tenant_rate_limit_per_minute=6)
    )
    token = f"Bearer {minted.full_key}"

    # 6 requests drain the tenant bucket (cap 6); per-key bucket (cap 60)
    # still has 54 left.
    for _ in range(6):
        assert client.get("/agents", headers={"Authorization": token}).status_code == 200

    # 7th: per-key allows, tenant denies → 429 with the TENANT's longer
    # retry-after. Tenant refill is 6/60 = 0.1 tok/s → ~10s to refill 1.
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 429
    retry_after = int(r.headers["Retry-After"])
    assert retry_after == 10  # ceil(1 / 0.1) — the tenant wait, not the 1s per-key wait
    # Tenant headers reflect the binding ceiling.
    assert r.headers["X-RateLimit-Tenant-Limit"] == "6"
    assert r.headers["X-RateLimit-Tenant-Remaining"] == "0"


@pytest.mark.unit
async def test_tenant_limit_env_override_respected(monkeypatch) -> None:
    """``MDK_TENANT_RATE_LIMIT_PER_MINUTE`` is honored when the kwarg is
    not passed (left at its ``None`` default)."""
    monkeypatch.setenv("MDK_TENANT_RATE_LIMIT_PER_MINUTE", "2")
    storage, tok_a, tok_b = await _two_keys_same_tenant()
    # No tenant_rate_limit_per_minute kwarg → falls back to the env var.
    client = TestClient(build_app(storage, rate_limit_per_minute=100))

    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_b}).status_code == 200
    # 3rd across the two keys → tenant bucket (env-configured cap 2) drained.
    r = client.get("/agents", headers={"Authorization": tok_a})
    assert r.status_code == 429
    assert r.headers["X-RateLimit-Tenant-Limit"] == "2"


@pytest.mark.unit
async def test_explicit_kwarg_wins_over_env(monkeypatch) -> None:
    """An explicit ``tenant_rate_limit_per_minute`` kwarg overrides the
    env var (precedence: kwarg > env > OFF)."""
    monkeypatch.setenv("MDK_TENANT_RATE_LIMIT_PER_MINUTE", "2")
    storage, tok_a, tok_b = await _two_keys_same_tenant()
    # Explicit high cap (1000) must beat the env's low cap (2).
    client = TestClient(
        build_app(storage, rate_limit_per_minute=100, tenant_rate_limit_per_minute=1000)
    )
    # Far more than the env's cap of 2 — never 429 because the kwarg won.
    for i in range(10):
        tok = tok_a if i % 2 == 0 else tok_b
        assert client.get("/agents", headers={"Authorization": tok}).status_code == 200


# Suppress an unused-import warning for ``time`` (only used by
# fixtures/monkeypatching — pyflakes can't see through the dotted
# string in ``monkeypatch.setattr``).
_ = time
