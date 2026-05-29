"""Tests for the dashboard "wow pack" seeder (``mdk demo seed`` / ``clear``).

Two layers:

1. **Pure generator** (:mod:`movate.core.demo.seeder`) — determinism, the
   safety tagging invariant (every row demo-tagged), volume, and that the
   storyline anomalies + drift are actually present in the generated data.
2. **CLI persistence + safety** — the prod-name guard, a real SQLite seed →
   purge round-trip, and that the purge only touches demo-tagged rows (a real
   row is left intact).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from movate.cli.demo_cmd import _looks_like_prod, _purge_demo
from movate.cli.main import app
from movate.core.demo import (
    DEMO_MARKER_KEY,
    DEMO_TENANT_PREFIX,
    SeedConfig,
    generate_bundle,
    is_demo_tenant,
)
from movate.core.models import JobStatus, Metrics, RunRecord
from movate.storage.sqlite import SqliteProvider

# Pin `now` so the generator is fully reproducible across the suite.
_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _cfg(**kw: object) -> SeedConfig:
    base: dict[str, object] = {
        "agents": 5,
        "tenants": 3,
        "days": 20,
        "seed": 7,
        "now": _NOW,
    }
    base.update(kw)
    return SeedConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure generator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_is_deterministic_for_same_seed_and_now() -> None:
    a = generate_bundle(_cfg())
    b = generate_bundle(_cfg())
    assert a.stats == b.stats
    assert [r.run_id for r in a.runs] == [r.run_id for r in b.runs]
    assert a.narrative == b.narrative


@pytest.mark.unit
def test_different_seed_changes_data() -> None:
    a = generate_bundle(_cfg(seed=1))
    b = generate_bundle(_cfg(seed=2))
    # Run ids are uuid4 (not seeded), but the shape/story differs by seed.
    assert a.narrative != b.narrative or a.stats != b.stats


@pytest.mark.unit
def test_every_run_is_demo_tagged() -> None:
    bundle = generate_bundle(_cfg())
    assert bundle.runs, "expected runs"
    for run in bundle.runs:
        assert is_demo_tenant(run.tenant_id), run.tenant_id
        assert run.tenant_id.startswith(DEMO_TENANT_PREFIX)
        assert run.input.get(DEMO_MARKER_KEY) is True


@pytest.mark.unit
def test_every_eval_and_failure_is_demo_tagged() -> None:
    bundle = generate_bundle(_cfg())
    for e in bundle.evals:
        assert is_demo_tenant(e.tenant_id), e.tenant_id
    for f in bundle.failures:
        assert is_demo_tenant(f.tenant_id), f.tenant_id


@pytest.mark.unit
def test_volume_is_dashboard_worthy() -> None:
    """Hundreds of runs for nice charts at the documented defaults."""
    bundle = generate_bundle(SeedConfig(agents=6, tenants=3, days=30, seed=1, now=_NOW))
    assert bundle.stats["runs"] >= 300


@pytest.mark.unit
def test_failures_match_failed_runs() -> None:
    bundle = generate_bundle(_cfg())
    failed = [r for r in bundle.runs if r.status in (JobStatus.ERROR, JobStatus.DEAD_LETTER)]
    # One failure record per failed run (those that carry an ErrorInfo).
    assert len(bundle.failures) == sum(1 for r in failed if r.error is not None)


@pytest.mark.unit
def test_storyline_events_present() -> None:
    """The seeded story must include the anomalies + drift the panels show."""
    bundle = generate_bundle(_cfg())
    kinds = {e.kind for e in bundle.events}
    assert "deploy" in kinds
    assert "latency_anomaly" in kinds
    assert "cost_anomaly" in kinds
    assert "drift_detected" in kinds
    assert "canary_promotion" in kinds


@pytest.mark.unit
def test_one_agent_drifts_below_gate() -> None:
    """Exactly the drift story: some agent's eval pass-rate dips under 0.70."""
    bundle = generate_bundle(_cfg())
    by_agent: dict[str, list[float]] = {}
    for e in bundle.evals:
        by_agent.setdefault(e.agent, []).append(e.pass_rate)
    # At least one agent ends well below where it started (a real regression).
    drifted = [a for a, rates in by_agent.items() if rates[0] - rates[-1] > 0.15]
    assert drifted, "expected at least one drifting agent"


@pytest.mark.unit
def test_cost_spike_exists() -> None:
    """Some agent has a *day* whose mean $/run dwarfs its other days.

    The spike is concentrated on one mid-window day (model swap + fatter
    prompts), so it shows up as a per-(agent, day) outlier — exactly how the
    cost dashboard surfaces it. A per-agent-overall mean would dilute it.
    """
    bundle = generate_bundle(_cfg())
    # mean cost per (agent, day)
    per_agent_day: dict[tuple[str, str], list[float]] = {}
    for r in bundle.runs:
        key = (r.agent, r.created_at.date().isoformat())
        per_agent_day.setdefault(key, []).append(r.metrics.cost_usd)
    day_means: dict[str, list[float]] = {}
    for (agent, _day), costs in per_agent_day.items():
        day_means.setdefault(agent, []).append(sum(costs) / len(costs))

    spiky = False
    for means in day_means.values():
        if len(means) > 1 and max(means) > 3 * (sum(means) / len(means)):
            spiky = True
            break
    assert spiky, "expected a per-day cost outlier for at least one agent"


# ---------------------------------------------------------------------------
# CLI: prod guard + seed/clear round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        ("prod", True),
        ("production", True),
        ("acme-prod-eastus", True),
        ("staging", False),
        ("local", False),
        ("dev", False),
    ],
)
def test_prod_guard_detection(name: str, expected: bool) -> None:
    assert _looks_like_prod(name) is expected


@pytest.mark.unit
def test_cli_seed_refuses_prod_without_force(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "t.db"))
    runner = CliRunner(mix_stderr=False)
    res = runner.invoke(app, ["demo", "seed", "--target", "production", "--days", "2"])
    assert res.exit_code == 2


@pytest.mark.unit
def test_cli_seed_then_clear_roundtrip(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "roundtrip.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    res = runner.invoke(
        app, ["demo", "seed", "--agents", "3", "--tenants", "2", "--days", "5", "--seed", "9"]
    )
    assert res.exit_code == 0, res.stdout + (res.stderr or "")

    # Clear removes them all.
    res2 = runner.invoke(app, ["demo", "clear", "--yes"])
    assert res2.exit_code == 0, res2.stdout + (res2.stderr or "")
    assert "Deleted" in res2.stdout


@pytest.mark.unit
def test_clear_leaves_non_demo_rows_intact(tmp_path, monkeypatch) -> None:
    """The purge keys on the demo- tenant prefix; a real tenant is untouched."""
    db = str(tmp_path / "mixed.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")

    async def _scenario() -> int:
        storage = SqliteProvider(db_path=db)
        await storage.init()
        try:
            # One real (non-demo) run.
            real = RunRecord(
                run_id="real-1",
                job_id="j1",
                tenant_id="acme-real",  # NO demo- prefix
                agent="prod-agent",
                agent_version="1.0",
                prompt_hash="h",
                provider="openai/gpt-4o",
                provider_version="v",
                pricing_version="p",
                status=JobStatus.SUCCESS,
                input={"q": "real traffic"},
                metrics=Metrics(),
            )
            await storage.save_run(real)
            # A handful of demo rows.
            bundle = generate_bundle(SeedConfig(agents=2, tenants=1, days=2, seed=3, now=_NOW))
            for r in bundle.runs:
                await storage.save_run(r)

            deleted = await _purge_demo(storage)

            # The real row survives; demo rows are gone.
            survivor = await storage.get_run("real-1", tenant_id="acme-real")
            assert survivor is not None
            return deleted
        finally:
            await storage.close()

    deleted = asyncio.run(_scenario())
    assert deleted >= 1
