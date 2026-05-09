"""SQLite-backed StorageProvider for local dev and CI.

Uses ``aiosqlite`` for async access. Schema is created on ``init()`` —
idempotent. v0.1 implements runs + failures; jobs/api_keys/evals tables
will be added in their respective phases.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from movate.core.models import (
    ErrorInfo,
    EvalRecord,
    FailureRecord,
    JobStatus,
    JudgeMethod,
    Metrics,
    RunRecord,
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
CREATE INDEX IF NOT EXISTS idx_runs_workflow_run
    ON runs(workflow_run_id) WHERE workflow_run_id IS NOT NULL;

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

# Lightweight migrations applied after `executescript(_SCHEMA)`. Each is
# wrapped in a try/except for the "duplicate column" error so reruns are safe.
_MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN workflow_run_id TEXT",
    "ALTER TABLE runs ADD COLUMN node_id TEXT",
]


class SqliteProvider:
    name = "sqlite"

    def __init__(self, db_path: str | Path = "~/.movate/local.db") -> None:
        self._path = Path(str(db_path)).expanduser()
        self._conn: aiosqlite.Connection | None = None

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
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        clauses: list[str] = []
        params: list[object] = []
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

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self._db.execute(
            "SELECT * FROM runs WHERE run_id = ? LIMIT 1", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_run(row) if row else None

    async def get_workflow_run(self, workflow_run_id: str) -> WorkflowRunRecord | None:
        async with self._db.execute(
            "SELECT * FROM workflow_runs WHERE workflow_run_id = ? LIMIT 1",
            (workflow_run_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_run(row) if row else None

    async def get_eval(self, eval_id: str) -> EvalRecord | None:
        async with self._db.execute(
            "SELECT * FROM evals WHERE eval_id = ? LIMIT 1", (eval_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_eval(row) if row else None

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
        workflow: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        clauses: list[str] = []
        params: list[object] = []
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
