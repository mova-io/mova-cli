"""ADR 031 D3 — `mdk report` offline rollup tests.

Layers (mirrors ``test_costs_report.py``):

1. **Pure aggregation** — percentiles, top-failing clustering, and the
   per-agent / overall :func:`_build_report` reduction over seeded records.
2. **Windowing** — ``--last N`` (the ``_filter_*_by_since`` helpers).
3. **CLI** — `mdk report` over a populated SQLite DB: table + ``--json``
   shape, per-agent scope, empty store, graceful degradation on records
   missing cost/latency.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.cli.report_cmd import (
    FailingCase,
    LatencyPercentiles,
    _build_report,
    _filter_evals_by_since,
    _filter_runs_by_since,
    _latency_percentiles,
    _percentile,
    _top_failing_cases,
)
from movate.core.models import (
    ErrorInfo,
    EvalRecord,
    JobStatus,
    JudgeMethod,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage import SqliteProvider

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _run(
    *,
    run_id: str = "r1",
    agent: str = "triage",
    status: JobStatus = JobStatus.SUCCESS,
    cost: float = 0.001,
    latency_ms: int = 100,
    inp: dict | None = None,
    when: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id="t1",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="hash",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2026-05",
        status=status,
        input=inp if inp is not None else {"q": "x"},
        output={"a": "y"} if status == JobStatus.SUCCESS else None,
        metrics=Metrics(
            cost_usd=cost,
            latency_ms=latency_ms,
            tokens=TokenUsage(input=10, output=5),
            provider="openai/gpt-4o-mini",
        ),
        created_at=when or datetime.now(UTC),
    )


def _eval(
    *,
    eval_id: str = "e1",
    agent: str = "triage",
    pass_rate: float = 1.0,
    mean_score: float = 0.9,
    cost: float = 0.01,
    when: datetime | None = None,
) -> EvalRecord:
    return EvalRecord(
        eval_id=eval_id,
        tenant_id="t1",
        agent=agent,
        agent_version="0.1.0",
        dataset_hash="dh",
        judge_method=JudgeMethod.LLM_JUDGE,
        judge_provider="openai/gpt-4o",
        runs_per_case=1,
        gate_mode="mean",
        threshold=0.7,
        mean_score=mean_score,
        pass_rate=pass_rate,
        sample_count=10,
        total_cost_usd=cost,
        created_at=when or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPercentiles:
    def test_single_value(self) -> None:
        assert _percentile([42.0], 95) == 42.0

    def test_p50_median_ish(self) -> None:
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert _percentile(vals, 50) == 30.0

    def test_p99_is_top(self) -> None:
        vals = [float(i) for i in range(1, 101)]
        assert _percentile(vals, 99) == 99.0
        assert _percentile(vals, 100) == 100.0

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            _percentile([], 50)

    def test_latency_percentiles_drops_missing(self) -> None:
        runs = [
            _run(run_id="a", latency_ms=100),
            _run(run_id="b", latency_ms=200),
            _run(run_id="c", latency_ms=0),  # missing/zero → dropped
        ]
        lp = _latency_percentiles(runs)
        assert lp.count == 2
        assert lp.p50 is not None and lp.p99 is not None

    def test_latency_percentiles_all_missing_is_none(self) -> None:
        runs = [_run(run_id="a", latency_ms=0), _run(run_id="b", latency_ms=0)]
        lp = _latency_percentiles(runs)
        assert lp == LatencyPercentiles(count=0)
        assert lp.p50 is None


# ---------------------------------------------------------------------------
# Top failing cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTopFailingCases:
    def test_clusters_by_input(self) -> None:
        runs = [
            _run(run_id="1", status=JobStatus.ERROR, inp={"q": "boom"}),
            _run(run_id="2", status=JobStatus.ERROR, inp={"q": "boom"}),
            _run(run_id="3", status=JobStatus.ERROR, inp={"q": "other"}),
            _run(run_id="4", status=JobStatus.SUCCESS, inp={"q": "boom"}),  # success ignored
        ]
        cases = _top_failing_cases(runs)
        assert cases[0].failures == 2  # "boom" failed twice
        assert len(cases) == 2

    def test_prefers_case_id_field(self) -> None:
        runs = [
            _run(run_id="1", status=JobStatus.ERROR, inp={"case_id": "C-42", "q": "a"}),
            _run(run_id="2", status=JobStatus.ERROR, inp={"case_id": "C-42", "q": "b"}),
        ]
        cases = _top_failing_cases(runs)
        assert cases[0].case == "C-42"
        assert cases[0].failures == 2

    def test_no_failures_empty(self) -> None:
        assert _top_failing_cases([_run(status=JobStatus.SUCCESS)]) == []

    def test_respects_limit(self) -> None:
        runs = [
            _run(run_id=str(i), status=JobStatus.ERROR, inp={"q": f"case-{i}"}) for i in range(10)
        ]
        assert len(_top_failing_cases(runs, limit=3)) == 3

    def test_captures_last_error(self) -> None:
        run = _run(run_id="1", status=JobStatus.ERROR, inp={"q": "boom"})
        run = run.model_copy(update={"error": ErrorInfo(type="Timeout", message="timed out")})
        cases = _top_failing_cases([run])
        assert cases[0].last_error == "timed out"


# ---------------------------------------------------------------------------
# Full report reduction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildReport:
    def test_per_agent_grouping(self) -> None:
        runs = [
            _run(run_id="1", agent="triage", cost=0.05),
            _run(run_id="2", agent="triage", cost=0.05),
            _run(run_id="3", agent="summary", cost=0.10),
        ]
        evals = [
            _eval(eval_id="e1", agent="triage", pass_rate=0.8),
            _eval(eval_id="e2", agent="summary", pass_rate=1.0),
        ]
        report = _build_report(runs, evals)
        names = {a.name for a in report.agents}
        assert names == {"triage", "summary"}
        triage = next(a for a in report.agents if a.name == "triage")
        assert triage.runs == 2
        assert abs(triage.total_cost_usd - 0.10) < 1e-9
        assert triage.latest_pass_rate == 0.8

    def test_sorted_by_cost_desc(self) -> None:
        runs = [
            _run(run_id="1", agent="cheap", cost=0.01),
            _run(run_id="2", agent="pricey", cost=10.0),
        ]
        report = _build_report(runs, [])
        assert report.agents[0].name == "pricey"

    def test_agent_with_only_evals_appears(self) -> None:
        """An agent eval'd but never ad-hoc run still shows its pass-rate."""
        report = _build_report([], [_eval(agent="eval-only", pass_rate=0.5)])
        assert len(report.agents) == 1
        a = report.agents[0]
        assert a.name == "eval-only"
        assert a.runs == 0
        assert a.latest_pass_rate == 0.5

    def test_latest_pass_rate_is_most_recent(self) -> None:
        old = _eval(eval_id="old", pass_rate=0.5, when=datetime.now(UTC) - timedelta(days=2))
        new = _eval(eval_id="new", pass_rate=0.9, when=datetime.now(UTC))
        report = _build_report([], [new, old])  # storage returns newest-first
        assert report.overall_latest_pass_rate == 0.9
        assert report.agents[0].latest_pass_rate == 0.9
        # mean over both
        assert abs(report.agents[0].mean_pass_rate - 0.7) < 1e-9

    def test_failed_runs_counted(self) -> None:
        runs = [
            _run(run_id="1", status=JobStatus.SUCCESS),
            _run(run_id="2", status=JobStatus.ERROR),
            _run(run_id="3", status=JobStatus.SAFETY_BLOCKED),
        ]
        report = _build_report(runs, [])
        assert report.total_failed_runs == 2

    def test_missing_cost_latency_degrades(self) -> None:
        """Records with zero cost / latency don't crash or divide-by-zero."""
        runs = [_run(run_id="1", cost=0.0, latency_ms=0)]
        report = _build_report(runs, [])
        a = report.agents[0]
        assert a.total_cost_usd == 0.0
        assert a.mean_cost_usd == 0.0  # not a ZeroDivisionError
        assert a.latency.p50 is None  # no latency signal
        assert report.overall_latency.count == 0

    def test_empty_report_has_no_agents(self) -> None:
        report = _build_report([], [])
        assert report.agents == []
        assert report.overall_latest_pass_rate is None
        assert report.top_failing_cases == []


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWindowing:
    def test_runs_zero_is_noop(self) -> None:
        runs = [_run()]
        assert _filter_runs_by_since(runs, 0) == runs

    def test_runs_filters_old(self) -> None:
        recent = _run(run_id="r", when=datetime.now(UTC) - timedelta(days=1))
        old = _run(run_id="o", when=datetime.now(UTC) - timedelta(days=30))
        out = _filter_runs_by_since([recent, old], 7)
        assert [r.run_id for r in out] == ["r"]

    def test_runs_naive_datetime_ok(self) -> None:
        naive = _run(run_id="n").model_copy(
            update={"created_at": datetime.now() - timedelta(hours=1)}
        )
        assert len(_filter_runs_by_since([naive], 7)) == 1

    def test_evals_filters_old(self) -> None:
        recent = _eval(eval_id="r", when=datetime.now(UTC) - timedelta(days=1))
        old = _eval(eval_id="o", when=datetime.now(UTC) - timedelta(days=30))
        out = _filter_evals_by_since([recent, old], 7)
        assert [e.eval_id for e in out] == ["r"]


# ---------------------------------------------------------------------------
# FailingCase / LatencyPercentiles dataclasses
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_failing_case_is_frozen() -> None:
    c = FailingCase(case="x", failures=1, agents=["a"])
    assert c.failures == 1
    assert c.agents == ["a"]


# ---------------------------------------------------------------------------
# CLI — end-to-end through a populated SQLite DB
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp SQLite DB: runs (some failing) + eval summaries across two agents."""
    db_path = tmp_path / "report.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    async def _seed() -> None:
        provider = SqliteProvider(db_path=str(db_path))
        await provider.init()
        try:
            # triage: 2 success + 1 failure, with cost + latency
            await provider.save_run(_run(run_id="t1", agent="triage", cost=0.05, latency_ms=120))
            await provider.save_run(_run(run_id="t2", agent="triage", cost=0.06, latency_ms=300))
            await provider.save_run(
                _run(
                    run_id="t3",
                    agent="triage",
                    status=JobStatus.ERROR,
                    inp={"q": "boom"},
                    latency_ms=80,
                )
            )
            # summary: 1 success
            await provider.save_run(_run(run_id="s1", agent="summary", cost=0.02, latency_ms=200))
            # evals
            await provider.save_eval(_eval(eval_id="e-triage", agent="triage", pass_rate=0.75))
            await provider.save_eval(_eval(eval_id="e-summary", agent="summary", pass_rate=1.0))
        finally:
            await provider.close()

    asyncio.run(_seed())
    return db_path


@pytest.mark.unit
def test_cli_report_shows_agents(populated_db: Path) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "triage" in result.stdout
    assert "summary" in result.stdout


@pytest.mark.unit
def test_cli_report_json_shape(populated_db: Path) -> None:
    result = runner.invoke(app, ["report", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    # totals block
    assert data["totals"]["runs"] == 4
    assert data["totals"]["eval_runs"] == 2
    assert data["totals"]["failed_runs"] == 1
    assert "latency_ms" in data["totals"]
    assert data["totals"]["latency_ms"]["p50"] is not None
    # per-agent
    names = {a["name"] for a in data["agents"]}
    assert names == {"triage", "summary"}
    triage = next(a for a in data["agents"] if a["name"] == "triage")
    assert triage["runs"] == 3
    assert triage["failed_runs"] == 1
    assert triage["latest_pass_rate"] == 0.75
    # top failing cases — the failing triage input clusters
    assert len(data["top_failing_cases"]) >= 1
    assert data["top_failing_cases"][0]["failures"] == 1


@pytest.mark.unit
def test_cli_report_scope_positional(populated_db: Path) -> None:
    result = runner.invoke(app, ["report", "triage", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    names = {a["name"] for a in data["agents"]}
    assert names == {"triage"}
    assert data["agent_filter"] == "triage"


@pytest.mark.unit
def test_cli_report_scope_option(populated_db: Path) -> None:
    result = runner.invoke(app, ["report", "--agent", "summary", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    names = {a["name"] for a in data["agents"]}
    assert names == {"summary"}


@pytest.mark.unit
def test_cli_report_empty_store_friendly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "empty.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "nothing to report" in result.stdout.lower()


@pytest.mark.unit
def test_cli_report_empty_store_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "empty.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    result = runner.invoke(app, ["report", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["totals"]["runs"] == 0
    assert data["agents"] == []


@pytest.mark.unit
def test_cli_report_last_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "windowed.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    async def _seed() -> None:
        provider = SqliteProvider(db_path=str(db_path))
        await provider.init()
        try:
            await provider.save_run(
                _run(run_id="old", agent="ancient", when=datetime.now(UTC) - timedelta(days=30))
            )
            await provider.save_run(
                _run(run_id="new", agent="fresh", when=datetime.now(UTC) - timedelta(hours=1))
            )
        finally:
            await provider.close()

    asyncio.run(_seed())
    result = runner.invoke(app, ["report", "--last", "7", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    names = {a["name"] for a in data["agents"]}
    assert "fresh" in names
    assert "ancient" not in names
    assert data["window_days"] == 7


@pytest.mark.unit
def test_cli_report_negative_last_exits_2(populated_db: Path) -> None:
    result = runner.invoke(app, ["report", "--last", "-1"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_report_zero_top_exits_2(populated_db: Path) -> None:
    result = runner.invoke(app, ["report", "--top", "0"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_report_missing_metrics_no_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older records with zero cost/latency render N/A, not a crash."""
    db_path = tmp_path / "sparse.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    async def _seed() -> None:
        provider = SqliteProvider(db_path=str(db_path))
        await provider.init()
        try:
            await provider.save_run(_run(run_id="z", agent="sparse", cost=0.0, latency_ms=0))
        finally:
            await provider.close()

    asyncio.run(_seed())
    result = runner.invoke(app, ["report", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    a = data["agents"][0]
    assert a["mean_cost_usd"] == 0.0
    assert a["latency_ms"]["p50"] is None
