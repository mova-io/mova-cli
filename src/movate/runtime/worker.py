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
from datetime import UTC, datetime, timedelta

from movate.core.alert_emit import dead_letter_alert, emit_alert
from movate.core.events import EventKind
from movate.core.job_retry import (
    DEFAULT_POLICY,
    DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
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
from movate.runtime.events import emit_event
from movate.storage.base import StorageProvider
from movate.tracing import dec_in_flight, inc_in_flight, record_job_completed

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

    visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS
    """How long a job may sit in ``RUNNING`` before the reaper treats it
    as orphaned (worker OOM/SIGKILL/node loss) and reclaims it. Default
    15 minutes.

    This MUST be generously larger than the longest expected job. A
    value smaller than a still-running job's actual runtime would let
    the reaper requeue a HEALTHY in-flight job, causing at-least-once
    DOUBLE EXECUTION (two workers running the same job). When in doubt,
    err high — a stuck job recovers a few minutes late, but a too-low
    timeout silently double-runs work."""

    reap_interval_seconds: float = 60.0
    """How often :meth:`Worker.run_forever` runs the stale-job reaper.
    Throttled independently of ``poll_interval_seconds`` so a fast poll
    loop doesn't hammer the reaper UPDATE every tick. The reaper is
    cheap and racing-safe across workers, so this is a coarse knob."""

    job_timeout_seconds: float = 600.0
    """Per-job execution timeout (item 34). Bounds a SLOW/HUNG job whose
    worker is still alive — complementary to the reaper, which only
    reclaims jobs orphaned by a CRASHED worker. When a single
    ``execute_job`` exceeds this, the worker cancels it and records a
    retryable ``timeout`` ERROR (re-queued with backoff, dead-lettered
    after the retry budget).

    ORDERING INVARIANT: this MUST stay strictly SMALLER than
    ``visibility_timeout_seconds`` (default 600 < 900). The per-job
    timeout fails a hung job fast — long before the reaper's visibility
    window would ever consider it orphaned — so the reaper never
    double-runs a job that's merely slow. Were this larger than the
    visibility timeout, the reaper could reclaim (and a second worker
    re-run) a job that this worker is still actively timing out.

    ``<= 0`` disables the bound entirely (operator opt-out) — the
    dispatch runs unwrapped, exactly today's behavior, with only the
    reaper as a backstop."""


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

        # Cancellation checkpoint #1 (item 36, R4b): a job whose cancel was
        # requested while it was still QUEUED-but-just-claimed (the claim and
        # the cancel raced, and the cancel won by flipping cancel_requested
        # before we read the row). Skip dispatch entirely and write CANCELLED —
        # there's no point running work we already know is cancelled. This is
        # the cooperative model: we never start execution. CANCELLED is
        # terminal, so it's never retried.
        if job.cancel_requested:
            await self._finalize_cancelled(job, started=time.monotonic())
            return job

        started = time.monotonic()
        # Bracket dispatch with the in-flight gauge (mdk.jobs.in_flight, R3 /
        # item 33). try/finally so the decrement always runs — even if dispatch
        # raises below — otherwise the gauge would leak upward. No-op when
        # metrics are off (OTel extra absent or sink not OTLP).
        inc_in_flight(tenant_id=job.tenant_id)
        try:
            outcome = await self._dispatch_job(job)
        finally:
            dec_in_flight(tenant_id=job.tenant_id)

        # Decide retry vs terminal BEFORE writing back to storage.
        # The decision is a pure function of the outcome's retryable
        # flag + the job's current attempt_count; centralizing it
        # here keeps the worker loop's three branches obvious. Then apply
        # the cancellation checkpoint #2 override (item 36).
        final_status, final_action = await self._resolve_final(job, outcome)

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
        # Job-queue golden signals (mdk.jobs.completed + mdk.job.duration_ms,
        # R3 / item 33). Record the *final* persisted status — a retryable error
        # that exhausted its budget surfaces here as dead_letter, not error.
        # A "retry" action is NOT terminal (the job re-queues), so we only record
        # on a true terminal status. No-op when metrics are off.
        if final_action == "terminal":
            record_job_completed(
                kind=job.kind.value,
                status=final_status.value,
                duration_ms=duration_ms,
                tenant_id=job.tenant_id,
            )
            self._emit_run_terminal_event(
                job,
                final_status=final_status,
                result_run_id=outcome.result_run_id,
                duration_ms=duration_ms,
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
        # terminal status. The dispatcher's contract is "never raise";
        # we still wrap to belt-and-suspender any future implementation
        # that slips and raises something the worker shouldn't die on.
        if final_action == "terminal" and self._notifier is not None and job.notify_email:
            try:
                # Use the post-update view so the email reflects the
                # terminal status, not the RUNNING snapshot we have here.
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

    async def _finalize_cancelled(self, job: JobRecord, *, started: float) -> None:
        """Write a CANCELLED terminal for a job cancelled BEFORE dispatch.

        Checkpoint #1 path (item 36, R4b): the job carried
        ``cancel_requested`` at claim time, so we never executed it — no
        dispatch, no in-flight gauge bracket (we never inc'd it), no
        retry decision (CANCELLED is terminal, never re-queued). We just
        write the terminal status, mirror the normal path's completion
        metrics / progress hook / notification so observers see a real
        terminal, and return.

        Storage-write failure must NOT sink the loop — same contract as
        the normal terminal write: log and move on (the job stays
        RUNNING; the reaper/an operator can recover it).
        """
        try:
            await self._storage.update_job(
                job.job_id,
                tenant_id=job.tenant_id,
                status=JobStatus.CANCELLED,
                result_run_id=None,
                error=None,
            )
        except Exception:
            logger.exception(
                "worker_update_failed job_id=%s status=cancelled — job stuck in RUNNING",
                job.job_id,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "worker_completed job_id=%s kind=%s target=%s status=cancelled "
            "duration_ms=%d (cancelled before dispatch; not executed)",
            job.job_id,
            job.kind.value,
            job.target,
            duration_ms,
        )
        record_job_completed(
            kind=job.kind.value,
            status=JobStatus.CANCELLED.value,
            duration_ms=duration_ms,
            tenant_id=job.tenant_id,
        )
        # ADR 035 D1 — emit ``run.failed`` for a cancel-before-dispatch
        # terminal (CANCELLED is one of the non-SUCCESS terminals the
        # main path bucketed into ``run.failed``).
        self._emit_run_terminal_event(
            job,
            final_status=JobStatus.CANCELLED,
            result_run_id=None,
            duration_ms=duration_ms,
        )
        if self._on_job_complete is not None:
            try:
                self._on_job_complete(
                    job,
                    DispatchOutcome(
                        status=JobStatus.CANCELLED,
                        result_run_id=None,
                        error=None,
                    ),
                    duration_ms,
                )
            except Exception:
                logger.warning("on_job_complete callback raised", exc_info=True)
        if self._notifier is not None and job.notify_email:
            try:
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

    def _emit_run_terminal_event(
        self,
        job: JobRecord,
        *,
        final_status: JobStatus,
        result_run_id: str | None,
        duration_ms: int,
    ) -> None:
        """Emit ADR 035 D1's lifecycle event for a terminal-state run.

        ``run.completed`` on SUCCESS, ``run.failed`` on every other
        terminal (ERROR / SAFETY_BLOCKED / DEAD_LETTER / CANCELLED).
        Only AGENT and WORKFLOW jobs emit here — EVAL jobs land their
        own ``eval.failed`` event at the dispatch layer (the eval is
        the meaningful thing, not the job wrapper); BENCH jobs don't
        emit in D1 (the canonical kind set stays small).

        Fire-and-forget via :func:`emit_event` — NEVER raises into the
        worker loop, NEVER waits on the storage write.
        """
        # ADR 057 D1 (step 2) — a job that exhausted its retry budget and
        # landed in DEAD_LETTER is an operator-actionable condition: raise a
        # typed ``dead_letter_spike`` alert onto the outbox so the router can
        # page on it. Emitted for EVERY job kind (the dead-letter is the
        # signal, not the job wrapper), so this sits ABOVE the AGENT/WORKFLOW
        # gate on the lifecycle event below. Fire-and-forget, best-effort (D5:
        # never raises into the worker loop); recorded-but-undelivered when no
        # routes are configured (D7).
        if final_status == JobStatus.DEAD_LETTER:
            emit_alert(
                self._storage,
                dead_letter_alert(
                    tenant_id=job.tenant_id,
                    subject=job.target,
                    summary=(
                        f"job for {job.kind.value}/{job.target} exhausted its retry "
                        f"budget and was dead-lettered (job {job.job_id})"
                    ),
                    data={
                        "job_id": job.job_id,
                        "agent": job.target,
                        "kind": job.kind.value,
                        "duration_ms": duration_ms,
                    },
                ),
            )
        if job.kind not in (JobKind.AGENT, JobKind.WORKFLOW):
            return
        run_kind = (
            EventKind.RUN_COMPLETED if final_status == JobStatus.SUCCESS else EventKind.RUN_FAILED
        )
        emit_event(
            self._storage,
            tenant_id=job.tenant_id,
            kind=run_kind,
            subject=result_run_id or job.job_id,
            data={
                "job_id": job.job_id,
                "agent": job.target,
                "status": final_status.value,
                "kind": job.kind.value,
                "duration_ms": duration_ms,
            },
        )

    async def _dispatch_job(self, job: JobRecord) -> DispatchOutcome:
        """Run dispatch for one job, converting failures into outcomes.

        Never raises: a per-job execution timeout (item 34) and a real
        dispatch crash both become a retryable ERROR ``DispatchOutcome``
        so the worker loop's single update path handles them uniformly.
        Lives between the in-flight try/finally in ``run_one_cycle`` —
        ``dec_in_flight`` still runs whichever branch fires.
        """
        timeout = self._config.job_timeout_seconds
        try:
            # Per-job execution timeout. Bound a slow/hung job so it can't
            # hold a worker slot indefinitely. The timeout MUST be <
            # visibility_timeout_seconds (see the WorkerConfig docstring):
            # we fail the job fast as a retryable error long before the
            # reaper would consider it orphaned, so a merely-slow job is
            # never double-run.
            #
            # ``<= 0`` opts out — call unwrapped (today's behavior).
            # asyncio.wait_for CANCELS the underlying coroutine on timeout,
            # which is intended: it frees the worker slot. A job cancelled
            # mid-execution may leave a partial run record; we record it as
            # a retryable timeout error and re-queue it, which is desired.
            if timeout > 0:
                return await asyncio.wait_for(self._dispatch.execute_job(job), timeout=timeout)
            return await self._dispatch.execute_job(job)
        except TimeoutError:
            # The dispatch exceeded job_timeout_seconds. asyncio.wait_for
            # (Python 3.11+) raises the builtin TimeoutError here; this
            # branch is checked BEFORE the generic Exception below
            # (TimeoutError IS an Exception) so a real timeout doesn't
            # masquerade as an "internal" crash. Matches how run_forever's
            # poll wait suppresses TimeoutError from the same wait_for.
            logger.warning("worker_job_timeout job_id=%s timeout=%.0fs", job.job_id, timeout)
            return DispatchOutcome(
                status=JobStatus.ERROR,
                result_run_id=None,
                error=ErrorInfo(
                    type="timeout",
                    message=f"job exceeded {timeout:.0f}s execution timeout",
                    retryable=True,
                ).model_dump(),
            )
        except Exception as exc:
            # Programming bug in dispatch (or in the executor it wraps).
            # Record as INTERNAL so operators can triage.
            logger.exception("worker_dispatch_crashed job_id=%s", job.job_id)
            return DispatchOutcome(
                status=JobStatus.ERROR,
                result_run_id=None,
                error=ErrorInfo(
                    type="internal",
                    message=f"worker dispatch crashed: {exc}",
                    retryable=True,
                ).model_dump(),
            )

    async def _resolve_final(
        self, job: JobRecord, outcome: DispatchOutcome
    ) -> tuple[JobStatus, str]:
        """Resolve the dispatch outcome, then apply the cancel override.

        Cancellation checkpoint #2 (item 36, R4b): the job was claimed
        and dispatched normally, but an operator may have requested
        cancellation WHILE it was running. We re-fetch to read the latest
        ``cancel_requested`` flag — the dispatch we just ran is NOT
        pre-empted (cooperative model: no mid-LLM-call interruption), so
        the in-flight work completed, but if cancellation was requested we
        DISCARD that outcome and write ``CANCELLED`` instead.

        ``CANCELLED`` is terminal, so it overrides any "retry" the
        resolver picked — a cancelled job is never re-queued. The
        in-flight gauge dec already ran (run_one_cycle's finally), and the
        retry/timeout decision in :meth:`_resolve_outcome` stays intact
        for the non-cancelled path.
        """
        final_status, final_action = self._resolve_outcome(job, outcome)
        latest = await self._storage.get_job(job.job_id, tenant_id=job.tenant_id)
        if latest is not None and latest.cancel_requested:
            return JobStatus.CANCELLED, "terminal"
        return final_status, final_action

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

    async def _maybe_reap(self, *, now: datetime, last_reap: datetime) -> datetime:
        """Run the stale-job reaper if ``reap_interval_seconds`` has elapsed.

        Returns the timestamp to use as the next ``last_reap`` — ``now``
        if we reaped this tick, otherwise the unchanged ``last_reap``.

        Crash-recovery for jobs orphaned in ``RUNNING`` by a hard-killed
        worker (OOM/SIGKILL/node loss). Racing with other workers' reapers
        is safe — the atomic ``UPDATE ... WHERE status='running'`` means
        the loser's predicate won't match rows the winner already flipped.

        A reaper hiccup (storage blip) must NEVER kill the worker loop, so
        the call is wrapped: we log and carry on, and we still advance
        ``last_reap`` so a persistently-failing reaper doesn't busy-loop.
        """
        if (now - last_reap).total_seconds() < self._config.reap_interval_seconds:
            return last_reap
        try:
            result = await self._storage.reclaim_stale_jobs(
                older_than=now - timedelta(seconds=self._config.visibility_timeout_seconds),
                max_attempts=self._config.retry_policy.max_attempts,
                now=now,
            )
            if result.requeued or result.dead_lettered:
                logger.info(
                    "reaper_reclaimed requeued=%d dead_lettered=%d",
                    result.requeued,
                    result.dead_lettered,
                )
        except Exception:
            logger.warning("reaper_failed — worker loop continues", exc_info=True)
        return now

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event`` is set. Sleeps when the queue is empty.

        Tests call ``run_one_cycle`` directly to avoid timing
        flakiness. The CLI uses this method with a SIGINT/SIGTERM
        handler that sets the event.

        Each iteration also runs the stale-job reaper on a throttle
        (``reap_interval_seconds``) — the PRIMARY crash-recovery path for
        jobs orphaned in ``RUNNING``. ``run_one_cycle`` stays reaper-free
        so tests that call it directly are unaffected.
        """
        logger.info(
            "worker_started tenant_id=%s poll_interval=%.2fs "
            "visibility_timeout=%.0fs reap_interval=%.0fs",
            self._config.tenant_id or "<all>",
            self._config.poll_interval_seconds,
            self._config.visibility_timeout_seconds,
            self._config.reap_interval_seconds,
        )
        # Start "fully elapsed" so the first iteration reaps immediately —
        # a worker booting after a crash should recover orphans right away,
        # not wait a full interval.
        last_reap = datetime.now(UTC) - timedelta(seconds=self._config.reap_interval_seconds)
        while not stop_event.is_set():
            last_reap = await self._maybe_reap(now=datetime.now(UTC), last_reap=last_reap)
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
