"""StorageProvider Protocol — every implementation passes the same conformance suite.

v0.1 surface is intentionally narrow: runs + failures, plus list_runs for
``movate logs``. Jobs / API keys / evals join in v0.2 and v0.5 as their
phases ship.
"""

from __future__ import annotations

from typing import Protocol

from movate.core.models import (
    EvalRecord,
    FailureRecord,
    RunRecord,
    WorkflowRunRecord,
)


class StorageProvider(Protocol):
    async def init(self) -> None:
        """Idempotent setup (schema migration, etc.)."""

    async def save_run(self, run: RunRecord) -> None: ...

    async def save_failure(self, f: FailureRecord) -> None: ...

    async def save_eval(self, e: EvalRecord) -> None: ...

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None: ...

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]: ...

    async def list_evals(
        self,
        *,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]: ...

    async def list_workflow_runs(
        self,
        *,
        workflow: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]: ...

    async def close(self) -> None: ...
