"""Sprint Q — `mdk costs report` tests.

Three layers:

1. **Aggregation** — :func:`_rollup_runs` correctly groups by
   agent/provider and tallies cost, runs, tokens; sorts by spend.
2. **Filtering** — :func:`_filter_by_since` drops runs older than
   the cutoff (and is a no-op for ``days <= 0``).
3. **CLI** — `mdk costs report` writes a table from a populated
   SQLite DB; --by / --agent / --json behave as expected.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.costs_cmd import (
    CostRollup,
    _filter_by_since,
    _rollup_runs,
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


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _run(
    *,
    agent: str = "triage",
    provider: str = "openai/gpt-4o-mini",
    cost: float = 0.001,
    tokens_in: int = 100,
    tokens_out: int = 50,
    when: datetime | None = None,
) -> RunRecord:
    """Build a minimal RunRecord for aggregation tests."""
    return RunRecord(
        run_id="r1",
        job_id="j1",
        tenant_id="t1",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="hash",
        provider=provider,
        provider_version="v1",
        pricing_version="2026-05",
        status=JobStatus.SUCCESS,
        input={"q": "x"},
        output={"a": "y"},
        metrics=Metrics(
            cost_usd=cost,
            tokens=TokenUsage(input=tokens_in, output=tokens_out),
            provider=provider,
        ),
        created_at=when or datetime.now(UTC),
    )


@pytest.mark.unit
class TestRollupRuns:
    def test_groups_by_agent(self) -> None:
        runs = [
            _run(agent="triage", cost=0.10),
            _run(agent="triage", cost=0.05),
            _run(agent="summary", cost=0.02),
        ]
        rollups = _rollup_runs(runs, group_by="agent")
        # Sorted by total cost desc → triage first
        assert rollups[0].key == "triage"
        assert rollups[0].runs == 2
        assert abs(rollups[0].total_cost_usd - 0.15) < 1e-9
        assert rollups[1].key == "summary"
        assert rollups[1].runs == 1

    def test_groups_by_provider(self) -> None:
        runs = [
            _run(provider="openai/gpt-4o-mini", cost=0.01),
            _run(provider="openai/gpt-4o-mini", cost=0.01),
            _run(provider="anthropic/claude-haiku", cost=0.005),
        ]
        rollups = _rollup_runs(runs, group_by="provider")
        keys = {r.key for r in rollups}
        assert "openai/gpt-4o-mini" in keys
        assert "anthropic/claude-haiku" in keys

    def test_aggregates_tokens(self) -> None:
        runs = [
            _run(tokens_in=100, tokens_out=50),
            _run(tokens_in=200, tokens_out=80),
        ]
        rollups = _rollup_runs(runs, group_by="agent")
        assert rollups[0].total_tokens_in == 300
        assert rollups[0].total_tokens_out == 130

    def test_mean_cost_computed_correctly(self) -> None:
        runs = [_run(cost=0.10), _run(cost=0.05), _run(cost=0.03)]
        rollups = _rollup_runs(runs, group_by="agent")
        assert abs(rollups[0].mean_cost_usd - 0.06) < 1e-9

    def test_empty_runs_returns_empty_list(self) -> None:
        assert _rollup_runs([], group_by="agent") == []

    def test_sorts_by_total_cost_desc(self) -> None:
        runs = [
            _run(agent="cheap", cost=0.001),
            _run(agent="expensive", cost=10.0),
            _run(agent="medium", cost=1.0),
        ]
        rollups = _rollup_runs(runs, group_by="agent")
        keys = [r.key for r in rollups]
        assert keys == ["expensive", "medium", "cheap"]

    def test_unknown_agent_handled(self) -> None:
        """An empty agent string shouldn't break the bucket; bucketed under a placeholder."""
        runs = [_run(agent="")]
        rollups = _rollup_runs(runs, group_by="agent")
        assert len(rollups) == 1
        assert "unknown" in rollups[0].key.lower()


# ---------------------------------------------------------------------------
# Filtering by date
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFilterBySince:
    def test_zero_days_is_noop(self) -> None:
        runs = [_run()]
        assert _filter_by_since(runs, 0) == runs

    def test_negative_days_is_noop(self) -> None:
        runs = [_run()]
        assert _filter_by_since(runs, -5) == runs

    def test_filters_old_runs(self) -> None:
        recent = _run(when=datetime.now(UTC) - timedelta(days=1))
        old = _run(when=datetime.now(UTC) - timedelta(days=30))
        filtered = _filter_by_since([recent, old], 7)
        assert len(filtered) == 1
        # Recent survives
        assert filtered[0].created_at == recent.created_at

    def test_naive_datetime_coerced_to_utc(self) -> None:
        """Some SQLite rows hand us naive datetimes; the filter must
        still work without crashing."""
        # Build a run with a naive datetime older than the cutoff
        naive_recent = _run(when=datetime.now(UTC) - timedelta(hours=1))
        # Replace with a naive datetime (simulating SQLite quirks)
        naive_recent = naive_recent.model_copy(
            update={"created_at": datetime.now() - timedelta(hours=1)}
        )
        result = _filter_by_since([naive_recent], 7)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# CostRollup dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cost_rollup_mean_with_zero_runs_is_zero() -> None:
    r = CostRollup(key="x", runs=0, total_cost_usd=0.0, total_tokens_in=0, total_tokens_out=0)
    assert r.mean_cost_usd == 0.0


# ---------------------------------------------------------------------------
# CLI — end-to-end through a populated SQLite DB
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Spin up a temp SQLite DB with three runs across two agents.

    Sets MOVATE_DB so `build_storage()` picks the temp file.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    async def _seed() -> None:
        provider = SqliteProvider(db_path=str(db_path))
        await provider.init()
        try:
            for i in range(2):
                await provider.save_run(
                    _run(agent="triage", cost=0.05 + i * 0.01).model_copy(
                        update={"run_id": f"triage-{i}", "job_id": f"j-{i}"}
                    )
                )
            await provider.save_run(
                _run(agent="summary", cost=0.02).model_copy(
                    update={"run_id": "summary-1", "job_id": "j-summary"}
                )
            )
        finally:
            await provider.close()

    asyncio.run(_seed())
    return db_path


@pytest.mark.unit
def test_cli_report_shows_per_agent_rollup(populated_db: Path) -> None:
    result = runner.invoke(app, ["costs", "report"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Both agents appear in the table
    assert "triage" in result.stdout
    assert "summary" in result.stdout


@pytest.mark.unit
def test_cli_report_by_provider(populated_db: Path) -> None:
    result = runner.invoke(app, ["costs", "report", "--by", "provider"])
    assert result.exit_code == 0
    # The provider key appears
    assert "openai" in result.stdout.lower()


@pytest.mark.unit
def test_cli_report_filter_to_one_agent(populated_db: Path) -> None:
    result = runner.invoke(app, ["costs", "report", "--agent", "triage"])
    assert result.exit_code == 0
    assert "triage" in result.stdout
    # `summary` shouldn't show up under triage filter
    assert "summary" not in result.stdout


@pytest.mark.unit
def test_cli_report_json_output(populated_db: Path) -> None:
    result = runner.invoke(app, ["costs", "report", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["group_by"] == "agent"
    assert data["total_runs"] == 3
    # `rollups` is a sorted list of group summaries
    rollup_keys = {r["key"] for r in data["rollups"]}
    assert rollup_keys == {"triage", "summary"}


@pytest.mark.unit
def test_cli_report_empty_db_prints_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No runs recorded yet → friendly message, not an error."""
    db_path = tmp_path / "empty.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    result = runner.invoke(app, ["costs", "report"])
    assert result.exit_code == 0
    assert "no runs" in result.stdout.lower()


@pytest.mark.unit
def test_cli_report_invalid_by_exits_2(populated_db: Path) -> None:
    result = runner.invoke(app, ["costs", "report", "--by", "bogus"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_report_since_days_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old run should be filtered out by --since-days."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    old = _run(agent="ancient", when=datetime.now(UTC) - timedelta(days=30))
    recent = _run(agent="recent", when=datetime.now(UTC) - timedelta(hours=1))

    async def _seed() -> None:
        provider = SqliteProvider(db_path=str(db_path))
        await provider.init()
        try:
            await provider.save_run(old.model_copy(update={"run_id": "old", "job_id": "j-old"}))
            await provider.save_run(
                recent.model_copy(update={"run_id": "recent", "job_id": "j-recent"})
            )
        finally:
            await provider.close()

    asyncio.run(_seed())

    result = runner.invoke(app, ["costs", "report", "--since-days", "7", "--json"])
    data = json.loads(result.stdout)
    keys = {r["key"] for r in data["rollups"]}
    assert "recent" in keys
    assert "ancient" not in keys
