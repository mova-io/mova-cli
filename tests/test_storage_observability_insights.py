"""Storage conformance for the append-only observability insights table (ADR 047).

Runs against every backend the ``storage`` fixture parametrizes (InMemory +
SQLite always; Postgres when ``MOVATE_PG_TEST_URL`` is set), so the three new
:class:`StorageProvider` methods round-trip identically:

* APPEND-ONLY — a re-run for the same (tenant, project, date) INSERTs a new row
  (no update method exists); the latest-by-created_at row wins on read.
* ``get_insight`` / ``list_insights`` are tenant-scoped (no cross-tenant leak).
* ``list_insights`` collapses append-only re-runs to one row per (project, date)
  and honors the project / since / until / limit filters.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from movate.core.observability.models import ObservabilityInsight
from movate.storage.base import StorageProvider


def _insight(
    *,
    tenant_id: str,
    project_id: str = "proj",
    day: date,
    health: float = 90.0,
    created_at: datetime | None = None,
    runs: int = 10,
) -> ObservabilityInsight:
    return ObservabilityInsight(
        tenant_id=tenant_id,
        project_id=project_id,
        date=day,
        health_score=health,
        anomalies=[
            {"metric": "cost", "severity": "warning", "value": 1.0, "baseline": 0.5, "z": 3.1}
        ],
        top_failures=[
            {"signature": "Timeout", "count": 3, "sample_message": "timed out", "agent": "a"}
        ],
        usage_rollup={"runs": runs, "errors": 1, "error_rate": 0.1, "cost_usd": 1.23},
        trends={"cost": {"value": 1.23, "baseline": 0.9, "delta_pct": 36.7}},
        narrative_digest="Yesterday: stable. Watch: cost is up.",
        created_at=created_at or datetime.now(UTC),
    )


async def test_save_and_get_roundtrip(storage: StorageProvider) -> None:
    day = date(2026, 5, 1)
    ins = _insight(tenant_id="t1", day=day)
    await storage.save_insight(ins)

    got = await storage.get_insight("t1", "proj", day)
    assert got is not None
    assert got.id == ins.id
    assert got.health_score == 90.0
    assert got.usage_rollup["runs"] == 10
    assert got.anomalies[0]["metric"] == "cost"
    assert got.top_failures[0]["signature"] == "Timeout"
    assert got.trends["cost"]["delta_pct"] == 36.7
    assert got.narrative_digest.startswith("Yesterday")


async def test_append_only_latest_per_day_wins(storage: StorageProvider) -> None:
    """A re-run for the same day appends a NEW row; reads take the latest."""
    day = date(2026, 5, 2)
    base = datetime(2026, 5, 2, 6, 0, tzinfo=UTC)
    first = _insight(tenant_id="t1", day=day, health=50.0, created_at=base)
    second = _insight(tenant_id="t1", day=day, health=95.0, created_at=base + timedelta(hours=1))
    await storage.save_insight(first)
    await storage.save_insight(second)

    # Both rows persist (append-only — no overwrite). The reader returns the
    # latest by created_at.
    got = await storage.get_insight("t1", "proj", day)
    assert got is not None
    assert got.health_score == 95.0
    assert got.id == second.id

    # list_insights also dedupes per (project, date) to the latest row.
    rows = await storage.list_insights("t1", project_id="proj")
    same_day = [r for r in rows if r.date == day]
    assert len(same_day) == 1
    assert same_day[0].health_score == 95.0


async def test_get_insight_is_tenant_scoped(storage: StorageProvider) -> None:
    day = date(2026, 5, 3)
    await storage.save_insight(_insight(tenant_id="owner", day=day))
    # Wrong tenant → None (no existence leak), same shape as a true miss.
    assert await storage.get_insight("intruder", "proj", day) is None
    assert await storage.get_insight("owner", "proj", day) is not None


async def test_list_insights_tenant_isolation(storage: StorageProvider) -> None:
    day = date(2026, 5, 4)
    await storage.save_insight(_insight(tenant_id="t1", day=day, runs=11))
    await storage.save_insight(_insight(tenant_id="t2", day=day, runs=99))

    t1_rows = await storage.list_insights("t1")
    assert all(r.tenant_id == "t1" for r in t1_rows)
    assert all(r.usage_rollup["runs"] == 11 for r in t1_rows)
    # t2's row must not leak into t1's list.
    assert not any(r.usage_rollup.get("runs") == 99 for r in t1_rows)


async def test_list_insights_filters_and_order(storage: StorageProvider) -> None:
    # Three days for proj-a, one day for proj-b.
    for d in (date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 7)):
        await storage.save_insight(_insight(tenant_id="t1", project_id="proj-a", day=d))
    await storage.save_insight(_insight(tenant_id="t1", project_id="proj-b", day=date(2026, 5, 6)))

    # project filter
    a_rows = await storage.list_insights("t1", project_id="proj-a")
    assert {r.date for r in a_rows} == {date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 7)}
    assert all(r.project_id == "proj-a" for r in a_rows)
    # newest-day-first ordering
    assert [r.date for r in a_rows] == sorted((r.date for r in a_rows), reverse=True)

    # date range (inclusive)
    ranged = await storage.list_insights(
        "t1", project_id="proj-a", since=date(2026, 5, 6), until=date(2026, 5, 6)
    )
    assert [r.date for r in ranged] == [date(2026, 5, 6)]

    # limit
    limited = await storage.list_insights("t1", project_id="proj-a", limit=2)
    assert len(limited) == 2


async def test_get_insight_missing_returns_none(storage: StorageProvider) -> None:
    assert await storage.get_insight("nobody", "noproj", date(2030, 1, 1)) is None


async def test_list_insights_empty_returns_empty(storage: StorageProvider) -> None:
    assert await storage.list_insights("nobody") == []
