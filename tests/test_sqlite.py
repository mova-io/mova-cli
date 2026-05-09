"""SqliteProvider round-trip tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from movate.core.models import (
    ErrorInfo,
    FailureRecord,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage.sqlite import SqliteProvider


def _make_run(*, agent: str = "demo", status: JobStatus = JobStatus.SUCCESS) -> RunRecord:
    return RunRecord(
        run_id=str(uuid4()),
        job_id=str(uuid4()),
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="abc123",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="0.0.1",
        pricing_version="2026.05.01",
        status=status,
        input={"text": "hi"},
        output={"message": "ok"} if status is JobStatus.SUCCESS else None,
        metrics=Metrics(
            latency_ms=42,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=0.0001,
            provider="openai/gpt-4o-mini-2024-07-18",
            pricing_version="2026.05.01",
        ),
        error=ErrorInfo(type="schema_error", message="bad", retryable=False)
        if status is JobStatus.ERROR
        else None,
    )


@pytest.mark.unit
async def test_save_and_list_runs(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    await db.init()

    run = _make_run()
    await db.save_run(run)

    rows = await db.list_runs()
    assert len(rows) == 1
    assert rows[0].run_id == run.run_id
    assert rows[0].metrics.cost_usd == 0.0001

    await db.close()


@pytest.mark.unit
async def test_list_runs_filters(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    await db.init()
    await db.save_run(_make_run(agent="alpha"))
    await db.save_run(_make_run(agent="beta"))
    await db.save_run(_make_run(agent="alpha", status=JobStatus.ERROR))

    alpha = await db.list_runs(agent="alpha")
    assert len(alpha) == 2
    beta = await db.list_runs(agent="beta")
    assert len(beta) == 1
    errored = await db.list_runs(status=JobStatus.ERROR.value)
    assert len(errored) == 1
    assert errored[0].error is not None
    assert errored[0].error.type == "schema_error"

    await db.close()


@pytest.mark.unit
async def test_save_failure(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    await db.init()
    await db.save_failure(
        FailureRecord(
            failure_id=str(uuid4()),
            run_id=str(uuid4()),
            tenant_id="local",
            agent="demo",
            failure_type="rate_limit",
            message="too many requests",
            retryable=True,
        )
    )
    # No public list_failures yet (Phase 4); just confirm no error and the
    # row was persisted by sniffing the underlying connection.
    async with db._db.execute("SELECT COUNT(*) FROM failures") as cur:
        count = (await cur.fetchone())[0]
    assert count == 1

    await db.close()


@pytest.mark.unit
async def test_init_is_idempotent(tmp_path: Path) -> None:
    """Second init() on the same DB should not raise."""
    db1 = SqliteProvider(db_path=tmp_path / "test.db")
    await db1.init()
    await db1.save_run(_make_run())
    await db1.close()

    db2 = SqliteProvider(db_path=tmp_path / "test.db")
    await db2.init()
    rows = await db2.list_runs()
    assert len(rows) == 1
    await db2.close()


@pytest.mark.unit
async def test_init_required_before_use(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "test.db")
    with pytest.raises(RuntimeError, match="init"):
        await db.save_run(_make_run())
