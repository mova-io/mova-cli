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
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_agent_created
    ON runs(agent, created_at DESC);

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
"""


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
        await self._conn.commit()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteProvider.init() not called")
        return self._conn

    async def save_run(self, run: RunRecord) -> None:
        await self._db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    )
