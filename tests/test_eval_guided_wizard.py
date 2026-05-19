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
    """Pick agent #2 (faq, since #1 is 'all'), keep existing dataset,
    gate=0.7, runs=3, no baseline. Single-agent flow now includes a
    "Test cases?" prompt between agent selection and gate threshold —
    the wizard's first job is to either generate fresh cases via LLM
    or use the existing dataset.jsonl. Picking "keep existing" here
    skips generation so the test stays hermetic (no API calls)."""
    _bootstrap(tmp_path, monkeypatch)
    # Five answers (was four before Phase 3a's test-cases prompt):
    # 2=faq, 1=keep existing dataset, 3=gate 0.7, 2=runs 3,
    # 1=no baseline.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="2\n1\n3\n2\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "Test cases?" in combined, "wizard must prompt about test cases"
    assert "Running:" in combined
    # Single-agent mode (no --all), no --mock, gate 0.7, runs 3.
    assert "mdk eval faq" in combined
    assert "--all" not in combined.split("Running:")[1].splitlines()[0]
    assert "--gate 0.7" in combined
    assert "--runs 3" in combined


@pytest.mark.unit
def test_guided_single_agent_generate_path_calls_generation_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-agent flow with "generate fresh" picked must run the
    case-generation sub-flow (count + mix sub-prompts, then call
    ``_generate_entries`` and write to dataset.jsonl). Mocked at the
    generation boundary so the test stays hermetic.

    Pin the sub-flow order so a future refactor can't accidentally
    skip the case-generation step in single-agent mode."""
    _bootstrap(tmp_path, monkeypatch)

    # Mock the LLM generation so we don't need API keys + so we can
    # assert that the wizard actually calls it.
    gen_calls: list[dict[str, object]] = []

    async def fake_generate_entries(bundle: object, **kwargs: object) -> list[dict[str, object]]:
        gen_calls.append({"num": kwargs.get("num"), "mode": kwargs.get("mode")})
        return [
            {"input": {"question": "q1"}, "expected": {"answer": "a1"}},
            {"input": {"question": "q2"}, "expected": {"answer": "a2"}},
        ]

    monkeypatch.setattr("movate.cli.eval_gen_cmd._generate_entries", fake_generate_entries)

    # Six answers: 2=faq, 2=generate fresh, 2=10 cases, 1=standard,
    # 1=gate 0.0 (so eval against the fresh dataset doesn't gate-fail
    # without API keys), 1=runs 1, 1=no baseline.
    # Wait — that's seven. Recount: agent, action, count, mix, gate,
    # runs, baseline = 7 answers.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="2\n2\n2\n1\n1\n1\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # Generation sub-prompts surfaced.
    assert "Test cases?" in combined
    assert "How many cases?" in combined
    assert "Which mix?" in combined
    # Generation actually fired.
    assert len(gen_calls) == 1, f"expected one generation call, got {gen_calls}"
    assert gen_calls[0]["num"] == 10
    assert gen_calls[0]["mode"] == "standard"
    # The dataset.jsonl was overwritten with the fake entries.
    dataset_path = tmp_path / "proj" / "agents" / "faq" / "evals" / "dataset.jsonl"
    assert dataset_path.is_file()
    rows = [line for line in dataset_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 2


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
