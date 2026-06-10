"""Job-schedule storage — save/get/list/delete/touch round-trip + tenant isolation.

ADR 017 D2. Mirrors ``tests/test_eval_schedule_storage.py``: the same three
backends in scope via the shared ``storage`` fixture in conftest.py —
``InMemoryStorage``, ``SqliteProvider``, and ``PostgresProvider`` (skipped
when ``MOVATE_PG_TEST_URL`` is unset).

Asserts the additive table is default-off (no rows until written), upserts on
``(tenant_id, name)``, round-trips every field (including the ``input``
payload), is tenant-scoped (no leak), and that ``touch`` stamps
``last_enqueued_at`` (which the scheduler tick uses for the due-check +
idempotency).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import JobKind, JobSchedule


def _make_schedule(
    *,
    tenant_id: str = "tenant-a",
    name: str = "nightly",
    kind: JobKind = JobKind.AGENT,
    target: str = "faq-agent",
    cadence_seconds: int = 3600,
    enabled: bool = True,
    created_at: datetime | None = None,
) -> JobSchedule:
    return JobSchedule(
        tenant_id=tenant_id,
        name=name,
        kind=kind,
        target=target,
        cadence_seconds=cadence_seconds,
        enabled=enabled,
        input={"text": "daily digest", "n": 3},
        notify_email="ops@example.com",
        created_by="key-xyz",
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Default-off + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    assert await storage.list_job_schedules(tenant_id="tenant-a") == []
    assert await storage.get_job_schedule("anything", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_save_and_get_round_trip(storage) -> None:
    s = _make_schedule(kind=JobKind.WORKFLOW, target="returns-pipeline")
    await storage.save_job_schedule(s)
    got = await storage.get_job_schedule("nightly", tenant_id="tenant-a")
    assert got is not None
    assert got.name == "nightly"
    assert got.tenant_id == "tenant-a"
    assert got.kind == JobKind.WORKFLOW
    assert got.target == "returns-pipeline"
    assert got.cadence_seconds == 3600
    assert got.enabled is True
    assert got.input == {"text": "daily digest", "n": 3}
    assert got.notify_email == "ops@example.com"
    assert got.created_by == "key-xyz"
    assert got.last_enqueued_at is None


@pytest.mark.unit
async def test_cron_fields_round_trip(storage) -> None:
    """ADR 100 D1: cron + timezone persist; NULL on interval rows."""
    s = JobSchedule(
        tenant_id="tenant-a",
        name="briefing",
        kind=JobKind.WORKFLOW,
        target="exec-briefing",
        cadence_seconds=0,
        cron="0 7 * * 1-5",
        timezone="America/New_York",
        input={"audience": "leadership"},
    )
    await storage.save_job_schedule(s)
    got = await storage.get_job_schedule("briefing", tenant_id="tenant-a")
    assert got is not None
    assert got.cron == "0 7 * * 1-5"
    assert got.timezone == "America/New_York"
    assert got.cadence_seconds == 0

    # An interval schedule reads back with both cron fields None (back-compat).
    await storage.save_job_schedule(_make_schedule(name="interval"))
    interval = await storage.get_job_schedule("interval", tenant_id="tenant-a")
    assert interval is not None
    assert interval.cron is None
    assert interval.timezone is None


@pytest.mark.unit
async def test_upsert_interval_to_cron_and_back(storage) -> None:
    """Re-setting a schedule flips cleanly between the two cadence forms."""
    await storage.save_job_schedule(_make_schedule(name="flip", cadence_seconds=3600))
    await storage.save_job_schedule(
        JobSchedule(
            tenant_id="tenant-a",
            name="flip",
            kind=JobKind.AGENT,
            target="faq-agent",
            cadence_seconds=0,
            cron="0 7 * * *",
        )
    )
    as_cron = await storage.get_job_schedule("flip", tenant_id="tenant-a")
    assert as_cron is not None and as_cron.cron == "0 7 * * *"
    await storage.save_job_schedule(_make_schedule(name="flip", cadence_seconds=600))
    back = await storage.get_job_schedule("flip", tenant_id="tenant-a")
    assert back is not None
    assert back.cron is None
    assert back.cadence_seconds == 600


@pytest.mark.unit
async def test_save_upserts_on_tenant_name(storage) -> None:
    """Re-saving the same (tenant, name) overwrites — one row, last write wins."""
    await storage.save_job_schedule(_make_schedule(cadence_seconds=3600))
    await storage.save_job_schedule(
        _make_schedule(cadence_seconds=600, kind=JobKind.WORKFLOW, target="other")
    )
    rows = await storage.list_job_schedules(tenant_id="tenant-a")
    assert len(rows) == 1
    assert rows[0].cadence_seconds == 600
    assert rows[0].kind == JobKind.WORKFLOW
    assert rows[0].target == "other"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_is_tenant_scoped(storage) -> None:
    await storage.save_job_schedule(_make_schedule(tenant_id="tenant-a"))
    assert await storage.get_job_schedule("nightly", tenant_id="tenant-a") is not None
    assert await storage.get_job_schedule("nightly", tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_list_is_tenant_scoped(storage) -> None:
    await storage.save_job_schedule(_make_schedule(tenant_id="tenant-a", name="foo"))
    await storage.save_job_schedule(_make_schedule(tenant_id="tenant-a", name="bar"))
    await storage.save_job_schedule(_make_schedule(tenant_id="tenant-b", name="foo"))
    only_a = await storage.list_job_schedules(tenant_id="tenant-a")
    assert {s.name for s in only_a} == {"foo", "bar"}
    only_b = await storage.list_job_schedules(tenant_id="tenant-b")
    assert {s.name for s in only_b} == {"foo"}


@pytest.mark.unit
async def test_list_all_tenants_for_cron_drain(storage) -> None:
    await storage.save_job_schedule(_make_schedule(tenant_id="tenant-a", name="foo"))
    await storage.save_job_schedule(_make_schedule(tenant_id="tenant-b", name="bar"))
    rows = await storage.list_job_schedules(tenant_id=None)
    assert {(s.tenant_id, s.name) for s in rows} == {
        ("tenant-a", "foo"),
        ("tenant-b", "bar"),
    }


# ---------------------------------------------------------------------------
# Delete + touch
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_returns_true_then_false(storage) -> None:
    await storage.save_job_schedule(_make_schedule())
    assert await storage.delete_job_schedule("nightly", tenant_id="tenant-a") is True
    assert await storage.get_job_schedule("nightly", tenant_id="tenant-a") is None
    assert await storage.delete_job_schedule("nightly", tenant_id="tenant-a") is False


@pytest.mark.unit
async def test_delete_is_tenant_scoped(storage) -> None:
    await storage.save_job_schedule(_make_schedule(tenant_id="tenant-a"))
    assert await storage.delete_job_schedule("nightly", tenant_id="tenant-b") is False
    assert await storage.get_job_schedule("nightly", tenant_id="tenant-a") is not None


@pytest.mark.unit
async def test_touch_stamps_last_enqueued_at(storage) -> None:
    await storage.save_job_schedule(_make_schedule())
    when = datetime.now(UTC) - timedelta(seconds=5)
    await storage.touch_job_schedule("nightly", tenant_id="tenant-a", last_enqueued_at=when)
    got = await storage.get_job_schedule("nightly", tenant_id="tenant-a")
    assert got is not None
    assert got.last_enqueued_at is not None
    assert abs((got.last_enqueued_at - when).total_seconds()) < 1.0


@pytest.mark.unit
async def test_touch_missing_schedule_is_noop(storage) -> None:
    await storage.touch_job_schedule(
        "ghost", tenant_id="tenant-a", last_enqueued_at=datetime.now(UTC)
    )
    assert await storage.get_job_schedule("ghost", tenant_id="tenant-a") is None
