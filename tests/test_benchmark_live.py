"""Sprint S — `mdk benchmark live` tests.

Three layers:

1. **Pure helpers** — _outputs_equal / summarize roll-up.
2. **Storage path** — _fetch_runs filters by since_days correctly.
3. **CLI** — end-to-end with --mock against a seeded SQLite DB.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.benchmark_cmd import (
    ReplayRow,
    _outputs_equal,
    summarize,
)
from movate.cli.main import app
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage import SqliteProvider

runner = CliRunner(mix_stderr=False)

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


def _scaffold_agent(dst: Path, name: str = "demo") -> Path:
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _scaffold_agent(tmp_path / "agents" / "demo", name="demo")
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "test.db"))
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutputsEqual:
    def test_equal_dicts_same_keys_different_order(self) -> None:
        assert _outputs_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})

    def test_unequal_dicts(self) -> None:
        assert not _outputs_equal({"a": 1}, {"a": 2})

    def test_none_treated_as_empty(self) -> None:
        assert _outputs_equal(None, {})


@pytest.mark.unit
class TestSummarize:
    def test_empty_rows_returns_zero_n(self) -> None:
        s = summarize([])
        assert s.n == 0
        assert s.match_rate == 0.0

    def test_aggregates_costs_and_latency(self) -> None:
        rows = [
            ReplayRow(
                run_id="a",
                original_provider="p",
                original_cost=0.10,
                original_latency_ms=100,
                candidate_cost=0.05,
                candidate_latency_ms=80,
                outputs_match=True,
            ),
            ReplayRow(
                run_id="b",
                original_provider="p",
                original_cost=0.20,
                original_latency_ms=200,
                candidate_cost=0.10,
                candidate_latency_ms=150,
                outputs_match=False,
            ),
        ]
        s = summarize(rows)
        assert s.n == 2
        assert abs(s.match_rate - 0.5) < 1e-9
        assert abs(s.mean_original_cost - 0.15) < 1e-9
        assert abs(s.mean_candidate_cost - 0.075) < 1e-9

    def test_errors_counted_separately(self) -> None:
        rows = [
            ReplayRow(
                run_id="a",
                original_provider="p",
                original_cost=0.10,
                original_latency_ms=100,
                candidate_cost=0.0,
                candidate_latency_ms=0,
                outputs_match=False,
                error="boom",
            )
        ]
        s = summarize(rows)
        assert s.errors == 1


# ---------------------------------------------------------------------------
# CLI: end-to-end with --mock
# ---------------------------------------------------------------------------


def _seed_run(db_path: Path, run_id: str = "r-1", agent: str = "demo") -> RunRecord:
    rec = RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="h",
        provider="openai/gpt-4o-mini",
        provider_version="0",
        pricing_version="2026-05",
        status=JobStatus.SUCCESS,
        input={"text": "hello"},
        output={"message": "ok"},
        metrics=Metrics(
            cost_usd=0.01,
            tokens=TokenUsage(input=10, output=5),
            provider="openai/gpt-4o-mini",
        ),
        created_at=datetime.now(UTC),
    )

    async def _save() -> None:
        p = SqliteProvider(db_path=str(db_path))
        await p.init()
        try:
            await p.save_run(rec)
        finally:
            await p.close()

    asyncio.run(_save())
    return rec


@pytest.fixture
def db_with_runs(project: Path, tmp_path: Path) -> Path:
    """Seed 3 successful runs."""
    db_path = tmp_path / "test.db"
    for i in range(3):
        _seed_run(db_path, run_id=f"r-{i}")
    return db_path


@pytest.mark.unit
def test_cli_benchmark_live_runs_with_mock(db_with_runs: Path, project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "benchmark",
            "live",
            "demo",
            "--candidate-model",
            "anthropic/claude-haiku-4-5-20251001",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Summary table renders
    assert "Shadow benchmark" in result.stdout
    # Match rate column appears
    assert "match rate" in result.stdout.lower()


@pytest.mark.unit
def test_cli_benchmark_live_empty_db_prints_hint(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No runs → friendly message, not an error."""
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "empty.db"))
    result = runner.invoke(
        app,
        [
            "benchmark",
            "live",
            "demo",
            "--candidate-model",
            "openai/gpt-4o-mini",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    assert "no recorded" in result.stdout.lower()


@pytest.mark.unit
def test_cli_benchmark_live_json_output(db_with_runs: Path, project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "benchmark",
            "live",
            "demo",
            "--candidate-model",
            "openai/gpt-4o-mini",
            "--mock",
            "--json",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["candidate_model"] == "openai/gpt-4o-mini"
    assert data["n"] == 3
    assert "rows" in data
    assert len(data["rows"]) == 3


@pytest.mark.unit
def test_cli_benchmark_live_limit_validation(db_with_runs: Path, project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "benchmark",
            "live",
            "demo",
            "--candidate-model",
            "x",
            "--limit",
            "0",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_benchmark_live_since_days_filters_out_old_runs(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old run should be filtered by --since-days."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    async def _seed_old() -> None:
        old = RunRecord(
            run_id="old-1",
            job_id="j-old",
            tenant_id="local",
            agent="demo",
            agent_version="0.1.0",
            prompt_hash="h",
            provider="openai/gpt-4o-mini",
            provider_version="0",
            pricing_version="2026-05",
            status=JobStatus.SUCCESS,
            input={"text": "old"},
            output={"message": "old"},
            metrics=Metrics(
                cost_usd=0.01,
                tokens=TokenUsage(input=10, output=5),
                provider="openai/gpt-4o-mini",
            ),
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
        p = SqliteProvider(db_path=str(db_path))
        await p.init()
        try:
            await p.save_run(old)
        finally:
            await p.close()

    asyncio.run(_seed_old())

    result = runner.invoke(
        app,
        [
            "benchmark",
            "live",
            "demo",
            "--candidate-model",
            "x",
            "--since-days",
            "7",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    # The 30-day-old run is filtered → "no recorded" message
    assert "no recorded" in result.stdout.lower()
