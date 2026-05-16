"""Sprint W — `mdk eval --all` sweeps every agent in the project.

The CI eval-gate workflow (`.github/workflows/eval-gate.yml`) uses
this single-command surface so workflows don't have to maintain a
matrix of agent names. Mirrors `mdk validate --all` (from #75) in
shape: one optional positional arg, an explicit `--all` flag for
script clarity, project-wide summary line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_with_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str = "faq"
) -> Path:
    """Build a project + scaffold one agent. Returns project root."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", template], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    return proj


@pytest.mark.unit
class TestEvalAllSweepsProject:
    def test_eval_all_runs_every_agent_in_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk eval --all --mock` runs eval against every agent in
        `./agents/`. Emits the summary line + project-eval table."""
        _bootstrap_with_agent(tmp_path, monkeypatch, "faq")
        result = runner.invoke(
            app,
            ["eval", "--all", "--mock", "--gate", "0.0"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Greppable summary fires.
        assert "mdk_eval_all_summary:" in result.stdout
        assert "agents_total=1" in result.stdout
        assert "ok=true" in result.stdout
        # Per-agent project-eval table.
        assert "Project eval" in result.stdout
        assert "faq" in result.stdout

    def test_eval_all_outside_project_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No project marker anywhere up the tree → exit 2 with a hint."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["eval", "--all", "--mock"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "not inside a movate project" in combined.lower()

    def test_eval_all_empty_project_warns_not_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty project (zero agents) is not a failure — gate
        passes vacuously. Greppable line reports `agents_total=0`."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "empty", "--skip-snapshot"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        monkeypatch.chdir(tmp_path / "empty")
        result = runner.invoke(app, ["eval", "--all", "--mock"], env={"COLUMNS": "200"})
        # Vacuous-pass exit 0.
        assert result.exit_code == 0
        assert "agents_total=0" in result.stdout
        assert "ok=true" in result.stdout


@pytest.mark.unit
class TestEvalAllMutex:
    def test_path_plus_all_is_mutex(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passing BOTH a path AND `--all` is rejected as ambiguous."""
        proj = _bootstrap_with_agent(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "eval",
                str(proj / "agents" / "faq"),
                "--all",
                "--mock",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "mutually exclusive" in combined.lower()

    def test_no_args_errors_with_hint_pointing_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk eval` with no path AND no --all errors with a hint
        that mentions both alternatives."""
        _bootstrap_with_agent(tmp_path, monkeypatch)
        result = runner.invoke(app, ["eval"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "path required" in combined.lower()
        assert "--all" in combined


@pytest.mark.unit
class TestEvalAllFailurePath:
    def test_failing_agent_marks_red_and_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent that fails its gate produces a red row in the
        project eval table AND --all exits 2 overall."""
        proj = _bootstrap_with_agent(tmp_path, monkeypatch)
        # Post-PR-#104 the mock auto-conforms to dataset expecteds so
        # eval scores 1.0 by default. Force a non-conforming response
        # via the env override so the gate genuinely fails — that's
        # what this test means by "agent fails its gate."
        monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"unexpected_field": "wrong"}')
        result = runner.invoke(
            app,
            ["eval", "--all", "--mock", "--gate", "1.0"],
            env={"COLUMNS": "200"},
        )
        # Exit non-zero on any agent failing its gate.
        assert result.exit_code != 0
        # Summary line carries `ok=false` so CI can grep that as
        # the canonical failure signal.
        assert "mdk_eval_all_summary:" in result.stdout
        assert "ok=false" in result.stdout
        _ = proj  # silence unused
