"""Portable, cron-driven scheduler (ADR 016 D2 + ADR 017 D2).

The scheduler is a stateless **tick** that, when invoked, finds the
schedules that are **due** and **enqueues a job** for each — reusing the
existing job queue + KEDA worker, which then execute them with their
existing retry / dead-letter behavior. There is no in-process timer
daemon; the tick is driven by an *external* cron:

* On Azure: a Container Apps **Job** with a cron trigger that runs
  ``mdk scheduler-tick`` (or ``mdk eval-scheduler-tick`` for the
  eval-only variant).
* Locally / anywhere: any cron, a CI schedule, or a manual invocation.

This keeps the scheduler vendor-neutral (ADR 001) — ACA Jobs is just the
cron that calls the tick; nothing here imports a cloud SDK.

Two scheduling surfaces share the same primitive:

* **Continuous eval (ADR 016 D2)** — :func:`run_scheduler_tick` reads
  :meth:`StorageProvider.list_eval_schedules` and enqueues a
  ``JobKind.EVAL`` job per due :class:`EvalSchedule` (via
  :func:`build_eval_job`), reusing the eval-as-job path
  (``WorkerDispatch._execute_eval``).
* **Generic agent/workflow schedules (ADR 017 D2)** —
  :func:`run_job_scheduler_tick` reads
  :meth:`StorageProvider.list_job_schedules` and enqueues a
  ``JobKind.AGENT``/``WORKFLOW`` job per due :class:`JobSchedule` (via
  :func:`build_scheduled_job`), reusing the exact ``mdk submit`` /
  ``POST /run`` job shape so the worker runs them with no new branch.

:func:`run_all_scheduler_ticks` drains both — the unified cron entrypoint
behind ``mdk scheduler-tick``.

**Idempotency.** The tick stamps ``last_enqueued_at`` on each schedule it
fires, and a schedule is "due" only when ``now - last_enqueued_at >=
cadence_seconds``. Running the tick more often than the cadence is safe —
it simply doesn't double-enqueue inside a cadence window.

**Factored for reuse (ADR 017).** The due-check (:func:`is_due`, typed
against the structural :class:`_Schedulable` protocol so both schedule
models satisfy it), the job-construction step (:func:`build_eval_job` /
:func:`build_scheduled_job`), and the generic enqueue loop
(:func:`enqueue_due`) are split out so a future trigger surface (item 13:
webhooks/events) can reuse the same enqueue path with a different job
builder.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, TypeVar
from uuid import uuid4

from movate.core.job_retry import DEFAULT_POLICY, DEFAULT_VISIBILITY_TIMEOUT_SECONDS
from movate.core.models import EvalSchedule, JobKind, JobRecord, JobSchedule, _now
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


class _Schedulable(Protocol):
    """The minimal due-check shape both schedule models satisfy.

    A structural :class:`typing.Protocol` so :func:`is_due` works for both
    :class:`EvalSchedule` and :class:`JobSchedule` (and any future schedule
    model) without a shared base class — each just needs these three
    fields. Keeps the cadence/idempotency logic in one place.
    """

    enabled: bool
    last_enqueued_at: datetime | None
    cadence_seconds: int


# Bound to the schedule type so ``enqueue_due``'s ``build_job`` / ``touch``
# / ``label`` callbacks stay type-coherent for a given schedule model.
_S = TypeVar("_S", bound=_Schedulable)


def _next_cron_occurrence(cron: str, timezone: str | None, *, after: datetime) -> datetime:
    """First occurrence of ``cron`` STRICTLY after ``after`` (ADR 100 D1).

    Evaluated in the schedule's IANA ``timezone`` (``None`` → UTC) so a
    "07:00 Mon-Fri" briefing tracks local wall-clock time across DST
    transitions — ``cronsim`` does the DST arithmetic (a nonexistent
    spring-forward slot fires at the post-jump instant; an ambiguous
    fall-back slot fires once). The returned datetime is tz-aware and
    directly comparable with the (UTC) tick ``now``.
    """
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from cronsim import CronSim  # noqa: PLC0415

    tz = ZoneInfo(timezone or "UTC")
    return next(CronSim(cron, after.astimezone(tz)))


def is_due(schedule: _Schedulable, *, now: datetime) -> bool:
    """Return whether ``schedule`` should enqueue at ``now``.

    Due when enabled AND the cadence window has elapsed. Two cadence forms
    share this one due-check (so the tick's idempotency story stays in one
    place):

    * **Interval** (``cadence_seconds``, the pre-ADR-100 behavior): due when
      never enqueued before OR the interval has fully elapsed since the
      last enqueue.
    * **Cron** (``cron`` + optional ``timezone``, ADR 100 D1 — only
      :class:`JobSchedule` carries it): due when the next occurrence after
      ``last_enqueued_at`` (or ``created_at`` for a never-fired schedule)
      is ``<= now``. Because the tick stamps ``last_enqueued_at = now`` on
      enqueue, a schedule fires AT MOST ONCE per matched window, and a
      missed window (tick down over a weekend) yields ONE catch-up run —
      never a backfill storm.

    Disabled schedules are never due — they're retained but dormant.

    Typed against the structural :class:`_Schedulable` protocol so both
    :class:`EvalSchedule` and :class:`JobSchedule` are accepted (``cron``
    is read structurally; a model without it is an interval schedule).
    """
    if not schedule.enabled:
        return False
    cron = getattr(schedule, "cron", None)
    if cron is not None:
        # Anchor at the last enqueue, or creation for a never-fired
        # schedule — occurrences predating the schedule itself never fire.
        anchor = schedule.last_enqueued_at or getattr(schedule, "created_at", None)
        if anchor is None:  # pragma: no cover — JobSchedule always stamps created_at
            return True
        try:
            occurrence = _next_cron_occurrence(
                cron, getattr(schedule, "timezone", None), after=anchor
            )
        except Exception:
            # A row with an unevaluable cron (e.g. hand-edited storage) is
            # skipped, never crashes the tick — same per-schedule fail-soft
            # posture as enqueue_due.
            logger.warning("cron_due_check_failed cron=%r — treating as not due", cron)
            return False
        return occurrence <= now
    if schedule.last_enqueued_at is None:
        return True
    elapsed = (now - schedule.last_enqueued_at).total_seconds()
    return elapsed >= schedule.cadence_seconds


def build_eval_job(schedule: EvalSchedule) -> JobRecord:
    """Construct the ``JobKind.EVAL`` :class:`JobRecord` for a schedule.

    The ``input`` payload mirrors the fields the API's ``EvalSubmission``
    sets on a manual eval job (``WorkerDispatch._execute_eval`` reads the
    same keys), so the scheduled path reuses the exact eval-as-job code —
    no separate execution branch. Tenant + notify target ride along so the
    drift alert (fired after the eval completes) can reach the operator.
    """
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id=schedule.tenant_id,
        kind=JobKind.EVAL,
        target=schedule.agent,
        input={
            "mock": schedule.mock,
            "runs": schedule.runs,
            "gate_mode": schedule.gate_mode,
            "gate": schedule.gate,
            "objective": schedule.objective,
            "baseline_id": schedule.baseline_id,
            "regression_tolerance": schedule.regression_tolerance,
            # Marks this as a scheduled (vs. ad-hoc) eval so the worker's
            # drift hook knows to run + alert. Also carries the alert target.
            "scheduled": True,
            "notify_email": schedule.notify_email,
        },
        notify_email=schedule.notify_email,
    )


def build_scheduled_job(schedule: JobSchedule) -> JobRecord:
    """Construct the ``JobKind.AGENT``/``WORKFLOW`` :class:`JobRecord` (ADR 017 D2).

    Mirrors how ``mdk submit`` (``cli.submit``) and ``POST /run``
    (``runtime.app.submit_run``) build an agent/workflow job: ``kind`` +
    ``target`` are copied straight through, and ``input`` is the schedule's
    stored payload (the ``RunRequest.input`` dict for agents, the initial
    state dict for workflows). Because the shape is identical, the worker's
    existing ``_execute_agent`` / ``_execute_workflow`` dispatch runs it
    with no new branch. ``notify_email`` rides along like the manual path.

    Kept deliberately generic so item 13 (webhook/event triggers) can reuse
    it to enqueue the same job kinds from a non-cron trigger.
    """
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id=schedule.tenant_id,
        kind=schedule.kind,
        target=schedule.target,
        input=schedule.input,
        notify_email=schedule.notify_email,
        # ADR 100 D4 provenance: walk the job back to the schedule that
        # enqueued it (the per-tenant handle). Manual submits carry None.
        origin=f"schedule:{schedule.name}",
    )


@dataclass
class TickResult:
    """Summary of one scheduler tick — what was enqueued and what was skipped."""

    now: datetime
    enqueued: list[str] = field(default_factory=list)
    """``job_id``s enqueued this tick (one per due schedule)."""
    skipped: list[str] = field(default_factory=list)
    """Schedule labels skipped because they weren't due (cadence not elapsed)."""
    reclaimed_requeued: int = 0
    """Orphaned ``RUNNING`` jobs requeued by the scaled-to-zero reaper
    backstop this tick (see :func:`run_all_scheduler_ticks`). ``0`` when
    the reaper didn't run (per-surface ticks) or found nothing stale."""
    reclaimed_dead_lettered: int = 0
    """Orphaned ``RUNNING`` jobs dead-lettered (retry budget exhausted) by
    the reaper backstop this tick. ``0`` when the reaper didn't run or
    found nothing exhausted."""

    @property
    def enqueued_count(self) -> int:
        return len(self.enqueued)

    def merge(self, other: TickResult) -> TickResult:
        """Combine two ticks' results (e.g. eval + job schedules).

        Keeps this tick's ``now`` and concatenates the enqueued / skipped
        lists, so :func:`run_all_scheduler_ticks` can report a single
        combined :class:`TickResult` across both scheduling surfaces.
        Reclaim counts are summed (the reaper runs once per unified tick,
        so in practice only one side carries non-zero values).
        """
        self.enqueued.extend(other.enqueued)
        self.skipped.extend(other.skipped)
        self.reclaimed_requeued += other.reclaimed_requeued
        self.reclaimed_dead_lettered += other.reclaimed_dead_lettered
        return self

    def summary(self) -> str:
        return (
            f"scheduler tick @ {self.now.isoformat()}: "
            f"enqueued {self.enqueued_count}, skipped {len(self.skipped)}"
        )


async def enqueue_due(
    storage: StorageProvider,
    schedules: list[_S],
    *,
    now: datetime,
    build_job: Callable[[_S], JobRecord] = build_eval_job,  # type: ignore[assignment]
    touch: Callable[[_S, datetime], Awaitable[None]] | None = None,
    label: Callable[[_S], str] = lambda s: s.agent,  # type: ignore[attr-defined]
) -> TickResult:
    """Enqueue one job per due schedule; stamp ``last_enqueued_at``.

    Generic over the job builder, the touch callback, and a ``label``
    extractor so ADR-017's agent/workflow scheduler (and item 13's trigger
    surface) can reuse this enqueue loop with a different ``build_job`` and
    persistence model. ``build_job`` / ``touch`` / ``label`` default to the
    eval scheduler's (``build_eval_job`` / :meth:`touch_eval_schedule` /
    ``schedule.agent``) when omitted.

    Per-schedule failures are logged and skipped — one bad schedule never
    blocks the rest of the tick.
    """
    result = TickResult(now=now)
    for schedule in schedules:
        if not is_due(schedule, now=now):
            result.skipped.append(label(schedule))
            continue
        try:
            job = build_job(schedule)
            await storage.save_job(job)
            if touch is not None:
                await touch(schedule, now)
            else:
                await storage.touch_eval_schedule(
                    schedule.agent,  # type: ignore[attr-defined]
                    tenant_id=schedule.tenant_id,  # type: ignore[attr-defined]
                    last_enqueued_at=now,
                )
            result.enqueued.append(job.job_id)
            logger.info(
                "scheduler_enqueued target=%s tenant=%s kind=%s job_id=%s cadence_s=%d",
                job.target,
                job.tenant_id,
                job.kind.value,
                job.job_id,
                schedule.cadence_seconds,
            )
        except Exception:
            logger.warning(
                "scheduler_enqueue_failed schedule=%s — skipping this "
                "schedule; other schedules continue",
                label(schedule),
                exc_info=True,
            )
            result.skipped.append(label(schedule))
    return result


async def run_scheduler_tick(
    storage: StorageProvider,
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> TickResult:
    """One scheduler tick: find due eval schedules and enqueue their jobs.

    This is the entrypoint an external cron calls (``mdk eval-scheduler-tick``
    on a Container Apps Job, or any cron locally). ``tenant_id=None`` ticks
    every tenant's schedules (operator/cron drain mode); a specific
    ``tenant_id`` scopes the tick to one tenant.

    Returns a :class:`TickResult` so the caller (CLI / cron logs) can report
    how many jobs were enqueued.
    """
    effective_now = now or _now()
    schedules = await storage.list_eval_schedules(tenant_id=tenant_id)
    result = await enqueue_due(storage, schedules, now=effective_now)
    logger.info(
        "scheduler_tick_done tenant=%s enqueued=%d skipped=%d",
        tenant_id or "<all>",
        result.enqueued_count,
        len(result.skipped),
    )
    return result


async def run_job_scheduler_tick(
    storage: StorageProvider,
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> TickResult:
    """One tick over the generic agent/workflow schedules (ADR 017 D2).

    Reuses :func:`enqueue_due` — the same due-check + idempotency machinery
    as the eval tick — with :func:`build_scheduled_job` and a ``touch`` that
    stamps :meth:`StorageProvider.touch_job_schedule`. ``tenant_id=None``
    drains every tenant (cron drain mode); a specific id scopes to one.
    """
    effective_now = now or _now()
    schedules = await storage.list_job_schedules(tenant_id=tenant_id)

    async def _touch(schedule: JobSchedule, when: datetime) -> None:
        await storage.touch_job_schedule(
            schedule.name,
            tenant_id=schedule.tenant_id,
            last_enqueued_at=when,
        )

    result = await enqueue_due(
        storage,
        schedules,
        now=effective_now,
        build_job=build_scheduled_job,
        touch=_touch,
        label=lambda s: s.name,
    )
    logger.info(
        "job_scheduler_tick_done tenant=%s enqueued=%d skipped=%d",
        tenant_id or "<all>",
        result.enqueued_count,
        len(result.skipped),
    )
    return result


async def run_all_scheduler_ticks(
    storage: StorageProvider,
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> TickResult:
    """Drain BOTH eval and generic agent/workflow schedules in one tick.

    Backs the unified ``mdk scheduler-tick`` cron entrypoint: runs
    :func:`run_scheduler_tick` (eval) then :func:`run_job_scheduler_tick`
    (agent/workflow) against the same ``now`` and merges their
    :class:`TickResult`\\ s. Either surface being empty is a no-op — the
    tick stays additive + default-off.

    It ALSO runs the stale-job reaper once, as the scaled-to-zero
    BACKSTOP: when the queue is empty and KEDA has scaled all workers to
    zero (it scales on ``queued`` depth, not ``running``), a single job
    orphaned in ``RUNNING`` has no worker to reap it. The cron tick fires
    regardless, so it recovers the orphan. Racing the worker-loop reaper
    is safe (atomic ``UPDATE ... WHERE status='running'``). The reaper
    call is wrapped so a storage hiccup never fails the tick.
    """
    effective_now = now or _now()
    eval_result = await run_scheduler_tick(storage, tenant_id=tenant_id, now=effective_now)
    job_result = await run_job_scheduler_tick(storage, tenant_id=tenant_id, now=effective_now)
    result = eval_result.merge(job_result)

    try:
        reclaimed = await storage.reclaim_stale_jobs(
            older_than=effective_now - timedelta(seconds=DEFAULT_VISIBILITY_TIMEOUT_SECONDS),
            max_attempts=DEFAULT_POLICY.max_attempts,
            now=effective_now,
        )
        result.reclaimed_requeued = reclaimed.requeued
        result.reclaimed_dead_lettered = reclaimed.dead_lettered
        if reclaimed.requeued or reclaimed.dead_lettered:
            logger.info(
                "scheduler_tick_reaper requeued=%d dead_lettered=%d",
                reclaimed.requeued,
                reclaimed.dead_lettered,
            )
    except Exception:
        logger.warning(
            "scheduler_tick_reaper_failed — tick result is otherwise valid",
            exc_info=True,
        )

    return result


__all__ = [
    "TickResult",
    "build_eval_job",
    "build_scheduled_job",
    "enqueue_due",
    "is_due",
    "run_all_scheduler_ticks",
    "run_job_scheduler_tick",
    "run_scheduler_tick",
]
