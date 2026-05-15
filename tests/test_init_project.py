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
    # Canonical filename (post-MVP rename, May 2026): project.yaml.
    assert (proj / "project.yaml").is_file()
    # Legacy names NOT written by `mdk init` going forward (they're
    # still accepted on load for back-compat).
    assert not (proj / "movate.yaml").exists()
    assert not (proj / "policy.yaml").exists()
    assert (proj / ".env.example").is_file()
    assert (proj / ".gitignore").is_file()
    assert (proj / "agents").is_dir()
    # .gitkeep so empty agents/ survives git add
    assert (proj / "agents" / ".gitkeep").is_file()


@pytest.mark.unit
def test_init_project_yaml_is_valid_project_config(tmp_path: Path) -> None:
    """The bootstrapped project.yaml MUST validate as ProjectConfig.

    Originally the template carried v1-spec metadata (api_version /
    kind / name / description / version) plus a stray
    ``defaults.model.provider`` field. ProjectConfig is ``extra=forbid``,
    so every freshly-bootstrapped project blew up on the first
    ``mdk validate``. The May-2026 MVP rename canonized the filename
    to `project.yaml`; the content remains strict-ProjectConfig and
    keeps the project name in a YAML comment header (docs/runbook
    falls back to ``root.name``).
    """
    from movate.core.config import ProjectConfig  # noqa: PLC0415

    runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    raw = (tmp_path / "my-proj" / "project.yaml").read_text()
    # Project name appears anywhere in the file (banner header is
    # multi-line, so we don't pin to line 1).
    assert "my-proj" in raw
    # Body parses + validates cleanly.
    data = yaml.safe_load(raw)
    cfg = ProjectConfig.model_validate(data)
    assert cfg.agents_dir == "./agents"


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

    The project name surfaces in the movate.yaml comment header (the
    YAML body itself contains only ProjectConfig-valid fields).
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--project"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "project.yaml").is_file()
    raw = (tmp_path / "project.yaml").read_text()
    # The project name appears in the canonical comment header (the
    # banner spans multiple lines, so search anywhere in the file).
    assert tmp_path.name in raw


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
    # Operator's existing legacy `movate.yaml` simulates a pre-MVP
    # project. `--force` should overwrite by writing the NEW canonical
    # `project.yaml`; the old legacy file is left untouched by the
    # writer (operator can delete it manually after migration).
    (tmp_path / "movate.yaml").write_text("existing: true\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--project", "--force"])
    assert result.exit_code == 0
    # New canonical filename is written.
    text = (tmp_path / "project.yaml").read_text()
    assert "agents_dir: ./agents" in text


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
    # Canonical post-MVP filename.
    assert (tmp_path / "my-proj" / "project.yaml").is_file()


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
    """Bare `mdk init` (no name, no --project, no -t) is a usage
    error. Error message surfaces the three common-uses paths."""
    result = runner.invoke(app, ["init", "--target", str(tmp_path)])
    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).lower()
    # Post-default-change wording: "name required" + at least one of
    # the common-uses lines pointing operators forward.
    assert "name required" in combined
    assert "mdk init" in combined  # at least one example shown
