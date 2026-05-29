"""Events-outbox storage — round-trip + filtering + tenant isolation
+ cursor pagination (ADR 035 D1).

Three backends in scope via the shared ``storage`` fixture in
``conftest.py``: ``InMemoryStorage``, ``SqliteProvider``, and
``PostgresProvider`` (skipped when ``MOVATE_PG_TEST_URL`` is unset).
Mirrors :mod:`tests.test_canary_storage` and :mod:`tests.test_trigger_storage`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movate.core.events import Event, EventKind


def _make_event(
    *,
    tenant_id: str = "tenant-a",
    kind: str = EventKind.RUN_COMPLETED.value,
    subject: str = "faq-agent",
    data: dict | None = None,
    created_at: datetime | None = None,
) -> Event:
    return Event(
        tenant_id=tenant_id,
        kind=kind,
        subject=subject,
        data=data or {"run_id": "r1"},
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Default-off + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    """An untouched outbox returns an empty list (no D1 sentinel rows)."""
    assert await storage.list_events("tenant-a") == []


@pytest.mark.unit
async def test_record_and_list_round_trip(storage) -> None:
    """A recorded event round-trips through list_events unchanged."""
    e = _make_event(
        kind="run.completed",
        subject="run-1",
        data={"agent": "faq-agent", "status": "success", "duration_ms": 1234},
    )
    await storage.record_event(e)
    rows = await storage.list_events("tenant-a")
    assert len(rows) == 1
    got = rows[0]
    assert got.id == e.id
    assert got.tenant_id == "tenant-a"
    assert got.kind == "run.completed"
    assert got.subject == "run-1"
    assert got.data == {"agent": "faq-agent", "status": "success", "duration_ms": 1234}
    # created_at round-trips at second resolution at minimum; isoformat
    # equivalence is the contract we care about.
    assert got.created_at == e.created_at


# ---------------------------------------------------------------------------
# Order — oldest-first
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_events_oldest_first(storage) -> None:
    """list_events returns rows in oldest-first order — the read order
    a forward-consumer (D2 webhook deliverer, D3 SSE pusher) needs."""
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    e1 = _make_event(subject="run-1", created_at=t0)
    e2 = _make_event(subject="run-2", created_at=t0 + timedelta(minutes=1))
    e3 = _make_event(subject="run-3", created_at=t0 + timedelta(minutes=2))
    # Record out of order on purpose.
    await storage.record_event(e2)
    await storage.record_event(e3)
    await storage.record_event(e1)
    rows = await storage.list_events("tenant-a")
    assert [r.subject for r in rows] == ["run-1", "run-2", "run-3"]


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_tenant_isolation(storage) -> None:
    """One tenant's events never appear in another tenant's list."""
    await storage.record_event(_make_event(tenant_id="tenant-a", subject="a-1"))
    await storage.record_event(_make_event(tenant_id="tenant-b", subject="b-1"))
    await storage.record_event(_make_event(tenant_id="tenant-a", subject="a-2"))
    a_rows = await storage.list_events("tenant-a")
    b_rows = await storage.list_events("tenant-b")
    assert sorted(r.subject for r in a_rows) == ["a-1", "a-2"]
    assert [r.subject for r in b_rows] == ["b-1"]


# ---------------------------------------------------------------------------
# Filters — kind / subject / since / until
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_filter_by_kind(storage) -> None:
    await storage.record_event(_make_event(kind="run.completed", subject="a"))
    await storage.record_event(_make_event(kind="run.failed", subject="b"))
    await storage.record_event(_make_event(kind="run.completed", subject="c"))
    rows = await storage.list_events("tenant-a", kind="run.completed")
    assert sorted(r.subject for r in rows) == ["a", "c"]


@pytest.mark.unit
async def test_filter_by_subject(storage) -> None:
    await storage.record_event(_make_event(subject="faq-agent"))
    await storage.record_event(_make_event(subject="support-agent"))
    await storage.record_event(_make_event(subject="faq-agent"))
    rows = await storage.list_events("tenant-a", subject="faq-agent")
    assert len(rows) == 2
    assert all(r.subject == "faq-agent" for r in rows)


@pytest.mark.unit
async def test_filter_by_since_until(storage) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=2)
    e_old = _make_event(subject="old", created_at=t0)
    e_mid = _make_event(subject="mid", created_at=t0 + timedelta(minutes=30))
    e_new = _make_event(subject="new", created_at=t0 + timedelta(hours=1))
    await storage.record_event(e_old)
    await storage.record_event(e_mid)
    await storage.record_event(e_new)
    # since is inclusive — mid + new (the >= t0+30m bound).
    rows = await storage.list_events("tenant-a", since=t0 + timedelta(minutes=30))
    assert [r.subject for r in rows] == ["mid", "new"]
    # until is exclusive — old + mid (the < t0+1h bound).
    rows = await storage.list_events("tenant-a", until=t0 + timedelta(hours=1))
    assert [r.subject for r in rows] == ["old", "mid"]


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cursor_pagination(storage) -> None:
    """Cursor pagination walks forward in oldest-first order without
    overlap or gap."""
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    events = [
        _make_event(subject=f"run-{i}", created_at=t0 + timedelta(minutes=i)) for i in range(5)
    ]
    for e in events:
        await storage.record_event(e)
    # Page 1 (limit=2).
    page1 = await storage.list_events("tenant-a", limit=2)
    assert [r.subject for r in page1] == ["run-0", "run-1"]
    # Page 2 — pass the last id back as the cursor.
    page2 = await storage.list_events("tenant-a", limit=2, after_id=page1[-1].id)
    assert [r.subject for r in page2] == ["run-2", "run-3"]
    # Page 3 — partial; one row left.
    page3 = await storage.list_events("tenant-a", limit=2, after_id=page2[-1].id)
    assert [r.subject for r in page3] == ["run-4"]
    # Page 4 — empty (tail of the outbox).
    page4 = await storage.list_events("tenant-a", limit=2, after_id=page3[-1].id)
    assert page4 == []


@pytest.mark.unit
async def test_cursor_unknown_id_falls_back_to_start(storage) -> None:
    """An unknown ``after_id`` (cross-tenant or invented) returns rows
    from the beginning rather than raising or leaking existence."""
    e = _make_event(subject="run-1")
    await storage.record_event(e)
    # Cross-tenant cursor — silently from the start.
    rows = await storage.list_events("tenant-a", after_id="nonexistent")
    assert [r.subject for r in rows] == ["run-1"]


@pytest.mark.unit
async def test_cursor_isolation_across_tenants(storage) -> None:
    """A cursor minted in one tenant doesn't leak into another tenant's
    list view (the cursor lookup is tenant-scoped)."""
    e_a = _make_event(tenant_id="tenant-a", subject="a-1")
    e_b1 = _make_event(tenant_id="tenant-b", subject="b-1")
    e_b2 = _make_event(tenant_id="tenant-b", subject="b-2")
    await storage.record_event(e_a)
    await storage.record_event(e_b1)
    await storage.record_event(e_b2)
    # Tenant B passes tenant A's event id — should be treated as
    # unknown and start from B's beginning.
    rows = await storage.list_events("tenant-b", after_id=e_a.id)
    assert [r.subject for r in rows] == ["b-1", "b-2"]


# ---------------------------------------------------------------------------
# Combined filters
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_combined_filters_and_together(storage) -> None:
    """Filters AND together — tenant + kind + subject + window."""
    t0 = datetime.now(UTC) - timedelta(hours=1)
    await storage.record_event(
        _make_event(kind="run.completed", subject="faq-agent", created_at=t0)
    )
    await storage.record_event(
        _make_event(kind="run.failed", subject="faq-agent", created_at=t0 + timedelta(minutes=1))
    )
    await storage.record_event(
        _make_event(
            kind="run.completed",
            subject="support-agent",
            created_at=t0 + timedelta(minutes=2),
        )
    )
    rows = await storage.list_events(
        "tenant-a",
        kind="run.completed",
        subject="faq-agent",
        since=t0,
        until=t0 + timedelta(minutes=2),
    )
    assert len(rows) == 1
    assert rows[0].kind == "run.completed"
    assert rows[0].subject == "faq-agent"
