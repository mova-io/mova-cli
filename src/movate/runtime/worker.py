"""Worker loop — drain the queue, dispatch each job, persist the result.

Two entry points:

* :meth:`Worker.run_one_cycle` — claim one job, dispatch it, update
  storage. Returns the handled :class:`JobRecord` or ``None`` when
  the queue is empty. Deterministic; tests call this directly so they
  don't need to coordinate sleeps or stop events.
* :meth:`Worker.run_forever` — loop on ``run_one_cycle``, sleeping
  ``poll_interval_seconds`` between empty-queue ticks. Runs until the
  caller-supplied :class:`asyncio.Event` is set (SIGINT / SIGTERM in
  the CLI; explicit ``set()`` in tests).

The worker NEVER crashes on a single bad job. Every job is wrapped in
a try/except: if dispatch raises (storage failure, programming error
in the dispatch layer, etc.), the job is updated to ``ERROR`` with a
synthetic error record and the loop continues. A whole queue's worth
of poison-pill jobs would generate noisy logs but wouldn't take the
worker down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from movate.core.job_retry import (
    DEFAULT_POLICY,
    JobRetryPolicy,
    compute_next_retry_at,
    is_exhausted,
    should_retry,
)
from movate.core.models import (
    ErrorInfo,
    JobKind,
    JobRecord,
    JobStatus,
)
from movate.core.notify import NotificationDispatcher
from movate.runtime.dispatch import DispatchOutcome, WorkerDispatch
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


@dataclass
class WorkerConfig:
    poll_interval_seconds: float = 0.5
    """How long to sleep between claim attempts when the queue is empty.
    Cheap polls (<1s) feel responsive; longer polls (~5s) reduce
    sqlite contention on shared dev DBs."""

    tenant_id: str | None = None
    """If set, only claim jobs for this tenant. ``None`` drains all
    tenants (operator/dev mode). The HTTP layer never sees this knob —
    workers are configured by the operator."""

    retry_policy: JobRetryPolicy = DEFAULT_POLICY
    """Retry policy applied to transient failures. Default: 3 total
    attempts (initial + 2 retries), 5s base, 3x factor, 5min cap,
    ±25% jitter. Set ``max_attempts=1`` to disable retries entirely
    (every retryable error → ``DEAD_LETTER``)."""


class Worker:
    """Drains a queue using a :class:`WorkerDispatch`."""

    def __init__(
        self,
        *,
        storage: StorageProvider,
        dispatch: WorkerDispatch,
        config: WorkerConfig | None = None,
        on_job_complete: Callable[[JobRecord, DispatchOutcome, int], None] | None = None,
        notifier: NotificationDispatcher | None = None,
    ) -> None:
        self._storage = storage
        self._dispatch = dispatch
        self._config = config or WorkerConfig()
        self._on_job_complete = on_job_complete
        """Optional progress hook: ``(job, outcome, duration_ms)``.
        Fires after each job completes (including ERROR / SAFETY_BLOCKED
        terminals); CLI uses it to render a per-job line in the live
        worker feed without coupling Worker to UI."""
        self._notifier = notifier
        """Optional :class:`NotificationDispatcher`. Fires after the
        job transitions to a terminal status; receives the post-update
        :class:`JobRecord` (with ``notify_email`` + final status set).
        Errors inside the dispatcher are swallowed — courtesy, not
        load-bearing."""

    async def run_one_cycle(self) -> JobRecord | None:
        """Claim one job (if any), dispatch, update. Returns the
        :class:`JobRecord` that was *claimed* (in its post-claim,
        ``RUNNING`` form) so tests can assert flow, or ``None`` if
        the queue was empty.

        The dispatch + update happen unconditionally on the same job
        — failing to update would leave it stuck in ``RUNNING`` and
        starve the queue, so the update is the last thing we do.
        """
        job = await self._storage.claim_next_job(tenant_id=self._config.tenant_id)
        if job is None:
            return None

        started = time.monotonic()
        try:
            outcome = await self._dispatch.execute_job(job)
        except Exception as exc:
            # Programming bug in dispatch (or in the executor it
            # wraps). Record as INTERNAL so operators can triage.
            logger.exception("worker_dispatch_crashed job_id=%s", job.job_id)
            outcome = DispatchOutcome(
                status=JobStatus.ERROR,
                result_run_id=None,
                error=ErrorInfo(
                    type="internal",
                    message=f"worker dispatch crashed: {exc}",
                    retryable=True,
                ).model_dump(),
            )

        # Decide retry vs terminal BEFORE writing back to storage.
        # The decision is a pure function of the outcome's retryable
        # flag + the job's current attempt_count; centralizing it
        # here keeps the worker loop's three branches obvious.
        final_status, final_action = self._resolve_outcome(job, outcome)

        # Even if the storage write fails, the loop should continue.
        # The job will appear stuck in RUNNING; an operator can
        # requeue or update manually. The job's tenant_id is the SQL
        # filter that prevents a misconfigured worker from mutating
        # another tenant's job — even if `claim_next_job` were called
        # without a tenant scope (operator drain mode), this guarantees
        # the write only ever lands on the row we just claimed.
        try:
            if final_action == "retry":
                # Transient failure with retry budget left → re-queue.
                next_attempt = job.attempt_count + 1
                next_retry_at = compute_next_retry_at(
                    attempt_count=next_attempt,
                    policy=self._config.retry_policy,
                )
                await self._storage.requeue_job(
                    job.job_id,
                    tenant_id=job.tenant_id,
                    next_retry_at=next_retry_at,
                    attempt_count=next_attempt,
                )
                logger.info(
                    "worker_requeued job_id=%s attempt=%d next_retry_at=%s reason=%s",
                    job.job_id,
                    next_attempt,
                    next_retry_at.isoformat(),
                    (outcome.error or {}).get("type", "unknown"),
                )
            else:
                # Terminal — SUCCESS / SAFETY_BLOCKED / ERROR / DEAD_LETTER.
                await self._storage.update_job(
                    job.job_id,
                    tenant_id=job.tenant_id,
                    status=final_status,
                    result_run_id=outcome.result_run_id,
                    error=outcome.error,
                )
        except Exception:
            logger.exception(
                "worker_update_failed job_id=%s status=%s — job stuck in RUNNING",
                job.job_id,
                final_status.value,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "worker_completed job_id=%s kind=%s target=%s status=%s duration_ms=%d",
            job.job_id,
            job.kind.value,
            job.target,
            final_status.value,
            duration_ms,
        )
        if self._on_job_complete is not None:
            # Decorative; never sink the worker on a buggy callback.
            # We pass an outcome reflecting the FINAL status (which
            # may be DEAD_LETTER even if the dispatch reported ERROR)
            # so the UI sees what was actually persisted.
            try:
                final_outcome = (
                    outcome
                    if outcome.status == final_status
                    else DispatchOutcome(
                        status=final_status,
                        result_run_id=outcome.result_run_id,
                        error=outcome.error,
                    )
                )
                self._on_job_complete(job, final_outcome, duration_ms)
            except Exception:
                logger.warning("on_job_complete callback raised", exc_info=True)

        # Fire-and-await notification AFTER the update has committed.
        # Re-queued jobs DON'T notify — the run isn't done yet; the
        # notification fires when the retry eventually lands a true
        # terminal status.
        #
        # We invoke the dispatcher on EVERY terminal job. Each composed
        # backend decides for itself whether the job is in scope:
        #   * Email: fires only if ``job.notify_email`` is set (per-job opt-in)
        #   * SMS: fires only if ``job.notify_sms`` is set (per-job opt-in)
        #   * Telegram: fires on every terminal job when its env config
        #     is present (operator-wide alert pattern — different shape
        #     from the per-job channels because it's the same human
        #     across all jobs)
        #   * Console fallbacks (when env not configured): log only
        #
        # The dispatcher contract is "never raise"; we still wrap to
        # belt-and-suspender any future implementation that breaks it.
        if final_action == "terminal" and self._notifier is not None:
            try:
                # Use the post-update view so the notification reflects
                # the terminal status, not the RUNNING snapshot we have
                # here.
                terminal_view = await self._storage.get_job(job.job_id, tenant_id=job.tenant_id)
                if terminal_view is not None:
                    await self._notifier.notify_terminal(terminal_view)
            except Exception:
                logger.warning(
                    "notify_dispatcher_raised job_id=%s — job state "
                    "is unchanged; this is notification path only",
                    job.job_id,
                    exc_info=True,
                )
        return job

    def _resolve_outcome(self, job: JobRecord, outcome: DispatchOutcome) -> tuple[JobStatus, str]:
        """Decide what to do with a dispatch outcome.

        Returns ``(final_status, action)`` where:

        * ``action == "retry"`` → caller calls ``requeue_job``;
          ``final_status`` is ``QUEUED`` (informational; not persisted
          via update_job).
        * ``action == "terminal"`` → caller calls ``update_job`` with
          ``final_status``. May be the original outcome status
          (SUCCESS / SAFETY_BLOCKED / ERROR) or ``DEAD_LETTER`` if a
          retryable failure exhausted its budget.
        """
        # Success and safety-blocked are always terminal; nothing to retry.
        if outcome.status in (JobStatus.SUCCESS, JobStatus.SAFETY_BLOCKED):
            return outcome.status, "terminal"

        # The outcome is ERROR. Check whether it's retryable + within budget.
        retryable = bool((outcome.error or {}).get("retryable", False))
        if should_retry(
            retryable=retryable,
            attempt_count=job.attempt_count,
            policy=self._config.retry_policy,
        ):
            return JobStatus.QUEUED, "retry"

        # Either not retryable, or retryable-but-exhausted. Distinguish
        # the two so operators triaging dead-letters know "we tried"
        # vs "this was always going to fail."
        if retryable and is_exhausted(
            attempt_count=job.attempt_count, policy=self._config.retry_policy
        ):
            return JobStatus.DEAD_LETTER, "terminal"
        return JobStatus.ERROR, "terminal"

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event`` is set. Sleeps when the queue is empty.

        Tests call ``run_one_cycle`` directly to avoid timing
        flakiness. The CLI uses this method with a SIGINT/SIGTERM
        handler that sets the event.
        """
        logger.info(
            "worker_started tenant_id=%s poll_interval=%.2fs",
            self._config.tenant_id or "<all>",
            self._config.poll_interval_seconds,
        )
        while not stop_event.is_set():
            handled = await self.run_one_cycle()
            if handled is None:
                # No work — wait, but cancel-able via the stop event.
                # Times out cleanly when the queue stays empty for the
                # whole poll interval.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._config.poll_interval_seconds,
                    )
        logger.info("worker_stopped")


__all__ = ["Worker", "WorkerConfig"]


# Re-export so callers don't have to reach into ``dispatch`` for the
# class they pair with the loop.
WorkerDispatch = WorkerDispatch  # noqa: PLW0127 — intentional re-export
JobKind = JobKind  # noqa: PLW0127 — for CLI imports without reaching into core
