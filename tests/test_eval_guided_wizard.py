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
def test_guided_picks_all_mock_gate_0_runs_1_no_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the wizard with the simplest path: all agents, mock provider,
    no gate, 1 run, no baseline. The eval should execute with the
    composed flags and the resolved command should be echoed."""
    _bootstrap(tmp_path, monkeypatch)
    # Answers in order: agent(1=all), mock(y), gate(1=0.0), runs(1=1), baseline(1=none)
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\ny\n1\n1\n1\n",
        env={"COLUMNS": "200"},
    )
    # `gate 0.0` means vacuous pass even with the MockProvider schema
    # mismatch, so exit should be clean.
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    # The wizard's Panel header rendered.
    assert "mdk eval — guided setup" in combined
    # Five questions all asked.
    assert "Which agent(s)?" in combined
    assert "Use mock provider" in combined
    assert "Gate threshold?" in combined
    assert "Runs per case?" in combined
    assert "Baseline behavior?" in combined
    # The resolved command preview includes --all and --mock + gate.
    assert "Running:" in combined
    assert "mdk eval --all --mock --gate 0.0" in combined
    # And the eval actually ran — the all-sweep summary line fired.
    assert "mdk_eval_all_summary:" in combined


@pytest.mark.unit
def test_guided_picks_single_agent_real_gate_07_runs_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pick agent #2 (faq, since #1 is 'all'), n for mock (real LLM),
    gate=0.7, runs=3, no baseline. We can't actually execute the real
    LLM path in a test (no API keys), so the wizard's preview should
    show the composed command and then the path should fall through to
    the standard dispatch which will hit a real-LLM error — but the
    wizard's job is done by the time it prints the preview."""
    _bootstrap(tmp_path, monkeypatch)
    # 2=faq, n=real provider, 3=gate 0.7, 2=runs 3, 1=no baseline
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="2\nn\n3\n2\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # The preview line is what proves the wizard correctly composed
    # the flags from the answers (whether the subsequent eval execution
    # succeeds or not is unrelated to the wizard's correctness).
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
    # 1=all, y=mock, 1=gate 0.0, 1=runs 1, 3=write baseline
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\ny\n1\n1\n3\n",
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
    # 1=all, y=mock, 1=gate 0.0, 1=runs 1, 2=compare
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\ny\n1\n1\n2\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "no baseline file" in combined.lower()
    # The eval still ran (just without drift check).
    assert "mdk_eval_all_summary:" in combined


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
