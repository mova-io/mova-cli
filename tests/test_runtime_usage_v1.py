"""ADR 036 D1 — per-tenant usage metering endpoint.

``GET /api/v1/usage`` — per-tenant billing-visibility rollup (scope ``read``):
``requests`` / ``tokens_in`` / ``tokens_out`` / ``cost_usd`` over a time
window, with optional ``by_agent`` + ``by_provider`` breakdowns. Reuses the
per-run records the executor already persists (ADR 024 ``RunRecord.metrics``);
the new aggregator lives in :mod:`movate.core.reporting` (``cli ⊥ runtime``).

Tenant scoping:

* Non-admin keys see only their **own** tenant — the ``tenant`` query param
  is silently ignored on the non-admin path (no cross-tenant leak).
* Admin keys (scope ``admin`` in addition to ``read``) may pass ``tenant=<id>``
  to read another tenant's rollup.

Hermetic: TestClient + ``InMemoryStorage`` seeded with ``RunRecord`` rows
directly (no LLM, no DB, no server). Mirrors ``tests/test_runtime_report_v1.py``.

Coverage:

* Empty store → a zeroed rollup (200), not a 500.
* Tenant scoping: non-admin restricted to own; admin can cross with ``tenant=``.
* ``window`` param narrows to the last N days; default is 30.
* ``by_agent`` + ``by_provider`` breakdowns sum to totals; sorted by cost.
* ``agent=`` query narrows to a single agent.
* Auth: 401 unauthed; ``read`` scope is the gate; bounded ``window`` (422).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import JobStatus, Metrics, RunRecord, TokenUsage
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Record builders (mirror tests/test_runtime_report_v1.py)
# ---------------------------------------------------------------------------


def _run(
    *,
    tenant_id: str,
    run_id: str = "r1",
    agent: str = "triage",
    provider: str = "openai/gpt-4o-mini",
    status: JobStatus = JobStatus.SUCCESS,
    cost: float = 0.001,
    tokens_in: int = 10,
    tokens_out: int = 5,
    when: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="hash",
        provider=provider,
        provider_version="v1",
        pricing_version="2026-05",
        status=status,
        input={"q": "x"},
        output={"a": "y"} if status == JobStatus.SUCCESS else None,
        metrics=Metrics(
            cost_usd=cost,
            latency_ms=100,
            tokens=TokenUsage(input=tokens_in, output=tokens_out),
            provider=provider,
        ),
        created_at=when or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    """A ``read``-scoped key + tenant_id. /usage gates on ``read``."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="usage-v1-tests", scopes=["read"]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


@pytest.fixture
async def admin_auth_setup(storage: InMemoryStorage):
    """An ``admin`` + ``read`` scoped key — the cross-tenant path."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="usage-v1-admin-tests",
        scopes=["read", "admin"],
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


async def _seed_three_runs(storage: InMemoryStorage, tenant_id: str) -> None:
    """Two agents, two providers, three runs."""
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="t1",
            agent="triage",
            provider="openai/gpt-4o-mini",
            cost=0.05,
            tokens_in=100,
            tokens_out=50,
        )
    )
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="t2",
            agent="triage",
            provider="openai/gpt-4o-mini",
            cost=0.06,
            tokens_in=200,
            tokens_out=80,
        )
    )
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="s1",
            agent="summary",
            provider="anthropic/claude-3-5",
            cost=0.02,
            tokens_in=40,
            tokens_out=20,
        )
    )


# ---------------------------------------------------------------------------
# Empty / zeroed paths
# ---------------------------------------------------------------------------


async def test_usage_empty_store_is_zeroed_not_500(client: TestClient, auth_setup) -> None:
    auth_header, tenant_id = auth_setup
    r = client.get("/api/v1/usage", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == tenant_id
    assert body["window_days"] == 30  # default
    assert body["agent_filter"] is None
    assert body["totals"]["requests"] == 0
    assert body["totals"]["tokens_in"] == 0
    assert body["totals"]["tokens_out"] == 0
    assert body["totals"]["cost_usd"] == 0.0
    assert body["totals"]["key"] == tenant_id
    assert body["by_agent"] == []
    assert body["by_provider"] == []


# ---------------------------------------------------------------------------
# Aggregation + breakdowns
# ---------------------------------------------------------------------------


async def test_usage_aggregates_seeded_runs(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_three_runs(storage, tenant_id)

    r = client.get("/api/v1/usage", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()

    # totals = sum across all three runs
    assert body["totals"]["requests"] == 3
    assert body["totals"]["tokens_in"] == 340  # 100 + 200 + 40
    assert body["totals"]["tokens_out"] == 150  # 50 + 80 + 20
    assert abs(body["totals"]["cost_usd"] - 0.13) < 1e-9

    # by_agent — two agents, sorted by cost desc (triage $0.11 > summary $0.02)
    assert [r["key"] for r in body["by_agent"]] == ["triage", "summary"]
    triage = next(r for r in body["by_agent"] if r["key"] == "triage")
    assert triage["requests"] == 2
    assert triage["tokens_in"] == 300
    assert triage["tokens_out"] == 130
    assert abs(triage["cost_usd"] - 0.11) < 1e-9

    # by_provider — two providers, sorted by cost desc (openai > anthropic)
    assert [r["key"] for r in body["by_provider"]] == [
        "openai/gpt-4o-mini",
        "anthropic/claude-3-5",
    ]

    # breakdowns sum back to totals
    assert sum(r["requests"] for r in body["by_agent"]) == body["totals"]["requests"]
    assert sum(r["cost_usd"] for r in body["by_provider"]) == pytest.approx(
        body["totals"]["cost_usd"]
    )


async def test_usage_agent_filter_narrows(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_three_runs(storage, tenant_id)

    r = client.get("/api/v1/usage?agent=triage", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_filter"] == "triage"
    # only triage's two runs contribute
    assert body["totals"]["requests"] == 2
    assert body["totals"]["tokens_in"] == 300
    assert {r["key"] for r in body["by_agent"]} == {"triage"}
    # summary's provider should not appear
    assert {r["key"] for r in body["by_provider"]} == {"openai/gpt-4o-mini"}


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


async def test_usage_non_admin_cannot_read_other_tenant(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """A non-admin key passing ``tenant=<other>`` is silently scoped back to
    its own tenant — no cross-tenant leak, no probe-able 403."""
    auth_header, tenant_id = auth_setup
    await _seed_three_runs(storage, tenant_id)
    # An OTHER tenant with a wildly different cost — must NOT appear.
    other = uuid4().hex
    await storage.save_run(
        _run(tenant_id=other, run_id="x1", agent="evil", cost=999.0, tokens_in=99_999)
    )

    r = client.get(f"/api/v1/usage?tenant={other}", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    # Scoped back to the caller's own tenant, not ``other``.
    assert body["tenant_id"] == tenant_id
    assert body["totals"]["requests"] == 3
    assert body["totals"]["cost_usd"] < 1.0  # the $999 other-tenant run excluded
    # ``evil`` agent must not surface.
    assert "evil" not in {r["key"] for r in body["by_agent"]}


async def test_usage_admin_can_read_another_tenant(
    client: TestClient, storage: InMemoryStorage, admin_auth_setup
) -> None:
    """An admin key may pass ``tenant=<id>`` to read another tenant's rollup."""
    auth_header, _admin_tenant = admin_auth_setup
    other = uuid4().hex
    await storage.save_run(
        _run(
            tenant_id=other,
            run_id="o1",
            agent="alpha",
            cost=1.25,
            tokens_in=500,
            tokens_out=200,
        )
    )

    r = client.get(f"/api/v1/usage?tenant={other}", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == other
    assert body["totals"]["requests"] == 1
    assert abs(body["totals"]["cost_usd"] - 1.25) < 1e-9
    assert {r["key"] for r in body["by_agent"]} == {"alpha"}


async def test_usage_admin_without_tenant_param_sees_own(
    client: TestClient, storage: InMemoryStorage, admin_auth_setup
) -> None:
    """Admin without ``tenant=`` falls back to own tenant (no implicit
    cross-tenant aggregation)."""
    auth_header, admin_tenant = admin_auth_setup
    await _seed_three_runs(storage, admin_tenant)
    # And some OTHER tenant data which must NOT appear (no ``tenant=`` param).
    other = uuid4().hex
    await storage.save_run(_run(tenant_id=other, run_id="x", agent="x", cost=42.0))

    r = client.get("/api/v1/usage", headers=auth_header)
    body = r.json()
    assert body["tenant_id"] == admin_tenant
    assert body["totals"]["requests"] == 3
    assert body["totals"]["cost_usd"] < 1.0


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


async def test_usage_window_narrows_to_recent(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="old",
            agent="ancient",
            cost=10.0,
            when=datetime.now(UTC) - timedelta(days=60),
        )
    )
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="new",
            agent="fresh",
            cost=0.5,
            when=datetime.now(UTC) - timedelta(hours=1),
        )
    )

    # window=7 days drops the 60-day-old run
    r = client.get("/api/v1/usage?window=7", headers=auth_header)
    body = r.json()
    assert body["window_days"] == 7
    assert body["totals"]["requests"] == 1
    assert "ancient" not in {r["key"] for r in body["by_agent"]}
    assert "fresh" in {r["key"] for r in body["by_agent"]}

    # window=0 = all-time
    r = client.get("/api/v1/usage?window=0", headers=auth_header)
    body = r.json()
    assert body["window_days"] == 0
    assert body["totals"]["requests"] == 2


async def test_usage_default_window_is_30_days(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Default ``window=30`` matches the typical billing period — older
    runs are excluded unless the caller asks for all-time."""
    auth_header, tenant_id = auth_setup
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="old",
            agent="ancient",
            when=datetime.now(UTC) - timedelta(days=45),
        )
    )
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="new",
            agent="fresh",
            when=datetime.now(UTC) - timedelta(days=10),
        )
    )
    r = client.get("/api/v1/usage", headers=auth_header)
    body = r.json()
    assert body["window_days"] == 30
    assert {r["key"] for r in body["by_agent"]} == {"fresh"}


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


async def test_usage_rejects_negative_window(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/usage?window=-1", headers=auth_header)
    assert r.status_code == 422


async def test_usage_rejects_excessive_window(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/usage?window=99999", headers=auth_header)
    assert r.status_code == 422


def test_usage_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/usage").status_code == 401


async def test_usage_requires_read_scope(client: TestClient, storage: InMemoryStorage) -> None:
    """A key WITHOUT the ``read`` scope is 403'd (the gate is ``read``)."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="usage-no-read",
        scopes=["run"],  # explicitly NOT ``read``
    )
    await storage.save_api_key(minted.record)
    r = client.get(
        "/api/v1/usage",
        headers={"Authorization": f"Bearer {minted.full_key}"},
    )
    assert r.status_code == 403
