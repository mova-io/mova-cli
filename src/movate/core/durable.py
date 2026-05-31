"""The ``DurableExecution`` seam (ADR 065 — Temporal as an optional backend).

A long-running mdk operation (an async run, an eval, a fine-tune that polls for
hours) needs *durable execution*: it must survive a process restart, retry on
transient failure, and be observable end-to-end. mdk has a hand-rolled engine
for this — the ``JobRecord`` queue + worker. ADR 065 puts that behind a Protocol
so a deployment can *optionally* upgrade to Temporal without touching callers,
exactly as the runner Protocol did for workflows (ADR 055).

Two impls:

* :class:`NativeDurableExecution` — **the floor.** Wraps the existing job queue.
  Zero infra, the default, and a complete path on its own. Local ``mdk serve``
  and every deployment without Temporal use it, unchanged.
* ``TemporalDurableExecution`` — **the opt-in upgrade** (arrives with the first
  per-operation adoption, ADR 065 D4.1). Selected by ``MOVATE_TEMPORAL_*`` config.

:func:`select_durable_execution` is the selection seam (mirrors how storage
selects on ``MOVATE_DB_URL``): Temporal when configured *and* wired, native
otherwise. Native is never removed (ADR 065 D2) — this module imports nothing
from ``temporalio`` and is safe without the ``[temporal]`` extra.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from movate.core.models import JobKind, JobRecord, JobStatus

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


class OperationState(StrEnum):
    """Backend-agnostic status of a durable operation.

    Collapses the native ``JobStatus`` (and a Temporal workflow's execution
    status) into the three states a caller actually branches on.
    """

    PENDING = "pending"
    """Submitted; queued or running — not yet terminal."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# Native ``JobStatus`` → the backend-agnostic state.
_JOB_STATE: dict[JobStatus, OperationState] = {
    JobStatus.QUEUED: OperationState.PENDING,
    JobStatus.RUNNING: OperationState.PENDING,
    JobStatus.SUCCESS: OperationState.SUCCEEDED,
    JobStatus.ERROR: OperationState.FAILED,
    JobStatus.SAFETY_BLOCKED: OperationState.FAILED,
    JobStatus.DEAD_LETTER: OperationState.FAILED,
    JobStatus.CANCELLED: OperationState.FAILED,
}


@dataclass(frozen=True)
class OperationStatus:
    """The observable status of a durable operation, backend-agnostic."""

    operation_id: str
    state: OperationState
    result_id: str | None = None
    """The produced record id (e.g. an ``eval_id`` / ``model_id``), when the
    operation surfaces one. ``None`` otherwise."""
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in (OperationState.SUCCEEDED, OperationState.FAILED)


class DurableExecution(Protocol):
    """Submit + observe a durable, long-running operation.

    Implementations guarantee at-least-once execution and survival across a
    process restart. The execution *body* (what the operation does) is the
    worker's / workflow's concern; this seam is the submit + status contract
    callers share across the native and Temporal backends.
    """

    name: str
    """``"native"`` | ``"temporal"`` — for logging + the capabilities matrix."""

    async def submit(
        self,
        *,
        kind: JobKind,
        target: str,
        tenant_id: str,
        input: dict[str, Any],
        api_key_id: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> str:
        """Enqueue a durable operation; return its id (poll :meth:`status`)."""
        ...

    async def status(self, operation_id: str, *, tenant_id: str) -> OperationStatus | None:
        """Current status, or ``None`` if no such op for this tenant (no leak)."""
        ...


class NativeDurableExecution:
    """The floor: durable execution via the existing ``JobRecord`` queue.

    ``submit`` persists a ``QUEUED`` job (the worker claims + runs it exactly as
    today); ``status`` reads it back and maps ``JobStatus`` → the agnostic
    state. Tenant-scoped reads (a cross-tenant id returns ``None``). This is a
    thin adapter over storage — it changes *nothing* about how jobs run.
    """

    name = "native"

    def __init__(self, storage: StorageProvider) -> None:
        self._storage = storage

    async def submit(
        self,
        *,
        kind: JobKind,
        target: str,
        tenant_id: str,
        input: dict[str, Any],
        api_key_id: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> str:
        job = JobRecord(
            job_id=uuid4().hex,
            tenant_id=tenant_id,
            kind=kind,
            target=target,
            input=input,
            api_key_id=api_key_id,
            trace_context=trace_context or {},
        )
        await self._storage.save_job(job)
        return job.job_id

    async def status(self, operation_id: str, *, tenant_id: str) -> OperationStatus | None:
        job = await self._storage.get_job(operation_id, tenant_id=tenant_id)
        if job is None:
            return None
        return OperationStatus(
            operation_id=job.job_id,
            state=_JOB_STATE.get(job.status, OperationState.PENDING),
            result_id=job.result_run_id,
            error=(job.error.message if job.error is not None else None),
        )


def select_durable_execution(
    storage: StorageProvider,
    *,
    temporal_address: str | None = None,
) -> DurableExecution:
    """Pick the durable-execution backend (ADR 065 D3 — config-selected).

    Temporal when ``MOVATE_TEMPORAL_ADDRESS`` (or ``temporal_address``) is set
    AND the impl is wired AND the ``[temporal]`` extra is importable; native
    otherwise. Native is the floor — any failure to reach Temporal degrades to
    native rather than erroring (ADR 065 D2).

    Today the Temporal impl is not yet wired (it lands with the first
    per-operation adoption, ADR 065 D4.1): when Temporal is *configured* we log
    that the hook is present and fall back to native, so an operator who sets
    the env var sees a clear "ready, not yet active" signal rather than silence.
    """
    address = temporal_address or os.environ.get("MOVATE_TEMPORAL_ADDRESS", "").strip()
    if address:
        logger.info(
            "durable-execution: MOVATE_TEMPORAL_ADDRESS=%s is set, but the Temporal "
            "backend is not yet wired (ADR 065 D4.1 — arrives with the first "
            "per-operation adoption); using the native job queue.",
            address,
        )
    return NativeDurableExecution(storage)


__all__ = [
    "DurableExecution",
    "NativeDurableExecution",
    "OperationState",
    "OperationStatus",
    "select_durable_execution",
]
