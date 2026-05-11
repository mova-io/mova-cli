"""Job-level retry policy — pure math, storage round-trip, worker integration.

Three layers, each tested in isolation:

1. **Retry policy math** — ``should_retry``, ``compute_next_retry_at``,
   ``is_exhausted``. Pure functions; no I/O.
2. **Storage** — ``requeue_job`` round-trip + ``claim_next_job``
   honoring ``next_retry_at`` + ``update_job`` accepting
   ``DEAD_LETTER``. Parametrized over memory + sqlite (+ postgres
   when configured).
3. **Worker integration** — full claim → dispatch → retry decision →
   ``requeue_job``/``update_job`` lifecycle. Uses
   ``InMemoryStorage`` so the tests are sync-fast; a fake dispatch
   returns the outcome shape we want without provider machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from movate.core.job_retry import (
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
from movate.runtime.dispatch import DispatchOutcome
from movate.runtime.worker import Worker, WorkerConfig
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# 1. Pure math — should_retry / is_exhausted / compute_next_retry_at
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_should_retry_false_for_non_retryable() -> None:
    """Auth errors, schema errors, policy violations stay terminal —
    no amount of retrying will help."""
    assert should_retry(retryable=False, attempt_count=0) is False
    assert should_retry(retryable=False, attempt_count=5) is False


@pytest.mark.unit
def test_should_retry_true_under_budget() -> None:
    """Transient errors retry while attempts remain. Default policy
    is max_attempts=3 → initial + 2 retries → True at attempt_count
    0 and 1, False at 2."""
    assert should_retry(retryable=True, attempt_count=0) is True
    assert should_retry(retryable=True, attempt_count=1) is True
    assert should_retry(retryable=True, attempt_count=2) is False


@pytest.mark.unit
def test_should_retry_respects_custom_policy() -> None:
    """max_attempts=1 means "no retries" — every retryable error goes
    straight to dead-letter."""
    no_retries = JobRetryPolicy(max_attempts=1)
    assert should_retry(retryable=True, attempt_count=0, policy=no_retries) is False

    generous = JobRetryPolicy(max_attempts=10)
    assert should_retry(retryable=True, attempt_count=8, policy=generous) is True
    assert should_retry(retryable=True, attempt_count=9, policy=generous) is False


@pytest.mark.unit
def test_is_exhausted_only_true_at_budget_boundary() -> None:
    """``is_exhausted`` is the same predicate as "retryable-but-can't-
    retry-anymore" — used to distinguish DEAD_LETTER from ERROR at
    the worker layer."""
    assert is_exhausted(attempt_count=0) is False
    assert is_exhausted(attempt_count=1) is False
    assert is_exhausted(attempt_count=2) is True  # 3rd attempt would exhaust


@pytest.mark.unit
def test_compute_next_retry_at_grows_exponentially() -> None:
    """attempt 1 ≈ base, attempt 2 ≈ base*factor, attempt 3 ≈
    base*factor², capped at cap_seconds.

    Use jitter=0 here so the math is deterministic; the jitter
    branch has its own dedicated test below.
    """
    no_jitter = JobRetryPolicy(base_seconds=10.0, factor=2.0, cap_seconds=120.0, jitter=0.0)
    now = datetime(2026, 5, 10, tzinfo=UTC)

    # attempt 1 → 10s after now
    t1 = compute_next_retry_at(attempt_count=1, policy=no_jitter, now=now)
    assert t1 == now + timedelta(seconds=10)

    # attempt 2 → 20s
    t2 = compute_next_retry_at(attempt_count=2, policy=no_jitter, now=now)
    assert t2 == now + timedelta(seconds=20)

    # attempt 3 → 40s
    t3 = compute_next_retry_at(attempt_count=3, policy=no_jitter, now=now)
    assert t3 == now + timedelta(seconds=40)

    # attempt 5 → would be 160s, capped at 120s
    t5 = compute_next_retry_at(attempt_count=5, policy=no_jitter, now=now)
    assert t5 == now + timedelta(seconds=120)


@pytest.mark.unit
def test_compute_next_retry_at_applies_jitter() -> None:
    """With jitter=0.25, the actual delay is base ± 25%. We assert the
    BAND not an exact value (jitter is by definition random)."""
    policy = JobRetryPolicy(base_seconds=10.0, factor=1.0, cap_seconds=1000.0, jitter=0.25)
    now = datetime(2026, 5, 10, tzinfo=UTC)

    for _ in range(20):
        retry_at = compute_next_retry_at(attempt_count=1, policy=policy, now=now)
        delay = (retry_at - now).total_seconds()
        # 10s ± 25% → [7.5, 12.5]
        assert 7.5 <= delay <= 12.5


@pytest.mark.unit
def test_compute_next_retry_at_never_returns_past() -> None:
    """Jitter could mathematically push delay negative on tiny bases;
    the implementation must floor at 0 so retries don't get scheduled
    in the past."""
    weird = JobRetryPolicy(
        base_seconds=0.01, factor=1.0, cap_seconds=100.0, jitter=2.0
    )  # jitter > base → could produce negative without the floor
    now = datetime(2026, 5, 10, tzinfo=UTC)
    for _ in range(50):
        retry_at = compute_next_retry_at(attempt_count=1, policy=weird, now=now)
        assert retry_at >= now


# ---------------------------------------------------------------------------
# 2. Storage round-trip — requeue_job, claim respects next_retry_at,
#    update_job accepts DEAD_LETTER
# ---------------------------------------------------------------------------


def _make_job_record(*, tenant_id: str = "t1") -> JobRecord:
    return JobRecord(
        job_id=uuid4().hex,
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="alpha",
        input={"text": "hi"},
    )


@pytest.mark.unit
async def test_storage_requeue_job_flips_running_to_queued(storage) -> None:
    """``requeue_job`` is the inverse of claim: status back to QUEUED,
    claimed_at cleared, attempt_count + next_retry_at stamped."""
    j = _make_job_record()
    await storage.save_job(j)
    claimed = await storage.claim_next_job()
    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING

    next_retry = datetime.now(UTC) + timedelta(seconds=60)
    await storage.requeue_job(
        j.job_id,
        tenant_id="t1",
        next_retry_at=next_retry,
        attempt_count=1,
    )

    refetched = await storage.get_job(j.job_id, tenant_id="t1")
    assert refetched is not None
    assert refetched.status == JobStatus.QUEUED
    assert refetched.claimed_at is None
    assert refetched.attempt_count == 1
    assert refetched.next_retry_at is not None
    # Round-trip tolerance: sqlite ISO strings + asyncpg TIMESTAMPTZ
    # are both microsecond-precise, so we expect exact equality
    # (modulo any tz normalization the backend does).
    assert abs((refetched.next_retry_at - next_retry).total_seconds()) < 1.0


@pytest.mark.unit
async def test_claim_next_job_skips_jobs_with_future_retry(storage) -> None:
    """A re-queued job with next_retry_at in the future must NOT be
    claimed — that's how backoff is enforced. Fresh jobs (no
    next_retry_at) are still claimable."""
    # Job A: fresh, claimable now.
    a = _make_job_record(tenant_id="t1")
    # Job B: re-queued with retry in the future — must be skipped.
    b = _make_job_record(tenant_id="t1")
    await storage.save_job(a)
    await storage.save_job(b)

    # Manually put B in the "running" state then re-queue with future retry.
    await storage.claim_next_job(tenant_id="t1")  # claims A first (older row)
    # Now make B the running one.
    await storage.update_job(
        a.job_id, tenant_id="t1", status=JobStatus.SUCCESS, result_run_id="r-a"
    )
    claimed_b = await storage.claim_next_job(tenant_id="t1")
    assert claimed_b is not None
    assert claimed_b.job_id == b.job_id

    future = datetime.now(UTC) + timedelta(seconds=300)
    await storage.requeue_job(b.job_id, tenant_id="t1", next_retry_at=future, attempt_count=1)

    # Next claim attempt — queue has only B, but B's next_retry_at is
    # in the future → claim returns None.
    assert await storage.claim_next_job(tenant_id="t1") is None


@pytest.mark.unit
async def test_claim_next_job_picks_up_after_retry_elapsed(storage) -> None:
    """Once next_retry_at is in the past, the job becomes claimable again."""
    j = _make_job_record(tenant_id="t1")
    await storage.save_job(j)
    await storage.claim_next_job(tenant_id="t1")
    past = datetime.now(UTC) - timedelta(seconds=60)
    await storage.requeue_job(j.job_id, tenant_id="t1", next_retry_at=past, attempt_count=1)

    claimed = await storage.claim_next_job(tenant_id="t1")
    assert claimed is not None
    assert claimed.job_id == j.job_id
    assert claimed.attempt_count == 1


@pytest.mark.unit
async def test_update_job_accepts_dead_letter(storage) -> None:
    """``DEAD_LETTER`` is now a valid terminal status. Catches the
    "operator forgot to add DEAD_LETTER to the allow-list" regression."""
    j = _make_job_record(tenant_id="t1")
    await storage.save_job(j)
    await storage.claim_next_job(tenant_id="t1")

    await storage.update_job(
        j.job_id,
        tenant_id="t1",
        status=JobStatus.DEAD_LETTER,
        error={"type": "rate_limit", "message": "exhausted retries", "retryable": True},
    )
    got = await storage.get_job(j.job_id, tenant_id="t1")
    assert got is not None
    assert got.status == JobStatus.DEAD_LETTER
    assert got.completed_at is not None


@pytest.mark.unit
async def test_save_job_persists_retry_fields(storage) -> None:
    """Fresh jobs round-trip with attempt_count=0, next_retry_at=None
    — the default state of "never retried"."""
    j = _make_job_record()
    await storage.save_job(j)
    got = await storage.get_job(j.job_id, tenant_id=j.tenant_id)
    assert got is not None
    assert got.attempt_count == 0
    assert got.next_retry_at is None


# ---------------------------------------------------------------------------
# 3. Worker integration — retry decision wired through run_one_cycle
# ---------------------------------------------------------------------------


@dataclass
class _FixedOutcomeDispatch:
    """Test double: returns a pre-configured outcome regardless of input.

    Lets us assert the worker's RESPONSE to a given outcome shape
    without dragging the whole executor/provider stack in.
    """

    outcome: DispatchOutcome

    async def execute_job(self, job: JobRecord) -> DispatchOutcome:
        return self.outcome


def _retryable_outcome(message: str = "boom") -> DispatchOutcome:
    return DispatchOutcome(
        status=JobStatus.ERROR,
        result_run_id=None,
        error=ErrorInfo(type="rate_limit", message=message, retryable=True).model_dump(),
    )


def _non_retryable_outcome() -> DispatchOutcome:
    return DispatchOutcome(
        status=JobStatus.ERROR,
        result_run_id=None,
        error=ErrorInfo(type="schema_error", message="bad input", retryable=False).model_dump(),
    )


def _success_outcome() -> DispatchOutcome:
    return DispatchOutcome(
        status=JobStatus.SUCCESS,
        result_run_id="r-1",
        error=None,
    )


@pytest.mark.unit
async def test_worker_requeues_transient_failure() -> None:
    """Retryable error + attempt budget remaining → requeue with
    incremented attempt_count + next_retry_at in the future."""
    storage = InMemoryStorage()
    await storage.init()
    job = _make_job_record(tenant_id="t1")
    await storage.save_job(job)

    dispatch = _FixedOutcomeDispatch(_retryable_outcome())
    worker = Worker(
        storage=storage,
        dispatch=dispatch,  # type: ignore[arg-type]
        config=WorkerConfig(tenant_id="t1"),
    )
    await worker.run_one_cycle()

    requeued = await storage.get_job(job.job_id, tenant_id="t1")
    assert requeued is not None
    assert requeued.status == JobStatus.QUEUED
    assert requeued.attempt_count == 1
    assert requeued.next_retry_at is not None
    assert requeued.next_retry_at > datetime.now(UTC)
    assert requeued.completed_at is None  # NOT a terminal transition


@pytest.mark.unit
async def test_worker_keeps_non_retryable_terminal() -> None:
    """Non-retryable error stays terminal as ERROR — no requeue."""
    storage = InMemoryStorage()
    await storage.init()
    job = _make_job_record(tenant_id="t1")
    await storage.save_job(job)

    dispatch = _FixedOutcomeDispatch(_non_retryable_outcome())
    worker = Worker(
        storage=storage,
        dispatch=dispatch,  # type: ignore[arg-type]
        config=WorkerConfig(tenant_id="t1"),
    )
    await worker.run_one_cycle()

    final = await storage.get_job(job.job_id, tenant_id="t1")
    assert final is not None
    assert final.status == JobStatus.ERROR
    assert final.attempt_count == 0  # never incremented; we didn't retry
    assert final.completed_at is not None


@pytest.mark.unit
async def test_worker_dead_letters_when_budget_exhausted() -> None:
    """A retryable error on the LAST allowed attempt → DEAD_LETTER,
    not back to QUEUED. Operator triages with the dedicated status."""
    storage = InMemoryStorage()
    await storage.init()
    # Seed a job that has already failed twice — one more failure
    # exhausts the default budget (max_attempts=3).
    job = JobRecord(
        job_id=uuid4().hex,
        tenant_id="t1",
        kind=JobKind.AGENT,
        target="alpha",
        input={"text": "hi"},
        attempt_count=2,
    )
    await storage.save_job(job)

    dispatch = _FixedOutcomeDispatch(_retryable_outcome("third strike"))
    worker = Worker(
        storage=storage,
        dispatch=dispatch,  # type: ignore[arg-type]
        config=WorkerConfig(tenant_id="t1"),
    )
    await worker.run_one_cycle()

    final = await storage.get_job(job.job_id, tenant_id="t1")
    assert final is not None
    assert final.status == JobStatus.DEAD_LETTER
    assert final.completed_at is not None
    # attempt_count remains at the value we set; the worker doesn't
    # increment on dead-letter because that bookkeeping is reserved
    # for the requeue path.
    assert final.attempt_count == 2


@pytest.mark.unit
async def test_worker_succeeds_on_eventual_consistency() -> None:
    """Three-attempt happy path: fail-fail-succeed. After two retries,
    the third dispatch succeeds and the job lands in SUCCESS with the
    attempt_count showing the history."""
    storage = InMemoryStorage()
    await storage.init()
    job = _make_job_record(tenant_id="t1")
    await storage.save_job(job)

    # Mutable counter so the dispatch returns different outcomes per call.
    class FlakyThenOk:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_job(self, job: JobRecord) -> DispatchOutcome:
            self.calls += 1
            if self.calls <= 2:
                return _retryable_outcome(f"call {self.calls} failed")
            return _success_outcome()

    dispatch = FlakyThenOk()
    # Tight retry policy so we don't actually wait. Set next_retry_at
    # back to "now" between cycles by overriding the policy to put
    # delay at 0.
    policy = JobRetryPolicy(
        max_attempts=3, base_seconds=0.0, factor=1.0, cap_seconds=0.0, jitter=0.0
    )
    worker = Worker(
        storage=storage,
        dispatch=dispatch,  # type: ignore[arg-type]
        config=WorkerConfig(tenant_id="t1", retry_policy=policy),
    )

    # Cycle 1: dispatch fails (retryable) → requeue with delay=0.
    handled = await worker.run_one_cycle()
    assert handled is not None
    state = await storage.get_job(job.job_id, tenant_id="t1")
    assert state is not None and state.status == JobStatus.QUEUED
    assert state.attempt_count == 1

    # Cycle 2: dispatch fails again → requeue.
    handled = await worker.run_one_cycle()
    assert handled is not None
    state = await storage.get_job(job.job_id, tenant_id="t1")
    assert state is not None and state.status == JobStatus.QUEUED
    assert state.attempt_count == 2

    # Cycle 3: dispatch succeeds → SUCCESS, no more retries.
    handled = await worker.run_one_cycle()
    assert handled is not None
    state = await storage.get_job(job.job_id, tenant_id="t1")
    assert state is not None
    assert state.status == JobStatus.SUCCESS
    assert state.result_run_id == "r-1"
    assert dispatch.calls == 3


@pytest.mark.unit
async def test_worker_doesnt_retry_when_policy_max_attempts_is_one() -> None:
    """``max_attempts=1`` disables retries — every retryable error
    goes straight to DEAD_LETTER. Useful for tests + the strict
    "fail fast" production mode."""
    storage = InMemoryStorage()
    await storage.init()
    job = _make_job_record(tenant_id="t1")
    await storage.save_job(job)

    dispatch = _FixedOutcomeDispatch(_retryable_outcome())
    worker = Worker(
        storage=storage,
        dispatch=dispatch,  # type: ignore[arg-type]
        config=WorkerConfig(
            tenant_id="t1",
            retry_policy=JobRetryPolicy(max_attempts=1),
        ),
    )
    await worker.run_one_cycle()

    final = await storage.get_job(job.job_id, tenant_id="t1")
    assert final is not None
    assert final.status == JobStatus.DEAD_LETTER


@pytest.mark.unit
async def test_worker_notifier_skipped_during_retry() -> None:
    """Re-queued jobs are NOT terminal — no email goes out until the
    job actually lands in a terminal status. Otherwise a long-running
    flaky job would spam the operator's inbox on every retry."""
    storage = InMemoryStorage()
    await storage.init()
    job = JobRecord(
        job_id=uuid4().hex,
        tenant_id="t1",
        kind=JobKind.AGENT,
        target="alpha",
        input={"text": "hi"},
        notify_email="ops@example.com",
    )
    await storage.save_job(job)

    notifications: list[JobRecord] = []

    class RecordingNotifier:
        async def notify_terminal(self, j: JobRecord) -> None:
            notifications.append(j)

    dispatch = _FixedOutcomeDispatch(_retryable_outcome())
    worker = Worker(
        storage=storage,
        dispatch=dispatch,  # type: ignore[arg-type]
        config=WorkerConfig(tenant_id="t1"),
        notifier=RecordingNotifier(),  # type: ignore[arg-type]
    )
    await worker.run_one_cycle()

    assert notifications == []  # retry → no notify

    # Now seed an exhausted job and verify DEAD_LETTER DOES notify.
    exhausted = JobRecord(
        job_id=uuid4().hex,
        tenant_id="t1",
        kind=JobKind.AGENT,
        target="alpha",
        input={"text": "bye"},
        notify_email="ops@example.com",
        attempt_count=2,
    )
    await storage.save_job(exhausted)
    await worker.run_one_cycle()
    assert len(notifications) == 1
    assert notifications[0].status == JobStatus.DEAD_LETTER
