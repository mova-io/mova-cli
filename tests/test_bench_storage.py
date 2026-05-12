"""Parametrized storage round-trip tests for ``BenchRecord``.

Mirror the existing ``test_eval_storage.py``-style coverage (or
``test_jobs_storage.py`` if you're looking at how the parametrized
``storage`` fixture from ``conftest.py`` is used) but for the new
bench persistence surface:

* save → get round-trip preserves every field including the nested
  ``models`` list
* tenant isolation — cross-tenant ``get_bench`` returns ``None``
* ``list_benches`` filters by tenant + agent and orders newest-first
* judge_method = None (cost-only bench, no judge) round-trips correctly

Backed by the ``storage`` fixture, which parametrizes over memory +
sqlite + (when ``MOVATE_PG_TEST_URL`` is set) postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from movate.core.models import BenchModelRow, BenchRecord, JudgeMethod


def _make_record(
    *,
    bench_id: str | None = None,
    tenant_id: str = "tenant-a",
    agent: str = "demo-agent",
    judge_method: JudgeMethod | None = JudgeMethod.LLM_JUDGE,
    judge_provider: str | None = "openai/gpt-4o-mini-2024-07-18",
    created_at: datetime | None = None,
    models: list[BenchModelRow] | None = None,
) -> BenchRecord:
    return BenchRecord(
        bench_id=bench_id or str(uuid4()),
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        input_hash="a1b2c3d4e5f60718",
        judge_method=judge_method,
        judge_provider=judge_provider,
        rubric="prefer concise correct answers",
        runs_per_model=3,
        gate_mode="mean",
        total_cost_usd=0.0042,
        models=models
        if models is not None
        else [
            BenchModelRow(
                provider="openai/gpt-4o-mini-2024-07-18",
                successful_runs=3,
                error_count=0,
                cost_total_usd=0.0012,
                cost_mean_usd=0.0004,
                latency_p50_ms=420,
                latency_p95_ms=510,
                score=0.85,
            ),
            BenchModelRow(
                provider="anthropic/claude-3-5-haiku-20241022",
                successful_runs=2,
                error_count=1,
                cost_total_usd=0.0030,
                cost_mean_usd=0.0015,
                latency_p50_ms=620,
                latency_p95_ms=780,
                score=None,
                skipped_reason="cross-family judge",
                skipped_score=True,
            ),
        ],
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# save → get round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_bench_round_trip(storage) -> None:
    """Every field of BenchRecord (including the nested models list and
    each BenchModelRow's optional fields) survives save → get."""
    record = _make_record()
    await storage.save_bench(record)

    got = await storage.get_bench(record.bench_id, tenant_id="tenant-a")
    assert got is not None
    # Pydantic models compare structurally — full equality is the
    # strongest possible assertion here.
    assert got == record


@pytest.mark.unit
async def test_get_bench_returns_none_for_missing(storage) -> None:
    assert await storage.get_bench("does-not-exist", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_get_bench_returns_none_for_cross_tenant(storage) -> None:
    """Tenant isolation: tenant B can't read tenant A's record even by
    guessing the bench_id. ``None`` (not 403, not 404) so existence
    isn't leaked across the tenancy boundary."""
    record = _make_record(tenant_id="tenant-a")
    await storage.save_bench(record)

    got = await storage.get_bench(record.bench_id, tenant_id="tenant-b")
    assert got is None


@pytest.mark.unit
async def test_save_and_get_bench_with_no_judge(storage) -> None:
    """Cost-only bench (no judge configured) → judge_method=None,
    judge_provider=None, every model's score=None. Round-trip preserves
    the optional fields without unwrapping them to default values."""
    record = _make_record(
        judge_method=None,
        judge_provider=None,
        models=[
            BenchModelRow(
                provider="openai/gpt-4o-mini-2024-07-18",
                successful_runs=3,
                error_count=0,
                cost_total_usd=0.0009,
                cost_mean_usd=0.0003,
                latency_p50_ms=400,
                latency_p95_ms=500,
                score=None,
            ),
        ],
    )
    await storage.save_bench(record)

    got = await storage.get_bench(record.bench_id, tenant_id="tenant-a")
    assert got is not None
    assert got.judge_method is None
    assert got.judge_provider is None
    assert got.models[0].score is None
    assert got == record


# ---------------------------------------------------------------------------
# list_benches — filtering + ordering
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_benches_filters_by_tenant_and_agent(storage) -> None:
    """``list_benches`` filters by tenant + agent. Cross-tenant rows
    never leak into a tenant-scoped listing."""
    a1 = _make_record(tenant_id="tenant-a", agent="alpha")
    a2 = _make_record(tenant_id="tenant-a", agent="beta")
    b1 = _make_record(tenant_id="tenant-b", agent="alpha")
    for r in (a1, a2, b1):
        await storage.save_bench(r)

    only_a = await storage.list_benches(tenant_id="tenant-a")
    assert {r.bench_id for r in only_a} == {a1.bench_id, a2.bench_id}

    a_alpha = await storage.list_benches(tenant_id="tenant-a", agent="alpha")
    assert {r.bench_id for r in a_alpha} == {a1.bench_id}


@pytest.mark.unit
async def test_list_benches_orders_newest_first(storage) -> None:
    """Trend dashboards / baseline lookups want the most recent first.
    Asserts ORDER BY created_at DESC across all three backends."""
    older = _make_record(created_at=datetime.now(UTC) - timedelta(seconds=10))
    newer = _make_record(created_at=datetime.now(UTC))
    # Save out of order on purpose — insertion order shouldn't matter.
    await storage.save_bench(newer)
    await storage.save_bench(older)

    rows = await storage.list_benches()
    assert [r.bench_id for r in rows] == [newer.bench_id, older.bench_id]


@pytest.mark.unit
async def test_list_benches_respects_limit(storage) -> None:
    """``limit`` caps the result set — default 20, but the parameter is
    honored. Trend queries fetch a small recent window, not all history."""
    for _ in range(5):
        await storage.save_bench(_make_record())

    rows = await storage.list_benches(limit=3)
    assert len(rows) == 3
