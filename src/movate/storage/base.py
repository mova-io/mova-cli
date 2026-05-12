"""StorageProvider Protocol — every implementation passes the same conformance suite.

v0.1 surface is intentionally narrow: runs + failures, plus list_runs for
``movate logs``. Jobs / API keys / evals join in v0.2 and v0.5 as their
phases ship.

**Tenant isolation (v1.0 stage 4).** Every read of a single record by id
takes a mandatory ``tenant_id`` kwarg and filters by it server-side, so a
caller authenticated as tenant A can never read tenant B's data even by
guessing ids. List methods that omit ``tenant_id`` reserve cross-tenant
reads for operator tooling (``movate worker --tenant-id=None`` drain
mode) — never exposed on the HTTP API. Mutating methods on per-tenant
rows (``update_job``, ``revoke_api_key``, ``touch_api_key``) likewise
require ``tenant_id`` so the WHERE clause stops cross-tenant writes at
the SQL layer, not just at the HTTP layer.

The one exception is ``get_api_key(key_id)`` — the auth middleware
parses the full ``mvt_<env>_<tenant>_<keyid>_<secret>`` key before
lookup and cross-checks the record's ``tenant_id`` against the
presented tenant prefix in ``check_record``. Tenant isolation on this
path is enforced by ``check_record``, not the storage method.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from movate.core.models import (
    ApiKeyRecord,
    BenchRecord,
    EvalRecord,
    FailureRecord,
    JobRecord,
    JobStatus,
    RunRecord,
    TenantBudget,
    WorkflowRunRecord,
)


class StorageProvider(Protocol):
    async def init(self) -> None:
        """Idempotent setup (schema migration, etc.)."""

    async def ping(self) -> None:
        """Cheap liveness check: validate the backend connection is alive.

        Used by ``GET /ready`` to gate ACA traffic — if this raises,
        the pod is reporting "not ready" and ACA stops routing to it
        without restarting it (the liveness probe on ``/healthz``
        stays green so the pod isn't killed for a transient blip).

        Implementations should make this as cheap as possible:
        sqlite does a ``SELECT 1``; postgres does a pool-acquire +
        ``SELECT 1``. Raises any backend error on failure (the
        caller catches and converts to a 503).
        """

    async def save_run(self, run: RunRecord) -> None: ...

    async def save_failure(self, f: FailureRecord) -> None: ...

    async def save_eval(self, e: EvalRecord) -> None: ...

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None: ...

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        """Exact lookup by run_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the run exists but belongs to
        a different tenant — same return shape either way so a caller
        can't probe for the existence of other tenants' runs.
        """

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        """Exact lookup by workflow_run_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the workflow run belongs to
        a different tenant.
        """

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        """Exact lookup by eval_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the eval belongs to a
        different tenant.
        """

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
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        """List evals newest-first, optionally filtered.

        ``tenant_id=None`` returns evals across all tenants — operator
        tooling only; never exposed on the HTTP API.
        """

    async def save_bench(self, b: BenchRecord) -> None:
        """Persist one bench-run summary. Sister to :meth:`save_eval`."""

    async def get_bench(self, bench_id: str, *, tenant_id: str) -> BenchRecord | None:
        """Exact lookup by ``bench_id``, scoped to ``tenant_id``.

        Returns ``None`` on missing-id OR cross-tenant — same response
        shape so callers can't distinguish "doesn't exist" from "exists
        under a different tenant." Same tenant-leak hygiene as
        :meth:`get_eval`.
        """

    async def list_benches(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[BenchRecord]:
        """List bench records newest-first, optionally filtered.

        ``tenant_id=None`` returns records across all tenants —
        operator tooling only; never exposed on the HTTP API.
        """

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        """List workflow runs newest-first, optionally filtered.

        ``tenant_id=None`` returns runs across all tenants — operator
        tooling only; never exposed on the HTTP API.
        """

    # ------------------------------------------------------------------
    # Job queue (v0.5)
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        """Insert a brand-new ``QUEUED`` job. Errors on duplicate ``job_id``."""

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        """Exact lookup by job_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the job belongs to a
        different tenant — same return shape either way so a caller
        can't probe for the existence of other tenants' jobs.
        """

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
        tenant_id: str,
        status: JobStatus,
        result_run_id: str | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        """Transition a claimed job to a terminal status, scoped to ``tenant_id``.

        ``status`` must be one of ``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED``
        / ``DEAD_LETTER``; ``QUEUED`` and ``RUNNING`` are reserved for the
        lifecycle helpers (``save_job``, ``claim_next_job``, ``requeue_job``).
        Sets ``completed_at = now()`` as a side effect.

        The ``tenant_id`` filter on WHERE is the SQL-layer enforcement
        that prevents a misconfigured worker (or a direct storage call
        from a buggy path) from mutating another tenant's job. Silently
        no-op if no row matches both id + tenant.
        """

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Re-queue a ``RUNNING`` job after a transient failure.

        Sets status back to ``QUEUED``, clears ``claimed_at``, bumps
        ``attempt_count``, and stamps ``next_retry_at`` so the claim
        path skips this row until backoff elapses.

        The worker calls this instead of ``update_job`` when the
        dispatch outcome reports a retryable error AND the retry
        budget isn't exhausted (see :mod:`movate.core.job_retry`).
        Tenant-scoped in WHERE; silently no-ops on mismatch.
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

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Set ``revoked_at`` to now, scoped to ``tenant_id``.

        Idempotent — re-revoking is a no-op. The ``tenant_id`` filter
        on WHERE prevents a tenant from revoking another tenant's keys
        by guessing key ids.
        """

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Bump ``last_used_at``, scoped to ``tenant_id``.

        Called fire-and-forget after a successful verify; failure to
        touch must not fail the request. The tenant filter is defense
        in depth — the auth middleware has already cross-checked the
        record's tenant matches the presented key, but the storage
        layer enforces it independently.
        """

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        """Return the budget row for ``tenant_id``, or ``None`` if no
        budget is set (= unlimited).

        Read on every ``Executor.execute`` entry, so implementations
        should be cheap (PK lookup; sub-millisecond).
        """

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        """Insert-or-update the row for ``budget.tenant_id``.

        Sets ``updated_at = now()`` server-side so the operator can see
        when a limit was last touched. ``created_at`` is preserved on
        update — only changes on the first insert for a tenant.
        """

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        """List all configured tenant budgets, oldest-first. Operator
        tooling only — never exposed on the HTTP API."""

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        """Sum ``runs.metrics.cost_usd`` for ``tenant_id`` for the
        current calendar month (UTC). 0.0 if no runs.

        ``Executor`` calls this at the top of every run to check
        against the budget; the cost-drift + per-run budget checks
        later in execute() are independent of this. Index on
        ``(tenant_id, created_at)`` is the perf path.
        """

    async def close(self) -> None: ...
