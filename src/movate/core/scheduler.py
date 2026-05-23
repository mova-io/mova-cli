"""Portable, cron-driven eval scheduler (ADR 016 D2).

The continuous-eval loop needs the eval suite to run *on a cadence* against
the live agent. This module is the portable scheduler primitive: a stateless
**tick** that, when invoked, finds the schedules that are **due** and
**enqueues a ``JobKind.EVAL`` job** for each — reusing the existing
eval-as-job path (``WorkerDispatch._execute_eval``). The existing Postgres
job queue + KEDA worker then execute them with their existing retry /
dead-letter behavior.

**There is no in-process timer daemon.** The tick is meant to be driven by
an *external* cron:

* On Azure: a Container Apps **Job** with a cron trigger that runs
  ``mdk eval-scheduler-tick`` (or calls :func:`run_scheduler_tick`).
* Locally / anywhere: any cron, a CI schedule, or a manual invocation.

This keeps the scheduler vendor-neutral (ADR 001) — ACA Jobs is just the
cron that calls the tick; nothing here imports a cloud SDK.

**Idempotency.** The tick stamps ``last_enqueued_at`` on each schedule it
fires, and a schedule is "due" only when ``now - last_enqueued_at >=
cadence_seconds``. Running the tick more often than the cadence is safe —
it simply doesn't double-enqueue inside a cadence window.

**Factored for reuse (ADR 017).** The job-construction step is split out as
:func:`build_eval_job` and the generic enqueue loop as :func:`enqueue_due`,
so a future agent/workflow scheduler can reuse the due-check + enqueue
machinery with a different job builder.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from movate.core.models import EvalSchedule, JobKind, JobRecord, _now
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


def is_due(schedule: EvalSchedule, *, now: datetime) -> bool:
    """Return whether ``schedule`` should enqueue at ``now``.

    Due when enabled AND (never enqueued before OR the cadence interval
    has fully elapsed since the last enqueue). Disabled schedules are
    never due — they're retained but dormant.
    """
    if not schedule.enabled:
        return False
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


@dataclass
class TickResult:
    """Summary of one scheduler tick — what was enqueued and what was skipped."""

    now: datetime
    enqueued: list[str] = field(default_factory=list)
    """``job_id``s enqueued this tick (one per due schedule)."""
    skipped: list[str] = field(default_factory=list)
    """Agent names skipped because they weren't due (cadence not elapsed)."""

    @property
    def enqueued_count(self) -> int:
        return len(self.enqueued)

    def summary(self) -> str:
        return (
            f"scheduler tick @ {self.now.isoformat()}: "
            f"enqueued {self.enqueued_count}, skipped {len(self.skipped)}"
        )


async def enqueue_due(
    storage: StorageProvider,
    schedules: list[EvalSchedule],
    *,
    now: datetime,
    build_job: Callable[[EvalSchedule], JobRecord] = build_eval_job,
    touch: Callable[[EvalSchedule, datetime], Awaitable[None]] | None = None,
) -> TickResult:
    """Enqueue one job per due schedule; stamp ``last_enqueued_at``.

    Generic over the job builder + the touch callback so ADR-017 can reuse
    this enqueue loop for agent/workflow schedules with a different
    ``build_job`` and persistence model. ``touch`` defaults to
    :meth:`StorageProvider.touch_eval_schedule` when omitted.

    Per-schedule failures are logged and skipped — one bad agent never
    blocks the rest of the tick.
    """
    result = TickResult(now=now)
    for schedule in schedules:
        if not is_due(schedule, now=now):
            result.skipped.append(schedule.agent)
            continue
        try:
            job = build_job(schedule)
            await storage.save_job(job)
            if touch is not None:
                await touch(schedule, now)
            else:
                await storage.touch_eval_schedule(
                    schedule.agent,
                    tenant_id=schedule.tenant_id,
                    last_enqueued_at=now,
                )
            result.enqueued.append(job.job_id)
            logger.info(
                "scheduler_enqueued_eval agent=%s tenant=%s job_id=%s cadence_s=%d",
                schedule.agent,
                schedule.tenant_id,
                job.job_id,
                schedule.cadence_seconds,
            )
        except Exception:
            logger.warning(
                "scheduler_enqueue_failed agent=%s tenant=%s — skipping this "
                "schedule; other schedules continue",
                schedule.agent,
                schedule.tenant_id,
                exc_info=True,
            )
            result.skipped.append(schedule.agent)
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


__all__ = [
    "TickResult",
    "build_eval_job",
    "enqueue_due",
    "is_due",
    "run_scheduler_tick",
]
