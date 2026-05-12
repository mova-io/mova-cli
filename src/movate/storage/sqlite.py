"""SQLite-backed StorageProvider for local dev and CI.

Uses ``aiosqlite`` for async access. Schema is created on ``init()`` —
idempotent. v0.1 implements runs + failures; jobs/api_keys/evals tables
will be added in their respective phases.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

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
    agent_version    TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    pricing_version  TEXT NOT NULL,
    status           TEXT NOT NULL,
    input            TEXT NOT NULL,
    output           TEXT,
    metrics          TEXT NOT NULL,
    error            TEXT,
    created_at       TEXT NOT NULL,
    workflow_run_id  TEXT,
    node_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_agent_created
    ON runs(agent, created_at DESC);
-- Note: idx_runs_workflow_run is created in _MIGRATIONS, not here, because
-- upgraders from a pre-v0.3 schema lack the workflow_run_id column when
-- this script runs. Sqlite errors on CREATE INDEX referencing a missing
-- column even with IF NOT EXISTS. Putting the index after the ALTER TABLE
-- migrations sidesteps the ordering trap.

CREATE TABLE IF NOT EXISTS failures (
    failure_id   TEXT PRIMARY KEY,
    run_id       TEXT,
    tenant_id    TEXT NOT NULL,
    agent        TEXT NOT NULL,
    failure_type TEXT NOT NULL,
    message      TEXT NOT NULL,
    retryable    INTEGER NOT NULL,
    created_at   TEXT NOT NULL
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
    threshold       REAL NOT NULL,
    mean_score      REAL NOT NULL,
    pass_rate       REAL NOT NULL,
    sample_count    INTEGER NOT NULL,
    total_cost_usd  REAL NOT NULL,
    created_at      TEXT NOT NULL
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
    total_cost_usd  REAL NOT NULL,
    models          TEXT NOT NULL,  -- JSON array of BenchModelRow
    created_at      TEXT NOT NULL
);
-- Trend / baseline lookups filter by tenant + agent first, then sort
-- by created_at desc to fetch the most recent. Without this index the
-- bench dashboard would table-scan every row in the tenant.
CREATE INDEX IF NOT EXISTS idx_bench_records_tenant_agent_created
    ON bench_records(tenant_id, agent, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_run_id   TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    workflow          TEXT NOT NULL,
    workflow_version  TEXT NOT NULL,
    status            TEXT NOT NULL,
    initial_state     TEXT NOT NULL,
    final_state       TEXT,
    error_node_id     TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_created
    ON workflow_runs(workflow, created_at DESC);
"""

# Lightweight migrations applied after `executescript(_SCHEMA)`. ALTERs are
# wrapped in a try/except for the "duplicate column" error so reruns are
# safe. Indexes use IF NOT EXISTS so reruns are safe too — but they MUST
# come after their corresponding ALTER TABLE so upgraders from a pre-v0.3
# schema (no workflow_run_id column yet) don't fail on the column reference.
#
# The jobs table (v0.5) lives here too rather than in _SCHEMA — keeps all
# additive schema changes in one ordered list so the upgrade path is
# obvious. Same idempotency story (CREATE TABLE IF NOT EXISTS).
_MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN workflow_run_id TEXT",
    "ALTER TABLE runs ADD COLUMN node_id TEXT",
    (
        "CREATE INDEX IF NOT EXISTS idx_runs_workflow_run "
        "ON runs(workflow_run_id) WHERE workflow_run_id IS NOT NULL"
    ),
    # v0.5: jobs queue.
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id        TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        kind          TEXT NOT NULL,
        target        TEXT NOT NULL,
        status        TEXT NOT NULL,
        input         TEXT NOT NULL,
        result_run_id TEXT,
        error         TEXT,
        api_key_id    TEXT,
        created_at    TEXT NOT NULL,
        claimed_at    TEXT,
        completed_at  TEXT
    )
    """,
    # Partial index over the queue head — `claim_next_job` reads the
    # oldest queued row, so this keeps that O(queued) regardless of
    # historical queue depth.
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_queue_head "
        "ON jobs(tenant_id, created_at) WHERE status = 'queued'"
    ),
    # Tenant-scoped listing for the `/jobs` endpoint and `movate worker`
    # filtering.
    ("CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created ON jobs(tenant_id, created_at DESC)"),
    # v0.5 stage 2: API keys.
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        key_id        TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        env           TEXT NOT NULL,
        secret_hash   TEXT NOT NULL,
        salt          TEXT NOT NULL,
        label         TEXT,
        created_at    TEXT NOT NULL,
        last_used_at  TEXT,
        revoked_at    TEXT
    )
    """,
    # Active-keys-by-tenant lookup for `movate auth list` and audit reports.
    # Partial index over WHERE revoked_at IS NULL keeps it tight as the table
    # grows with revocations.
    (
        "CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_active "
        "ON api_keys(tenant_id) WHERE revoked_at IS NULL"
    ),
    # post-v1.0: per-job email notification. SMS column was added later
    # under v1.0 SMS work (docs/v1.0-azure-design.md §10).
    "ALTER TABLE jobs ADD COLUMN notify_email TEXT",
    "ALTER TABLE jobs ADD COLUMN notify_sms TEXT",
    # post-v1.0: per-tenant monthly cost ceiling. One row per tenant;
    # absent row = unlimited (the default, backwards-compatible with
    # v0.x). Executor queries this on every run entry → PK lookup is
    # the perf path.
    """
    CREATE TABLE IF NOT EXISTS tenant_budgets (
        tenant_id          TEXT PRIMARY KEY,
        monthly_usd_limit  REAL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    # Cover the per-tenant current-month aggregation. Without this,
    # SUM(metrics.cost_usd) WHERE tenant_id=? AND created_at>=? is a
    # table scan; with it, an index range scan over month-of-rows.
    ("CREATE INDEX IF NOT EXISTS idx_runs_tenant_created ON runs(tenant_id, created_at)"),
    # post-v1.0: job-level retry policy. attempt_count tracks how
    # many times we've dispatched; next_retry_at lets the claim path
    # skip a job until its backoff has elapsed. NOT NULL DEFAULT 0
    # so existing rows from before this migration get a sensible
    # value (they've been "dispatched" some implicit number of times
    # but for retry purposes a fresh attempt budget is fine).
    "ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN next_retry_at TEXT",
    # Update the queue-head index to include next_retry_at so the claim
    # path can skip jobs whose retry hasn't yet elapsed without a table
    # scan. Use a separate index — keeps the original tight for jobs
    # that don't have a retry pending (next_retry_at IS NULL, the
    # common case).
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_retry_at "
        "ON jobs(tenant_id, next_retry_at) "
        "WHERE status = 'queued' AND next_retry_at IS NOT NULL"
    ),
]


class SqliteProvider:
    name = "sqlite"

    def __init__(self, db_path: str | Path = "~/.movate/local.db") -> None:
        self._path = Path(str(db_path)).expanduser()
        self._conn: aiosqlite.Connection | None = None

    async def ping(self) -> None:
        """``SELECT 1`` — confirms the connection is alive without
        touching any application table. Cheap enough to run on every
        ACA readiness probe (default cadence ~10s)."""
        async with self._db.execute("SELECT 1") as cur:
            await cur.fetchone()

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        # Idempotent column additions for upgraders. ALTER TABLE in sqlite
        # raises OperationalError("duplicate column name") on re-run; swallow
        # only that and re-raise everything else.
        for stmt in _MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
        await self._conn.commit()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteProvider.init() not called")
        return self._conn

    async def save_run(self, run: RunRecord) -> None:
        # Use named columns rather than positional VALUES so column order in
        # the schema can drift without breaking inserts (and so lightweight
        # ALTER-added columns work).
        await self._db.execute(
            """
            INSERT INTO runs (
                run_id, job_id, tenant_id, agent, agent_version, prompt_hash,
                provider, provider_version, pricing_version, status,
                input, output, metrics, error, created_at,
                workflow_run_id, node_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                json.dumps(run.input),
                json.dumps(run.output) if run.output is not None else None,
                run.metrics.model_dump_json(),
                run.error.model_dump_json() if run.error else None,
                run.created_at.isoformat(),
                run.workflow_run_id,
                run.node_id,
            ),
        )
        await self._db.commit()

    async def save_failure(self, f: FailureRecord) -> None:
        await self._db.execute(
            "INSERT INTO failures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f.failure_id,
                f.run_id,
                f.tenant_id,
                f.agent,
                f.failure_type,
                f.message,
                int(f.retryable),
                f.created_at.isoformat(),
            ),
        )
        await self._db.commit()

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
        params: list[object] = []
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if workflow_run_id:
            clauses.append("workflow_run_id = ?")
            params.append(workflow_run_id)
        sql = "SELECT * FROM runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]

    async def save_eval(self, e: EvalRecord) -> None:
        await self._db.execute(
            "INSERT INTO evals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
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
                e.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def list_evals(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        sql = "SELECT * FROM evals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_eval(r) for r in rows]

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        # tenant_id in WHERE is the SQL-layer enforcement; a caller can't
        # read another tenant's run even by guessing the run_id.
        async with self._db.execute(
            "SELECT * FROM runs WHERE run_id = ? AND tenant_id = ? LIMIT 1",
            (run_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_run(row) if row else None

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        async with self._db.execute(
            "SELECT * FROM workflow_runs WHERE workflow_run_id = ? AND tenant_id = ? LIMIT 1",
            (workflow_run_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_run(row) if row else None

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        async with self._db.execute(
            "SELECT * FROM evals WHERE eval_id = ? AND tenant_id = ? LIMIT 1",
            (eval_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_eval(row) if row else None

    async def save_bench(self, b: BenchRecord) -> None:
        await self._db.execute(
            "INSERT INTO bench_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
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
                # Per-model rows stored as a JSON array. Pydantic's
                # model_dump emits enums + datetimes natively but we
                # don't have either nested here — plain dicts of
                # primitives, safe to json.dumps directly.
                json.dumps([m.model_dump() for m in b.models]),
                b.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_bench(self, bench_id: str, *, tenant_id: str) -> BenchRecord | None:
        # Same tenant-scoped lookup pattern as get_eval — caller can't
        # read another tenant's record even by guessing the bench_id.
        async with self._db.execute(
            "SELECT * FROM bench_records WHERE bench_id = ? AND tenant_id = ? LIMIT 1",
            (bench_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_bench(row) if row else None

    async def list_benches(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[BenchRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        sql = "SELECT * FROM bench_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_bench(r) for r in rows]

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO workflow_runs (
                workflow_run_id, tenant_id, workflow, workflow_version,
                status, initial_state, final_state, error_node_id, error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                w.workflow_run_id,
                w.tenant_id,
                w.workflow,
                w.workflow_version,
                w.status.value,
                json.dumps(w.initial_state),
                json.dumps(w.final_state) if w.final_state is not None else None,
                w.error_node_id,
                w.error.model_dump_json() if w.error else None,
                w.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if workflow:
            clauses.append("workflow = ?")
            params.append(workflow)
        sql = "SELECT * FROM workflow_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Jobs (v0.5)
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO jobs (
                job_id, tenant_id, kind, target, status, input,
                result_run_id, error, api_key_id,
                created_at, claimed_at, completed_at,
                notify_email, notify_sms, attempt_count, next_retry_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.tenant_id,
                job.kind.value,
                job.target,
                job.status.value,
                json.dumps(job.input),
                job.result_run_id,
                json.dumps(job.error.model_dump()) if job.error else None,
                job.api_key_id,
                job.created_at.isoformat(),
                job.claimed_at.isoformat() if job.claimed_at else None,
                job.completed_at.isoformat() if job.completed_at else None,
                job.notify_email,
                job.notify_sms,
                job.attempt_count,
                job.next_retry_at.isoformat() if job.next_retry_at else None,
            ),
        )
        await self._db.commit()

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        async with self._db.execute(
            "SELECT * FROM jobs WHERE job_id = ? AND tenant_id = ?",
            (job_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None

    # claim_next_job needs to re-fetch the just-claimed row by id (the
    # caller's tenant matches by construction), so we use this small
    # internal helper that bypasses the tenant filter. It's safe because
    # we just inserted the row id in claim_next_job's own transaction.
    async def _get_job_unchecked(self, job_id: str) -> JobRecord | None:
        async with self._db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """Atomic claim: pick the oldest queued row, flip to RUNNING, return it.

        sqlite implementation uses ``BEGIN IMMEDIATE`` to take the
        reserved write lock up front so the SELECT-then-UPDATE pair is
        serialized across concurrent claimers. Postgres provider will
        use ``SELECT ... FOR UPDATE SKIP LOCKED`` (no IMMEDIATE needed
        — the row lock is finer-grained).

        ``tenant_id`` is optional so a single shared worker can drain
        all tenants. The HTTP layer never accepts a tenant-less claim;
        it's reserved for ``movate worker --all-tenants``.
        """
        # `aiosqlite` queues writes per connection so this whole block runs
        # serialized against other writers anyway, but BEGIN IMMEDIATE makes
        # the intent explicit (and works correctly under multi-process
        # access too, where aiosqlite's queue offers nothing).
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            now_iso = datetime.now(UTC).isoformat()
            # Retry-aware claim: skip jobs whose next_retry_at is in the
            # future. The `next_retry_at IS NULL` branch is the common
            # case (fresh jobs, jobs that have never failed); the
            # `<= now` branch is for re-queued jobs whose backoff has
            # elapsed.
            tenant_clause = "AND tenant_id = ?" if tenant_id is not None else ""
            params: tuple[object, ...] = (now_iso,)
            if tenant_id is not None:
                params = (now_iso, tenant_id)
            async with self._db.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  {tenant_clause}
                ORDER BY created_at
                LIMIT 1
                """,
                params,
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await self._db.commit()
                return None

            await self._db.execute(
                "UPDATE jobs SET status = 'running', claimed_at = ? WHERE job_id = ?",
                (now_iso, row["job_id"]),
            )
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

        # Re-fetch so the returned record reflects the updated columns.
        # _get_job_unchecked since we just claimed this row inside our
        # own transaction; the caller's tenant matches by construction
        # (claim_next_job either filtered by tenant_id or was the
        # operator drain-all path).
        return await self._get_job_unchecked(row["job_id"])

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
        now = datetime.now(UTC).isoformat()
        # tenant_id in WHERE: even a misconfigured worker can't mutate
        # another tenant's job. Silently no-ops on tenant mismatch
        # (matches the "404 not 403" cross-tenant probe defense).
        await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, result_run_id = ?, error = ?, completed_at = ?
            WHERE job_id = ? AND tenant_id = ?
            """,
            (
                status.value,
                result_run_id,
                json.dumps(error) if error else None,
                now,
                job_id,
                tenant_id,
            ),
        )
        await self._db.commit()

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Transition a ``RUNNING`` job back to ``QUEUED`` for a retry.

        The job sits in the queue but ``claim_next_job`` won't pick
        it up until ``now >= next_retry_at`` — that's how exponential
        backoff is enforced. ``claimed_at`` is cleared so the next
        claim records the new attempt cleanly.

        Tenant-scoped in WHERE (defense in depth — same rationale as
        ``update_job``). Silently no-ops on tenant mismatch.
        """
        await self._db.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                claimed_at = NULL,
                attempt_count = ?,
                next_retry_at = ?
            WHERE job_id = ? AND tenant_id = ?
            """,
            (
                attempt_count,
                next_retry_at.isoformat(),
                job_id,
                tenant_id,
            ),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # API keys (v0.5 stage 2)
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO api_keys (
                key_id, tenant_id, env, secret_hash, salt, label,
                created_at, last_used_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key.key_id,
                key.tenant_id,
                key.env.value,
                key.secret_hash,
                key.salt,
                key.label,
                key.created_at.isoformat(),
                key.last_used_at.isoformat() if key.last_used_at else None,
                key.revoked_at.isoformat() if key.revoked_at else None,
            ),
        )
        await self._db.commit()

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        async with self._db.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_api_key(row) if row else None

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM api_keys {where} ORDER BY created_at DESC"
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_api_key(r) for r in rows]

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Set ``revoked_at`` on a key. Idempotent — re-revoking is a no-op.

        ``tenant_id`` in WHERE: a tenant can only revoke its own keys
        even if it discovers another tenant's key_id (8-char random
        suffix; not high-entropy but still secret-ish).
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE key_id = ? AND tenant_id = ? AND revoked_at IS NULL",
            (now, key_id, tenant_id),
        )
        await self._db.commit()

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Bump ``last_used_at``. Called inline after a successful verify;
        failure to touch must not fail the request (caller swallows
        exceptions). ``tenant_id`` is defense in depth — the auth path
        already cross-checks the presented key's tenant prefix against
        the looked-up record, but the storage layer enforces it
        independently."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_id = ? AND tenant_id = ?",
            (now, key_id, tenant_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        async with self._db.execute(
            "SELECT * FROM tenant_budgets WHERE tenant_id = ?",
            (tenant_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return TenantBudget(
            tenant_id=row["tenant_id"],
            monthly_usd_limit=row["monthly_usd_limit"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        # ``INSERT ... ON CONFLICT`` preserves the original created_at
        # on updates — operators see "when was this tenant first
        # given a budget?" separately from "when did we last change
        # the limit?". updated_at refreshes either way.
        now_iso = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO tenant_budgets (
                tenant_id, monthly_usd_limit, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                monthly_usd_limit = excluded.monthly_usd_limit,
                updated_at = excluded.updated_at
            """,
            (
                budget.tenant_id,
                budget.monthly_usd_limit,
                budget.created_at.isoformat(),
                now_iso,
            ),
        )
        await self._db.commit()

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        async with self._db.execute("SELECT * FROM tenant_budgets ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [
            TenantBudget(
                tenant_id=r["tenant_id"],
                monthly_usd_limit=r["monthly_usd_limit"],
                created_at=datetime.fromisoformat(r["created_at"]),
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
            for r in rows
        ]

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        # First-of-the-month UTC. We do this in Python so the SQL
        # stays portable (sqlite's date functions are quirky); the
        # index on (tenant_id, created_at) covers the lookup either way.
        month_start = _first_of_month_utc().isoformat()
        async with self._db.execute(
            """
            SELECT COALESCE(SUM(
                CAST(json_extract(metrics, '$.cost_usd') AS REAL)
            ), 0.0) AS total
            FROM runs
            WHERE tenant_id = ? AND created_at >= ?
            """,
            (tenant_id, month_start),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0.0
        return float(row["total"] or 0.0)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def _row_to_eval(row: aiosqlite.Row) -> EvalRecord:
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
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_bench(row: aiosqlite.Row) -> BenchRecord:
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
        models=[BenchModelRow(**m) for m in json.loads(row["models"])],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_run(row: aiosqlite.Row) -> RunRecord:
    keys = row.keys() if hasattr(row, "keys") else []
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
        input=json.loads(row["input"]),
        output=json.loads(row["output"]) if row["output"] else None,
        metrics=Metrics.model_validate_json(row["metrics"]),
        error=ErrorInfo.model_validate_json(row["error"]) if row["error"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        workflow_run_id=row["workflow_run_id"] if "workflow_run_id" in keys else None,
        node_id=row["node_id"] if "node_id" in keys else None,
    )


def _row_to_workflow_run(row: aiosqlite.Row) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=row["workflow_run_id"],
        tenant_id=row["tenant_id"],
        workflow=row["workflow"],
        workflow_version=row["workflow_version"],
        status=WorkflowStatus(row["status"]),
        initial_state=json.loads(row["initial_state"]),
        final_state=json.loads(row["final_state"]) if row["final_state"] else None,
        error_node_id=row["error_node_id"],
        error=ErrorInfo.model_validate_json(row["error"]) if row["error"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_job(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        status=JobStatus(row["status"]),
        input=json.loads(row["input"]),
        result_run_id=row["result_run_id"],
        error=ErrorInfo.model_validate_json(row["error"]) if row["error"] else None,
        api_key_id=row["api_key_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        claimed_at=datetime.fromisoformat(row["claimed_at"]) if row["claimed_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        notify_email=row["notify_email"],
        notify_sms=row["notify_sms"],
        # Defensive: rows from pre-retry-migration schemas could
        # theoretically lack the column on a fresh open. aiosqlite.Row
        # KeyError on missing column, so we use bracket access only —
        # the migrations have run by the time we get here.
        attempt_count=row["attempt_count"],
        next_retry_at=(
            datetime.fromisoformat(row["next_retry_at"]) if row["next_retry_at"] else None
        ),
    )


def _row_to_api_key(row: aiosqlite.Row) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row["key_id"],
        tenant_id=row["tenant_id"],
        env=ApiKeyEnv(row["env"]),
        secret_hash=row["secret_hash"],
        salt=row["salt"],
        label=row["label"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_used_at=datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None,
        revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
    )


def _first_of_month_utc() -> datetime:
    """Midnight UTC on the 1st of the current month.

    The boundary for ``sum_tenant_cost_current_month``. The postgres
    backend has its own equivalent in :mod:`movate.storage.postgres`
    so the two implementations stay in lockstep on month-boundary
    semantics (operator's local time treating Jan 1 UTC as start of
    January).
    """
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
