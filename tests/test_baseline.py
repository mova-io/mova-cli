"""BaselineDiff math + storage round-trip + CLI integration."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.baseline import (
    compute_baseline_diff,
    format_delta,
    regression_summary,
)
from movate.core.models import EvalRecord, JudgeMethod
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_eval(
    *,
    eval_id: str | None = None,
    agent: str = "demo",
    mean_score: float = 0.8,
    pass_rate: float = 0.9,
    sample_count: int = 10,
    total_cost_usd: float = 0.001,
    dataset_hash: str = "abc123" * 6,  # 36-char-ish, but doesn't need to be sha256-shaped
    created_at: datetime | None = None,
) -> EvalRecord:
    return EvalRecord(
        eval_id=eval_id or str(uuid4()),
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        dataset_hash=dataset_hash,
        judge_method=JudgeMethod.EXACT,
        judge_provider=None,
        runs_per_case=1,
        gate_mode="mean",
        threshold=0.7,
        mean_score=mean_score,
        pass_rate=pass_rate,
        sample_count=sample_count,
        total_cost_usd=total_cost_usd,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_in_memory_storage_get_eval_returns_existing() -> None:
    s = InMemoryStorage()
    await s.init()
    target = _make_eval()
    await s.save_eval(target)
    await s.save_eval(_make_eval())  # noise

    got = await s.get_eval(target.eval_id)
    assert got is not None
    assert got.eval_id == target.eval_id


@pytest.mark.unit
async def test_in_memory_storage_get_eval_returns_none_for_missing() -> None:
    s = InMemoryStorage()
    await s.init()
    assert await s.get_eval("ghost") is None


@pytest.mark.unit
async def test_sqlite_storage_get_eval_round_trip(tmp_path: Path) -> None:
    db = SqliteProvider(db_path=tmp_path / "t.db")
    await db.init()
    target = _make_eval()
    await db.save_eval(target)
    got = await db.get_eval(target.eval_id)
    assert got is not None
    assert got.eval_id == target.eval_id
    assert got.mean_score == pytest.approx(target.mean_score)
    await db.close()


# ---------------------------------------------------------------------------
# BaselineDiff math
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_baseline_diff_basic_deltas() -> None:
    base = _make_eval(mean_score=0.7, pass_rate=0.8, sample_count=10, total_cost_usd=0.001)
    cur = _make_eval(mean_score=0.85, pass_rate=0.9, sample_count=12, total_cost_usd=0.0015)

    d = compute_baseline_diff(base, cur)
    assert d.mean_score_delta == pytest.approx(0.15)
    assert d.pass_rate_delta == pytest.approx(0.1)
    assert d.sample_count_delta == 2
    assert d.cost_delta == pytest.approx(0.0005)
    assert d.dataset_changed is False  # same hash by default


@pytest.mark.unit
def test_compute_baseline_diff_rejects_cross_agent() -> None:
    base = _make_eval(agent="alice")
    cur = _make_eval(agent="bob")
    with pytest.raises(ValueError, match="differs from current"):
        compute_baseline_diff(base, cur)


@pytest.mark.unit
def test_baseline_diff_dataset_changed_when_hash_differs() -> None:
    base = _make_eval(dataset_hash="aaa" * 6)
    cur = _make_eval(dataset_hash="bbb" * 6)
    d = compute_baseline_diff(base, cur)
    assert d.dataset_changed is True


@pytest.mark.unit
def test_baseline_diff_age_seconds() -> None:
    older = datetime.now(UTC) - timedelta(hours=2)
    newer = datetime.now(UTC)
    base = _make_eval(created_at=older)
    cur = _make_eval(created_at=newer)
    d = compute_baseline_diff(base, cur)
    # Within ~1s tolerance for test scheduling.
    assert 7195 < d.baseline_age_seconds < 7205


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_regression_when_mean_score_drops() -> None:
    base = _make_eval(mean_score=0.9)
    cur = _make_eval(mean_score=0.7)
    d = compute_baseline_diff(base, cur)
    assert d.is_regression(tolerance=0.0) is True
    # A 0.05 tolerance does NOT cover a 0.2 drop.
    assert d.is_regression(tolerance=0.05) is True
    # Tolerance ≥ drop allows it through.
    assert d.is_regression(tolerance=0.25) is False


@pytest.mark.unit
def test_regression_when_pass_rate_drops() -> None:
    base = _make_eval(pass_rate=1.0)
    cur = _make_eval(pass_rate=0.9)
    d = compute_baseline_diff(base, cur)
    assert d.is_regression(tolerance=0.0) is True
    assert d.is_regression(tolerance=0.2) is False


@pytest.mark.unit
def test_no_regression_when_scores_improve() -> None:
    base = _make_eval(mean_score=0.7, pass_rate=0.8)
    cur = _make_eval(mean_score=0.85, pass_rate=0.9)
    d = compute_baseline_diff(base, cur)
    assert d.is_regression(tolerance=0.0) is False


@pytest.mark.unit
def test_no_regression_when_scores_match() -> None:
    base = _make_eval(mean_score=0.8)
    cur = _make_eval(mean_score=0.8)
    d = compute_baseline_diff(base, cur)
    assert d.is_regression() is False


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,kwargs,expected",
    [
        (0.05, {}, "+0.0500"),
        (-0.123, {}, "-0.1230"),
        (0.0, {}, "+0.0000"),
        (0.05, {"percent": True}, "+5.0%"),
    ],
)
def test_format_delta(value: float, kwargs: dict, expected: str) -> None:
    assert format_delta(value, **kwargs) == expected


@pytest.mark.unit
def test_regression_summary_strings() -> None:
    base = _make_eval(mean_score=0.9, pass_rate=1.0)
    cur = _make_eval(mean_score=0.5, pass_rate=0.6)
    d = compute_baseline_diff(base, cur)
    assert "REGRESSION" in regression_summary(d, tolerance=0.0)
    # With huge tolerance, no regression flagged.
    assert regression_summary(d, tolerance=1.0).startswith("OK")


# ---------------------------------------------------------------------------
# CLI integration — run eval, capture eval_id, re-run with --baseline
# ---------------------------------------------------------------------------


def _scaffold_default_agent(parent: Path) -> Path:
    result = runner.invoke(app, ["init", "demo-agent", "-t", "default", "--target", str(parent)])
    assert result.exit_code == 0, result.stdout
    return parent / "demo-agent"


def _read_latest_eval_id(home: Path) -> str:
    db_path = home / ".movate" / "local.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT eval_id FROM evals ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None, "expected an evals row"
    return row[0]


@pytest.mark.unit
def test_cli_eval_emits_eval_id_in_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    result = runner.invoke(app, ["eval", str(agent_dir), "--mock", "--gate", "0.0", "-o", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "eval_id" in payload
    # No baseline given → no baseline block.
    assert "baseline" not in payload


@pytest.mark.unit
def test_cli_eval_baseline_no_regression_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    # First run → baseline.
    base_result = runner.invoke(
        app, ["eval", str(agent_dir), "--mock", "--gate", "0.0", "-o", "json"]
    )
    assert base_result.exit_code == 0, base_result.stdout
    baseline_id = json.loads(base_result.stdout)["eval_id"]

    # Second run with same mock → identical scores → no regression.
    cur_result = runner.invoke(
        app,
        [
            "eval",
            str(agent_dir),
            "--mock",
            "--gate",
            "0.0",
            "--baseline",
            baseline_id,
            "-o",
            "json",
        ],
    )
    assert cur_result.exit_code == 0, cur_result.stdout
    payload = json.loads(cur_result.stdout)
    assert payload["baseline"]["eval_id"] == baseline_id
    assert payload["baseline"]["mean_score_delta"] == 0.0
    assert payload["baseline"]["regression"] is False


@pytest.mark.unit
def test_cli_eval_baseline_regression_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_dir = _scaffold_default_agent(tmp_path)

    # Baseline: mock response matches the dataset's first case → 0.5 mean (half pass).
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    base = runner.invoke(app, ["eval", str(agent_dir), "--mock", "--gate", "0.0", "-o", "json"])
    assert base.exit_code == 0
    baseline_id = json.loads(base.stdout)["eval_id"]

    # Current run: mock returns wrong shape → score 0.0 → regression.
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "totally wrong"}')
    cur = runner.invoke(
        app,
        [
            "eval",
            str(agent_dir),
            "--mock",
            "--gate",
            "0.0",
            "--baseline",
            baseline_id,
            "-o",
            "json",
        ],
    )
    assert cur.exit_code == 1, cur.stdout
    payload = json.loads(cur.stdout)
    assert payload["baseline"]["regression"] is True
    assert payload["baseline"]["mean_score_delta"] < 0


@pytest.mark.unit
def test_cli_eval_baseline_unknown_id_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    result = runner.invoke(
        app,
        [
            "eval",
            str(agent_dir),
            "--mock",
            "--gate",
            "0.0",
            "--baseline",
            "no-such-id",
        ],
    )
    assert result.exit_code == 2
    assert "not found" in result.stderr


@pytest.mark.unit
def test_cli_eval_baseline_with_tolerance_allows_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small drop within --regression-tolerance should NOT fail the gate."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_dir = _scaffold_default_agent(tmp_path)

    # Baseline → 1.0 mean (mock matches first dataset row exactly).
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    base = runner.invoke(app, ["eval", str(agent_dir), "--mock", "--gate", "0.0", "-o", "json"])
    base_payload = json.loads(base.stdout)
    baseline_id = base_payload["eval_id"]
    baseline_mean = base_payload["mean_score"]

    # Current → drop the mean. Use the same response to simulate a small noise
    # pattern... actually with exact-match the score IS deterministic by mock.
    # Force a regression but allow it via tolerance.
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "different"}')
    cur = runner.invoke(
        app,
        [
            "eval",
            str(agent_dir),
            "--mock",
            "--gate",
            "0.0",
            "--baseline",
            baseline_id,
            "--regression-tolerance",
            "1.0",  # huge tolerance allows any drop
            "-o",
            "json",
        ],
    )
    assert cur.exit_code == 0, cur.stdout
    payload = json.loads(cur.stdout)
    # Confirm there WAS a drop, just not flagged as regression.
    assert payload["baseline"]["mean_score_delta"] < 0
    assert payload["baseline"]["regression"] is False
    _ = baseline_mean  # context for the reader; not asserted
