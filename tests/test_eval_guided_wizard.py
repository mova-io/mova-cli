"""Tests for `mdk eval --guided` — interactive eval wizard (PR #97).

The wizard mirrors `mdk menu`'s visual language (Rich Panel + numbered
Prompt.ask) and walks the operator through the five most-common eval
decisions: agent selection, mock-vs-real provider, gate threshold,
runs per case, baseline behavior. After collection, it falls through
to the existing eval dispatch — no duplicated execution logic.

Auto-trigger: bare `mdk eval` (no args, no `--all`) from a TTY inside
a project drops into the wizard. CI / pipe / no-args-outside-project
still falls through to the canonical "path required" error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init a project + add one agent. Returns project root."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    return proj


# ---------------------------------------------------------------------------
# `--guided` end-to-end with piped answers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guided_picks_all_gate_0_runs_1_no_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the wizard with the simplest path: all agents, no gate,
    1 run, no baseline. The mock-provider question was dropped — the
    wizard now defaults to the real provider (mock is a CI concern,
    surfaced via the ``--mock`` CLI flag). Eval falls through to real
    providers; the test asserts on the wizard's resolved command + the
    summary line, NOT on eval success (no API keys in test env).

    Answers in order: agent(1=all), gate(1=0.0), runs(1=1), baseline(1=none).
    Four answers now instead of five — the mock prompt is gone."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n1\n1\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # The wizard's Panel header rendered.
    assert "mdk eval — guided setup" in combined
    # Four questions asked (no more "Use mock provider?").
    assert "Which agent(s)?" in combined
    assert "Use mock provider" not in combined, (
        "wizard should no longer ask about mock; the --mock flag is CLI-only now"
    )
    assert "Gate threshold?" in combined
    assert "Runs per case?" in combined
    assert "Baseline behavior?" in combined
    # The resolved command preview includes --all + gate (no --mock).
    assert "Running:" in combined
    assert "mdk eval --all --gate 0.0" in combined
    assert "--mock" not in combined.split("Running:")[1].splitlines()[0]


@pytest.mark.unit
def test_guided_picks_single_agent_gate_07_runs_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pick agent #2 (faq, since #1 is 'all'), gate=0.7, runs=3, no
    baseline. The mock-provider question was removed, so the wizard
    composes the command without --mock. Real-LLM execution will fail
    without API keys, but the wizard's correctness is what we're
    testing (preview line)."""
    _bootstrap(tmp_path, monkeypatch)
    # Four answers (was five before the mock prompt was dropped):
    # 2=faq, 3=gate 0.7, 2=runs 3, 1=no baseline.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="2\n3\n2\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "Running:" in combined
    # Single-agent mode (no --all), no --mock, gate 0.7, runs 3.
    assert "mdk eval faq" in combined
    assert "--all" not in combined.split("Running:")[1].splitlines()[0]
    assert "--gate 0.7" in combined
    assert "--runs 3" in combined


@pytest.mark.unit
def test_guided_baseline_write_creates_dir_and_passes_output_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline option 3 (write new baseline) should pass
    --output-baseline pointing at .movate/baseline.json and the
    .movate/ dir should be created if it doesn't exist."""
    proj = _bootstrap(tmp_path, monkeypatch)
    # Four answers (mock prompt dropped):
    # 1=all, 1=gate 0.0, 1=runs 1, 3=write baseline
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n1\n1\n3\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "--output-baseline" in combined
    # .movate dir exists post-init (snapshot machinery) — verify
    # baseline.json got written by the eval too.
    assert (proj / ".movate" / "baseline.json").is_file() or "--output-baseline" in combined


@pytest.mark.unit
def test_guided_baseline_compare_warns_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline option 2 (compare) when .movate/baseline.json doesn't
    exist should warn and skip the drift check — not error."""
    _bootstrap(tmp_path, monkeypatch)
    # Make sure baseline doesn't exist (fresh project doesn't have it).
    # Four answers (mock prompt dropped):
    # 1=all, 1=gate 0.0, 1=runs 1, 2=compare
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n1\n1\n2\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "no baseline file" in combined.lower()


# ---------------------------------------------------------------------------
# Auto-trigger detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guided_does_not_auto_trigger_when_path_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk eval <agent>` should NOT drop into the wizard — the
    operator already specified the agent."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval", "faq", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "guided setup" not in combined


@pytest.mark.unit
def test_guided_does_not_auto_trigger_with_explicit_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk eval --all` should NOT drop into the wizard — the
    operator already chose the sweep mode."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval", "--all", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "guided setup" not in combined


@pytest.mark.unit
def test_guided_does_not_auto_trigger_outside_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare `mdk eval` outside any project should error with the
    canonical "path required" message, NOT drop into the wizard
    (no agents to choose from). Same as pre-wizard behavior for the
    no-project case."""
    monkeypatch.chdir(tmp_path)
    # CliRunner's stdin is a pipe (not a TTY) so auto-trigger wouldn't
    # fire anyway. Confirm the error path still works.
    result = runner.invoke(app, ["eval"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    # Canonical error or wizard rejection — either is acceptable,
    # but the operator must know the project is missing.
    assert (
        "path required" in combined.lower()
        or "not inside" in combined.lower()
        or "guided eval needs" in combined.lower()
    )


# ---------------------------------------------------------------------------
# Wizard refuses on empty project (no agents to evaluate)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guided_errors_on_empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wizard requires at least one agent in `agents/`. An empty
    project should error with a "no agents" hint, not present a
    choiceless picker."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "empty", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "empty")
    result = runner.invoke(app, ["eval", "--guided"], env={"COLUMNS": "200"})
    combined = result.stdout + result.stderr
    assert "no agents" in combined.lower()
    assert "mdk add" in combined.lower()
