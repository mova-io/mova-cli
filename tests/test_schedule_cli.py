"""CLI — ``mdk schedule set|list|clear`` + ``mdk scheduler-tick`` (ADR 017 D2).

Covers:

* ``mdk schedule set`` upserts a generic agent/workflow schedule under the
  local tenant (and ``list`` shows it; ``--format json`` round-trips).
* ``--name`` overrides the handle; ``--input`` parses a JSON payload;
  ``--disabled`` creates a dormant schedule; ``--kind eval`` is rejected.
* ``mdk schedule clear`` removes the schedule.
* ``mdk scheduler-tick`` is the unified tick — it enqueues a job for a due
  generic schedule (and drains eval schedules too, but here we assert the
  generic path).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import JobKind
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's sqlite at a tmp file (+ silence tracer)."""
    db_path = tmp_path / "local.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    monkeypatch.setenv("MOVATE_TRACER", "silent")
    return db_path


async def _list_schedules() -> list:
    storage = build_storage()
    await storage.init()
    try:
        return await storage.list_job_schedules(tenant_id="local")
    finally:
        await storage.close()


async def _list_jobs() -> list:
    storage = build_storage()
    await storage.init()
    try:
        return await storage.list_jobs(tenant_id="local")
    finally:
        await storage.close()


@pytest.mark.unit
def test_set_then_list(local_db: Path) -> None:
    r = runner.invoke(
        app,
        ["schedule", "set", "faq-agent", "--cadence", "6h", "--input", '{"text": "hi"}'],
    )
    assert r.exit_code == 0, r.stdout + r.stderr

    rows = asyncio.run(_list_schedules())
    assert len(rows) == 1
    assert rows[0].name == "faq-agent"
    assert rows[0].kind == JobKind.AGENT
    assert rows[0].target == "faq-agent"
    assert rows[0].cadence_seconds == 21600
    assert rows[0].input == {"text": "hi"}
    assert rows[0].enabled is True

    lst = runner.invoke(app, ["schedule", "list"])
    assert lst.exit_code == 0
    assert "faq-agent" in lst.stdout


@pytest.mark.unit
def test_set_workflow_with_name_and_disabled(local_db: Path) -> None:
    r = runner.invoke(
        app,
        [
            "schedule",
            "set",
            "returns-pipeline",
            "--kind",
            "workflow",
            "--cadence",
            "1d",
            "--name",
            "nightly-returns",
            "--disabled",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    rows = asyncio.run(_list_schedules())
    assert len(rows) == 1
    assert rows[0].name == "nightly-returns"
    assert rows[0].kind == JobKind.WORKFLOW
    assert rows[0].cadence_seconds == 86400
    assert rows[0].enabled is False


@pytest.mark.unit
def test_set_cron_round_trip(local_db: Path) -> None:
    """--cron + --tz persist and round-trip (ADR 100 D1)."""
    r = runner.invoke(
        app,
        [
            "schedule",
            "set",
            "exec-briefing",
            "--kind",
            "workflow",
            "--cron",
            "0 7 * * 1-5",
            "--tz",
            "America/New_York",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr

    rows = asyncio.run(_list_schedules())
    assert len(rows) == 1
    assert rows[0].cron == "0 7 * * 1-5"
    assert rows[0].timezone == "America/New_York"
    assert rows[0].cadence_seconds == 0  # the cron sentinel
    assert rows[0].kind == JobKind.WORKFLOW

    # The table view renders the cron form (Rich may wrap/truncate the wide
    # cell, so assert via the machine-readable list).
    lst = runner.invoke(app, ["schedule", "list", "--format", "json"])
    assert lst.exit_code == 0
    listed = json.loads(lst.stdout)
    assert listed[0]["cron"] == "0 7 * * 1-5"
    assert listed[0]["timezone"] == "America/New_York"


@pytest.mark.unit
def test_set_cron_json_output(local_db: Path) -> None:
    r = runner.invoke(
        app,
        ["schedule", "set", "faq", "--cron", "0 7 * * *", "--format", "json"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["cron"] == "0 7 * * *"
    assert payload["timezone"] is None
    assert payload["cadence_seconds"] == 0


@pytest.mark.unit
def test_set_rejects_both_cadence_and_cron(local_db: Path) -> None:
    r = runner.invoke(
        app,
        ["schedule", "set", "faq", "--cadence", "1h", "--cron", "0 7 * * *"],
    )
    assert r.exit_code == 2
    assert "exactly one" in r.stderr


@pytest.mark.unit
def test_set_rejects_neither_cadence_nor_cron(local_db: Path) -> None:
    r = runner.invoke(app, ["schedule", "set", "faq"])
    assert r.exit_code == 2
    assert "exactly one" in r.stderr


@pytest.mark.unit
def test_set_rejects_tz_without_cron(local_db: Path) -> None:
    r = runner.invoke(
        app,
        ["schedule", "set", "faq", "--cadence", "1h", "--tz", "America/New_York"],
    )
    assert r.exit_code == 2
    assert "--tz" in r.stderr


@pytest.mark.unit
def test_set_rejects_invalid_cron_expression(local_db: Path) -> None:
    r = runner.invoke(app, ["schedule", "set", "faq", "--cron", "99 99 * * *"])
    assert r.exit_code == 2
    assert "invalid cron expression" in r.stderr
    assert asyncio.run(_list_schedules()) == []


@pytest.mark.unit
def test_set_rejects_eval_kind(local_db: Path) -> None:
    r = runner.invoke(app, ["schedule", "set", "faq", "--kind", "eval", "--cadence", "1h"])
    assert r.exit_code == 2
    assert "agent" in r.stderr and "workflow" in r.stderr


@pytest.mark.unit
def test_set_json_output(local_db: Path) -> None:
    r = runner.invoke(app, ["schedule", "set", "faq", "--cadence", "300", "--format", "json"])
    assert r.exit_code == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["name"] == "faq"
    assert payload["cadence_seconds"] == 300


@pytest.mark.unit
def test_clear_removes_schedule(local_db: Path) -> None:
    runner.invoke(app, ["schedule", "set", "faq", "--cadence", "1h", "--name", "s1"])
    assert len(asyncio.run(_list_schedules())) == 1
    r = runner.invoke(app, ["schedule", "clear", "s1"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert asyncio.run(_list_schedules()) == []
    # Clearing a missing schedule is a friendly no-op (exit 0).
    again = runner.invoke(app, ["schedule", "clear", "s1"])
    assert again.exit_code == 0


@pytest.mark.unit
def test_scheduler_tick_enqueues_due_generic_job(local_db: Path) -> None:
    # A never-enqueued schedule is immediately due.
    runner.invoke(
        app,
        ["schedule", "set", "faq-agent", "--cadence", "1h", "--input", '{"text": "hi"}'],
    )
    r = runner.invoke(app, ["scheduler-tick"])
    assert r.exit_code == 0, r.stdout + r.stderr

    jobs = asyncio.run(_list_jobs())
    agent_jobs = [j for j in jobs if j.kind == JobKind.AGENT]
    assert len(agent_jobs) == 1
    assert agent_jobs[0].target == "faq-agent"
    assert agent_jobs[0].input == {"text": "hi"}


@pytest.mark.unit
def test_scheduler_tick_no_schedules_is_noop(local_db: Path) -> None:
    r = runner.invoke(app, ["scheduler-tick"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert asyncio.run(_list_jobs()) == []
