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
    ConversationThread,
    ErrorInfo,
    EvalRecord,
    FailureRecord,
    FeedbackRecord,
    JobKind,
    JobRecord,
    JobStatus,
    JudgeMethod,
    KbChunk,
    KbChunkWithScore,
    Metrics,
    RunRecord,
    TenantBudget,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.storage._cosine import rank_chunks_by_cosine as _rank_chunks_by_cosine  # noqa: F401

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
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ
);
-- Upgrade paths for PG instances created on older schemas. PG natively
-- supports ADD COLUMN IF NOT EXISTS so this is idempotent on every init.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS notify_email TEXT;
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
    revoked_at    TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,
    scope         TEXT
);
-- Idempotent self-healing migrations for tables that pre-date a
-- column. `CREATE TABLE IF NOT EXISTS` is a no-op when the table is
-- already there, so any column added after the table was first
-- created has to be ALTERed in explicitly. Without this block,
-- upgrading a runtime against a long-lived database surfaces as
-- `UndefinedColumnError` on the first query that names the new
-- column (caught live on dev when an older deployment had created
-- `api_keys` without `expires_at` or `scope`).
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scope TEXT;
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_active
    ON api_keys(tenant_id) WHERE revoked_at IS NULL;

-- Operator feedback on runs. Captured by the Chainlit playground
-- (or any client POSTing to /api/v1/runs/{id}/feedback). Mirrored
-- to Langfuse as a score when Langfuse is configured.
CREATE TABLE IF NOT EXISTS run_feedback (
    feedback_id        TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    tenant_id          TEXT NOT NULL,
    agent              TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    score              SMALLINT NOT NULL,
    dimensions         JSONB,
    comment            TEXT,
    langfuse_score_id  TEXT,
    created_at         TIMESTAMPTZ NOT NULL
);
-- Per-run lookup (playground re-opening a rated run).
CREATE INDEX IF NOT EXISTS idx_run_feedback_run_id
    ON run_feedback(run_id);
-- Per-agent + time aggregation (dashboard: "agent X over last 30d").
CREATE INDEX IF NOT EXISTS idx_run_feedback_agent_created
    ON run_feedback(agent, created_at DESC);
-- Per-tenant listing (analytics scoped to a workspace).
CREATE INDEX IF NOT EXISTS idx_run_feedback_tenant_created
    ON run_feedback(tenant_id, created_at DESC);

-- KB chunks for vector retrieval (added 0.8.2.13). Embeddings stored
-- as JSONB float arrays — NOT pgvector yet. Cosine similarity is
-- computed in Python at query time. Future: swap to ``embedding
-- vector(1536)`` + ivfflat index when KB sizes warrant the perf
-- jump; the storage protocol stays unchanged.
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent           TEXT NOT NULL,
    source          TEXT NOT NULL,
    text            TEXT NOT NULL,
    embedding       JSONB NOT NULL,
    embedding_model TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL,
    -- PR-EE: OCR provenance flag. True when the chunk's text was
    -- extracted via Tesseract OCR rather than native text extraction
    -- (pypdf text layer, docx paragraphs, readability HTML strip).
    -- Default false so existing rows (native extraction) are
    -- unaffected on schema upgrade.
    ocr             BOOLEAN NOT NULL DEFAULT false
);
-- Per-agent + per-tenant retrieval scope. Search scans this index
-- range so the Python cosine loop touches only the relevant chunks.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_agent_tenant
    ON kb_chunks(agent, tenant_id);
-- Dedup: re-ingesting the same content for the same agent is a no-op
-- via this unique constraint + ON CONFLICT in the upsert path.
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_chunks_dedup
    ON kb_chunks(agent, tenant_id, content_hash);
-- Per-source listing (mdk kb ls --source <path>).
CREATE INDEX IF NOT EXISTS idx_kb_chunks_source
    ON kb_chunks(agent, tenant_id, source);
-- PR-AA: GIN index on tsvector for native full-text BM25 search.
-- Indexed with the 'english' text search configuration (stemming +
-- stopwords). Queries use plainto_tsquery('english', ...) which
-- converts free text to AND-connected lexemes — same semantics as
-- the Python BM25 scorer in movate.kb.lexical.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_text_gin
    ON kb_chunks USING gin(to_tsvector('english', text));

-- Conversation threads (PR-N) — group runs for multi-turn agents.
-- Runs link via the new ``runs.thread_id`` column (added below as an
-- ADD COLUMN IF NOT EXISTS — idempotent so re-running init() on an
-- upgraded database is safe).
CREATE TABLE IF NOT EXISTS conversation_threads (
    thread_id   TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_tenant_updated
    ON conversation_threads(tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_agent_tenant
    ON conversation_threads(agent, tenant_id);

-- Per-run thread linkage. NULL = standalone (non-threaded) run.
-- Postgres supports ADD COLUMN IF NOT EXISTS so re-running init() on
-- a database that already has the column is a no-op.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS thread_id TEXT;
CREATE INDEX IF NOT EXISTS idx_runs_thread
    ON runs(thread_id, created_at)
    WHERE thread_id IS NOT NULL;

-- PR-Q: jobs carry the thread linkage from queue time so the worker
-- can propagate it onto the spawned run. NULL = standalone.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS thread_id TEXT;
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
        # ACA wires the DSN with an empty password slot
        # (``postgresql://user:@host/db``) and surfaces the actual
        # password as a separate ``PGPASSWORD`` env var — see the
        # comment block in
        # ``infra/azure/modules/containerapp-api.bicep`` next to
        # MOVATE_DB_URL. asyncpg's documented "fall back to
        # PGPASSWORD" only fires when the DSN's password component
        # is MISSING (``user@host``); when it's present-but-empty
        # (``user:@host``) asyncpg authenticates with the empty
        # string and the server rejects it as
        # ``InvalidPasswordError``. We sidestep this by passing
        # PGPASSWORD as an explicit kwarg — asyncpg uses the kwarg
        # in preference to the DSN's password component, so the
        # bicep keeps working with no infra changes.
        import os  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "min_size": self._min_size,
            "max_size": self._max_size,
            "init": _init_connection,
        }
        env_password = os.environ.get("PGPASSWORD")
        if env_password:
            kwargs["password"] = env_password
        self._pool = await asyncpg.create_pool(self._dsn, **kwargs)
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
                workflow_run_id, node_id, thread_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18
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
            run.thread_id,
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
                notify_email, attempt_count, next_retry_at, thread_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16
            )
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
            job.attempt_count,
            job.next_retry_at,
            job.thread_id,
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
        target: str | None = None,
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
        if target is not None:
            params.append(target)
            clauses.append(f"target = ${len(params)}")
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
                created_at, last_used_at, revoked_at, expires_at, scope
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
            key.expires_at,
            key.scope,
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

    # ------------------------------------------------------------------
    # Run feedback (Chainlit playground writes here)
    # ------------------------------------------------------------------

    async def save_feedback(self, feedback: FeedbackRecord) -> None:
        # ON CONFLICT: operators can edit their feedback (replace
        # score / comment / dimensions). The primary key is the
        # feedback_id so re-saving with the same id updates in place.
        # ``run_id`` + ``tenant_id`` + ``agent`` + ``user_id`` +
        # ``created_at`` are intentionally NOT updated on conflict —
        # those identify the feedback row's provenance and shouldn't
        # mutate post-create.
        await self._db.execute(
            """
            INSERT INTO run_feedback (
                feedback_id, run_id, tenant_id, agent, user_id,
                score, dimensions, comment, langfuse_score_id, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (feedback_id) DO UPDATE
                SET score = EXCLUDED.score,
                    dimensions = EXCLUDED.dimensions,
                    comment = EXCLUDED.comment,
                    langfuse_score_id = EXCLUDED.langfuse_score_id
            """,
            feedback.feedback_id,
            feedback.run_id,
            feedback.tenant_id,
            feedback.agent,
            feedback.user_id,
            feedback.score,
            feedback.dimensions,
            feedback.comment,
            feedback.langfuse_score_id,
            feedback.created_at,
        )

    async def list_feedback(
        self,
        *,
        run_id: str | None = None,
        agent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        # Build WHERE clauses dynamically — keeps the indexed paths
        # (run_id, agent+created_at, tenant_id+created_at) usable
        # depending on which filters are set.
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            params.append(run_id)
            clauses.append(f"run_id = ${len(params)}")
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if user_id is not None:
            params.append(user_id)
            clauses.append(f"user_id = ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        sql = (
            "SELECT feedback_id, run_id, tenant_id, agent, user_id, score, "
            "dimensions, comment, langfuse_score_id, created_at "
            "FROM run_feedback" + where + " ORDER BY created_at DESC LIMIT $" + str(len(params))
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_feedback(r) for r in rows]

    # ------------------------------------------------------------------
    # KB chunks — vector retrieval. Cosine in Python (no pgvector yet).
    # ------------------------------------------------------------------

    async def save_kb_chunk(self, chunk: KbChunk) -> None:
        await self._db.execute(
            """
            INSERT INTO kb_chunks (
                chunk_id, tenant_id, agent, source, text, embedding,
                embedding_model, content_hash, metadata, created_at, ocr
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (agent, tenant_id, content_hash) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    embedding_model = EXCLUDED.embedding_model,
                    metadata = EXCLUDED.metadata,
                    source = EXCLUDED.source,
                    ocr = EXCLUDED.ocr
            """,
            chunk.chunk_id,
            chunk.tenant_id,
            chunk.agent,
            chunk.source,
            chunk.text,
            chunk.embedding,
            chunk.embedding_model,
            chunk.content_hash,
            chunk.metadata,
            chunk.created_at,
            chunk.ocr,
        )

    async def search_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        # No pgvector yet — load matching chunks and rank in Python.
        # The (agent, tenant_id) index scopes the scan to one KB; for
        # KBs under ~10k chunks the Python cosine loop completes in
        # <50ms. Future pgvector swap will compute the similarity in
        # SQL and only return the top-K.
        rows = await self._db.fetch(
            """
            SELECT chunk_id, tenant_id, agent, source, text, embedding,
                   embedding_model, content_hash, metadata, created_at, ocr
            FROM kb_chunks
            WHERE agent = $1 AND tenant_id = $2
            """,
            agent,
            tenant_id,
        )
        chunks = [_row_to_kb_chunk(r) for r in rows]
        from movate.storage._cosine import rank_chunks_by_cosine  # noqa: PLC0415
        return rank_chunks_by_cosine(chunks, query_embedding, limit)

    async def list_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
        limit: int = 1000,
    ) -> list[KbChunk]:
        clauses = ["agent = $1", "tenant_id = $2"]
        params: list[Any] = [agent, tenant_id]
        if source is not None:
            params.append(source)
            clauses.append(f"source = ${len(params)}")
        params.append(int(limit))
        sql = (
            "SELECT chunk_id, tenant_id, agent, source, text, embedding, "
            "embedding_model, content_hash, metadata, created_at, ocr "
            "FROM kb_chunks WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT $"
            + str(len(params))
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_kb_chunk(r) for r in rows]

    async def delete_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        if source is not None:
            result = await self._db.execute(
                "DELETE FROM kb_chunks WHERE agent = $1 AND tenant_id = $2 AND source = $3",
                agent,
                tenant_id,
                source,
            )
        else:
            result = await self._db.execute(
                "DELETE FROM kb_chunks WHERE agent = $1 AND tenant_id = $2",
                agent,
                tenant_id,
            )
        # asyncpg's execute returns "DELETE <count>" — parse the count.
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def search_kb_chunks_lexical(
        self,
        *,
        agent: str,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """Postgres tsvector + GIN index BM25-style lexical search.

        Uses ``plainto_tsquery`` (converts plain text to AND-connected
        lexemes) + ``ts_rank`` for scoring. Returns up to ``limit``
        chunks ranked most-relevant first. Empty query or no matches
        → empty list. NEVER raises.
        """
        if not query.strip():
            return []
        try:
            rows = await self._db.fetch(
                """
                SELECT chunk_id, tenant_id, agent, source, text, embedding,
                       embedding_model, content_hash, metadata, created_at, ocr,
                       ts_rank(
                           to_tsvector('english', text),
                           plainto_tsquery('english', $3)
                       ) AS rank
                FROM kb_chunks
                WHERE agent = $1
                  AND tenant_id = $2
                  AND to_tsvector('english', text) @@ plainto_tsquery('english', $3)
                ORDER BY rank DESC
                LIMIT $4
                """,
                agent,
                tenant_id,
                query,
                int(limit),
            )
        except Exception:
            return []
        results = []
        for r in rows:
            rank = float(r["rank"] or 0.0)
            # ts_rank returns [0, 1] normally. Clamp defensively.
            score = min(max(rank, 0.0), 1.0)
            results.append(KbChunkWithScore(chunk=_row_to_kb_chunk(r), score=score))
        return results

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

    # ------------------------------------------------------------------
    # Conversation threads (PR-N) — multi-turn agent foundation.
    # ------------------------------------------------------------------

    async def save_conversation_thread(self, thread: ConversationThread) -> None:
        # ON CONFLICT DO UPDATE so clients can call this on every
        # appended message to refresh ``updated_at`` (and optionally
        # ``title``) without re-inserting.
        await self._db.execute(
            """
            INSERT INTO conversation_threads
            (thread_id, tenant_id, agent, title, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (thread_id) DO UPDATE SET
                title = EXCLUDED.title,
                updated_at = EXCLUDED.updated_at
            """,
            thread.thread_id,
            thread.tenant_id,
            thread.agent,
            thread.title,
            thread.created_at,
            thread.updated_at,
        )

    async def get_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> ConversationThread | None:
        row = await self._db.fetchrow(
            "SELECT * FROM conversation_threads WHERE thread_id = $1 AND tenant_id = $2",
            thread_id,
            tenant_id,
        )
        if row is None:
            return None
        return _row_to_thread(row)

    async def list_conversation_threads(
        self,
        *,
        tenant_id: str,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[ConversationThread]:
        if agent is not None:
            rows = await self._db.fetch(
                """
                SELECT * FROM conversation_threads
                WHERE tenant_id = $1 AND agent = $2
                ORDER BY updated_at DESC
                LIMIT $3
                """,
                tenant_id,
                agent,
                int(limit),
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT * FROM conversation_threads
                WHERE tenant_id = $1
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                tenant_id,
                int(limit),
            )
        return [_row_to_thread(r) for r in rows]

    async def list_runs_for_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[RunRecord]:
        # Tenant-scoped via the WHERE clause — cross-tenant returns [].
        # ASC by created_at for chronological conversation history.
        rows = await self._db.fetch(
            """
            SELECT * FROM runs
            WHERE thread_id = $1 AND tenant_id = $2
            ORDER BY created_at ASC
            LIMIT $3
            """,
            thread_id,
            tenant_id,
            int(limit),
        )
        return [_row_to_run(r) for r in rows]

    async def delete_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        # asyncpg returns a status string like ``DELETE 1`` /
        # ``DELETE 0``; parse the trailing count for the bool.
        # Tenant-scoped via the WHERE clause — cross-tenant deletes
        # don't touch the row + return False.
        status = await self._db.execute(
            "DELETE FROM conversation_threads WHERE thread_id = $1 AND tenant_id = $2",
            thread_id,
            tenant_id,
        )
        return status.endswith(" 1") if isinstance(status, str) else False


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
        thread_id=row["thread_id"],
    )


def _row_to_thread(row: asyncpg.Record) -> ConversationThread:
    return ConversationThread(
        thread_id=row["thread_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        title=row["title"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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
        attempt_count=row["attempt_count"],
        next_retry_at=row["next_retry_at"],
        thread_id=row["thread_id"],
    )


def _row_to_kb_chunk(row: asyncpg.Record) -> KbChunk:
    row_dict = dict(row)
    return KbChunk(
        chunk_id=row_dict["chunk_id"],
        tenant_id=row_dict["tenant_id"],
        agent=row_dict["agent"],
        source=row_dict["source"],
        text=row_dict["text"],
        embedding=list(row_dict["embedding"]),
        embedding_model=row_dict["embedding_model"],
        content_hash=row_dict["content_hash"],
        metadata=row_dict.get("metadata"),
        ocr=bool(row_dict.get("ocr", False)),
        created_at=row_dict["created_at"],
    )



def _row_to_feedback(row: asyncpg.Record) -> FeedbackRecord:
    row_dict = dict(row)
    return FeedbackRecord(
        feedback_id=row_dict["feedback_id"],
        run_id=row_dict["run_id"],
        tenant_id=row_dict["tenant_id"],
        agent=row_dict["agent"],
        user_id=row_dict["user_id"],
        score=row_dict["score"],
        dimensions=row_dict.get("dimensions"),
        comment=row_dict.get("comment"),
        langfuse_score_id=row_dict.get("langfuse_score_id"),
        created_at=row_dict["created_at"],
    )


def _row_to_api_key(row: asyncpg.Record) -> ApiKeyRecord:
    row_dict = dict(row)
    return ApiKeyRecord(
        key_id=row_dict["key_id"],
        tenant_id=row_dict["tenant_id"],
        env=ApiKeyEnv(row_dict["env"]),
        secret_hash=row_dict["secret_hash"],
        salt=row_dict["salt"],
        label=row_dict["label"],
        created_at=row_dict["created_at"],
        last_used_at=row_dict["last_used_at"],
        revoked_at=row_dict["revoked_at"],
        expires_at=row_dict.get("expires_at"),
        scope=row_dict.get("scope"),
    )


__all__ = ["PostgresProvider"]
