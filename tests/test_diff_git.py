"""Sprint Q — `mdk diff --git` tests.

Two layers:

1. **Parser** — :func:`_parse_git_name_status` correctly extracts
   status + path from git's tab-separated output.
2. **CLI** — `mdk diff --git` works end-to-end on a real temp git
   repo, refuses non-git directories, ignores stray positional args.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.diff_cmd import _parse_git_name_status
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# Many CI environments don't have git on PATH. Skip the whole suite
# if it's missing — better than red CI for an environment problem.
_GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not available on PATH")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseGitNameStatus:
    def test_empty_output_returns_empty_list(self) -> None:
        assert _parse_git_name_status("") == []
        assert _parse_git_name_status("\n\n") == []

    def test_modified_file(self) -> None:
        changes = _parse_git_name_status("M\tagents/triage/agent.yaml\n")
        assert changes == [{"status": "modified", "path": "agents/triage/agent.yaml"}]

    def test_added_file(self) -> None:
        changes = _parse_git_name_status("A\tagents/new-agent/agent.yaml\n")
        assert changes == [{"status": "added", "path": "agents/new-agent/agent.yaml"}]

    def test_deleted_file(self) -> None:
        changes = _parse_git_name_status("D\tagents/old/agent.yaml\n")
        assert changes == [{"status": "deleted", "path": "agents/old/agent.yaml"}]

    def test_renamed_uses_destination_path(self) -> None:
        """Renamed entries are `R100\told\tnew` — we report `new`."""
        changes = _parse_git_name_status(
            "R100\tagents/old-name/agent.yaml\tagents/new-name/agent.yaml\n"
        )
        assert len(changes) == 1
        assert changes[0]["status"] == "renamed"
        assert changes[0]["path"] == "agents/new-name/agent.yaml"

    def test_multiple_lines(self) -> None:
        output = "M\tmovate.yaml\nA\tagents/new/agent.yaml\nD\tagents/old/prompt.md\n"
        changes = _parse_git_name_status(output)
        assert len(changes) == 3
        statuses = [c["status"] for c in changes]
        assert statuses == ["modified", "added", "deleted"]

    def test_skips_blank_lines(self) -> None:
        output = "M\tx\n\n\nA\ty\n"
        changes = _parse_git_name_status(output)
        assert len(changes) == 2


# ---------------------------------------------------------------------------
# CLI — end-to-end on a real temp git repo
# ---------------------------------------------------------------------------


def _make_git_repo(root: Path) -> None:
    """Initialize a minimal git repo with one committed agent."""
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)

    (root / "movate.yaml").write_text("api_version: movate/v1\nname: test\n")
    (root / "agents").mkdir()
    (root / "agents" / "triage").mkdir()
    (root / "agents" / "triage" / "agent.yaml").write_text(
        "name: triage\nmodel: openai/gpt-4o-mini\n"
    )

    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial"], cwd=root, check=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Temp git repo with one committed agent."""
    _make_git_repo(tmp_path)
    return tmp_path


@pytest.mark.unit
def test_cli_diff_git_clean_repo_exits_0(git_repo: Path) -> None:
    """No drift → exit 0 + 'no drift' message."""
    result = runner.invoke(app, ["diff", "--git", "--project", str(git_repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no drift" in result.stdout.lower()


@pytest.mark.unit
def test_cli_diff_git_modified_file_exits_1(git_repo: Path) -> None:
    """Modified file → exit 1 + table shows the file."""
    (git_repo / "agents" / "triage" / "agent.yaml").write_text(
        "name: triage\nmodel: anthropic/claude-haiku\n"  # changed
    )
    result = runner.invoke(app, ["diff", "--git", "--project", str(git_repo)])
    assert result.exit_code == 1
    assert "modified" in result.stdout
    assert "agents/triage/agent.yaml" in result.stdout


@pytest.mark.unit
def test_cli_diff_git_added_file_exits_1(git_repo: Path) -> None:
    """Untracked file in captured root → shows as added.

    Note: git diff HEAD only shows tracked changes; untracked files
    aren't reported. We add+stage to surface the addition.
    """
    new_agent = git_repo / "agents" / "summary"
    new_agent.mkdir()
    (new_agent / "agent.yaml").write_text("name: summary\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)

    result = runner.invoke(app, ["diff", "--git", "--project", str(git_repo)])
    assert result.exit_code == 1
    assert "added" in result.stdout
    assert "agents/summary/agent.yaml" in result.stdout


@pytest.mark.unit
def test_cli_diff_git_non_git_directory_exits_2(tmp_path: Path) -> None:
    """Project without .git/ → exit 2 with a clear error."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\n")
    result = runner.invoke(app, ["diff", "--git", "--project", str(tmp_path)])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "not a git" in combined.lower() or ".git" in combined


@pytest.mark.unit
def test_cli_diff_git_with_stray_snap_args_warns_but_proceeds(
    git_repo: Path,
) -> None:
    """If operator mixes --git with snap args, we warn but use git mode."""
    result = runner.invoke(
        app,
        ["diff", "abc1234", "def5678", "--git", "--project", str(git_repo)],
    )
    # No drift in the test repo → exit 0 (git mode took over)
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "ignores positional" in combined.lower() or "no drift" in combined.lower()


@pytest.mark.unit
def test_cli_diff_git_json_output(git_repo: Path) -> None:
    """--json emits a structured payload pipeable to jq."""
    (git_repo / "movate.yaml").write_text("api_version: movate/v1\nname: changed\n")
    result = runner.invoke(app, ["diff", "--git", "--json", "--project", str(git_repo)])
    assert result.exit_code == 1
    # The JSON output contains a `changes` list
    assert '"changes"' in result.stdout
    assert '"modified"' in result.stdout
    assert "movate.yaml" in result.stdout


@pytest.mark.unit
def test_cli_diff_git_custom_ref(git_repo: Path) -> None:
    """--ref accepts an arbitrary git ref (commit sha, branch, tag)."""
    # Make a second commit so we have two refs to compare against.
    (git_repo / "movate.yaml").write_text("api_version: movate/v1\nname: v2\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "v2"], cwd=git_repo, check=True)

    # Diff working tree against HEAD~1 (the first commit) — movate.yaml
    # changed between then and now.
    result = runner.invoke(
        app,
        ["diff", "--git", "--ref", "HEAD~1", "--project", str(git_repo)],
    )
    assert result.exit_code == 1
    assert "modified" in result.stdout
    assert "movate.yaml" in result.stdout


@pytest.mark.unit
def test_cli_diff_git_unknown_ref_exits_2(git_repo: Path) -> None:
    """Bad --ref → git returns nonzero → exit 2 with error."""
    result = runner.invoke(
        app,
        [
            "diff",
            "--git",
            "--ref",
            "nonexistent-ref",
            "--project",
            str(git_repo),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Snapshot-mode regression
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_diff_snapshot_mode_still_needs_two_args(tmp_path: Path) -> None:
    """Bare `mdk diff` (no --git, no snap args) is a usage error."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\n")
    result = runner.invoke(app, ["diff", "--project", str(tmp_path)])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "two positional" in combined.lower() or "--git" in combined
