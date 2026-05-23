"""Eval-schedule storage — save/get/list/delete/touch round-trip + tenant isolation.

ADR 016 D2. Mirrors ``tests/test_bench_storage.py``: the same three backends
in scope via the shared ``storage`` fixture in conftest.py — ``InMemoryStorage``,
``SqliteProvider``, and ``PostgresProvider`` (skipped when ``MOVATE_PG_TEST_URL``
is unset).

Asserts the additive table is default-off (no rows until written), upserts on
``(tenant_id, agent)``, round-trips every field, is tenant-scoped (no leak),
and that ``touch`` stamps ``last_enqueued_at`` (which the scheduler tick uses
for the due-check + idempotency).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import EvalSchedule


def _make_schedule(
    *,
    tenant_id: str = "tenant-a",
    agent: str = "demo-agent",
    cadence_seconds: int = 3600,
    enabled: bool = True,
    mock: bool = False,
    created_at: datetime | None = None,
) -> EvalSchedule:
    return EvalSchedule(
        tenant_id=tenant_id,
        agent=agent,
        cadence_seconds=cadence_seconds,
        enabled=enabled,
        mock=mock,
        runs=3,
        gate_mode="mean",
        gate=0.8,
        objective="triage",
        regression_tolerance=0.03,
        baseline_id="base-123",
        notify_email="ops@example.com",
        created_by="key-xyz",
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Default-off + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    """A fresh backend has no schedules — additive + default-off."""
    assert await storage.list_eval_schedules(tenant_id="tenant-a") == []
    assert await storage.get_eval_schedule("anything", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_save_and_get_round_trip(storage) -> None:
    s = _make_schedule()
    await storage.save_eval_schedule(s)
    got = await storage.get_eval_schedule("demo-agent", tenant_id="tenant-a")
    assert got is not None
    assert got.agent == "demo-agent"
    assert got.tenant_id == "tenant-a"
    assert got.cadence_seconds == 3600
    assert got.enabled is True
    assert got.mock is False
    assert got.runs == 3
    assert got.gate_mode == "mean"
    assert got.gate == 0.8
    assert got.objective == "triage"
    assert got.regression_tolerance == 0.03
    assert got.baseline_id == "base-123"
    assert got.notify_email == "ops@example.com"
    assert got.created_by == "key-xyz"
    assert got.last_enqueued_at is None


@pytest.mark.unit
async def test_save_upserts_on_tenant_agent(storage) -> None:
    """Re-saving the same (tenant, agent) overwrites — one row, last write wins."""
    await storage.save_eval_schedule(_make_schedule(cadence_seconds=3600))
    await storage.save_eval_schedule(_make_schedule(cadence_seconds=600, mock=True))
    rows = await storage.list_eval_schedules(tenant_id="tenant-a")
    assert len(rows) == 1
    assert rows[0].cadence_seconds == 600
    assert rows[0].mock is True


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_is_tenant_scoped(storage) -> None:
    await storage.save_eval_schedule(_make_schedule(tenant_id="tenant-a"))
    assert await storage.get_eval_schedule("demo-agent", tenant_id="tenant-a") is not None
    # Wrong tenant → None (no existence leak).
    assert await storage.get_eval_schedule("demo-agent", tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_list_is_tenant_scoped(storage) -> None:
    await storage.save_eval_schedule(_make_schedule(tenant_id="tenant-a", agent="foo"))
    await storage.save_eval_schedule(_make_schedule(tenant_id="tenant-a", agent="bar"))
    await storage.save_eval_schedule(_make_schedule(tenant_id="tenant-b", agent="foo"))
    only_a = await storage.list_eval_schedules(tenant_id="tenant-a")
    assert {s.agent for s in only_a} == {"foo", "bar"}
    # A same-name agent under tenant-b is a distinct row, not an upsert.
    only_b = await storage.list_eval_schedules(tenant_id="tenant-b")
    assert {s.agent for s in only_b} == {"foo"}


@pytest.mark.unit
async def test_list_all_tenants_for_cron_drain(storage) -> None:
    """tenant_id=None returns every tenant's schedules (cron drain mode)."""
    await storage.save_eval_schedule(_make_schedule(tenant_id="tenant-a", agent="foo"))
    await storage.save_eval_schedule(_make_schedule(tenant_id="tenant-b", agent="bar"))
    rows = await storage.list_eval_schedules(tenant_id=None)
    assert {(s.tenant_id, s.agent) for s in rows} == {
        ("tenant-a", "foo"),
        ("tenant-b", "bar"),
    }


# ---------------------------------------------------------------------------
# Delete + touch
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_returns_true_then_false(storage) -> None:
    await storage.save_eval_schedule(_make_schedule())
    assert await storage.delete_eval_schedule("demo-agent", tenant_id="tenant-a") is True
    assert await storage.get_eval_schedule("demo-agent", tenant_id="tenant-a") is None
    # Second delete is a no-op.
    assert await storage.delete_eval_schedule("demo-agent", tenant_id="tenant-a") is False


@pytest.mark.unit
async def test_delete_is_tenant_scoped(storage) -> None:
    await storage.save_eval_schedule(_make_schedule(tenant_id="tenant-a"))
    # Wrong-tenant delete is a no-op and leaves the row intact.
    assert await storage.delete_eval_schedule("demo-agent", tenant_id="tenant-b") is False
    assert await storage.get_eval_schedule("demo-agent", tenant_id="tenant-a") is not None


@pytest.mark.unit
async def test_touch_stamps_last_enqueued_at(storage) -> None:
    await storage.save_eval_schedule(_make_schedule())
    when = datetime.now(UTC) - timedelta(seconds=5)
    await storage.touch_eval_schedule("demo-agent", tenant_id="tenant-a", last_enqueued_at=when)
    got = await storage.get_eval_schedule("demo-agent", tenant_id="tenant-a")
    assert got is not None
    assert got.last_enqueued_at is not None
    # Within a second (backends serialize ISO / TIMESTAMPTZ).
    assert abs((got.last_enqueued_at - when).total_seconds()) < 1.0


@pytest.mark.unit
async def test_touch_missing_schedule_is_noop(storage) -> None:
    # Touching a non-existent schedule must not raise.
    await storage.touch_eval_schedule(
        "ghost", tenant_id="tenant-a", last_enqueued_at=datetime.now(UTC)
    )
    assert await storage.get_eval_schedule("ghost", tenant_id="tenant-a") is None
