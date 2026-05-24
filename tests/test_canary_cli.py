"""CLI — ``mdk canary set|status|compare|promote|off`` (ADR 016 D3).

Mirrors tests/test_trigger_cli.py: drive the CLI against a tmp local sqlite
under the ``local`` tenant. Covers set (incl. --auto-promote needing
--eval-gate), status, compare aggregation, promote (challenger→champion,
weight→0), and off (kill switch + --delete).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import FeedbackRecord, JobStatus, Metrics, RunRecord
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "local.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    monkeypatch.setenv("MOVATE_TRACER", "silent")
    return db_path


async def _get_canary(agent: str):
    storage = build_storage()
    await storage.init()
    try:
        return await storage.get_canary_config(agent, tenant_id="local")
    finally:
        await storage.close()


async def _seed_runs_and_feedback() -> None:
    storage = build_storage()
    await storage.init()
    try:
        for version, score in (("1.0.0", 1), ("2.0.0", 1)):
            rid = uuid4().hex
            await storage.save_run(
                RunRecord(
                    run_id=rid,
                    job_id=uuid4().hex,
                    tenant_id="local",
                    agent="bot",
                    agent_version=version,
                    prompt_hash="ph",
                    provider="mock",
                    provider_version="1",
                    pricing_version="1",
                    status=JobStatus.SUCCESS,
                    input={"text": "x"},
                    metrics=Metrics(),
                )
            )
            await storage.save_feedback(
                FeedbackRecord(run_id=rid, tenant_id="local", agent="bot", user_id="u", score=score)
            )
    finally:
        await storage.close()


@pytest.mark.unit
def test_set_then_status(local_db: Path) -> None:
    r = runner.invoke(app, ["canary", "set", "bot", "--challenger", "2.0.0", "--weight", "25"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "25%" in r.stdout

    cfg = asyncio.run(_get_canary("bot"))
    assert cfg is not None
    assert cfg.challenger_version == "2.0.0"
    assert cfg.weight == 25
    assert cfg.sticky is True

    s = runner.invoke(app, ["canary", "status", "bot"])
    assert s.exit_code == 0
    assert "2.0.0" in s.stdout
    assert "25%" in s.stdout


@pytest.mark.unit
def test_set_json_round_trips(local_db: Path) -> None:
    r = runner.invoke(
        app,
        ["canary", "set", "bot", "--challenger", "2.0.0", "--weight", "10", "--format", "json"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["challenger_version"] == "2.0.0"
    assert payload["weight"] == 10


@pytest.mark.unit
def test_auto_promote_without_gate_rejected(local_db: Path) -> None:
    r = runner.invoke(
        app,
        ["canary", "set", "bot", "--challenger", "2.0.0", "--weight", "10", "--auto-promote"],
    )
    assert r.exit_code == 2
    assert "eval-gate" in r.stderr


@pytest.mark.unit
def test_status_no_canary_is_friendly(local_db: Path) -> None:
    r = runner.invoke(app, ["canary", "status", "ghost"])
    assert r.exit_code == 0
    assert "no canary" in r.stdout


@pytest.mark.unit
def test_off_kill_switch_sets_weight_zero(local_db: Path) -> None:
    runner.invoke(app, ["canary", "set", "bot", "--challenger", "2.0.0", "--weight", "50"])
    r = runner.invoke(app, ["canary", "off", "bot"])
    assert r.exit_code == 0, r.stdout + r.stderr
    cfg = asyncio.run(_get_canary("bot"))
    assert cfg is not None
    assert cfg.weight == 0


@pytest.mark.unit
def test_off_delete_removes_row(local_db: Path) -> None:
    runner.invoke(app, ["canary", "set", "bot", "--challenger", "2.0.0", "--weight", "50"])
    r = runner.invoke(app, ["canary", "off", "bot", "--delete"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert asyncio.run(_get_canary("bot")) is None
    # Friendly no-op when already gone.
    again = runner.invoke(app, ["canary", "off", "bot", "--delete"])
    assert again.exit_code == 0


@pytest.mark.unit
def test_promote_sets_champion_and_zeroes_weight(local_db: Path) -> None:
    runner.invoke(
        app,
        [
            "canary",
            "set",
            "bot",
            "--challenger",
            "2.0.0",
            "--champion",
            "1.0.0",
            "--weight",
            "50",
        ],
    )
    r = runner.invoke(app, ["canary", "promote", "bot"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "2.0.0" in r.stdout
    cfg = asyncio.run(_get_canary("bot"))
    assert cfg is not None
    assert cfg.champion_version == "2.0.0"
    assert cfg.weight == 0


@pytest.mark.unit
def test_compare_renders_split(local_db: Path) -> None:
    runner.invoke(
        app,
        [
            "canary",
            "set",
            "bot",
            "--challenger",
            "2.0.0",
            "--champion",
            "1.0.0",
            "--weight",
            "50",
        ],
    )
    asyncio.run(_seed_runs_and_feedback())
    r = runner.invoke(app, ["canary", "compare", "bot", "--format", "json"])
    assert r.exit_code == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["champion"]["version"] == "1.0.0"
    assert payload["challenger"]["version"] == "2.0.0"
    assert payload["champion"]["run_count"] == 1
    assert payload["challenger"]["run_count"] == 1
