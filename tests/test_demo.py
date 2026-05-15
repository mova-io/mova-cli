"""Sprint P — `mdk demo` tests.

Three layers:

1. **Helpers** — _resolve_template_root finds the bundled template;
   _write_project_files writes the right set of files.
2. **CLI happy path** — `mdk demo` creates a complete runnable project,
   re-running with --force overwrites cleanly.
3. **CLI safety** — re-running without --force exits 2; --dry-run
   doesn't write; the resulting project is loadable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.demo_cmd import (
    _AGENT_NAME,
    _resolve_template_root,
    _write_project_files,
)
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_template_root_returns_existing_dir() -> None:
    root = _resolve_template_root()
    assert root.is_dir()
    # Sanity: the template has the files demo needs to copy.
    assert (root / "agent.yaml").is_file()
    assert (root / "prompt.md").is_file()


@pytest.mark.unit
def test_write_project_files_creates_expected_files(tmp_path: Path) -> None:
    created = _write_project_files(tmp_path)
    paths = {p.name for p in created}
    assert paths == {"movate.yaml", ".env.example", ".gitignore"}
    # All three actually exist on disk
    for p in created:
        assert p.is_file(), p


@pytest.mark.unit
def test_write_project_files_movate_yaml_is_valid(tmp_path: Path) -> None:
    """The generated movate.yaml must parse — operators trust it works."""
    _write_project_files(tmp_path)
    data = yaml.safe_load((tmp_path / "movate.yaml").read_text())
    assert data["api_version"] == "movate/v1"
    assert data["kind"] == "Project"
    assert data["name"] == "demo-faq"


# ---------------------------------------------------------------------------
# CLI: happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_demo_creates_full_project(tmp_path: Path) -> None:
    target = tmp_path / "demo-faq"
    result = runner.invoke(app, ["demo", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr

    # Project files
    assert (target / "movate.yaml").is_file()
    assert (target / ".env.example").is_file()
    assert (target / ".gitignore").is_file()

    # Agent files
    agent = target / "agents" / _AGENT_NAME
    assert (agent / "agent.yaml").is_file()
    assert (agent / "prompt.md").is_file()

    # Sample eval dataset
    dataset = agent / "evals" / "dataset.jsonl"
    assert dataset.is_file()
    # Each line should be valid JSON
    for line in dataset.read_text().splitlines():
        if line.strip():
            json.loads(line)  # raises if not valid


@pytest.mark.unit
def test_cli_demo_substitutes_agent_name_in_yaml(tmp_path: Path) -> None:
    """The template's __AGENT_NAME__ sentinel must be replaced."""
    target = tmp_path / "demo-faq"
    runner.invoke(app, ["demo", str(target)])
    agent_yaml = (target / "agents" / _AGENT_NAME / "agent.yaml").read_text()
    assert "__AGENT_NAME__" not in agent_yaml
    assert f"name: {_AGENT_NAME}" in agent_yaml


@pytest.mark.unit
def test_cli_demo_default_directory_is_demo_faq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No arg → creates ./demo-faq relative to cwd."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "demo-faq").is_dir()


@pytest.mark.unit
def test_cli_demo_custom_directory_name(tmp_path: Path) -> None:
    target = tmp_path / "my-first-agent"
    result = runner.invoke(app, ["demo", str(target)])
    assert result.exit_code == 0
    assert target.is_dir()


# ---------------------------------------------------------------------------
# CLI: safety gates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_demo_refuses_existing_directory_without_force(tmp_path: Path) -> None:
    target = tmp_path / "demo-faq"
    target.mkdir()
    (target / "important.txt").write_text("don't lose me!\n")

    result = runner.invoke(app, ["demo", str(target)])
    assert result.exit_code == 2
    # Operator's file is intact
    assert (target / "important.txt").read_text() == "don't lose me!\n"


@pytest.mark.unit
def test_cli_demo_force_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "demo-faq"
    target.mkdir()
    (target / "old.txt").write_text("will be wiped\n")

    result = runner.invoke(app, ["demo", str(target), "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Old file is gone
    assert not (target / "old.txt").exists()
    # New project structure is there
    assert (target / "movate.yaml").is_file()


@pytest.mark.unit
def test_cli_demo_dry_run_does_not_write(tmp_path: Path) -> None:
    target = tmp_path / "demo-faq"
    result = runner.invoke(app, ["demo", str(target), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    # Nothing written
    assert not target.exists()


@pytest.mark.unit
def test_cli_demo_output_lists_next_steps(tmp_path: Path) -> None:
    """Operators should see actionable next-step commands in the output."""
    target = tmp_path / "demo-faq"
    result = runner.invoke(app, ["demo", str(target)])
    assert result.exit_code == 0
    # Each suggested command appears in the panel
    assert "cd " in result.stdout
    assert "mdk run" in result.stdout
    assert "mdk eval" in result.stdout


# ---------------------------------------------------------------------------
# Resulting project loadability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_demo_project_movate_yaml_parses(tmp_path: Path) -> None:
    target = tmp_path / "demo-faq"
    runner.invoke(app, ["demo", str(target)])
    data = yaml.safe_load((target / "movate.yaml").read_text())
    assert data["api_version"] == "movate/v1"


@pytest.mark.unit
def test_demo_project_agent_yaml_parses(tmp_path: Path) -> None:
    target = tmp_path / "demo-faq"
    runner.invoke(app, ["demo", str(target)])
    agent_yaml = target / "agents" / _AGENT_NAME / "agent.yaml"
    data = yaml.safe_load(agent_yaml.read_text())
    assert data["api_version"] == "movate/v1"
    assert data["kind"] == "Agent"
    assert data["name"] == _AGENT_NAME
