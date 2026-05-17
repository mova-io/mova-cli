"""`mdk add` — project-aware role-agent scaffolding.

Tests covering:

1. `--list` prints the role + core template catalogs.
2. Calling `add` outside a project errors with a pointer to `init --project`.
3. Inside a project: `mdk add rag-qa` creates ./agents/rag-qa/ from the
   rag-qa template, and the result passes load_agent().
4. Positional name override: `mdk add rag-qa pricing-qa` → ./agents/pricing-qa/.
5. Missing `agents/` dir: scaffold drops at project root.
6. Unknown template errors with a typo suggestion.
7. Existing agent dir + no `--force` errors before scaffold runs.
8. `--force` overwrites an existing agent dir.
9. Greppable `mdk_add_summary:` line surfaces on success.
10. Empty template arg errors with hint to use `--list`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Create a minimal movate project layout at tmp_path/proj."""
    proj = tmp_path / "proj"
    proj.mkdir()
    # Minimal valid ProjectConfig — strict (extra="forbid") so we only
    # write the agents_dir field. The wrapper's project-root detection
    # only looks for movate.yaml's existence, not its content.
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


# ---------------------------------------------------------------------------
# Item 1: --list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_prints_role_table_only() -> None:
    """--list (and 'mdk add list') shows the role-based catalog only;
    the core templates table was removed to reduce noise."""
    for invoke_args in (["add", "--list"], ["add", "list"]):
        result = runner.invoke(app, invoke_args, env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Role-based templates" in result.stdout
        assert "Core templates" not in result.stdout
        assert "rag-qa" in result.stdout
        assert "ticket-triager" in result.stdout


# ---------------------------------------------------------------------------
# Item 2: outside a project → error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_outside_project_errors_with_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No movate.yaml anywhere up the tree → exit 2 with pointer to
    `mdk init --project`."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["add", "rag-qa"])
    assert result.exit_code == 2
    assert "not inside a movate project" in result.stderr
    assert "mdk init --project" in result.stderr


# ---------------------------------------------------------------------------
# Item 3: happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_role_template_creates_agent_in_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "rag-qa"])
    assert result.exit_code == 0, result.stdout + result.stderr
    agent_dir = proj / "agents" / "rag-qa"
    assert (agent_dir / "agent.yaml").is_file()
    assert (agent_dir / "prompt.md").is_file()
    bundle = load_agent(agent_dir)
    assert bundle.spec.name == "rag-qa"


# ---------------------------------------------------------------------------
# Item 4: positional name override
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_positional_name_renames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "rag-qa", "pricing-qa"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Renamed dir.
    assert (proj / "agents" / "pricing-qa" / "agent.yaml").is_file()
    # Not the default name.
    assert not (proj / "agents" / "rag-qa").exists()
    # And agent_yaml.name reflects the rename (loader applies the
    # __AGENT_NAME__ substitution).
    bundle = load_agent(proj / "agents" / "pricing-qa")
    assert bundle.spec.name == "pricing-qa"


# ---------------------------------------------------------------------------
# Item 5: no agents/ dir → scaffold at project root
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_drops_at_project_root_when_no_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "noagents"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    # Deliberately NO agents/ dir.
    monkeypatch.chdir(proj)

    result = runner.invoke(app, ["add", "rag-qa"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Lands at project root, not under a non-existent agents/.
    assert (proj / "rag-qa" / "agent.yaml").is_file()
    assert not (proj / "agents").exists()


# ---------------------------------------------------------------------------
# Item 6: unknown template → suggestion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_template_suggests_close_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "ragqa"])
    assert result.exit_code == 2
    # The error contains the suggestion AND the unknown name.
    assert "unknown template" in result.stderr
    assert "ragqa" in result.stderr
    # The closest match should be rag-qa.
    assert "rag-qa" in result.stderr


@pytest.mark.unit
def test_unknown_template_with_no_close_match_lists_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "totally-bogus-name"])
    assert result.exit_code == 2
    # Should at minimum surface the available list.
    assert "rag-qa" in result.stderr or "available" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Item 7: existing dir without --force errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_existing_agent_without_force_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    # Pre-create the destination so the add command can't write to it.
    occupied = proj / "agents" / "rag-qa"
    occupied.mkdir()
    (occupied / "marker.txt").write_text("don't overwrite")

    result = runner.invoke(app, ["add", "rag-qa"])
    assert result.exit_code == 2
    assert "already exists" in (result.stdout + result.stderr)
    # Marker survived.
    assert (occupied / "marker.txt").read_text() == "don't overwrite"


# ---------------------------------------------------------------------------
# Item 8: --force overwrites
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_force_overwrites_existing_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    occupied = proj / "agents" / "rag-qa"
    occupied.mkdir()
    (occupied / "stale.txt").write_text("old")

    result = runner.invoke(app, ["add", "rag-qa", "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Stale file is gone, replaced by template files.
    assert not (occupied / "stale.txt").exists()
    assert (occupied / "agent.yaml").is_file()


# ---------------------------------------------------------------------------
# Item 9: greppable summary line
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_emits_greppable_summary_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "rag-qa", "--force"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "mdk_add_summary:" in result.stdout
    assert "template=rag-qa" in result.stdout
    assert "name=rag-qa" in result.stdout
    assert "ok=true" in result.stdout


# ---------------------------------------------------------------------------
# Item 10: missing template arg
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_template_arg_errors_with_hint_to_use_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add"])
    assert result.exit_code == 2
    assert "mdk add --list" in result.stderr or "required" in result.stderr.lower()
