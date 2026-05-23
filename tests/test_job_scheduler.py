"""Generic agent/workflow scheduler tick (ADR 017 D2).

Asserts the generalization of the ADR-016 eval scheduler:

* ``is_due`` works *structurally* for BOTH ``EvalSchedule`` and
  ``JobSchedule`` (the shared due-check protocol).
* ``build_scheduled_job`` produces a JobKind.AGENT/WORKFLOW JobRecord with
  the schedule's target / input / tenant — the same shape ``mdk submit``
  builds, so the existing dispatch path runs it unchanged.
* ``run_job_scheduler_tick`` enqueues one job per due schedule, is
  idempotent within a cadence window (stamps ``last_enqueued_at``), is
  tenant-scoped, and per-schedule failures don't abort the tick.
* ``run_all_scheduler_ticks`` drains BOTH eval and job schedules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import EvalSchedule, JobKind, JobSchedule, JobStatus
from movate.core.scheduler import (
    build_scheduled_job,
    is_due,
    run_all_scheduler_ticks,
    run_job_scheduler_tick,
)
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _job_schedule(
    *,
    name: str = "nightly",
    tenant_id: str = "tenant-a",
    kind: JobKind = JobKind.AGENT,
    target: str = "faq-agent",
    cadence_seconds: int = 3600,
    enabled: bool = True,
    last_enqueued_at: datetime | None = None,
    input: dict | None = None,
) -> JobSchedule:
    return JobSchedule(
        tenant_id=tenant_id,
        name=name,
        kind=kind,
        target=target,
        cadence_seconds=cadence_seconds,
        enabled=enabled,
        input=input if input is not None else {"text": "hi"},
        notify_email="ops@example.com",
        last_enqueued_at=last_enqueued_at,
    )


def _eval_schedule(
    *,
    agent: str = "demo",
    tenant_id: str = "tenant-a",
    cadence_seconds: int = 3600,
    last_enqueued_at: datetime | None = None,
) -> EvalSchedule:
    return EvalSchedule(
        tenant_id=tenant_id,
        agent=agent,
        cadence_seconds=cadence_seconds,
        mock=True,
        last_enqueued_at=last_enqueued_at,
    )


# ---------------------------------------------------------------------------
# is_due — structural over both schedule models
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_due_works_for_job_schedule() -> None:
    now = datetime.now(UTC)
    assert is_due(_job_schedule(last_enqueued_at=None), now=now) is True
    assert (
        is_due(
            _job_schedule(cadence_seconds=3600, last_enqueued_at=now - timedelta(minutes=5)),
            now=now,
        )
        is False
    )
    assert (
        is_due(
            _job_schedule(cadence_seconds=3600, last_enqueued_at=now - timedelta(hours=2)),
            now=now,
        )
        is True
    )
    assert is_due(_job_schedule(enabled=False), now=now) is False


@pytest.mark.unit
def test_is_due_works_for_eval_schedule() -> None:
    """The same function still accepts an EvalSchedule (back-compat)."""
    now = datetime.now(UTC)
    assert is_due(_eval_schedule(last_enqueued_at=None), now=now) is True
    assert (
        is_due(
            _eval_schedule(cadence_seconds=3600, last_enqueued_at=now - timedelta(minutes=5)),
            now=now,
        )
        is False
    )


# ---------------------------------------------------------------------------
# build_scheduled_job
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_scheduled_job_agent() -> None:
    job = build_scheduled_job(_job_schedule(kind=JobKind.AGENT, target="faq", input={"q": "x"}))
    assert job.kind == JobKind.AGENT
    assert job.target == "faq"
    assert job.tenant_id == "tenant-a"
    assert job.input == {"q": "x"}
    assert job.status == JobStatus.QUEUED
    assert job.notify_email == "ops@example.com"


@pytest.mark.unit
def test_build_scheduled_job_workflow() -> None:
    job = build_scheduled_job(
        _job_schedule(kind=JobKind.WORKFLOW, target="pipeline", input={"state": 1})
    )
    assert job.kind == JobKind.WORKFLOW
    assert job.target == "pipeline"
    assert job.input == {"state": 1}


# ---------------------------------------------------------------------------
# run_job_scheduler_tick
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_tick_enqueues_only_due_schedules(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_job_schedule(_job_schedule(name="due", last_enqueued_at=None))
    await storage.save_job_schedule(
        _job_schedule(
            name="fresh", cadence_seconds=3600, last_enqueued_at=now - timedelta(minutes=5)
        )
    )
    result = await run_job_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    assert result.enqueued_count == 1
    assert "fresh" in result.skipped
    jobs = [j for j in storage.jobs if j.kind == JobKind.AGENT]
    assert len(jobs) == 1
    assert jobs[0].target == "faq-agent"
    assert jobs[0].status == JobStatus.QUEUED


@pytest.mark.unit
async def test_tick_stamps_last_enqueued_at(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_job_schedule(_job_schedule(name="nightly", last_enqueued_at=None))
    await run_job_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    refreshed = await storage.get_job_schedule("nightly", tenant_id="tenant-a")
    assert refreshed is not None
    assert refreshed.last_enqueued_at == now


@pytest.mark.unit
async def test_tick_is_idempotent_within_window(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_job_schedule(
        _job_schedule(name="nightly", cadence_seconds=3600, last_enqueued_at=None)
    )
    first = await run_job_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    second = await run_job_scheduler_tick(
        storage, tenant_id="tenant-a", now=now + timedelta(minutes=5)
    )
    assert first.enqueued_count == 1
    assert second.enqueued_count == 0
    assert len([j for j in storage.jobs if j.kind == JobKind.AGENT]) == 1


@pytest.mark.unit
async def test_tick_re_enqueues_after_cadence(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_job_schedule(
        _job_schedule(name="nightly", cadence_seconds=3600, last_enqueued_at=None)
    )
    await run_job_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    later = await run_job_scheduler_tick(
        storage, tenant_id="tenant-a", now=now + timedelta(hours=2)
    )
    assert later.enqueued_count == 1
    assert len([j for j in storage.jobs if j.kind == JobKind.AGENT]) == 2


@pytest.mark.unit
async def test_tick_with_no_schedules_enqueues_nothing(storage: InMemoryStorage) -> None:
    result = await run_job_scheduler_tick(storage, tenant_id="tenant-a")
    assert result.enqueued_count == 0
    assert storage.jobs == []


@pytest.mark.unit
async def test_tick_is_tenant_scoped(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_job_schedule(_job_schedule(name="a", tenant_id="tenant-a"))
    await storage.save_job_schedule(_job_schedule(name="b", tenant_id="tenant-b"))
    await run_job_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    jobs = [j for j in storage.jobs if j.kind == JobKind.AGENT]
    assert len(jobs) == 1
    assert jobs[0].tenant_id == "tenant-a"


@pytest.mark.unit
async def test_per_schedule_failure_does_not_abort_tick(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save_job failure on one schedule is logged + skipped; others proceed."""
    now = datetime.now(UTC)
    await storage.save_job_schedule(_job_schedule(name="bad", target="boom"))
    await storage.save_job_schedule(_job_schedule(name="good", target="ok"))

    real_save_job = storage.save_job

    async def flaky_save_job(job) -> None:
        if job.target == "boom":
            raise RuntimeError("queue write failed")
        await real_save_job(job)

    monkeypatch.setattr(storage, "save_job", flaky_save_job)
    result = await run_job_scheduler_tick(storage, tenant_id="tenant-a", now=now)
    assert result.enqueued_count == 1
    assert "bad" in result.skipped
    good_jobs = [j for j in storage.jobs if j.target == "ok"]
    assert len(good_jobs) == 1


# ---------------------------------------------------------------------------
# run_all_scheduler_ticks — drains both surfaces
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_all_drains_both_surfaces(storage: InMemoryStorage) -> None:
    now = datetime.now(UTC)
    await storage.save_eval_schedule(_eval_schedule(agent="eval-agent", last_enqueued_at=None))
    await storage.save_job_schedule(_job_schedule(name="job-sched", last_enqueued_at=None))
    result = await run_all_scheduler_ticks(storage, tenant_id="tenant-a", now=now)
    assert result.enqueued_count == 2
    kinds = {j.kind for j in storage.jobs}
    assert kinds == {JobKind.EVAL, JobKind.AGENT}


@pytest.mark.unit
async def test_run_all_with_no_schedules_is_noop(storage: InMemoryStorage) -> None:
    result = await run_all_scheduler_ticks(storage, tenant_id="tenant-a")
    assert result.enqueued_count == 0
    assert storage.jobs == []
