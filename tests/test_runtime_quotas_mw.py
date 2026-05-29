"""Per-tenant quota admission middleware (ADR 036 D2) — hermetic runtime tests.

Builds a FastAPI test app per case with ``InMemoryStorage`` + a monkeypatched
quota config, exercises the write routes that gate on
:func:`~movate.runtime.middleware.make_quota_dependency`, and asserts the
admission behavior matches the spec:

* no config = byte-for-byte zero behavior change (no enforcement)
* ``warn`` mode = 2xx + ``X-Quota-Warning`` header when over a ceiling
* ``deny`` mode = ``429 quota_exceeded`` envelope when over a ceiling
* under-limit = always passes (no header, no block)
* admin tenant = always allowed even with a configured ceiling
* the per-(tenant, route_class) cache reduces ``build_usage`` call count

Mirrors the test pattern of ``tests/test_runtime_usage_v1.py`` (same
:class:`InMemoryStorage` + ``mint_api_key`` plumbing) so the new module reads
the same as the rest of the runtime suite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import JobStatus, Metrics, RunRecord, TokenUsage
from movate.core.quotas import (
    QuotaConfig,
    QuotaMode,
    TenantQuota,
)
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Helpers — synthesize persisted runs so build_usage has real input
# ---------------------------------------------------------------------------


def _run(
    *,
    tenant_id: str,
    run_id: str,
    agent: str = "triage",
    tokens_in: int = 100,
    tokens_out: int = 50,
    cost: float = 0.05,
    when: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="hash",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2026-05",
        status=JobStatus.SUCCESS,
        input={"q": "x"},
        output={"a": "y"},
        metrics=Metrics(
            cost_usd=cost,
            latency_ms=100,
            tokens=TokenUsage(input=tokens_in, output=tokens_out),
            provider="openai/gpt-4o-mini",
        ),
        created_at=when or datetime.now(UTC),
    )


def _build_test_app(
    storage: InMemoryStorage,
    *,
    monkeypatch: pytest.MonkeyPatch,
    config: QuotaConfig | None,
) -> TestClient:
    """Build a FastAPI app with a hermetic quota config injected.

    Monkeypatches ``movate.runtime.app.load_quota_config`` to return
    ``config`` (rather than reading disk), then calls ``build_app``. The
    rate limiter is set high so no rate-limit 429s interfere with the
    quota-429 path.
    """
    monkeypatch.setattr(
        "movate.runtime.app.load_quota_config",
        lambda *_a, **_kw: config,
    )
    app = build_app(storage, rate_limit_per_minute=10_000)
    return TestClient(app)


async def _mint(
    storage: InMemoryStorage,
    *,
    scopes: list[str],
    tenant_id: str | None = None,
) -> tuple[dict[str, str], str]:
    """Mint an API key + return ``(Authorization header, tenant_id)``."""
    tid = tenant_id or uuid4().hex
    minted = mint_api_key(
        tenant_id=tid,
        env=ApiKeyEnv.LIVE,
        label="quota-mw-tests",
        scopes=scopes,
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tid


# ---------------------------------------------------------------------------
# 1. No config = no enforcement (opt-in posture)
# ---------------------------------------------------------------------------


async def test_no_config_no_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no ``quotas.yaml`` is loaded, write routes pass through
    untouched even with a tenant whose persisted usage would otherwise trip
    a ceiling — byte-for-byte unchanged from pre-D2 behavior."""
    storage = InMemoryStorage()
    await storage.init()
    client = _build_test_app(storage, monkeypatch=monkeypatch, config=None)
    auth_header, tenant_id = await _mint(storage, scopes=["run"])

    # Seed enormous usage that WOULD trip every ceiling — should still pass.
    for i in range(5):
        await storage.save_run(
            _run(
                tenant_id=tenant_id,
                run_id=f"r{i}",
                tokens_in=10_000,
                tokens_out=10_000,
                cost=100.0,
            )
        )

    # Hit the unversioned /run endpoint (smallest body shape; same _quota gate).
    r = client.post(
        "/run",
        headers=auth_header,
        json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
    )
    # We don't care about the body — only that the quota gate didn't 429.
    assert r.status_code != 429, r.text
    assert "X-Quota-Warning" not in r.headers


# ---------------------------------------------------------------------------
# 2. Warn mode — passthrough + header
# ---------------------------------------------------------------------------


async def test_warn_mode_over_limit_returns_header_but_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``warn`` mode = log + ``X-Quota-Warning`` header + allow (2xx).
    The middleware NEVER blocks in warn mode, no matter how far over."""
    storage = InMemoryStorage()
    await storage.init()
    auth_header, tenant_id = await _mint(storage, scopes=["run"])
    # Seed a single run that's ALREADY over a 100-token ceiling.
    await storage.save_run(_run(tenant_id=tenant_id, run_id="r1", tokens_in=200, tokens_out=200))

    config = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id=tenant_id,
                daily_token_limit=100,
                mode=QuotaMode.WARN,
            )
        ]
    )
    client = _build_test_app(storage, monkeypatch=monkeypatch, config=config)

    r = client.post(
        "/run",
        headers=auth_header,
        json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
    )
    # Allowed — but the warn header is attached.
    assert r.status_code != 429, r.text
    assert "X-Quota-Warning" in r.headers
    assert "daily_tokens" in r.headers["X-Quota-Warning"]


# ---------------------------------------------------------------------------
# 3. Deny mode — 429 envelope shape
# ---------------------------------------------------------------------------


async def test_deny_mode_over_limit_returns_429_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``deny`` mode = 429 ``quota_exceeded`` envelope with ``remaining``."""
    storage = InMemoryStorage()
    await storage.init()
    auth_header, tenant_id = await _mint(storage, scopes=["run"])
    await storage.save_run(_run(tenant_id=tenant_id, run_id="r1", tokens_in=200, tokens_out=200))

    config = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id=tenant_id,
                daily_token_limit=100,
                mode=QuotaMode.DENY,
            )
        ]
    )
    client = _build_test_app(storage, monkeypatch=monkeypatch, config=config)

    r = client.post(
        "/run",
        headers=auth_header,
        json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
    )
    assert r.status_code == 429
    body = r.json()
    # Same {error: {code, message, request_id}} envelope as the rest of
    # the runtime — plus the additive ``remaining`` field.
    assert body["detail"]["error"]["code"] == "quota_exceeded"
    assert "daily_tokens" in body["detail"]["error"]["message"]
    # remaining clamps to >=0 — current was way over the 100-token cap.
    assert body["detail"]["error"]["remaining"]["daily_tokens"] == 0


# ---------------------------------------------------------------------------
# 4. Under-limit — clean pass
# ---------------------------------------------------------------------------


async def test_under_limit_passes_with_no_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured ceiling, but usage well under it → no warn header, no
    429, request flows to the handler."""
    storage = InMemoryStorage()
    await storage.init()
    auth_header, tenant_id = await _mint(storage, scopes=["run"])
    # Seed 10 tokens — well under the 1000-token ceiling.
    await storage.save_run(_run(tenant_id=tenant_id, run_id="r1", tokens_in=5, tokens_out=5))

    config = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id=tenant_id,
                daily_token_limit=1000,
                mode=QuotaMode.DENY,
            )
        ]
    )
    client = _build_test_app(storage, monkeypatch=monkeypatch, config=config)

    r = client.post(
        "/run",
        headers=auth_header,
        json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
    )
    assert r.status_code != 429, r.text
    assert "X-Quota-Warning" not in r.headers


# ---------------------------------------------------------------------------
# 5. Admin-tenant bypass — always allowed
# ---------------------------------------------------------------------------


async def test_admin_tenant_bypass_always_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant listed in ``admin_tenant_ids`` is never blocked / warned,
    even with a ``deny`` row + over-the-ceiling usage."""
    storage = InMemoryStorage()
    await storage.init()
    auth_header, tenant_id = await _mint(storage, scopes=["run"])
    await storage.save_run(_run(tenant_id=tenant_id, run_id="r1", tokens_in=200, tokens_out=200))

    config = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id=tenant_id,
                daily_token_limit=100,
                mode=QuotaMode.DENY,
            )
        ],
        admin_tenant_ids=[tenant_id],  # the very same tenant — admin bypass
    )
    client = _build_test_app(storage, monkeypatch=monkeypatch, config=config)

    r = client.post(
        "/run",
        headers=auth_header,
        json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
    )
    assert r.status_code != 429, r.text
    assert "X-Quota-Warning" not in r.headers


# ---------------------------------------------------------------------------
# 6. Cache reduces aggregation call count
# ---------------------------------------------------------------------------


async def test_cache_reduces_aggregation_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the per-(tenant, route_class) cache in front, the underlying
    aggregation (``build_usage``) runs ONCE for a burst of requests — not
    once per request. Asserts the spec's perf requirement.

    Implementation: monkeypatch ``movate.runtime.middleware.build_usage`` to
    count how often the aggregation is invoked, fire two requests, and
    confirm the count is 1 (the cache TTL is well above the test's wall
    time, so the second request must hit the cache)."""
    storage = InMemoryStorage()
    await storage.init()
    auth_header, tenant_id = await _mint(storage, scopes=["run"])
    # Modest usage so we don't trip a ceiling — we care about the cache,
    # not the decision.
    for i in range(2):
        await storage.save_run(_run(tenant_id=tenant_id, run_id=f"r{i}"))

    config = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id=tenant_id,
                daily_token_limit=1_000_000,  # very high — won't trip
                mode=QuotaMode.DENY,
            )
        ]
    )

    # Wrap the real build_usage with a counter. The middleware imports
    # ``build_usage`` at the module level, so we patch the binding it sees.
    import movate.runtime.middleware as mw_mod  # noqa: PLC0415

    call_count = {"n": 0}
    real_build = mw_mod.build_usage

    def _counting_build(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(mw_mod, "build_usage", _counting_build)

    client = _build_test_app(storage, monkeypatch=monkeypatch, config=config)

    # Two requests in a row. With a working cache, build_usage runs on the
    # first (cache miss → compute) and the second hits the cache.
    for _ in range(2):
        r = client.post(
            "/run",
            headers=auth_header,
            json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
        )
        assert r.status_code != 429, r.text

    # One miss → daily + monthly = 2 calls to build_usage. Without the
    # cache it would be 4. Either way the assertion is "less than per-
    # request": >0 but <= the once-per-request count for two requests.
    assert call_count["n"] == 2, (
        f"expected 2 build_usage calls (one cache miss x daily+monthly), got {call_count['n']}"
    )


async def test_cache_disabled_recomputes_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control case: with TTL=0 (cache disabled), aggregation runs on every
    request. This proves the previous test's cache claim is real."""
    storage = InMemoryStorage()
    await storage.init()
    auth_header, tenant_id = await _mint(storage, scopes=["run"])
    await storage.save_run(_run(tenant_id=tenant_id, run_id="r1"))

    config = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id=tenant_id,
                daily_token_limit=1_000_000,
                mode=QuotaMode.DENY,
            )
        ]
    )

    # Patch the constant the factory uses, so make_quota_dependency builds
    # a TTL=0 cache (no caching).
    import movate.runtime.middleware as mw_mod  # noqa: PLC0415

    monkeypatch.setattr(mw_mod, "QUOTA_CACHE_TTL_S", 0)

    call_count = {"n": 0}
    real_build = mw_mod.build_usage

    def _counting_build(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(mw_mod, "build_usage", _counting_build)

    client = _build_test_app(storage, monkeypatch=monkeypatch, config=config)

    for _ in range(2):
        r = client.post(
            "/run",
            headers=auth_header,
            json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
        )
        assert r.status_code != 429, r.text

    # No caching → daily + monthly per request, 2 requests = 4 calls.
    assert call_count["n"] == 4, (
        f"expected 4 build_usage calls with TTL=0 (every request recomputes), got {call_count['n']}"
    )


# ---------------------------------------------------------------------------
# 7. Unrelated tenant unaffected
# ---------------------------------------------------------------------------


async def test_tenant_without_row_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant who has no row in the config passes through regardless of
    what other tenants in the same config have configured — quotas are
    opt-in per tenant."""
    storage = InMemoryStorage()
    await storage.init()
    auth_header, my_tenant = await _mint(storage, scopes=["run"])
    # Someone else has a deny-mode row, but that's not us.
    other_tenant = uuid4().hex

    config = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id=other_tenant,
                daily_token_limit=1,
                mode=QuotaMode.DENY,
            )
        ]
    )
    client = _build_test_app(storage, monkeypatch=monkeypatch, config=config)

    # Even with usage that WOULD trip the other tenant's ceiling, we pass.
    await storage.save_run(
        _run(tenant_id=my_tenant, run_id="r1", tokens_in=10_000, tokens_out=10_000),
    )

    r = client.post(
        "/run",
        headers=auth_header,
        json={"kind": "agent", "target": "triage", "input": {"q": "hi"}},
    )
    assert r.status_code != 429, r.text
    assert "X-Quota-Warning" not in r.headers
