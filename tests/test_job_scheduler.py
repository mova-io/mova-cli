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
from uuid import uuid4

import pytest

from movate.core.job_retry import DEFAULT_VISIBILITY_TIMEOUT_SECONDS
from movate.core.models import EvalSchedule, JobKind, JobRecord, JobSchedule, JobStatus
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
# is_due — cron form (ADR 100 D1)
# ---------------------------------------------------------------------------


def _cron_schedule(
    *,
    cron: str = "0 7 * * 1-5",
    timezone: str | None = "America/New_York",
    created_at: datetime | None = None,
    last_enqueued_at: datetime | None = None,
    name: str = "briefing",
) -> JobSchedule:
    return JobSchedule(
        tenant_id="tenant-a",
        name=name,
        kind=JobKind.WORKFLOW,
        target="exec-briefing",
        cadence_seconds=0,
        cron=cron,
        timezone=timezone,
        input={"audience": "leadership"},
        created_at=created_at or datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        last_enqueued_at=last_enqueued_at,
    )


@pytest.mark.unit
def test_cron_due_when_window_passed() -> None:
    """07:00 Mon-Fri NY: due at 11:30 UTC Monday (07:00 EDT == 11:00 UTC)."""
    s = _cron_schedule(last_enqueued_at=datetime(2026, 6, 5, 11, 5, tzinfo=UTC))  # Fri post-fire
    # Monday 2026-06-08 11:30 UTC = 07:30 EDT — the 07:00 window has passed.
    assert is_due(s, now=datetime(2026, 6, 8, 11, 30, tzinfo=UTC)) is True


@pytest.mark.unit
def test_cron_not_due_before_window() -> None:
    s = _cron_schedule(last_enqueued_at=datetime(2026, 6, 5, 11, 5, tzinfo=UTC))
    # Monday 10:30 UTC = 06:30 EDT — before the 07:00 window.
    assert is_due(s, now=datetime(2026, 6, 8, 10, 30, tzinfo=UTC)) is False


@pytest.mark.unit
def test_cron_not_due_on_weekend() -> None:
    s = _cron_schedule(last_enqueued_at=datetime(2026, 6, 5, 11, 5, tzinfo=UTC))
    # Saturday 2026-06-06 12:00 UTC — "1-5" excludes weekends.
    assert is_due(s, now=datetime(2026, 6, 6, 12, 0, tzinfo=UTC)) is False


@pytest.mark.unit
def test_cron_never_fired_anchors_at_created_at() -> None:
    """A never-fired schedule is due only once a window AFTER creation passes —
    occurrences predating the schedule never fire."""
    created = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)  # Mon 08:00 EDT, after 07:00
    s = _cron_schedule(created_at=created, last_enqueued_at=None)
    # Still Monday, 13:00 UTC: today's 07:00 EDT window predates creation.
    assert is_due(s, now=datetime(2026, 6, 8, 13, 0, tzinfo=UTC)) is False
    # Tuesday 11:30 UTC = 07:30 EDT — the first post-creation window passed.
    assert is_due(s, now=datetime(2026, 6, 9, 11, 30, tzinfo=UTC)) is True


@pytest.mark.unit
async def test_cron_tick_fires_once_per_window(storage: InMemoryStorage) -> None:
    """Two ticks inside one matched window enqueue exactly ONE job."""
    await storage.save_job_schedule(
        _cron_schedule(last_enqueued_at=datetime(2026, 6, 5, 11, 5, tzinfo=UTC))
    )
    first = await run_job_scheduler_tick(
        storage, tenant_id="tenant-a", now=datetime(2026, 6, 8, 11, 10, tzinfo=UTC)
    )
    second = await run_job_scheduler_tick(
        storage, tenant_id="tenant-a", now=datetime(2026, 6, 8, 11, 20, tzinfo=UTC)
    )
    assert first.enqueued_count == 1
    assert second.enqueued_count == 0
    assert len(storage.jobs) == 1
    # The next weekday window fires again.
    tuesday = await run_job_scheduler_tick(
        storage, tenant_id="tenant-a", now=datetime(2026, 6, 9, 11, 10, tzinfo=UTC)
    )
    assert tuesday.enqueued_count == 1


@pytest.mark.unit
async def test_cron_missed_windows_yield_one_catchup_run(storage: InMemoryStorage) -> None:
    """A tick down across several windows fires ONE catch-up, not a backfill."""
    await storage.save_job_schedule(
        _cron_schedule(last_enqueued_at=datetime(2026, 6, 1, 11, 5, tzinfo=UTC))  # Mon
    )
    # The tick comes back Thursday — Tue + Wed + Thu windows were all missed.
    result = await run_job_scheduler_tick(
        storage, tenant_id="tenant-a", now=datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    )
    assert result.enqueued_count == 1
    assert len(storage.jobs) == 1
    # And the stamp moved to "now", so the SAME tick window won't re-fire.
    again = await run_job_scheduler_tick(
        storage, tenant_id="tenant-a", now=datetime(2026, 6, 4, 12, 5, tzinfo=UTC)
    )
    assert again.enqueued_count == 0


@pytest.mark.unit
def test_cron_due_tracks_wall_clock_across_dst_transition() -> None:
    """07:00 America/New_York is 12:00 UTC in EST but 11:00 UTC in EDT.

    US spring-forward 2026 is Sun Mar 8. The schedule must stay pinned to
    07:00 local on both sides of the transition.
    """
    # Friday 2026-03-06 (EST): 07:00 local == 12:00 UTC.
    pre = _cron_schedule(last_enqueued_at=datetime(2026, 3, 5, 12, 5, tzinfo=UTC))
    assert is_due(pre, now=datetime(2026, 3, 6, 11, 30, tzinfo=UTC)) is False  # 06:30 EST
    assert is_due(pre, now=datetime(2026, 3, 6, 12, 30, tzinfo=UTC)) is True  # 07:30 EST
    # Monday 2026-03-09 (EDT): 07:00 local == 11:00 UTC.
    post = _cron_schedule(last_enqueued_at=datetime(2026, 3, 6, 12, 5, tzinfo=UTC))
    assert is_due(post, now=datetime(2026, 3, 9, 10, 30, tzinfo=UTC)) is False  # 06:30 EDT
    assert is_due(post, now=datetime(2026, 3, 9, 11, 30, tzinfo=UTC)) is True  # 07:30 EDT


@pytest.mark.unit
def test_cron_nonexistent_spring_forward_slot_fires_after_jump() -> None:
    """02:30 local doesn't exist on 2026-03-08 (clocks jump 02:00→03:00);
    cronsim resolves it to the post-jump instant rather than skipping the day."""
    s = _cron_schedule(
        cron="30 2 * * *",
        last_enqueued_at=datetime(2026, 3, 7, 8, 0, tzinfo=UTC),  # Sat 03:00 EST
    )
    # 03:00 EDT on Mar 8 == 07:00 UTC — the resolved slot has passed by 07:30.
    assert is_due(s, now=datetime(2026, 3, 8, 7, 30, tzinfo=UTC)) is True
    # …but not before the jump (06:30 UTC == 01:30 EST).
    assert is_due(s, now=datetime(2026, 3, 8, 6, 30, tzinfo=UTC)) is False


@pytest.mark.unit
def test_cron_defaults_to_utc_without_timezone() -> None:
    s = _cron_schedule(
        cron="0 7 * * *", timezone=None, last_enqueued_at=datetime(2026, 6, 7, 8, 0, tzinfo=UTC)
    )
    assert is_due(s, now=datetime(2026, 6, 8, 6, 30, tzinfo=UTC)) is False
    assert is_due(s, now=datetime(2026, 6, 8, 7, 30, tzinfo=UTC)) is True


@pytest.mark.unit
def test_cron_disabled_never_due() -> None:
    s = _cron_schedule(last_enqueued_at=None).model_copy(update={"enabled": False})
    assert is_due(s, now=datetime(2026, 6, 8, 23, 59, tzinfo=UTC)) is False


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


# ---------------------------------------------------------------------------
# Scheduler-tick reaper backstop (item 31) — the scaled-to-zero path:
# all workers gone, the cron tick still reclaims orphaned RUNNING jobs.
# ---------------------------------------------------------------------------


def _stale_running_job(*, tenant_id: str = "tenant-a", attempt_count: int = 0) -> JobRecord:
    """A job orphaned in RUNNING with a claimed_at well past the timeout."""
    stale = datetime.now(UTC) - timedelta(seconds=DEFAULT_VISIBILITY_TIMEOUT_SECONDS + 60)
    return JobRecord(
        job_id=uuid4().hex,
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="alpha",
        input={"text": "hi"},
        status=JobStatus.RUNNING,
        claimed_at=stale,
        attempt_count=attempt_count,
    )


@pytest.mark.unit
async def test_run_all_reclaims_orphaned_running(storage: InMemoryStorage) -> None:
    """With no schedules and no workers, the unified tick still reaps a
    job orphaned in RUNNING — the scaled-to-zero backstop."""
    orphan = _stale_running_job(attempt_count=0)
    await storage.save_job(orphan)

    result = await run_all_scheduler_ticks(storage, tenant_id="tenant-a")
    assert result.reclaimed_requeued == 1
    assert result.reclaimed_dead_lettered == 0

    got = await storage.get_job(orphan.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.QUEUED
    assert got.attempt_count == 1


@pytest.mark.unit
async def test_run_all_reaper_dead_letters_exhausted(storage: InMemoryStorage) -> None:
    """The backstop dead-letters an orphan that's out of retry budget."""
    orphan = _stale_running_job(attempt_count=2)  # 2 + 1 >= 3 (default budget)
    await storage.save_job(orphan)

    result = await run_all_scheduler_ticks(storage, tenant_id="tenant-a")
    assert result.reclaimed_requeued == 0
    assert result.reclaimed_dead_lettered == 1

    got = await storage.get_job(orphan.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.DEAD_LETTER


@pytest.mark.unit
async def test_run_all_reaper_zero_when_nothing_stale(storage: InMemoryStorage) -> None:
    """A healthy in-flight RUNNING job (claimed recently) is not reaped
    by the backstop — reclaim counts stay zero."""
    fresh = JobRecord(
        job_id=uuid4().hex,
        tenant_id="tenant-a",
        kind=JobKind.AGENT,
        target="alpha",
        input={"text": "hi"},
        status=JobStatus.RUNNING,
        claimed_at=datetime.now(UTC) - timedelta(seconds=5),
        attempt_count=0,
    )
    await storage.save_job(fresh)

    result = await run_all_scheduler_ticks(storage, tenant_id="tenant-a")
    assert result.reclaimed_requeued == 0
    assert result.reclaimed_dead_lettered == 0

    got = await storage.get_job(fresh.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.RUNNING
