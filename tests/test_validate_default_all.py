"""``mdk validate`` defaults to "all in project" when no path is given.

Previously ``mdk validate`` required either a path argument OR the
``--all`` flag — operators inside a project who wanted to check
everything had to type the flag every time. Now the no-arg case
defaults to the same whole-project sweep, matching the most common
intent. ``--all`` stays as an explicit form for back-compat.

The natural flow becomes:

    mdk init --project foo --with-agents rag-qa,ticket-triager
    cd foo
    mdk validate            # ← no flag needed

The behavior matrix:

    inside project + no path  → validate all (was: error)
    inside project + path     → validate that one (unchanged)
    inside project + --all    → validate all (back-compat, unchanged)
    inside project + path + --all → mutex error (unchanged)
    outside project + no path → error (still: pass a path or init)
    outside project + path    → validate that path (unchanged)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agents: str = "rag-qa") -> Path:
    """Build a project + scaffold the named agent(s), chdir in."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--project",
            "proj",
            "--skip-snapshot",
            "--with-agents",
            agents,
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    project = tmp_path / "proj"
    monkeypatch.chdir(project)
    return project


@pytest.mark.unit
class TestNoArgDefaultsToAll:
    def test_no_args_inside_project_validates_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate` with no args (inside a project) should sweep
        the whole project — same as `mdk validate --all`."""
        _bootstrap(tmp_path, monkeypatch, agents="rag-qa,ticket-triager")
        result = runner.invoke(app, ["validate"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        # Same greppable summary line as the --all flow fires.
        summary = next(
            (line for line in result.stdout.splitlines() if "mdk_validate_summary:" in line),
            None,
        )
        assert summary is not None
        assert "passed=2" in summary
        assert "failed=0" in summary
        assert "ok=true" in summary

    def test_no_args_outside_project_errors_with_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate` with no args + outside a project must still
        error — but with a friendlier hint pointing operators at either
        a path argument or `mdk init --project`."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["validate"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        # Either output stream may carry the error (Rich writes errors
        # via the main console in this codebase, not the err_console).
        assert "not inside a movate project" in combined.lower()
        # The hint mentions both alternatives.
        assert "mdk init --project" in combined or "<path>" in combined

    def test_no_args_matches_explicit_all_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate` and `mdk validate --all` should produce
        identical output (modulo timing) inside a project."""
        _bootstrap(tmp_path, monkeypatch, agents="rag-qa")
        r_default = runner.invoke(app, ["validate"], env={"COLUMNS": "200"})
        r_explicit = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert r_default.exit_code == 0
        assert r_explicit.exit_code == 0
        # Both emit the same shape of summary line.
        for r in (r_default, r_explicit):
            assert "passed=1" in r.stdout
            assert "failed=0" in r.stdout
        # Both render the workspace validation table.
        for r in (r_default, r_explicit):
            assert "Project validation" in r.stdout


@pytest.mark.unit
class TestBackCompat:
    def test_explicit_all_flag_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate --all` keeps working for scripts that pass
        the flag verbatim."""
        _bootstrap(tmp_path, monkeypatch, agents="rag-qa,ticket-triager")
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "passed=2" in result.stdout

    def test_path_argument_still_validates_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate rag-qa` (bare name) still validates exactly
        that one — not the whole project."""
        project = _bootstrap(tmp_path, monkeypatch, agents="rag-qa,ticket-triager")
        result = runner.invoke(app, ["validate", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        # Single-agent output shape: no `Project validation` table,
        # no `mdk_validate_summary:` greppable line (that's the
        # all-sweep summary, not a per-agent one).
        assert "Project validation" not in result.stdout
        assert "mdk_validate_summary:" not in result.stdout
        # Just the regular single-agent OK output.
        assert "rag-qa" in result.stdout
        assert "(agent)" in result.stdout
        _ = project  # silence unused

    def test_path_plus_all_is_still_mutex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing BOTH a path AND `--all` is still an explicit usage
        error (the same mutex shipped with `--all`)."""
        _bootstrap(tmp_path, monkeypatch, agents="rag-qa")
        result = runner.invoke(
            app,
            ["validate", "rag-qa", "--all"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "mutually exclusive" in combined.lower()


@pytest.mark.unit
class TestOutsideProject:
    def test_path_argument_outside_project_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing an explicit path WORKS outside a project — the path
        carries everything `_validate_agent` needs."""
        # Scaffold an agent at tmp_path (NO project root).
        agent_dir = tmp_path / "standalone-agent"
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", "standalone-agent", "--target", str(tmp_path)])
        assert (agent_dir / "agent.yaml").is_file()

        # Validate it by path; no project required.
        result = runner.invoke(
            app,
            ["validate", str(agent_dir)],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
