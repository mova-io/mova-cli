"""ADR 065 — the DurableExecution seam: native floor + config selection.

Pins the contract callers depend on: submit persists a real job, status maps
the native JobStatus to the agnostic state (tenant-scoped, no leak), and the
selector returns native by default — and STILL native (the floor) when Temporal
is configured but not yet wired.
"""

from __future__ import annotations

from movate.core.durable import (
    _JOB_STATE,
    NativeDurableExecution,
    OperationState,
    select_durable_execution,
)
from movate.core.models import ErrorInfo, JobKind, JobStatus
from movate.testing import InMemoryStorage

TENANT = "tenant-a"


async def _storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


async def test_submit_persists_a_queued_job() -> None:
    storage = await _storage()
    de = NativeDurableExecution(storage)

    op_id = await de.submit(
        kind=JobKind.EVAL, target="demo", tenant_id=TENANT, input={"base_model": "x"}
    )

    job = await storage.get_job(op_id, tenant_id=TENANT)
    assert job is not None
    assert job.kind is JobKind.EVAL
    assert job.target == "demo"
    assert job.status is JobStatus.QUEUED
    assert job.input == {"base_model": "x"}


async def test_status_queued_is_pending() -> None:
    storage = await _storage()
    de = NativeDurableExecution(storage)
    op_id = await de.submit(kind=JobKind.EVAL, target="demo", tenant_id=TENANT, input={})
    st = await de.status(op_id, tenant_id=TENANT)
    assert st is not None and st.state is OperationState.PENDING and not st.terminal


async def test_status_success_surfaces_result_id() -> None:
    storage = await _storage()
    de = NativeDurableExecution(storage)
    op_id = await de.submit(kind=JobKind.EVAL, target="demo", tenant_id=TENANT, input={})
    await storage.update_job(
        op_id, tenant_id=TENANT, status=JobStatus.SUCCESS, result_run_id="eval-123"
    )
    st = await de.status(op_id, tenant_id=TENANT)
    assert st.state is OperationState.SUCCEEDED and st.terminal
    assert st.result_id == "eval-123"


async def test_status_failure_carries_the_error() -> None:
    storage = await _storage()
    de = NativeDurableExecution(storage)
    op_id = await de.submit(kind=JobKind.EVAL, target="demo", tenant_id=TENANT, input={})
    await storage.update_job(
        op_id,
        tenant_id=TENANT,
        status=JobStatus.ERROR,
        error=ErrorInfo(type="internal", message="boom", retryable=False).model_dump(),
    )
    st = await de.status(op_id, tenant_id=TENANT)
    assert st.state is OperationState.FAILED and st.error == "boom"


async def test_status_is_tenant_scoped() -> None:
    """A cross-tenant operation id returns None — no existence leak."""
    storage = await _storage()
    de = NativeDurableExecution(storage)
    op_id = await de.submit(kind=JobKind.EVAL, target="demo", tenant_id=TENANT, input={})
    assert await de.status(op_id, tenant_id="other-tenant") is None


async def test_unknown_op_is_none() -> None:
    storage = await _storage()
    de = NativeDurableExecution(storage)
    assert await de.status("does-not-exist", tenant_id=TENANT) is None


async def test_selector_defaults_to_native() -> None:
    storage = await _storage()
    de = select_durable_execution(storage)
    assert de.name == "native"
    assert isinstance(de, NativeDurableExecution)


async def test_selector_stays_native_when_temporal_configured_but_unwired() -> None:
    """ADR 065 D2: native is the floor. Even with MOVATE_TEMPORAL_ADDRESS set,
    the seam degrades to native until the Temporal impl is wired (D4.1)."""
    storage = await _storage()
    de = select_durable_execution(storage, temporal_address="my-temporal:7233")
    assert de.name == "native"


def test_mapping_covers_every_job_status() -> None:
    """Every JobStatus has an explicit agnostic mapping (no status falls through
    to a silent default), and terminals map to terminal states."""
    for status in JobStatus:
        assert status in _JOB_STATE, f"JobStatus.{status.name} unmapped"
    assert _JOB_STATE[JobStatus.QUEUED] is OperationState.PENDING
    assert _JOB_STATE[JobStatus.RUNNING] is OperationState.PENDING
    assert _JOB_STATE[JobStatus.SUCCESS] is OperationState.SUCCEEDED
    for failed in (JobStatus.ERROR, JobStatus.SAFETY_BLOCKED, JobStatus.DEAD_LETTER):
        assert _JOB_STATE[failed] is OperationState.FAILED
