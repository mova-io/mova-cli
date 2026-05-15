"""Polish bundle — small UX improvements across multiple sprints.

Each test targets one item from the polish bundle:

1. ``mdk audit`` — emits a greppable ``mdk_audit_summary:`` line at end.
2. ``mdk simulate --pass-rate-gate`` — exit 1 if rate below threshold.
3. ``mdk costs report --highlight-over`` — red-bar rows over threshold.
4. ``mdk fix --explain <id>`` — shows description + applicability.
5. ``mdk inspect agent --raw`` — prints raw agent.yaml + prompt.md.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

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


# ---------------------------------------------------------------------------
# Item 1: mdk audit greppable summary line
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_audit_emits_summary_line(tmp_path: Path) -> None:
    """The ``mdk_audit_summary:`` line is grep-able at the end."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("# test\n")
    agent = proj / "agents" / "x"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text(
        "api_version: movate/v1\nkind: Agent\nname: x\nversion: 0.1.0\n"
        "model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
        "  fallback:\n    - provider: anthropic/claude-haiku-4-5-20251001\n"
        "prompt: ./prompt.md\n"
        "schema:\n  input: { q: string }\n  output: { a: string }\n"
    )
    (agent / "prompt.md").write_text("hello")
    result = runner.invoke(app, ["audit", "--project", str(proj)])
    assert result.exit_code in {0, 1}
    # The grep line shows up
    assert "mdk_audit_summary:" in result.stdout
    # Key=value structure intact
    assert "agents=1" in result.stdout
    assert "errors=" in result.stdout
    assert "warnings=" in result.stdout


# ---------------------------------------------------------------------------
# Item 2: mdk simulate --pass-rate-gate
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_chatbot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _scaffold_agent(tmp_path / "agents" / "chatbot", name="chatbot")
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "test.db"))
    return tmp_path


@pytest.mark.unit
def test_simulate_pass_rate_gate_met_exits_0(project_with_chatbot: Path) -> None:
    """--mock auto-marks every scenario achieved, so gate=0.5 passes."""
    result = runner.invoke(
        app,
        [
            "simulate",
            "chatbot",
            "--num",
            "2",
            "--mock",
            "--pass-rate-gate",
            "0.5",
            "--project-root",
            str(project_with_chatbot),
        ],
    )
    assert result.exit_code == 0
    assert "pass-rate gate met" in result.stdout.lower()


@pytest.mark.unit
def test_simulate_pass_rate_gate_above_range_exits_2(
    project_with_chatbot: Path,
) -> None:
    """--pass-rate-gate > 1.0 is invalid → exit 2."""
    result = runner.invoke(
        app,
        [
            "simulate",
            "chatbot",
            "--num",
            "1",
            "--mock",
            "--pass-rate-gate",
            "1.5",
            "--project-root",
            str(project_with_chatbot),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Item 3: mdk costs report --highlight-over
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_costly_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed two runs: one cheap, one expensive."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))

    async def _seed() -> None:
        p = SqliteProvider(db_path=str(db_path))
        await p.init()
        try:
            for i, (agent, cost) in enumerate([("cheap", 0.001), ("expensive", 5.0)]):
                rec = RunRecord(
                    run_id=f"r-{i}",
                    job_id=f"j-{i}",
                    tenant_id="local",
                    agent=agent,
                    agent_version="0.1.0",
                    prompt_hash="h",
                    provider="openai/gpt-4o-mini",
                    provider_version="0",
                    pricing_version="2026-05",
                    status=JobStatus.SUCCESS,
                    input={},
                    output={},
                    metrics=Metrics(
                        cost_usd=cost,
                        tokens=TokenUsage(input=10, output=5),
                        provider="openai/gpt-4o-mini",
                    ),
                    created_at=datetime.now(UTC),
                )
                await p.save_run(rec)
        finally:
            await p.close()

    asyncio.run(_seed())
    return db_path


@pytest.mark.unit
def test_costs_highlight_over_flags_expensive(db_with_costly_runs: Path) -> None:
    """A row above the threshold should be styled + a summary line emitted."""
    result = runner.invoke(app, ["costs", "report", "--highlight-over", "1.0"])
    assert result.exit_code == 0
    # Summary line tags the over-threshold count
    assert "over the" in result.stdout.lower()
    assert "$1.0000" in result.stdout or "$1" in result.stdout


@pytest.mark.unit
def test_costs_highlight_over_zero_disables(db_with_costly_runs: Path) -> None:
    """Default 0.0 = no highlight summary line."""
    result = runner.invoke(app, ["costs", "report"])
    assert result.exit_code == 0
    assert "over the" not in result.stdout.lower()


@pytest.mark.unit
def test_costs_highlight_over_negative_exits_2(db_with_costly_runs: Path) -> None:
    result = runner.invoke(app, ["costs", "report", "--highlight-over", "-1.0"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Item 4: mdk fix --explain <id>
# ---------------------------------------------------------------------------


@pytest.fixture
def fix_project(tmp_path: Path) -> Path:
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\nkind: Project\n")
    return tmp_path


@pytest.mark.unit
def test_fix_explain_known_fix(fix_project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fix",
            "--explain",
            "ensure-gitignore",
            "--project-root",
            str(fix_project),
        ],
    )
    assert result.exit_code == 0
    # Label + description appear
    assert "ensure-gitignore" in result.stdout
    assert "gitignore" in result.stdout.lower()
    # Applicability verdict
    combined = result.stdout.lower()
    assert "applies here" in combined


@pytest.mark.unit
def test_fix_explain_unknown_fix_exits_2(fix_project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fix",
            "--explain",
            "bogus-fix",
            "--project-root",
            str(fix_project),
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "unknown fix" in combined.lower() or "bogus-fix" in combined


# ---------------------------------------------------------------------------
# Item 5: mdk inspect agent --raw
# ---------------------------------------------------------------------------


@pytest.fixture
def inspect_project(tmp_path: Path) -> Path:
    _scaffold_agent(tmp_path / "agents" / "triage", name="triage")
    (tmp_path / "movate.yaml").write_text("# test\n")
    return tmp_path


@pytest.mark.unit
def test_inspect_raw_shows_both_raw_and_resolved(inspect_project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--raw",
            "--project-root",
            str(inspect_project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Resolved view still renders (Identity panel)
    assert "Identity" in result.stdout
    # Raw view renders too
    assert "Raw:" in result.stdout
    assert "agent.yaml" in result.stdout
    assert "prompt.md" in result.stdout


@pytest.mark.unit
def test_inspect_without_raw_omits_raw_panels(inspect_project: Path) -> None:
    """Default (no --raw) → only the resolved view, no raw panels."""
    result = runner.invoke(
        app,
        ["inspect", "agent", "triage", "--project-root", str(inspect_project)],
    )
    assert result.exit_code == 0
    assert "Raw:" not in result.stdout
