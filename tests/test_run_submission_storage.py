"""Run-submission dedup storage (item 37 — submission idempotency).

Mirrors the trigger-delivery dedup tests in tests/test_trigger_storage.py: the
same three backends via the shared ``storage`` fixture in conftest.py —
``InMemoryStorage``, ``SqliteProvider``, and ``PostgresProvider`` (skipped when
``MOVATE_PG_TEST_URL`` is unset).

Asserts the additive ``run_submissions`` table is default-off (no rows until
written), round-trips ``(tenant_id, idempotency_key) -> job_id``, is race-safe
(a second record for the same key returns False and keeps the first job_id),
and is PER-TENANT scoped (two tenants reusing the same key string never
collide).
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    assert await storage.get_run_submission("tenant-a", "key-1") is None


@pytest.mark.unit
async def test_record_round_trip(storage) -> None:
    assert await storage.record_run_submission("tenant-a", "key-1", "job-1") is True
    assert await storage.get_run_submission("tenant-a", "key-1") == "job-1"


@pytest.mark.unit
async def test_record_second_call_returns_false_and_keeps_first_job(storage) -> None:
    """A retry does NOT overwrite the stored job_id (atomic dedup)."""
    assert await storage.record_run_submission("tenant-a", "key-1", "job-1") is True
    # Same (tenant_id, idempotency_key) again — INSERT-OR-IGNORE: no insert.
    assert await storage.record_run_submission("tenant-a", "key-1", "job-2") is False
    # The first writer's job_id wins; the second is dropped.
    assert await storage.get_run_submission("tenant-a", "key-1") == "job-1"


@pytest.mark.unit
async def test_is_per_tenant_scoped(storage) -> None:
    """The same idempotency key under a different tenant is independent."""
    assert await storage.record_run_submission("tenant-a", "key-1", "job-a") is True
    # Different tenant, same key string → a distinct row, inserts fine.
    assert await storage.record_run_submission("tenant-b", "key-1", "job-b") is True
    assert await storage.get_run_submission("tenant-a", "key-1") == "job-a"
    assert await storage.get_run_submission("tenant-b", "key-1") == "job-b"


@pytest.mark.unit
async def test_distinct_keys_independent(storage) -> None:
    assert await storage.record_run_submission("tenant-a", "key-1", "job-1") is True
    assert await storage.record_run_submission("tenant-a", "key-2", "job-2") is True
    assert await storage.get_run_submission("tenant-a", "key-1") == "job-1"
    assert await storage.get_run_submission("tenant-a", "key-2") == "job-2"


@pytest.mark.unit
async def test_get_unknown_key_returns_none(storage) -> None:
    await storage.record_run_submission("tenant-a", "key-1", "job-1")
    assert await storage.get_run_submission("tenant-a", "missing") is None
    assert await storage.get_run_submission("tenant-other", "key-1") is None


# ---------------------------------------------------------------------------
# item 37 payload-conflict guard: request_hash fingerprint round-trip. The
# additive nullable column lets the submit endpoint 409 a key reused for a
# DIFFERENT payload. A row recorded without a hash (back-compat / legacy)
# reads back as None → "unknown" → no conflict.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_request_hash_round_trips(storage) -> None:
    """``record_run_submission(..., request_hash=...)`` persists the fingerprint."""
    assert await storage.record_run_submission("tenant-a", "key-1", "job-1", "hash-abc") is True
    rec = await storage.get_run_submission_record("tenant-a", "key-1")
    assert rec is not None
    assert rec.job_id == "job-1"
    assert rec.request_hash == "hash-abc"


@pytest.mark.unit
async def test_request_hash_defaults_to_none(storage) -> None:
    """Omitting ``request_hash`` (the pre-guard call shape) stores None."""
    assert await storage.record_run_submission("tenant-a", "key-1", "job-1") is True
    rec = await storage.get_run_submission_record("tenant-a", "key-1")
    assert rec is not None
    assert rec.job_id == "job-1"
    assert rec.request_hash is None


@pytest.mark.unit
async def test_get_run_submission_record_unknown_key_is_none(storage) -> None:
    assert await storage.get_run_submission_record("tenant-a", "missing") is None


@pytest.mark.unit
async def test_get_run_submission_record_is_per_tenant_scoped(storage) -> None:
    await storage.record_run_submission("tenant-a", "key-1", "job-a", "hash-a")
    await storage.record_run_submission("tenant-b", "key-1", "job-b", "hash-b")
    rec_a = await storage.get_run_submission_record("tenant-a", "key-1")
    rec_b = await storage.get_run_submission_record("tenant-b", "key-1")
    assert rec_a is not None and rec_a.request_hash == "hash-a"
    assert rec_b is not None and rec_b.request_hash == "hash-b"
