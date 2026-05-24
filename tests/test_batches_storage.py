"""Batch storage — BatchRecord CRUD + JobRecord.batch_id round-trip + filter.

Item 17 (batch inference). Parametrized over the shared ``storage`` fixture
(InMemory + sqlite + postgres-behind-skip-guard) from conftest.py.

Covers:

* ``batches`` CRUD round-trip + tenant scoping + newest-first listing.
* ``JobRecord.batch_id`` round-trips through ``save_job`` / ``get_job`` /
  ``claim_next_job``; a job saved WITHOUT a batch_id reads back ``None`` (the
  additive-nullable contract — old rows are unaffected).
* ``list_jobs(batch_id=...)`` filters to one batch's children and stays
  tenant-scoped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from movate.core.models import BatchRecord, JobKind, JobRecord, JobStatus


def _make_batch(
    *,
    batch_id: str | None = None,
    tenant_id: str = "tenant-a",
    agent: str = "demo-agent",
    total: int = 3,
    created_at: datetime | None = None,
) -> BatchRecord:
    return BatchRecord(
        batch_id=batch_id or str(uuid4()),
        tenant_id=tenant_id,
        agent=agent,
        total=total,
        created_by="key-123",
        created_at=created_at or datetime.now(UTC),
    )


def _make_job(
    *,
    job_id: str | None = None,
    tenant_id: str = "tenant-a",
    target: str = "demo-agent",
    batch_id: str | None = None,
    status: JobStatus = JobStatus.QUEUED,
    created_at: datetime | None = None,
) -> JobRecord:
    return JobRecord(
        job_id=job_id or str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target=target,
        status=status,
        input={"text": "hi"},
        batch_id=batch_id,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# BatchRecord CRUD
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_batch(storage) -> None:
    b = _make_batch(total=5)
    await storage.save_batch(b)
    got = await storage.get_batch(b.batch_id, tenant_id="tenant-a")
    assert got is not None
    assert got.batch_id == b.batch_id
    assert got.agent == "demo-agent"
    assert got.total == 5
    assert got.created_by == "key-123"


@pytest.mark.unit
async def test_get_batch_missing_returns_none(storage) -> None:
    assert await storage.get_batch("ghost", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_get_batch_is_tenant_scoped(storage) -> None:
    """A cross-tenant lookup returns None — indistinguishable from missing,
    so a caller can't probe for another tenant's batch ids."""
    b = _make_batch(tenant_id="tenant-a")
    await storage.save_batch(b)
    assert await storage.get_batch(b.batch_id, tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_list_batches_tenant_scoped_newest_first(storage) -> None:
    older = _make_batch(tenant_id="tenant-a", created_at=datetime.now(UTC) - timedelta(seconds=10))
    newer = _make_batch(tenant_id="tenant-a", created_at=datetime.now(UTC))
    other = _make_batch(tenant_id="tenant-b")
    for b in (older, newer, other):
        await storage.save_batch(b)

    rows = await storage.list_batches(tenant_id="tenant-a")
    assert [b.batch_id for b in rows] == [newer.batch_id, older.batch_id]
    # tenant-b's batch is invisible to tenant-a.
    assert other.batch_id not in {b.batch_id for b in rows}


# ---------------------------------------------------------------------------
# JobRecord.batch_id round-trip + additive-nullable contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_job_batch_id_round_trips(storage) -> None:
    j = _make_job(batch_id="batch-xyz")
    await storage.save_job(j)
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.batch_id == "batch-xyz"


@pytest.mark.unit
async def test_job_without_batch_id_reads_none(storage) -> None:
    """A non-batch job (the overwhelming common case) reads batch_id=None —
    proves the additive column doesn't perturb ordinary jobs."""
    j = _make_job(batch_id=None)
    await storage.save_job(j)
    got = await storage.get_job(j.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.batch_id is None


@pytest.mark.unit
async def test_job_batch_id_survives_claim(storage) -> None:
    """batch_id round-trips through claim_next_job too — the worker sees the
    linkage on the claimed record."""
    j = _make_job(batch_id="batch-claim")
    await storage.save_job(j)
    claimed = await storage.claim_next_job(tenant_id="tenant-a")
    assert claimed is not None
    assert claimed.batch_id == "batch-claim"


# ---------------------------------------------------------------------------
# list_jobs(batch_id=...) filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_jobs_filters_by_batch_id(storage) -> None:
    in_batch = [_make_job(batch_id="batch-1") for _ in range(3)]
    other_batch = _make_job(batch_id="batch-2")
    standalone = _make_job(batch_id=None)
    for j in [*in_batch, other_batch, standalone]:
        await storage.save_job(j)

    rows = await storage.list_jobs(tenant_id="tenant-a", batch_id="batch-1", limit=100)
    assert {j.job_id for j in rows} == {j.job_id for j in in_batch}


@pytest.mark.unit
async def test_list_jobs_batch_filter_is_tenant_scoped(storage) -> None:
    """Even with a matching batch_id, another tenant's children are invisible.

    (Batch ids are uuids so a real collision is impossible, but the filter
    must still AND with tenant_id at the storage layer.)"""
    mine = _make_job(tenant_id="tenant-a", batch_id="shared-id")
    theirs = _make_job(tenant_id="tenant-b", batch_id="shared-id")
    await storage.save_job(mine)
    await storage.save_job(theirs)

    rows = await storage.list_jobs(tenant_id="tenant-a", batch_id="shared-id", limit=100)
    assert {j.job_id for j in rows} == {mine.job_id}
