"""Sprint Q — `mdk monitor` tests.

Three layers:

1. **Helpers** — _short_run_id / _short_time / _short_provider produce
   compact display strings.
2. **render_dashboard** — pure function: list[RunRecord] → Rich Table
   with the expected columns + status colorization.
3. **CLI** — `mdk monitor --once` smokes the dashboard against a
   populated SQLite DB; flag validation surfaces operator errors
   without spinning up the live loop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.cli.monitor_cmd import (
    _short_provider,
    _short_run_id,
    _short_time,
    render_dashboard,
)
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage import SqliteProvider

runner = CliRunner(mix_stderr=False)


# CliRunner's default terminal is 80 cols. The 8-column monitor dashboard
# wraps badly there + tests can't grep agent names. We pass a wider
# COLUMNS env var so Rich auto-detects a sane width.
_WIDE_ENV = {"COLUMNS": "200"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShortRunId:
    def test_truncates_long_id(self) -> None:
        assert _short_run_id("abcdef0123456789") == "abcdef01"

    def test_keeps_short_id_unchanged(self) -> None:
        assert _short_run_id("abc") == "abc"

    def test_handles_empty(self) -> None:
        assert _short_run_id("") == ""


@pytest.mark.unit
class TestShortTime:
    def test_none_renders_dash(self) -> None:
        assert _short_time(None) == "—"

    def test_renders_hms_only(self) -> None:
        ts = datetime(2026, 5, 15, 14, 30, 45, tzinfo=UTC)
        out = _short_time(ts)
        # We want HH:MM:SS — no date, no microseconds.
        assert out == "14:30:45"


@pytest.mark.unit
class TestShortProvider:
    def test_keeps_short(self) -> None:
        assert _short_provider("openai") == "openai"

    def test_truncates_long(self) -> None:
        long = "x" * 100
        out = _short_provider(long, max_chars=10)
        assert len(out) == 10
        assert out.endswith("…")


# ---------------------------------------------------------------------------
# render_dashboard
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: str = "r-test-abcdef0123",
    agent: str = "triage",
    status: JobStatus = JobStatus.SUCCESS,
    cost: float = 0.001,
    provider: str = "openai/gpt-4o-mini",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id="j-1",
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="h",
        provider=provider,
        provider_version="0",
        pricing_version="2026-05",
        status=status,
        input={"x": 1},
        output={"y": 2},
        metrics=Metrics(
            cost_usd=cost,
            tokens=TokenUsage(input=100, output=50),
            provider=provider,
        ),
        created_at=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    )


@pytest.mark.unit
def test_render_dashboard_empty_uses_placeholder_row() -> None:
    """Empty input → one row of dashes, not a zero-row table (Rich's
    empty table renders weirdly + operators want to see they're
    pointed at the right DB)."""
    table = render_dashboard([])
    # row count == 1 for the placeholder
    assert table.row_count == 1


@pytest.mark.unit
def test_render_dashboard_one_run_per_row() -> None:
    runs = [_make_run(run_id="a"), _make_run(run_id="b"), _make_run(run_id="c")]
    table = render_dashboard(runs)
    assert table.row_count == 3


@pytest.mark.unit
def test_render_dashboard_has_expected_columns() -> None:
    table = render_dashboard([_make_run()])
    column_headers = [c.header for c in table.columns]
    assert column_headers == [
        "Time",
        "Run",
        "Agent",
        "Status",
        "Provider",
        "Cost ($)",
        "Latency (ms)",
        "Tokens in/out",
    ]


# ---------------------------------------------------------------------------
# CLI: --once
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a SQLite DB with three runs and point MOVATE_DB at it."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    async def _seed() -> None:
        p = SqliteProvider(db_path=str(db_path))
        await p.init()
        try:
            for i, agent in enumerate(["triage", "summary", "triage"]):
                await p.save_run(
                    _make_run(
                        run_id=f"r-{i}",
                        agent=agent,
                        cost=0.001 * (i + 1),
                    ).model_copy(update={"job_id": f"j-{i}"})
                )
        finally:
            await p.close()

    asyncio.run(_seed())
    return db_path


@pytest.mark.unit
def test_cli_monitor_once_renders_table(db_with_runs: Path) -> None:
    result = runner.invoke(app, ["monitor", "--once"], env=_WIDE_ENV)
    assert result.exit_code == 0, result.stdout + result.stderr
    # Both agents appear
    assert "triage" in result.stdout
    assert "summary" in result.stdout


@pytest.mark.unit
def test_cli_monitor_once_filters_by_agent(db_with_runs: Path) -> None:
    result = runner.invoke(
        app, ["monitor", "--once", "--agent", "summary"], env=_WIDE_ENV
    )
    assert result.exit_code == 0
    assert "summary" in result.stdout
    # `triage` filtered out
    assert "triage" not in result.stdout


@pytest.mark.unit
def test_cli_monitor_once_empty_db_renders_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty DB should render the placeholder row, not crash."""
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "empty.db"))
    result = runner.invoke(app, ["monitor", "--once"])
    assert result.exit_code == 0
    # Placeholder dash appears
    assert "—" in result.stdout


@pytest.mark.unit
def test_cli_monitor_title_reflects_filters(db_with_runs: Path) -> None:
    """The title row should mention --agent / --status if set, so the
    operator at-a-glance knows what they're tailing."""
    result = runner.invoke(
        app,
        ["monitor", "--once", "--agent", "triage", "--limit", "5"],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 0
    # Title row includes the filter
    assert "agent=triage" in result.stdout
    assert "last 5" in result.stdout


# ---------------------------------------------------------------------------
# CLI: flag validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_monitor_too_short_interval_exits_2(db_with_runs: Path) -> None:
    """--interval below the minimum is rejected before entering the
    live loop (which would otherwise hammer storage)."""
    result = runner.invoke(app, ["monitor", "--interval", "0.1"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_monitor_too_short_interval_ok_with_once(db_with_runs: Path) -> None:
    """--once skips the live loop, so the interval check shouldn't fire."""
    result = runner.invoke(app, ["monitor", "--once", "--interval", "0.1"])
    assert result.exit_code == 0


@pytest.mark.unit
def test_cli_monitor_zero_limit_exits_2(db_with_runs: Path) -> None:
    result = runner.invoke(app, ["monitor", "--once", "--limit", "0"])
    assert result.exit_code == 2
