"""StorageProvider Protocol — every implementation passes the same conformance suite.

v0.1 surface is intentionally narrow: runs + failures, plus list_runs for
``movate logs``. Jobs / API keys / evals join in v0.2 and v0.5 as their
phases ship.
"""

from __future__ import annotations

from typing import Protocol

from movate.core.models import (
    ApiKeyRecord,
    EvalRecord,
    FailureRecord,
    JobRecord,
    JobStatus,
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

    async def get_run(self, run_id: str) -> RunRecord | None:
        """Exact lookup by run_id. Returns ``None`` if no match."""

    async def get_workflow_run(self, workflow_run_id: str) -> WorkflowRunRecord | None:
        """Exact lookup by workflow_run_id. Returns ``None`` if no match."""

    async def get_eval(self, eval_id: str) -> EvalRecord | None:
        """Exact lookup by eval_id. Returns ``None`` if no match."""

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

    # ------------------------------------------------------------------
    # Job queue (v0.5)
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        """Insert a brand-new ``QUEUED`` job. Errors on duplicate ``job_id``."""

    async def get_job(self, job_id: str) -> JobRecord | None:
        """Exact lookup by job_id. Returns ``None`` if no match."""

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        """List jobs newest-first, optionally filtered.

        Tenants must filter by ``tenant_id`` for the multi-tenant audit
        path. Listing across tenants (``tenant_id=None``) is reserved for
        operator tooling (``movate worker --all-tenants``) — never exposed
        on the HTTP API.
        """

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """Atomically claim the oldest ``QUEUED`` job and flip it to ``RUNNING``.

        Returns the now-claimed :class:`JobRecord` (with ``claimed_at`` set)
        or ``None`` if the queue is empty for this tenant.

        Implementations must guarantee no two callers ever return the same
        job — Postgres uses ``SELECT ... FOR UPDATE SKIP LOCKED``;
        sqlite uses ``BEGIN IMMEDIATE`` + atomic UPDATE.
        """

    async def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus,
        result_run_id: str | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        """Transition a claimed job to a terminal status.

        ``status`` must be one of ``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED``;
        ``QUEUED`` and ``RUNNING`` are reserved for the lifecycle helpers
        (``save_job``, ``claim_next_job``). Sets ``completed_at = now()``
        as a side effect.
        """

    # ------------------------------------------------------------------
    # API keys (v0.5 stage 2)
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        """Persist a freshly-minted ApiKeyRecord (no plaintext secret)."""

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        """Exact lookup by key_id. Returns ``None`` if no match.

        The HTTP middleware uses this to resolve the presented key into
        a record for verification.
        """

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        """List keys for the management UI. Defaults to active keys only.

        ``tenant_id=None`` returns keys across all tenants — operator-only,
        never exposed on the HTTP API.
        """

    async def revoke_api_key(self, key_id: str) -> None:
        """Set ``revoked_at`` to now. Idempotent — re-revoking is a no-op."""

    async def touch_api_key(self, key_id: str) -> None:
        """Bump ``last_used_at``.

        Called fire-and-forget after a successful verify; failure to
        touch must not fail the request.
        """

    async def close(self) -> None: ...
