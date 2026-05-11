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
