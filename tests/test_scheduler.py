"""Continuous-eval scheduler tick (ADR 016 D2).

Asserts the portable, cron-driven tick:

* ``is_due`` honors cadence + enabled flag.
* ``run_scheduler_tick`` enqueues a ``JobKind.EVAL`` job ONLY for due
  schedules (clock mocked), reusing the existing eval-job path.
* The tick is idempotent — a second tick inside the cadence window does
  NOT double-enqueue (``last_enqueued_at`` stamped by the first).
* ``build_eval_job`` carries the eval config + scheduled marker so the
  worker's drift hook fires + alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import EvalSchedule, JobKind, JobStatus
from movate.core.scheduler import build_eval_job, is_due, run_scheduler_tick
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _schedule(
    *,
    agent: str = "demo",
    tenant_id: str = "tenant-a",
    cadence_seconds: int = 3600,
    enabled: bool = True,
    last_enqueued_at: datetime | None = None,
    mock: bool = True,
) -> EvalSchedule:
    return EvalSchedule(
        tenant_id=tenant_id,
        agent=agent,
        cadence_seconds=cadence_seconds,
        enabled=enabled,
        mock=mock,
        runs=1,
        gate_mode="mean",
        gate=0.7,
        regression_tolerance=0.05,
        notify_email="ops@example.com",
        last_enqueued_at=last_enqueued_at,
    )


# ---------------------------------------------------------------------------
# is_due
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_due_when_never_enqueued() -> None:
    assert is_due(_schedule(last_enqueued_at=None), now=datetime.now(UTC)) is True


@pytest.mark.unit
def test_not_due_inside_cadence_window() -> None:
    now = datetime.now(UTC)
    s = _schedule(cadence_seconds=3600, last_enqueued_at=now - timedelta(minutes=10))
    assert is_due(s, now=now) is False


@pytest.mark.unit
def test_due_after_cadence_elapsed() -> None:
    now = datetime.now(UTC)
    s = _schedule(cadence_seconds=3600, last_enqueued_at=now - timedelta(hours=2))
    assert is_due(s, now=now) is True


@pytest.mark.unit
def test_disabled_is_never_due() -> None:
    assert is_due(_schedule(enabled=False, last_enqueued_at=None), now=datetime.now(UTC)) is False


# ---------------------------------------------------------------------------
# build_eval_job
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_eval_job_carries_config_and_marker() -> None:
    job = build_eval_job(_schedule(agent="demo", mock=True))
    assert job.kind == JobKind.EVAL
    assert job.target == "demo"
    assert job.tenant_id == "tenant-a"
    assert job.input["mock"] is True
    assert job.input["scheduled"] is True
    assert job.input["regression_tolerance"] == 0.05
    assert job.input["notify_email"] == "ops@example.com"
    assert job.notify_email == "ops@example.com"


# ---------------------------------------------------------------------------
# run_scheduler_tick
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_tick_enqueues_only_due_schedules(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    due = _schedule(agent="due-agent", last_enqueued_at=None)
    not_due = _schedule(
        agent="fresh-agent",
        cadence_seconds=3600,
        last_enqueued_at=now - timedelta(minutes=5),
    )
    await storage.save_eval_schedule(due)
    await storage.save_eval_schedule(not_due)

    result = await run_scheduler_tick(storage, tenant_id="tenant-a", now=now)

    assert result.enqueued_count == 1
    assert "fresh-agent" in result.skipped
    eval_jobs = [j for j in storage.jobs if j.kind == JobKind.EVAL]
    assert len(eval_jobs) == 1
    assert eval_jobs[0].target == "due-agent"
    assert eval_jobs[0].status == JobStatus.QUEUED


@pytest.mark.unit
async def test_tick_stamps_last_enqueued_at(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_eval_schedule(_schedule(agent="demo", last_enqueued_at=None))
    await run_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    refreshed = await storage.get_eval_schedule("demo", tenant_id="tenant-a")
    assert refreshed is not None
    assert refreshed.last_enqueued_at == now


@pytest.mark.unit
async def test_tick_is_idempotent_within_window(storage: InMemoryStorage) -> None:
    """A second tick inside the cadence window does not double-enqueue."""
    now = datetime.now(UTC)
    await storage.save_eval_schedule(
        _schedule(agent="demo", cadence_seconds=3600, last_enqueued_at=None)
    )
    first = await run_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    # Tick again 5 minutes later — still inside the 1h cadence.
    second = await run_scheduler_tick(storage, tenant_id="tenant-a", now=now + timedelta(minutes=5))
    assert first.enqueued_count == 1
    assert second.enqueued_count == 0
    assert len([j for j in storage.jobs if j.kind == JobKind.EVAL]) == 1


@pytest.mark.unit
async def test_tick_re_enqueues_after_cadence(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_eval_schedule(
        _schedule(agent="demo", cadence_seconds=3600, last_enqueued_at=None)
    )
    await run_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    # Two hours later → due again.
    later = await run_scheduler_tick(storage, tenant_id="tenant-a", now=now + timedelta(hours=2))
    assert later.enqueued_count == 1
    assert len([j for j in storage.jobs if j.kind == JobKind.EVAL]) == 2


@pytest.mark.unit
async def test_tick_with_no_schedules_enqueues_nothing(storage: InMemoryStorage) -> None:
    result = await run_scheduler_tick(storage, tenant_id="tenant-a")
    assert result.enqueued_count == 0
    assert storage.jobs == []


@pytest.mark.unit
async def test_tick_is_tenant_scoped(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_eval_schedule(_schedule(agent="a", tenant_id="tenant-a"))
    await storage.save_eval_schedule(_schedule(agent="b", tenant_id="tenant-b"))
    await run_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    eval_jobs = [j for j in storage.jobs if j.kind == JobKind.EVAL]
    assert len(eval_jobs) == 1
    assert eval_jobs[0].tenant_id == "tenant-a"
