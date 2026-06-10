"""Job queue storage — CRUD round-trip + claim semantics + tenant isolation.

Three storage backends in scope (parametrized via the shared ``storage``
fixture in conftest.py): ``InMemoryStorage``, ``SqliteProvider``, and
``PostgresProvider`` (skipped when ``MOVATE_PG_TEST_URL`` is unset).

Claim-under-contention semantics are exercised explicitly against the
real backends — the in-memory version's "atomic by single event loop"
story is correct but doesn't tell us anything about the actual lock
behavior we ship.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from movate.core.models import (
    ErrorInfo,
    JobKind,
    JobRecord,
    JobStatus,
)
from movate.storage.sqlite import SqliteProvider

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: str | None = None,
    tenant_id: str = "tenant-a",
    kind: JobKind = JobKind.AGENT,
    target: str = "demo-agent",
    status: JobStatus = JobStatus.QUEUED,
    created_at: datetime | None = None,
) -> JobRecord:
    return JobRecord(
        job_id=job_id or str(uuid4()),
        tenant_id=tenant_id,
        kind=kind,
        target=target,
        status=status,
        input={"text": "hi"},
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# CRUD round-trip — uses the shared ``storage`` fixture from conftest.py
# (parametrized over memory + sqlite + postgres)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_job(storage) -> None:
    j = _make_job()
    await storage.save_job(j)
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.job_id == j.job_id
    assert got.kind == JobKind.AGENT
    assert got.status == JobStatus.QUEUED
    assert got.input == {"text": "hi"}


@pytest.mark.unit
async def test_get_job_returns_none_for_missing(storage) -> None:
    assert await storage.get_job("ghost", tenant_id="tenant-a") is None


# ---------------------------------------------------------------------------
# trace_context round-trip (ADR 019, item 32) — additive JSONB/TEXT column.
# Parametrized over memory + sqlite + postgres via the shared ``storage``
# fixture (postgres skips when MOVATE_PG_TEST_URL is unset).
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_trace_context_roundtrip_preserves_carrier(storage) -> None:
    """A non-empty W3C carrier saved on a job survives a save/get round-trip
    on every backend, so the worker can continue the originating trace."""
    carrier = {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "movate=1",
    }
    j = JobRecord(
        job_id=str(uuid4()),
        tenant_id="tenant-a",
        kind=JobKind.AGENT,
        target="demo-agent",
        status=JobStatus.QUEUED,
        input={"text": "hi"},
        created_at=datetime.now(UTC),
        trace_context=carrier,
    )
    await storage.save_job(j)
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.trace_context == carrier


@pytest.mark.unit
async def test_trace_context_defaults_empty_and_roundtrips(storage) -> None:
    """Back-compat: a job with no carrier (the default — pre-R2 rows, or OTel
    off at enqueue) round-trips as ``{}`` on every backend → the worker starts
    a fresh root span, byte-for-byte the pre-R2 behaviour."""
    j = _make_job()
    assert j.trace_context == {}  # default_factory=dict, no ambient magic
    await storage.save_job(j)
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.trace_context == {}


@pytest.mark.unit
async def test_list_jobs_filters_by_tenant_and_status(storage) -> None:
    a1 = _make_job(tenant_id="tenant-a", status=JobStatus.QUEUED)
    a2 = _make_job(tenant_id="tenant-a", status=JobStatus.SUCCESS)
    b1 = _make_job(tenant_id="tenant-b", status=JobStatus.QUEUED)
    for j in (a1, a2, b1):
        await storage.save_job(j)

    only_a = await storage.list_jobs(tenant_id="tenant-a")
    assert {j.job_id for j in only_a} == {a1.job_id, a2.job_id}

    a_queued = await storage.list_jobs(tenant_id="tenant-a", status=JobStatus.QUEUED)
    assert {j.job_id for j in a_queued} == {a1.job_id}


@pytest.mark.unit
async def test_list_jobs_orders_newest_first(storage) -> None:
    older = _make_job(created_at=datetime.now(UTC) - timedelta(seconds=10))
    newer = _make_job(created_at=datetime.now(UTC))
    await storage.save_job(older)
    await storage.save_job(newer)
    rows = await storage.list_jobs()
    assert [j.job_id for j in rows] == [newer.job_id, older.job_id]


# ---------------------------------------------------------------------------
# Claim semantics — FIFO + status guard + tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_claim_returns_none_on_empty_queue(storage) -> None:
    assert await storage.claim_next_job() is None


@pytest.mark.unit
async def test_claim_returns_oldest_queued_first(storage) -> None:
    older = _make_job(created_at=datetime.now(UTC) - timedelta(seconds=10))
    newer = _make_job(created_at=datetime.now(UTC))
    await storage.save_job(newer)  # save in reverse order on purpose
    await storage.save_job(older)

    claimed = await storage.claim_next_job()
    assert claimed is not None
    assert claimed.job_id == older.job_id  # FIFO
    assert claimed.status == JobStatus.RUNNING
    assert claimed.claimed_at is not None


@pytest.mark.unit
async def test_claim_skips_running_and_terminal_rows(storage) -> None:
    """Status guard: only QUEUED rows ever get claimed."""
    running = _make_job(status=JobStatus.RUNNING)
    done = _make_job(status=JobStatus.SUCCESS)
    queued = _make_job(status=JobStatus.QUEUED)
    for j in (running, done, queued):
        await storage.save_job(j)

    claimed = await storage.claim_next_job()
    assert claimed is not None
    assert claimed.job_id == queued.job_id


@pytest.mark.unit
async def test_claim_respects_tenant_scoping(storage) -> None:
    """A worker bound to tenant-a never sees tenant-b's jobs."""
    a = _make_job(tenant_id="tenant-a")
    b = _make_job(tenant_id="tenant-b")
    await storage.save_job(a)
    await storage.save_job(b)

    claimed_a = await storage.claim_next_job(tenant_id="tenant-a")
    assert claimed_a is not None
    assert claimed_a.tenant_id == "tenant-a"

    # tenant-b's job is still queued.
    rows = await storage.list_jobs(tenant_id="tenant-b", status=JobStatus.QUEUED)
    assert len(rows) == 1
    assert rows[0].job_id == b.job_id


@pytest.mark.unit
async def test_claim_persists_running_state(storage) -> None:
    """After claim, a fresh get_job sees RUNNING + claimed_at — proves the
    UPDATE actually committed (not just returned a copy)."""
    j = _make_job()
    await storage.save_job(j)
    await storage.claim_next_job()
    refetched = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert refetched is not None
    assert refetched.status == JobStatus.RUNNING
    assert refetched.claimed_at is not None


# ---------------------------------------------------------------------------
# update_job — terminal transitions only
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_update_job_to_success(storage) -> None:
    j = _make_job()
    await storage.save_job(j)
    await storage.claim_next_job()

    await storage.update_job(
        j.job_id, tenant_id="tenant-a", status=JobStatus.SUCCESS, result_run_id="run-xyz"
    )
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.SUCCESS
    assert got.result_run_id == "run-xyz"
    assert got.completed_at is not None


@pytest.mark.unit
async def test_update_job_to_error_persists_error_info(storage) -> None:
    j = _make_job()
    await storage.save_job(j)
    await storage.claim_next_job()

    err = ErrorInfo(type="provider_error", message="boom", retryable=False)
    await storage.update_job(
        j.job_id, tenant_id="tenant-a", status=JobStatus.ERROR, error=err.model_dump()
    )
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.ERROR
    assert got.error is not None
    assert got.error.message == "boom"


@pytest.mark.unit
async def test_update_job_rejects_non_terminal_status(storage) -> None:
    """Calling update_job with QUEUED/RUNNING is a programming error.

    Lifecycle helpers (save_job, claim_next_job) own those transitions.
    """
    j = _make_job()
    await storage.save_job(j)
    with pytest.raises(ValueError, match="terminal"):
        await storage.update_job(j.job_id, tenant_id="tenant-a", status=JobStatus.QUEUED)
    with pytest.raises(ValueError, match="terminal"):
        await storage.update_job(j.job_id, tenant_id="tenant-a", status=JobStatus.RUNNING)


# ---------------------------------------------------------------------------
# Cooperative run cancellation (item 36, R4b) — request_job_cancel +
# cancel_requested additive column. Parametrized over memory + sqlite +
# postgres via the shared ``storage`` fixture.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cancel_requested_defaults_false_and_roundtrips(storage) -> None:
    """The additive column defaults False on a fresh row and round-trips."""
    j = _make_job()
    await storage.save_job(j)
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.cancel_requested is False


@pytest.mark.unit
async def test_request_cancel_queued_becomes_cancelled_terminal(storage) -> None:
    """A QUEUED job → CANCELLED immediately; never claimable afterwards."""
    j = _make_job()
    await storage.save_job(j)

    result = await storage.request_job_cancel(j.job_id, tenant_id="tenant-a")
    assert result == JobStatus.CANCELLED

    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.CANCELLED
    assert got.completed_at is not None

    # The claim path only takes 'queued' rows, so a cancelled job is skipped.
    assert await storage.claim_next_job() is None


@pytest.mark.unit
async def test_request_cancel_running_sets_flag_keeps_running(storage) -> None:
    """A RUNNING job → cancel_requested flag set; status stays RUNNING.

    The worker (not the storage call) finalizes it; request_job_cancel
    returns RUNNING so the caller knows the cancel is pending."""
    j = _make_job()
    await storage.save_job(j)
    await storage.claim_next_job()  # → RUNNING

    result = await storage.request_job_cancel(j.job_id, tenant_id="tenant-a")
    assert result == JobStatus.RUNNING

    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.RUNNING
    assert got.cancel_requested is True


@pytest.mark.unit
async def test_request_cancel_terminal_is_noop(storage) -> None:
    """Cancelling an already-terminal job is a no-op; returns its status."""
    j = _make_job()
    await storage.save_job(j)
    await storage.claim_next_job()
    await storage.update_job(
        j.job_id, tenant_id="tenant-a", status=JobStatus.SUCCESS, result_run_id="run-1"
    )

    result = await storage.request_job_cancel(j.job_id, tenant_id="tenant-a")
    assert result == JobStatus.SUCCESS

    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.SUCCESS  # unchanged
    assert got.cancel_requested is False


@pytest.mark.unit
async def test_request_cancel_cross_tenant_returns_none_no_effect(storage) -> None:
    """A cross-tenant cancel returns None (→ 404) and never mutates the row."""
    j = _make_job(tenant_id="tenant-a")
    await storage.save_job(j)

    result = await storage.request_job_cancel(j.job_id, tenant_id="tenant-b")
    assert result is None

    # The job is untouched for its real tenant — still queued.
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.QUEUED
    assert got.cancel_requested is False


@pytest.mark.unit
async def test_request_cancel_missing_returns_none(storage) -> None:
    assert await storage.request_job_cancel("ghost", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_update_job_accepts_cancelled_terminal(storage) -> None:
    """The worker writes CANCELLED via update_job — the terminal-status
    allow-list must accept it (item 36)."""
    j = _make_job()
    await storage.save_job(j)
    await storage.claim_next_job()
    await storage.update_job(j.job_id, tenant_id="tenant-a", status=JobStatus.CANCELLED)
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.CANCELLED
    assert got.completed_at is not None


# ---------------------------------------------------------------------------
# Dead-letter operations — list / requeue / purge. Parametrized over
# memory + sqlite + postgres via the shared ``storage`` fixture (postgres
# skips when MOVATE_PG_TEST_URL is unset). These operate the EXISTING
# DEAD_LETTER status the retry policy produces; additive, no schema change.
# ---------------------------------------------------------------------------


async def _dead_letter(storage, *, tenant_id: str = "tenant-a", target: str = "demo-agent"):
    """Insert a job and drive it to DEAD_LETTER via the worker's own path
    (claim → update_job), so the test exercises the real terminal write."""
    j = _make_job(tenant_id=tenant_id, target=target)
    await storage.save_job(j)
    await storage.claim_next_job(tenant_id=tenant_id)
    err = ErrorInfo(type="provider_error", message="exhausted", retryable=True)
    await storage.update_job(
        j.job_id, tenant_id=tenant_id, status=JobStatus.DEAD_LETTER, error=err.model_dump()
    )
    return j


@pytest.mark.unit
async def test_list_dead_letter_jobs_tenant_scoped(storage) -> None:
    dl_a = await _dead_letter(storage, tenant_id="tenant-a")
    await _dead_letter(storage, tenant_id="tenant-b")
    # A non-dead-letter job for tenant-a must NOT appear.
    live = _make_job(tenant_id="tenant-a", status=JobStatus.QUEUED)
    await storage.save_job(live)

    rows = await storage.list_dead_letter_jobs("tenant-a")
    assert {j.job_id for j in rows} == {dl_a.job_id}
    assert all(j.status == JobStatus.DEAD_LETTER for j in rows)


@pytest.mark.unit
async def test_list_dead_letter_jobs_agent_filter(storage) -> None:
    a = await _dead_letter(storage, target="alpha")
    await _dead_letter(storage, target="beta")
    rows = await storage.list_dead_letter_jobs("tenant-a", agent="alpha")
    assert {j.job_id for j in rows} == {a.job_id}


@pytest.mark.unit
async def test_list_dead_letter_jobs_newest_first_and_limit(storage) -> None:
    older = _make_job(status=JobStatus.DEAD_LETTER, created_at=datetime.now(UTC) - timedelta(60))
    newer = _make_job(status=JobStatus.DEAD_LETTER, created_at=datetime.now(UTC))
    await storage.save_job(older)
    await storage.save_job(newer)
    rows = await storage.list_dead_letter_jobs("tenant-a", limit=1)
    assert [j.job_id for j in rows] == [newer.job_id]


@pytest.mark.unit
async def test_requeue_dead_letter_resets_to_fresh_queued(storage) -> None:
    """THE state transition: DEAD_LETTER → QUEUED with a fresh budget
    (attempt_count=0, next_retry_at cleared) so the worker reclaims it."""
    dl = await _dead_letter(storage)
    # Sanity: it really is dead-lettered with a recorded error + completed_at.
    pre = await storage.get_job(dl.job_id, tenant_id="tenant-a")
    assert pre is not None and pre.status == JobStatus.DEAD_LETTER
    assert pre.error is not None and pre.completed_at is not None

    ok = await storage.requeue_dead_letter_job(dl.job_id, tenant_id="tenant-a")
    assert ok is True

    got = await storage.get_job(dl.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.QUEUED
    assert got.attempt_count == 0
    assert got.next_retry_at is None
    assert got.claimed_at is None
    assert got.completed_at is None
    assert got.error is None

    # And it is claimable again — the whole point of a requeue.
    claimed = await storage.claim_next_job(tenant_id="tenant-a")
    assert claimed is not None
    assert claimed.job_id == dl.job_id


@pytest.mark.unit
async def test_requeue_non_dead_letter_is_rejected(storage) -> None:
    """Requeuing a job that is NOT dead-lettered (here: queued) must be a
    no-op returning False — never silently corrupt a live job."""
    live = _make_job(status=JobStatus.QUEUED)
    await storage.save_job(live)
    ok = await storage.requeue_dead_letter_job(live.job_id, tenant_id="tenant-a")
    assert ok is False
    got = await storage.get_job(live.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.QUEUED  # untouched


@pytest.mark.unit
async def test_requeue_missing_returns_false(storage) -> None:
    assert await storage.requeue_dead_letter_job("ghost", tenant_id="tenant-a") is False


@pytest.mark.unit
async def test_requeue_cross_tenant_returns_false_no_effect(storage) -> None:
    dl = await _dead_letter(storage, tenant_id="tenant-a")
    ok = await storage.requeue_dead_letter_job(dl.job_id, tenant_id="tenant-b")
    assert ok is False
    got = await storage.get_job(dl.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.status == JobStatus.DEAD_LETTER  # untouched for its real tenant


@pytest.mark.unit
async def test_purge_dead_letter_jobs_tenant_scoped(storage) -> None:
    dl_a = await _dead_letter(storage, tenant_id="tenant-a")
    dl_b = await _dead_letter(storage, tenant_id="tenant-b")
    live = _make_job(tenant_id="tenant-a", status=JobStatus.QUEUED)
    await storage.save_job(live)

    deleted = await storage.purge_dead_letter_jobs("tenant-a")
    assert deleted == 1
    # tenant-a's dead-letter is gone; its live job + tenant-b's are intact.
    assert await storage.get_job(dl_a.job_id, tenant_id="tenant-a") is None
    assert await storage.get_job(live.job_id, tenant_id="tenant-a") is not None
    assert await storage.get_job(dl_b.job_id, tenant_id="tenant-b") is not None


@pytest.mark.unit
async def test_purge_dead_letter_jobs_before_cutoff(storage) -> None:
    """``before`` keeps recent dead-letters, prunes stale ones (by
    completed_at). We set completed_at via the real update path, then
    purge with a cutoff between the two."""
    old = _make_job(status=JobStatus.DEAD_LETTER, created_at=datetime.now(UTC) - timedelta(days=2))
    old = old.model_copy(update={"completed_at": datetime.now(UTC) - timedelta(days=2)})
    recent = _make_job(status=JobStatus.DEAD_LETTER)
    recent = recent.model_copy(update={"completed_at": datetime.now(UTC)})
    await storage.save_job(old)
    await storage.save_job(recent)

    cutoff = datetime.now(UTC) - timedelta(days=1)
    deleted = await storage.purge_dead_letter_jobs("tenant-a", before=cutoff)
    assert deleted == 1
    assert await storage.get_job(old.job_id, tenant_id="tenant-a") is None
    assert await storage.get_job(recent.job_id, tenant_id="tenant-a") is not None


@pytest.mark.unit
async def test_purge_dead_letter_jobs_empty_returns_zero(storage) -> None:
    assert await storage.purge_dead_letter_jobs("tenant-a") == 0


# ---------------------------------------------------------------------------
# Concurrent claim — sqlite-only because the in-memory double is single-loop
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_sqlite_claim_no_double_dispatch(tmp_path: Path) -> None:
    """Two concurrent workers (separate connections) must not both claim
    the same job.

    Each ``SqliteProvider`` instance owns its own ``aiosqlite.Connection``
    — that's the realistic worker-process model (one process, one DB
    handle). On the same DB file, BEGIN IMMEDIATE serializes via
    sqlite's reserved write lock: one connection wins the lock and
    runs SELECT-then-UPDATE; the other waits for the lock, then sees
    the row already RUNNING and returns ``None``.

    Same connection isn't a meaningful test — sqlite3's Python driver
    can't reentrantly BEGIN on a single connection at all, so the
    asyncio.gather there would just deadlock on transaction state.
    """
    db_path = tmp_path / "claim.db"

    # Seed the queue using a dedicated setup connection.
    setup = SqliteProvider(db_path=db_path)
    await setup.init()
    job = _make_job()
    await setup.save_job(job)
    await setup.close()

    # Two independent worker connections, each with its own DB handle.
    worker_a = SqliteProvider(db_path=db_path)
    worker_b = SqliteProvider(db_path=db_path)
    await worker_a.init()
    await worker_b.init()
    try:
        results = await asyncio.gather(worker_a.claim_next_job(), worker_b.claim_next_job())
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 1, f"expected exactly one claim, got {len(claimed)}"
        assert claimed[0].job_id == job.job_id
    finally:
        await worker_a.close()
        await worker_b.close()


@pytest.mark.unit
@pytest.mark.postgres
async def test_postgres_claim_skip_locked_runs_concurrent() -> None:
    """Postgres ``FOR UPDATE SKIP LOCKED`` lets two workers grab two
    DIFFERENT queued jobs simultaneously — superior to sqlite, which
    serializes the SELECT-then-UPDATE pair across all workers.

    Setup: two queued jobs. Two workers concurrent-claim. Expected:
    each worker gets one (different) job; no contention, no double-
    dispatch.
    """
    import os  # noqa: PLC0415

    url = os.environ.get("MOVATE_PG_TEST_URL")
    if url is None:
        pytest.skip("MOVATE_PG_TEST_URL not set")

    from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

    setup = PostgresProvider(dsn=url)
    await setup.init()
    # Hermetic — wipe before this test so we don't inherit prior state.
    pool = setup._db
    await pool.execute("TRUNCATE TABLE jobs RESTART IDENTITY CASCADE")
    j1 = _make_job(created_at=datetime.now(UTC) - timedelta(seconds=1))
    j2 = _make_job(created_at=datetime.now(UTC))
    await setup.save_job(j1)
    await setup.save_job(j2)
    await setup.close()

    worker_a = PostgresProvider(dsn=url)
    worker_b = PostgresProvider(dsn=url)
    await worker_a.init()
    await worker_b.init()
    try:
        # Run two claims concurrently. SKIP LOCKED means neither
        # worker should block; each picks a different row.
        results = await asyncio.gather(
            worker_a.claim_next_job(),
            worker_b.claim_next_job(),
        )
        claimed_ids = {r.job_id for r in results if r is not None}
        # Both got something AND they're different — that's the
        # SKIP LOCKED win over sqlite.
        assert len(claimed_ids) == 2, (
            f"expected two distinct claims under SKIP LOCKED; got {len(claimed_ids)} "
            f"({claimed_ids!r})"
        )
        assert claimed_ids == {j1.job_id, j2.job_id}
    finally:
        await worker_a.close()
        await worker_b.close()


# ---------------------------------------------------------------------------
# `mdk jobs reap` — local-runtime reaper escape hatch (item 31)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_jobs_reap_cli_reclaims_orphan(tmp_path: Path, monkeypatch) -> None:
    """`mdk jobs reap --output json` runs the reaper against the local
    sqlite DB and reports counts. Seeds an orphaned RUNNING job, then
    asserts the command requeues it."""
    import json as _json  # noqa: PLC0415

    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app as cli_app  # noqa: PLC0415

    # Redirect the local DB to a tmp file so we never touch ~/.movate.
    db_path = tmp_path / "local.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.delenv("MDK_DB_URL", raising=False)

    # Seed an orphaned RUNNING job via the same sqlite provider the CLI
    # will open. local tenant matches build_local_runtime's executor.
    async def _seed() -> str:
        provider = SqliteProvider(db_path=db_path)
        await provider.init()
        stale = datetime.now(UTC) - timedelta(seconds=10_000)
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id="local",
            kind=JobKind.AGENT,
            target="alpha",
            input={"text": "hi"},
            status=JobStatus.RUNNING,
            claimed_at=stale,
            attempt_count=0,
        )
        await provider.save_job(job)
        await provider.close()
        return job.job_id

    job_id = asyncio.run(_seed())

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(cli_app, ["jobs", "reap", "--output", "json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _json.loads(result.stdout)
    assert payload == {"requeued": 1, "dead_lettered": 0}

    # Verify the row was actually requeued in the DB.
    async def _check() -> JobStatus:
        provider = SqliteProvider(db_path=db_path)
        await provider.init()
        got = await provider.get_job(job_id, tenant_id="local")
        await provider.close()
        assert got is not None
        return got.status

    assert asyncio.run(_check()) == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# Provenance (ADR 100 D4) — origin round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_origin_round_trips_and_defaults_none(storage) -> None:
    """ADR 100 D4: origin persists; a job saved without one (every manual
    submit + every pre-ADR-100 row) reads back as None."""
    stamped = _make_job(job_id="job-stamped")
    stamped = stamped.model_copy(update={"origin": "trigger:trig-abc123"})
    await storage.save_job(stamped)
    got = await storage.get_job("job-stamped", tenant_id="tenant-a")
    assert got is not None
    assert got.origin == "trigger:trig-abc123"

    await storage.save_job(_make_job(job_id="job-manual"))
    manual = await storage.get_job("job-manual", tenant_id="tenant-a")
    assert manual is not None
    assert manual.origin is None
