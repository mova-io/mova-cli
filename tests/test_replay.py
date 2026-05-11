"""Replay engine: storage round-trip, load_replay dispatch, render shapes.

CLI integration is a single end-to-end smoke at the bottom — the heavy
unit testing lives against ``InMemoryStorage`` so the replay engine is
exercised without touching disk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from movate.core.models import (
    ErrorInfo,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.replay import (
    ReplayNotFoundError,
    load_replay,
    render_replay_json,
    truncate,
)
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: str | None = None,
    workflow_run_id: str | None = None,
    node_id: str | None = None,
    status: JobStatus = JobStatus.SUCCESS,
    output: dict | None = None,
    cost: float = 0.0001,
    agent: str = "demo",
) -> RunRecord:
    return RunRecord(
        run_id=run_id or str(uuid4()),
        job_id=str(uuid4()),
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="abc123def456789012345",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="0.0.1",
        pricing_version="2026.05.01",
        status=status,
        input={"text": "hi"},
        output=output if status is JobStatus.SUCCESS else None,
        metrics=Metrics(
            latency_ms=42,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=cost,
            provider="openai/gpt-4o-mini-2024-07-18",
            pricing_version="2026.05.01",
        ),
        error=ErrorInfo(type="schema_error", message="bad", retryable=False)
        if status is JobStatus.ERROR
        else None,
        created_at=datetime.now(UTC),
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )


def _make_workflow_run(
    *,
    workflow_run_id: str | None = None,
    status: WorkflowStatus = WorkflowStatus.SUCCESS,
    error_node_id: str | None = None,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=workflow_run_id or str(uuid4()),
        tenant_id="local",
        workflow="returns-pipeline",
        workflow_version="0.1.0",
        status=status,
        initial_state={"text": "seed"},
        final_state={"text": "seed", "step1": "alpha", "step2": "beta"},
        error_node_id=error_node_id,
        error=None,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Storage round-trip — get_run / get_workflow_run
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_in_memory_storage_get_run_returns_existing() -> None:
    s = InMemoryStorage()
    await s.init()
    target = _make_run()
    await s.save_run(target)
    await s.save_run(_make_run())  # noise
    got = await s.get_run(target.run_id, tenant_id="local")
    assert got is not None
    assert got.run_id == target.run_id


@pytest.mark.unit
async def test_in_memory_storage_get_run_returns_none_for_missing() -> None:
    s = InMemoryStorage()
    await s.init()
    assert await s.get_run("does-not-exist", tenant_id="local") is None


@pytest.mark.unit
async def test_in_memory_storage_get_workflow_run_returns_existing() -> None:
    s = InMemoryStorage()
    await s.init()
    target = _make_workflow_run()
    await s.save_workflow_run(target)
    got = await s.get_workflow_run(target.workflow_run_id, tenant_id="local")
    assert got is not None
    assert got.workflow_run_id == target.workflow_run_id


@pytest.mark.unit
async def test_sqlite_storage_get_run_round_trip(tmp_path) -> None:
    """Sanity: the SQLite path produces RunRecords with workflow_run_id /
    node_id columns populated correctly. Touches the migrations path too.
    """
    db = SqliteProvider(db_path=tmp_path / "t.db")
    await db.init()
    r = _make_run(workflow_run_id="wf-123", node_id="first")
    await db.save_run(r)
    got = await db.get_run(r.run_id, tenant_id="local")
    assert got is not None
    assert got.run_id == r.run_id
    assert got.workflow_run_id == "wf-123"
    assert got.node_id == "first"
    await db.close()


# ---------------------------------------------------------------------------
# load_replay dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_load_replay_returns_agent_kind_for_run_id() -> None:
    s = InMemoryStorage()
    await s.init()
    r = _make_run(output={"message": "hi"})
    await s.save_run(r)

    replay = await load_replay(s, r.run_id)
    assert replay.kind == "agent"
    assert replay.run is not None
    assert replay.run.run_id == r.run_id
    assert replay.workflow is None
    assert replay.children is None


@pytest.mark.unit
async def test_load_replay_returns_workflow_kind_with_children() -> None:
    s = InMemoryStorage()
    await s.init()
    wf = _make_workflow_run()
    await s.save_workflow_run(wf)
    # Two child runs linked to the workflow.
    child1 = _make_run(workflow_run_id=wf.workflow_run_id, node_id="first", output={"step1": "x"})
    child2 = _make_run(workflow_run_id=wf.workflow_run_id, node_id="second", output={"step2": "y"})
    other = _make_run()  # noise; unrelated workflow
    await s.save_run(child1)
    await s.save_run(child2)
    await s.save_run(other)

    replay = await load_replay(s, wf.workflow_run_id)
    assert replay.kind == "workflow"
    assert replay.workflow is not None
    assert replay.children is not None
    assert {c.run_id for c in replay.children} == {child1.run_id, child2.run_id}


@pytest.mark.unit
async def test_load_replay_raises_for_unknown_id() -> None:
    s = InMemoryStorage()
    await s.init()
    with pytest.raises(ReplayNotFoundError, match="no run or workflow_run found"):
        await load_replay(s, "ghost-id")


@pytest.mark.unit
async def test_replay_total_cost_and_latency_for_workflow() -> None:
    s = InMemoryStorage()
    await s.init()
    wf = _make_workflow_run()
    await s.save_workflow_run(wf)
    await s.save_run(_make_run(workflow_run_id=wf.workflow_run_id, cost=0.0001))
    await s.save_run(_make_run(workflow_run_id=wf.workflow_run_id, cost=0.0002))

    replay = await load_replay(s, wf.workflow_run_id)
    assert replay.total_cost_usd == pytest.approx(0.0003)
    # Each child has latency_ms=42 in the builder.
    assert replay.total_latency_ms == 84


@pytest.mark.unit
async def test_replay_total_cost_for_agent_is_single_run() -> None:
    s = InMemoryStorage()
    await s.init()
    r = _make_run(cost=0.0005)
    await s.save_run(r)

    replay = await load_replay(s, r.run_id)
    assert replay.total_cost_usd == pytest.approx(0.0005)
    assert replay.total_latency_ms == 42


# ---------------------------------------------------------------------------
# render_replay_json — shape contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_render_replay_json_for_agent() -> None:
    s = InMemoryStorage()
    await s.init()
    r = _make_run(output={"message": "ok"})
    await s.save_run(r)
    replay = await load_replay(s, r.run_id)

    payload = json.loads(render_replay_json(replay))
    assert payload["kind"] == "agent"
    assert payload["run"]["run_id"] == r.run_id
    assert payload["run"]["status"] == "success"
    assert payload["run"]["output"] == {"message": "ok"}
    assert payload["run"]["metrics"]["cost_usd"] == r.metrics.cost_usd


@pytest.mark.unit
async def test_render_replay_json_for_workflow() -> None:
    s = InMemoryStorage()
    await s.init()
    wf = _make_workflow_run(status=WorkflowStatus.ERROR, error_node_id="second")
    await s.save_workflow_run(wf)
    await s.save_run(
        _make_run(workflow_run_id=wf.workflow_run_id, node_id="first", output={"step1": "x"})
    )
    await s.save_run(
        _make_run(
            workflow_run_id=wf.workflow_run_id,
            node_id="second",
            status=JobStatus.ERROR,
        )
    )

    replay = await load_replay(s, wf.workflow_run_id)
    payload = json.loads(render_replay_json(replay))
    assert payload["kind"] == "workflow"
    assert payload["workflow"]["status"] == "error"
    assert payload["workflow"]["error_node_id"] == "second"
    assert len(payload["nodes"]) == 2
    assert payload["nodes"][1]["status"] == "error"


# ---------------------------------------------------------------------------
# truncate helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,max_chars,expected",
    [
        (None, 100, "—"),
        ("short", 100, "short"),
        ("x" * 200, 100, "x" * 99 + "…"),
        ({"k": "v"}, 100, '{"k": "v"}'),
    ],
)
def test_truncate(value, max_chars, expected) -> None:
    assert truncate(value, max_chars=max_chars) == expected
