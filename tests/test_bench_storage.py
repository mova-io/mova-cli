"""Bench result storage — save/get/list round-trip + tenant isolation.

BACKLOG #64. Mirrors ``tests/test_jobs_storage.py`` (and the eval storage
coverage): the same three backends in scope via the shared ``storage``
fixture in conftest.py — ``InMemoryStorage``, ``SqliteProvider``, and
``PostgresProvider`` (skipped when ``MOVATE_PG_TEST_URL`` is unset).

Asserts the contract the API + dispatch rely on:

* ``save_bench`` then ``get_bench`` round-trips every field, including
  the per-model rows and the JSON ``input`` payload.
* ``get_bench`` is tenant-scoped — a wrong-tenant id returns ``None``
  (404-not-403 semantics, no existence leak).
* ``list_bench`` filters by agent, returns newest-first, and honors limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from movate.core.models import BenchModelResult, BenchRecord, JudgeMethod

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_bench(
    *,
    bench_id: str | None = None,
    tenant_id: str = "tenant-a",
    agent: str = "demo-agent",
    created_at: datetime | None = None,
    scored: bool = False,
    prompt_hash: str | None = None,
) -> BenchRecord:
    return BenchRecord(
        bench_id=bench_id or str(uuid4()),
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash=prompt_hash,
        input={"text": "hi"},
        judge_method=JudgeMethod.LLM_JUDGE if scored else None,
        judge_provider="anthropic/claude-haiku-4-5-20251001" if scored else None,
        runs_per_model=2,
        gate_mode="mean",
        models=[
            BenchModelResult(
                provider="openai/gpt-4o-mini-2024-07-18",
                score=0.85 if scored else None,
                judge_skipped=False,
                cost_mean_usd=0.000123,
                cost_total_usd=0.000246,
                latency_p50_ms=420,
                latency_p95_ms=600,
                error_count=0,
                sample_output={"output": "hello"},
            ),
            BenchModelResult(
                provider="anthropic/claude-haiku-4-5-20251001",
                score=None,
                judge_skipped=True,  # same family as judge → skipped
                cost_mean_usd=0.0,
                cost_total_usd=0.0,
                latency_p50_ms=0,
                latency_p95_ms=0,
                error_count=1,
                sample_output=None,
            ),
        ],
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_bench(storage) -> None:
    b = _make_bench(scored=True)
    await storage.save_bench(b)
    got = await storage.get_bench(b.bench_id, tenant_id="tenant-a")
    assert got is not None
    assert got.bench_id == b.bench_id
    assert got.agent == "demo-agent"
    assert got.agent_version == "0.1.0"
    assert got.input == {"text": "hi"}
    assert got.judge_method == JudgeMethod.LLM_JUDGE
    assert got.judge_provider == "anthropic/claude-haiku-4-5-20251001"
    assert got.runs_per_model == 2
    assert got.gate_mode == "mean"
    # Per-model rows survive the JSON round-trip.
    assert len(got.models) == 2
    first = got.models[0]
    assert first.provider == "openai/gpt-4o-mini-2024-07-18"
    assert first.score == 0.85
    assert first.judge_skipped is False
    assert first.cost_mean_usd == 0.000123
    assert first.latency_p50_ms == 420
    assert first.sample_output == {"output": "hello"}
    second = got.models[1]
    assert second.score is None
    assert second.judge_skipped is True
    assert second.error_count == 1
    assert second.sample_output is None
    # Legacy-shaped record (no prompt_hash) reads back as None (ADR 102 D1).
    assert got.prompt_hash is None


@pytest.mark.unit
async def test_bench_prompt_hash_round_trip(storage) -> None:
    """ADR 102 D1: prompt_hash survives the round-trip when set."""
    b = _make_bench(prompt_hash="beef" * 16)
    await storage.save_bench(b)
    got = await storage.get_bench(b.bench_id, tenant_id="tenant-a")
    assert got is not None
    assert got.prompt_hash == "beef" * 16


@pytest.mark.unit
async def test_save_and_get_bench_no_judge(storage) -> None:
    """Cost+latency-only bench (no scoring) round-trips with null judge."""
    b = _make_bench(scored=False)
    await storage.save_bench(b)
    got = await storage.get_bench(b.bench_id, tenant_id="tenant-a")
    assert got is not None
    assert got.judge_method is None
    assert got.judge_provider is None
    assert got.models[0].score is None


@pytest.mark.unit
async def test_get_bench_returns_none_for_missing(storage) -> None:
    assert await storage.get_bench("ghost", tenant_id="tenant-a") is None


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_bench_is_tenant_scoped(storage) -> None:
    """A wrong-tenant id returns None — no existence leak across tenants."""
    b = _make_bench(tenant_id="tenant-a")
    await storage.save_bench(b)
    # Right tenant sees it.
    assert await storage.get_bench(b.bench_id, tenant_id="tenant-a") is not None
    # Wrong tenant gets None (404-not-403).
    assert await storage.get_bench(b.bench_id, tenant_id="tenant-b") is None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_bench_filters_by_tenant_and_agent(storage) -> None:
    a_foo = _make_bench(tenant_id="tenant-a", agent="foo")
    a_bar = _make_bench(tenant_id="tenant-a", agent="bar")
    b_foo = _make_bench(tenant_id="tenant-b", agent="foo")
    for rec in (a_foo, a_bar, b_foo):
        await storage.save_bench(rec)

    only_a = await storage.list_bench(tenant_id="tenant-a")
    assert {r.bench_id for r in only_a} == {a_foo.bench_id, a_bar.bench_id}

    a_foo_only = await storage.list_bench(tenant_id="tenant-a", agent="foo")
    assert {r.bench_id for r in a_foo_only} == {a_foo.bench_id}


@pytest.mark.unit
async def test_list_bench_orders_newest_first(storage) -> None:
    older = _make_bench(created_at=datetime.now(UTC) - timedelta(seconds=10))
    newer = _make_bench(created_at=datetime.now(UTC))
    await storage.save_bench(older)
    await storage.save_bench(newer)
    rows = await storage.list_bench(tenant_id="tenant-a")
    assert [r.bench_id for r in rows] == [newer.bench_id, older.bench_id]


@pytest.mark.unit
async def test_list_bench_honors_limit(storage) -> None:
    for i in range(5):
        await storage.save_bench(_make_bench(created_at=datetime.now(UTC) - timedelta(seconds=i)))
    rows = await storage.list_bench(tenant_id="tenant-a", limit=3)
    assert len(rows) == 3
