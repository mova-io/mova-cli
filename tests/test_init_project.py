"""Sprint P — `mdk init --project` tests.

Three layers:

1. **CLI happy path** — `mdk init --project my-proj` creates the
   expected file layout, takes an initial snapshot.
2. **In-place bootstrap** — `mdk init --project` (no name) bootstraps
   the current directory.
3. **Safety + agent-mode regression** — refuses overwrite without
   --force, --skip-snapshot works, agent mode still functions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Happy path: named project
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_project_creates_full_layout(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + result.stderr

    proj = tmp_path / "my-proj"
    assert proj.is_dir()
    assert (proj / "movate.yaml").is_file()
    assert (proj / ".env.example").is_file()
    assert (proj / ".gitignore").is_file()
    assert (proj / "agents").is_dir()
    # .gitkeep so empty agents/ survives git add
    assert (proj / "agents" / ".gitkeep").is_file()


@pytest.mark.unit
def test_init_project_movate_yaml_has_project_name(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    data = yaml.safe_load((tmp_path / "my-proj" / "movate.yaml").read_text())
    assert data["api_version"] == "movate/v1"
    assert data["kind"] == "Project"
    assert data["name"] == "my-proj"


@pytest.mark.unit
def test_init_project_creates_initial_snapshot(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    assert result.exit_code == 0
    proj = tmp_path / "my-proj"
    # Snapshot directory should exist
    assert (proj / ".movate" / "snapshots").is_dir()
    # At least one snapshot subdirectory (the initial one)
    snaps = [p for p in (proj / ".movate" / "snapshots").iterdir() if p.is_dir()]
    assert len(snaps) >= 1
    # The hint appears in the output
    assert "snapshot" in result.stdout.lower()


@pytest.mark.unit
def test_init_project_skip_snapshot_flag(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "init",
            "my-proj",
            "--project",
            "--skip-snapshot",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    proj = tmp_path / "my-proj"
    # No snapshots dir created
    assert not (proj / ".movate" / "snapshots").is_dir()


# ---------------------------------------------------------------------------
# Happy path: in-place bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_project_in_place_uses_cwd_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a name, --project bootstraps the current directory in place.

    The project name in movate.yaml is derived from the directory name.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--project"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "movate.yaml").is_file()
    data = yaml.safe_load((tmp_path / "movate.yaml").read_text())
    assert data["name"] == tmp_path.name


@pytest.mark.unit
def test_init_project_in_place_refuses_existing_movate_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrapping a directory that already has movate.yaml is rejected."""
    (tmp_path / "movate.yaml").write_text("existing: true\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--project"])
    assert result.exit_code == 2
    # Existing file untouched
    assert (tmp_path / "movate.yaml").read_text() == "existing: true\n"


@pytest.mark.unit
def test_init_project_force_overwrites_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "movate.yaml").write_text("existing: true\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--project", "--force"])
    assert result.exit_code == 0
    # File replaced
    text = (tmp_path / "movate.yaml").read_text()
    assert "existing: true" not in text
    assert "api_version: movate/v1" in text


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_project_named_refuses_existing_dir_without_force(
    tmp_path: Path,
) -> None:
    (tmp_path / "my-proj").mkdir()
    (tmp_path / "my-proj" / "important.txt").write_text("keep me\n")

    result = runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    assert result.exit_code == 2
    # Operator's file is intact
    assert (tmp_path / "my-proj" / "important.txt").read_text() == "keep me\n"


@pytest.mark.unit
def test_init_project_named_force_wipes_and_recreates(tmp_path: Path) -> None:
    (tmp_path / "my-proj").mkdir()
    (tmp_path / "my-proj" / "old.txt").write_text("doomed\n")

    result = runner.invoke(
        app,
        ["init", "my-proj", "--project", "--force", "--target", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert not (tmp_path / "my-proj" / "old.txt").exists()
    assert (tmp_path / "my-proj" / "movate.yaml").is_file()


# ---------------------------------------------------------------------------
# Agent-mode regression
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_mode_still_works(tmp_path: Path) -> None:
    """mdk init <name> without --project still scaffolds an agent."""
    result = runner.invoke(app, ["init", "my-agent", "-t", "default", "--target", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "my-agent" / "agent.yaml").is_file()


@pytest.mark.unit
def test_no_name_no_project_flag_exits_2(tmp_path: Path) -> None:
    """Bare `mdk init` (no name, no --project) is a usage error."""
    result = runner.invoke(app, ["init", "--target", str(tmp_path)])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "agent name required" in combined.lower() or "--project" in combined
