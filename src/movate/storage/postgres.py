"""PostgreSQL-backed StorageProvider for production deployments.

Same Protocol surface as :class:`SqliteProvider`, same conformance
test suite. Differences are pure mechanics:

* JSON columns use ``JSONB`` (indexed, queryable) instead of ``TEXT``.
* Timestamps use ``TIMESTAMPTZ`` instead of ISO strings.
* Booleans use ``BOOLEAN`` instead of ``INTEGER``.
* The job-claim path uses ``SELECT ... FOR UPDATE SKIP LOCKED`` —
  superior to sqlite's ``BEGIN IMMEDIATE`` because finer-grained row
  locks let multiple workers truly run concurrently.

The pool is configured once with a per-connection ``init`` callback
that registers a ``json.dumps`` / ``json.loads`` codec for ``jsonb``,
so callers pass and receive plain dicts.

Connection string is read from ``MOVATE_DB_URL`` (e.g.
``postgresql://user:pw@host:5432/movate``); see :func:`build_storage`
in :mod:`movate.storage`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg

from movate.core.models import (
    ApiKeyEnv,
    ApiKeyRecord,
    BenchModelRow,
    BenchRecord,
    ErrorInfo,
    EvalRecord,
    FailureRecord,
    JobKind,
    JobRecord,
    JobStatus,
    JudgeMethod,
    Metrics,
    RunRecord,
    TenantBudget,
    WorkflowRunRecord,
    WorkflowStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    agent            TEXT NOT NULL,
    agent_version   TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    pricing_version  TEXT NOT NULL,
    status           TEXT NOT NULL,
    input            JSONB NOT NULL,
    output           JSONB,
    metrics          JSONB NOT NULL,
    error            JSONB,
    created_at       TIMESTAMPTZ NOT NULL,
    workflow_run_id  TEXT,
    node_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_agent_created
    ON runs(agent, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_workflow_run
    ON runs(workflow_run_id) WHERE workflow_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS failures (
    failure_id   TEXT PRIMARY KEY,
    run_id       TEXT,
    tenant_id    TEXT NOT NULL,
    agent        TEXT NOT NULL,
    failure_type TEXT NOT NULL,
    message      TEXT NOT NULL,
    retryable    BOOLEAN NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evals (
    eval_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent           TEXT NOT NULL,
    agent_version   TEXT NOT NULL,
    dataset_hash    TEXT NOT NULL,
    judge_method    TEXT NOT NULL,
    judge_provider  TEXT,
    runs_per_case   INTEGER NOT NULL,
    gate_mode       TEXT NOT NULL,
    threshold       DOUBLE PRECISION NOT NULL,
    mean_score      DOUBLE PRECISION NOT NULL,
    pass_rate       DOUBLE PRECISION NOT NULL,
    sample_count    INTEGER NOT NULL,
    total_cost_usd  DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evals_agent_created
    ON evals(agent, created_at DESC);

CREATE TABLE IF NOT EXISTS bench_records (
    bench_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent           TEXT NOT NULL,
    agent_version   TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    judge_method    TEXT,
    judge_provider  TEXT,
    rubric          TEXT,
    runs_per_model  INTEGER NOT NULL,
    gate_mode       TEXT NOT NULL,
    total_cost_usd  DOUBLE PRECISION NOT NULL,
    models          JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_records_tenant_agent_created
    ON bench_records(tenant_id, agent, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_run_id   TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    workflow          TEXT NOT NULL,
    workflow_version  TEXT NOT NULL,
    status            TEXT NOT NULL,
    initial_state     JSONB NOT NULL,
    final_state       JSONB,
    error_node_id     TEXT,
    error             JSONB,
    created_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_created
    ON workflow_runs(workflow, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    target        TEXT NOT NULL,
    status        TEXT NOT NULL,
    input         JSONB NOT NULL,
    result_run_id TEXT,
    error         JSONB,
    api_key_id    TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    claimed_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    notify_email  TEXT,
    notify_sms    TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ
);
-- Upgrade paths for PG instances created on older schemas. PG natively
-- supports ADD COLUMN IF NOT EXISTS so this is idempotent on every init.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS notify_email TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS notify_sms TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_jobs_queue_head
    ON jobs(tenant_id, created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created
    ON jobs(tenant_id, created_at DESC);
-- Retry-aware claim path: lets `claim_next_job` skip jobs whose
-- next_retry_at hasn't elapsed without a table scan. Common-case
-- (next_retry_at IS NULL, never-failed) bypasses this index via the
-- existing idx_jobs_queue_head.
CREATE INDEX IF NOT EXISTS idx_jobs_retry_at
    ON jobs(tenant_id, next_retry_at)
    WHERE status = 'queued' AND next_retry_at IS NOT NULL;

-- Per-tenant monthly cost ceiling. Absent row = unlimited.
CREATE TABLE IF NOT EXISTS tenant_budgets (
    tenant_id          TEXT PRIMARY KEY,
    monthly_usd_limit  DOUBLE PRECISION,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL
);
-- Cover the per-tenant current-month aggregation. Without this,
-- SUM(metrics->>'cost_usd') WHERE tenant_id=$1 AND created_at>=$2 is
-- a Seq Scan over the full runs table; with it, an index range scan
-- bounded to the month's rows for that tenant.
CREATE INDEX IF NOT EXISTS idx_runs_tenant_created
    ON runs(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    env           TEXT NOT NULL,
    secret_hash   TEXT NOT NULL,
    salt          TEXT NOT NULL,
    label         TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_active
    ON api_keys(tenant_id) WHERE revoked_at IS NULL;
"""


# ---------------------------------------------------------------------------
# Pool init — register JSONB ↔ dict codec on every connection
# ---------------------------------------------------------------------------


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup: tell asyncpg how to round-trip JSONB.

    Without this, asyncpg returns JSONB columns as ``str`` (they were
    inserted as ``str`` too). Registering the codec lets handlers pass
    and receive plain Python dicts — same UX as the sqlite path
    (which wraps json.dumps/loads inline).
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class PostgresProvider:
    """Implements :class:`StorageProvider` against PostgreSQL via asyncpg.

    Connections come from a pool. Single-statement methods acquire a
    connection per call; the claim path takes a transaction so the
    SELECT-then-UPDATE pair is atomic across concurrent workers.
    """

    name = "postgres"

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            init=_init_connection,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)

    async def ping(self) -> None:
        """``SELECT 1`` against the pool — picks up DB-down /
        pool-exhausted / network-blip conditions for the ``/ready``
        endpoint. Acquires from the pool so we exercise the same
        path real queries take."""
        await self._db.execute("SELECT 1")

    @property
    def _db(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresProvider.init() not called")
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def save_run(self, run: RunRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO runs (
                run_id, job_id, tenant_id, agent, agent_version, prompt_hash,
                provider, provider_version, pricing_version, status,
                input, output, metrics, error, created_at,
                workflow_run_id, node_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17
            )
            """,
            run.run_id,
            run.job_id,
            run.tenant_id,
            run.agent,
            run.agent_version,
            run.prompt_hash,
            run.provider,
            run.provider_version,
            run.pricing_version,
            run.status.value,
            run.input,
            run.output,
            run.metrics.model_dump(),
            run.error.model_dump() if run.error else None,
            run.created_at,
            run.workflow_run_id,
            run.node_id,
        )

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        # tenant_id in WHERE is the SQL-layer enforcement; a caller can't
        # read another tenant's run even by guessing the run_id.
        row = await self._db.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1 AND tenant_id = $2",
            run_id,
            tenant_id,
        )
        return _row_to_run(row) if row else None

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if workflow_run_id is not None:
            params.append(workflow_run_id)
            clauses.append(f"workflow_run_id = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------

    async def save_failure(self, f: FailureRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO failures (
                failure_id, run_id, tenant_id, agent, failure_type,
                message, retryable, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            f.failure_id,
            f.run_id,
            f.tenant_id,
            f.agent,
            f.failure_type,
            f.message,
            f.retryable,
            f.created_at,
        )

    # ------------------------------------------------------------------
    # Evals
    # ------------------------------------------------------------------

    async def save_eval(self, e: EvalRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO evals (
                eval_id, tenant_id, agent, agent_version, dataset_hash,
                judge_method, judge_provider, runs_per_case, gate_mode,
                threshold, mean_score, pass_rate, sample_count,
                total_cost_usd, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15
            )
            """,
            e.eval_id,
            e.tenant_id,
            e.agent,
            e.agent_version,
            e.dataset_hash,
            e.judge_method.value,
            e.judge_provider,
            e.runs_per_case,
            e.gate_mode,
            e.threshold,
            e.mean_score,
            e.pass_rate,
            e.sample_count,
            e.total_cost_usd,
            e.created_at,
        )

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM evals WHERE eval_id = $1 AND tenant_id = $2",
            eval_id,
            tenant_id,
        )
        return _row_to_eval(row) if row else None

    async def list_evals(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM evals {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_eval(r) for r in rows]

    async def save_bench(self, b: BenchRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO bench_records (
                bench_id, tenant_id, agent, agent_version, input_hash,
                judge_method, judge_provider, rubric, runs_per_model,
                gate_mode, total_cost_usd, models, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13
            )
            """,
            b.bench_id,
            b.tenant_id,
            b.agent,
            b.agent_version,
            b.input_hash,
            b.judge_method.value if b.judge_method else None,
            b.judge_provider,
            b.rubric,
            b.runs_per_model,
            b.gate_mode,
            b.total_cost_usd,
            # asyncpg serializes JSONB from a Python str/bytes — easier
            # than registering a custom codec. The ::jsonb cast in the
            # SQL converts the TEXT param to JSONB at insert time.
            json.dumps([m.model_dump() for m in b.models]),
            b.created_at,
        )

    async def get_bench(self, bench_id: str, *, tenant_id: str) -> BenchRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM bench_records WHERE bench_id = $1 AND tenant_id = $2",
            bench_id,
            tenant_id,
        )
        return _row_to_bench(row) if row else None

    async def list_benches(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[BenchRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM bench_records {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_bench(r) for r in rows]

    # ------------------------------------------------------------------
    # Workflow runs
    # ------------------------------------------------------------------

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO workflow_runs (
                workflow_run_id, tenant_id, workflow, workflow_version,
                status, initial_state, final_state, error_node_id, error,
                created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            w.workflow_run_id,
            w.tenant_id,
            w.workflow,
            w.workflow_version,
            w.status.value,
            w.initial_state,
            w.final_state,
            w.error_node_id,
            w.error.model_dump() if w.error else None,
            w.created_at,
        )

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM workflow_runs WHERE workflow_run_id = $1 AND tenant_id = $2",
            workflow_run_id,
            tenant_id,
        )
        return _row_to_workflow_run(row) if row else None

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if workflow is not None:
            params.append(workflow)
            clauses.append(f"workflow = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM workflow_runs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_workflow_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO jobs (
                job_id, tenant_id, kind, target, status, input,
                result_run_id, error, api_key_id,
                created_at, claimed_at, completed_at,
                notify_email, notify_sms, attempt_count, next_retry_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            """,
            job.job_id,
            job.tenant_id,
            job.kind.value,
            job.target,
            job.status.value,
            job.input,
            job.result_run_id,
            job.error.model_dump() if job.error else None,
            job.api_key_id,
            job.created_at,
            job.claimed_at,
            job.completed_at,
            job.notify_email,
            job.notify_sms,
            job.attempt_count,
            job.next_retry_at,
        )

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM jobs WHERE job_id = $1 AND tenant_id = $2",
            job_id,
            tenant_id,
        )
        return _row_to_job(row) if row else None

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if status is not None:
            params.append(status.value)
            clauses.append(f"status = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_job(r) for r in rows]

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """Postgres claim path: ``SELECT ... FOR UPDATE SKIP LOCKED``.

        Two workers hitting this concurrently take row-level locks on
        DIFFERENT rows; whichever sees the oldest queued first wins
        that row, the second sees no available rows (or moves on to
        the next queued one). No global serialization, no retries.
        """
        async with self._db.acquire() as conn, conn.transaction():
            now = datetime.now(UTC)
            # Retry-aware claim: skip rows whose next_retry_at is in
            # the future. The `next_retry_at IS NULL` branch is the
            # common case (fresh jobs); `<= now` covers re-queued jobs
            # whose backoff has elapsed.
            if tenant_id:
                tenant_clause = "AND tenant_id = $2"
                params: tuple[Any, ...] = (now, tenant_id)
            else:
                tenant_clause = ""
                params = (now,)
            row = await conn.fetchrow(
                f"""
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= $1)
                  {tenant_clause}
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                *params,
            )
            if row is None:
                return None

            # Reuse the `now` we computed for the claim-window filter.
            await conn.execute(
                """
                UPDATE jobs SET status = 'running', claimed_at = $1
                WHERE job_id = $2
                """,
                now,
                row["job_id"],
            )

            # Re-fetch to return the post-update row. (asyncpg's
            # transaction context closes on exit; we do this inside
            # so the caller sees a consistent snapshot.)
            updated = await conn.fetchrow("SELECT * FROM jobs WHERE job_id = $1", row["job_id"])
            return _row_to_job(updated) if updated else None

    async def update_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        status: JobStatus,
        result_run_id: str | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        if status not in (
            JobStatus.SUCCESS,
            JobStatus.ERROR,
            JobStatus.SAFETY_BLOCKED,
            JobStatus.DEAD_LETTER,
        ):
            raise ValueError(
                f"update_job only accepts terminal statuses; got {status!r}. "
                f"Use save_job/claim_next_job/requeue_job for non-terminal transitions."
            )
        # tenant_id in WHERE: even a misconfigured worker can't mutate
        # another tenant's job. Silently no-ops on tenant mismatch.
        await self._db.execute(
            """
            UPDATE jobs
            SET status = $1, result_run_id = $2, error = $3, completed_at = $4
            WHERE job_id = $5 AND tenant_id = $6
            """,
            status.value,
            result_run_id,
            error,
            datetime.now(UTC),
            job_id,
            tenant_id,
        )

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Re-queue a ``RUNNING`` job for a retry after a transient failure.

        Sets status back to ``QUEUED``, clears ``claimed_at`` (so the
        next claim records a fresh attempt), bumps ``attempt_count``,
        and sets ``next_retry_at`` so the claim path skips this row
        until backoff elapses.
        """
        await self._db.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                claimed_at = NULL,
                attempt_count = $1,
                next_retry_at = $2
            WHERE job_id = $3 AND tenant_id = $4
            """,
            attempt_count,
            next_retry_at,
            job_id,
            tenant_id,
        )

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO api_keys (
                key_id, tenant_id, env, secret_hash, salt, label,
                created_at, last_used_at, revoked_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            key.key_id,
            key.tenant_id,
            key.env.value,
            key.secret_hash,
            key.salt,
            key.label,
            key.created_at,
            key.last_used_at,
            key.revoked_at,
        )

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        row = await self._db.fetchrow("SELECT * FROM api_keys WHERE key_id = $1", key_id)
        return _row_to_api_key(row) if row else None

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM api_keys {where} ORDER BY created_at DESC"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_api_key(r) for r in rows]

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        # tenant_id in WHERE: a tenant can only revoke its own keys
        # even if it discovers another tenant's key_id.
        await self._db.execute(
            """
            UPDATE api_keys SET revoked_at = $1
            WHERE key_id = $2 AND tenant_id = $3 AND revoked_at IS NULL
            """,
            datetime.now(UTC),
            key_id,
            tenant_id,
        )

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        # tenant_id is defense in depth — the auth path already
        # cross-checks the presented key's tenant prefix against the
        # looked-up record. The storage layer enforces independently.
        await self._db.execute(
            "UPDATE api_keys SET last_used_at = $1 WHERE key_id = $2 AND tenant_id = $3",
            datetime.now(UTC),
            key_id,
            tenant_id,
        )

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        row = await self._db.fetchrow(
            "SELECT * FROM tenant_budgets WHERE tenant_id = $1", tenant_id
        )
        if row is None:
            return None
        return TenantBudget(
            tenant_id=row["tenant_id"],
            monthly_usd_limit=row["monthly_usd_limit"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        # ``ON CONFLICT ... DO UPDATE`` preserves created_at on
        # updates — operators see "first set" vs "last touched"
        # separately.
        now = datetime.now(UTC)
        await self._db.execute(
            """
            INSERT INTO tenant_budgets (
                tenant_id, monthly_usd_limit, created_at, updated_at
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id) DO UPDATE SET
                monthly_usd_limit = EXCLUDED.monthly_usd_limit,
                updated_at = EXCLUDED.updated_at
            """,
            budget.tenant_id,
            budget.monthly_usd_limit,
            budget.created_at,
            now,
        )

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        rows = await self._db.fetch("SELECT * FROM tenant_budgets ORDER BY created_at")
        return [
            TenantBudget(
                tenant_id=r["tenant_id"],
                monthly_usd_limit=r["monthly_usd_limit"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        # ``date_trunc('month', now() at time zone 'utc')`` returns the
        # 1st-of-month UTC. The ``metrics->>'cost_usd'`` extraction is
        # JSONB-aware — postgres treats this as a typed real cast.
        # COALESCE keeps the no-runs-yet case as 0.0.
        result = await self._db.fetchval(
            """
            SELECT COALESCE(SUM(
                (metrics->>'cost_usd')::DOUBLE PRECISION
            ), 0.0)
            FROM runs
            WHERE tenant_id = $1
              AND created_at >= date_trunc('month', NOW() AT TIME ZONE 'utc')
            """,
            tenant_id,
        )
        return float(result or 0.0)


# ---------------------------------------------------------------------------
# Row → model converters
# ---------------------------------------------------------------------------


def _row_to_run(row: asyncpg.Record) -> RunRecord:
    metrics_data = dict(row["metrics"])
    return RunRecord(
        run_id=row["run_id"],
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        prompt_hash=row["prompt_hash"],
        provider=row["provider"],
        provider_version=row["provider_version"],
        pricing_version=row["pricing_version"],
        status=JobStatus(row["status"]),
        input=dict(row["input"]),
        output=dict(row["output"]) if row["output"] else None,
        metrics=Metrics.model_validate(metrics_data),
        error=ErrorInfo.model_validate(dict(row["error"])) if row["error"] else None,
        created_at=row["created_at"],
        workflow_run_id=row["workflow_run_id"],
        node_id=row["node_id"],
    )


def _row_to_eval(row: asyncpg.Record) -> EvalRecord:
    return EvalRecord(
        eval_id=row["eval_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        dataset_hash=row["dataset_hash"],
        judge_method=JudgeMethod(row["judge_method"]),
        judge_provider=row["judge_provider"],
        runs_per_case=row["runs_per_case"],
        gate_mode=row["gate_mode"],
        threshold=row["threshold"],
        mean_score=row["mean_score"],
        pass_rate=row["pass_rate"],
        sample_count=row["sample_count"],
        total_cost_usd=row["total_cost_usd"],
        created_at=row["created_at"],
    )


def _row_to_bench(row: asyncpg.Record) -> BenchRecord:
    # JSONB column comes back as either a str (driver default) or a
    # parsed object depending on the asyncpg version + codec setup.
    # Handle both: parse if str, use as-is if already a list.
    models_raw = row["models"]
    if isinstance(models_raw, str):
        models_raw = json.loads(models_raw)
    return BenchRecord(
        bench_id=row["bench_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        input_hash=row["input_hash"],
        judge_method=JudgeMethod(row["judge_method"]) if row["judge_method"] else None,
        judge_provider=row["judge_provider"],
        rubric=row["rubric"],
        runs_per_model=row["runs_per_model"],
        gate_mode=row["gate_mode"],
        total_cost_usd=row["total_cost_usd"],
        models=[BenchModelRow(**m) for m in models_raw],
        created_at=row["created_at"],
    )


def _row_to_workflow_run(row: asyncpg.Record) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=row["workflow_run_id"],
        tenant_id=row["tenant_id"],
        workflow=row["workflow"],
        workflow_version=row["workflow_version"],
        status=WorkflowStatus(row["status"]),
        initial_state=dict(row["initial_state"]),
        final_state=dict(row["final_state"]) if row["final_state"] else None,
        error_node_id=row["error_node_id"],
        error=ErrorInfo.model_validate(dict(row["error"])) if row["error"] else None,
        created_at=row["created_at"],
    )


def _row_to_job(row: asyncpg.Record) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        status=JobStatus(row["status"]),
        input=dict(row["input"]),
        result_run_id=row["result_run_id"],
        error=ErrorInfo.model_validate(dict(row["error"])) if row["error"] else None,
        api_key_id=row["api_key_id"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        notify_email=row["notify_email"],
        notify_sms=row["notify_sms"],
        attempt_count=row["attempt_count"],
        next_retry_at=row["next_retry_at"],
    )


def _row_to_api_key(row: asyncpg.Record) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row["key_id"],
        tenant_id=row["tenant_id"],
        env=ApiKeyEnv(row["env"]),
        secret_hash=row["secret_hash"],
        salt=row["salt"],
        label=row["label"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


__all__ = ["PostgresProvider"]
