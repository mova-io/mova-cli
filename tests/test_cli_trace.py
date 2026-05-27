"""``movate trace replay`` CLI integration.

Drives the full CLI path through ``CliRunner`` against a real
``SqliteProvider`` rooted in ``tmp_path`` (via ``HOME=tmp_path`` so the
default ``~/.movate/local.db`` resolves into the sandbox).

Each test seeds the DB by calling the CLI runner first, then replays.
Avoids touching the user's actual ``~/.movate/local.db``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    SkillCallRecord,
    TokenUsage,
    TurnRecord,
)
from movate.storage.sqlite import SqliteProvider

# mix_stderr=False keeps the stdout-tracer's NDJSON spans out of the JSON
# we read for assertions.
runner = CliRunner(mix_stderr=False)


def _make_default_agent(parent: Path, name: str = "demo-agent") -> Path:
    """Scaffold a minimal default-template agent under ``parent/<name>``."""
    result = runner.invoke(app, ["init", name, "-t", "default", "--target", str(parent)])
    assert result.exit_code == 0, result.stdout
    return parent / name


def _make_workflow(parent: Path) -> Path:
    """A single-node workflow wrapping a default-template agent."""
    _make_default_agent(parent, name="only-agent")
    wf_dir = parent / "wf"
    wf_dir.mkdir()
    (wf_dir / "state.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
            }
        )
    )
    (wf_dir / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "smoke-pipeline",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "first",
                "nodes": [{"id": "first", "type": "agent", "ref": "../only-agent"}],
                "edges": [],
            }
        )
    )
    return wf_dir


# ---------------------------------------------------------------------------
# Replay an agent run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_agent_run_after_movate_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "hi"}')

    agent_dir = _make_default_agent(tmp_path)
    run_result = runner.invoke(
        app, ["run", str(agent_dir), '{"text": "seed"}', "--mock", "-o", "json"]
    )
    assert run_result.exit_code == 0, run_result.stdout
    # Trace ID is in the run response (RunResponse.trace_id).
    response = json.loads(run_result.stdout)
    # We need the run_id, which lives in storage but is NOT returned in the
    # default `movate run` response. Pull it from `list_runs` via a follow-up
    # query: the most recent agent run for "demo-agent" is the one we just made.
    # Instead of plumbing list-runs through the CLI, use the trace_id which
    # is also a unique id per run.
    _ = response  # confirm the run completed

    # Read the DB directly to find the run_id we just persisted.

    db_path = tmp_path / ".movate" / "local.db"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None
    run_id = row[0]

    replay_result = runner.invoke(app, ["trace", "replay", run_id, "-o", "json"])
    assert replay_result.exit_code == 0, replay_result.stdout
    payload = json.loads(replay_result.stdout)
    assert payload["kind"] == "agent"
    assert payload["run"]["run_id"] == run_id
    assert payload["run"]["status"] == "success"
    assert payload["run"]["output"] == {"message": "hi"}


# ---------------------------------------------------------------------------
# Replay a workflow run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_workflow_run_after_movate_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "ok"}')

    wf_dir = _make_workflow(tmp_path)
    run_result = runner.invoke(
        app, ["run", str(wf_dir), '{"text": "seed"}', "--mock", "-o", "json"]
    )
    assert run_result.exit_code == 0, run_result.stdout
    payload = json.loads(run_result.stdout)
    workflow_run_id = payload["workflow_run_id"]

    replay_result = runner.invoke(app, ["trace", "replay", workflow_run_id, "-o", "json"])
    assert replay_result.exit_code == 0, replay_result.stdout
    replay_payload = json.loads(replay_result.stdout)
    assert replay_payload["kind"] == "workflow"
    assert replay_payload["workflow"]["status"] == "success"
    assert replay_payload["workflow"]["workflow_run_id"] == workflow_run_id
    # One per-node run linked.
    assert len(replay_payload["nodes"]) == 1
    assert replay_payload["nodes"][0]["node_id"] == "first"
    assert replay_payload["nodes"][0]["workflow_run_id"] == workflow_run_id


# ---------------------------------------------------------------------------
# Unknown id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_unknown_id_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["trace", "replay", "no-such-id"])
    assert result.exit_code == 1
    assert "no run or workflow_run found" in result.stderr


# ---------------------------------------------------------------------------
# Table output renders without crashing on a real run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_table_output_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "hi"}')

    agent_dir = _make_default_agent(tmp_path)
    runner.invoke(app, ["run", str(agent_dir), '{"text": "seed"}', "--mock"])

    db_path = tmp_path / ".movate" / "local.db"
    with sqlite3.connect(db_path) as conn:
        run_id = conn.execute(
            "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]

    result = runner.invoke(app, ["trace", "replay", run_id])
    assert result.exit_code == 0, result.stdout
    assert "trace replay" in result.stdout
    assert "demo-agent" in result.stdout
    assert "✓ SUCCESS" in result.stdout


# ---------------------------------------------------------------------------
# ADR 024 PR 2 (#101): `mdk trace replay` renders the per-step execution tree
# (turns → skill/retrieval children) for the replayed agent run.
# ---------------------------------------------------------------------------


def _seed_multi_turn_run(db_path: Path) -> str:
    """Persist a multi-turn, tool-using RunRecord straight into a sandbox DB.

    Returns the run_id. Bypasses a full `mdk run` so the test can exercise a
    record that carries `turns` + per-turn `skill_calls` (a mock run is
    single-turn / no-skill).
    """
    run_id = "11112222-3333-4444-5555-666677778888"
    record = RunRecord(
        run_id=run_id,
        job_id="job-trace",
        tenant_id="local",
        agent="kb-agent",
        agent_version="0.1.0",
        prompt_hash="hash",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2026",
        status=JobStatus.SUCCESS,
        input={"question": "refund window?"},
        output={"answer": "30 days"},
        metrics=Metrics(
            latency_ms=140,
            cost_usd=0.000031,
            tokens=TokenUsage(input=420, output=95),
            provider="openai/gpt-4o-mini-2024-07-18",
        ),
        created_at=datetime(2026, 5, 26, 9, 0, 0, tzinfo=UTC),
        turns=[
            TurnRecord(
                index=1,
                model="openai/gpt-4o-mini-2024-07-18",
                input_tokens=220,
                output_tokens=30,
                cost_usd=0.000012,
                latency_ms=55,
                finish_reason="tool_use",
            ),
            TurnRecord(
                index=2,
                model="openai/gpt-4o-mini-2024-07-18",
                input_tokens=200,
                output_tokens=65,
                cost_usd=0.000019,
                latency_ms=85,
                finish_reason="final",
            ),
        ],
        skill_calls=[
            SkillCallRecord(
                step=1,
                skill="retrieval.kb-vector-lookup",
                input={"query": "refund window"},
                output={"chunks": []},
                latency_ms=18.0,
                cost_usd=0.0,
                turn=1,
            ),
        ],
    )

    async def _persist() -> None:
        store = SqliteProvider(db_path)
        await store.init()
        await store.save_run(record)
        await store.close()

    asyncio.run(_persist())
    return run_id


@pytest.mark.unit
def test_replay_renders_execution_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk trace replay <id>` renders the turn → skill/retrieval tree."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".movate" / "local.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = _seed_multi_turn_run(db_path)

    result = runner.invoke(app, ["trace", "replay", run_id])

    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "Execution tree" in out
    assert "turn 1" in out
    assert "turn 2" in out
    # Retrieval call nests under its turn as a retrieval node.
    assert "retrieval.kb-vector-lookup" in out
    # Per-node latency surfaced (turn 1 latency).
    assert "55 ms" in out
