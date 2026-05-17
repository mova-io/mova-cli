"""``mdk add`` — tests for the current add_cmd.add interface.

Covers the live CLI surface registered in main.py (add_cmd.add, not the
legacy add.py). Key interface facts:

* **Discovery** — ``--list`` (no JSON mode) and ``--preview <template>``
* **Scaffolding** — ``mdk add <template>`` or ``mdk add <template> <rename>``
  or ``mdk add <template> --name <rename>``
* **Project-aware** — walks up from cwd for movate.yaml; exits 2 when no
  project found (no fallback to cwd)
* **Failure modes** — unknown template, existing dest, no args, unknown rename

Template names used in tests are from TEMPLATES (the current registry), not
from the legacy ROLE_TEMPLATES. Use 'rag-qa' and 'sql-writer' as stable test
targets; both are simple, have no external deps, and scaffold quickly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import TEMPLATES

runner = CliRunner(mix_stderr=False)


def _write_movate_yaml(parent: Path) -> Path:
    """Drop a minimal ``movate.yaml`` so add_cmd's walk-up finds ``parent``
    as a project root."""
    cfg = parent / "movate.yaml"
    cfg.write_text("# test project marker\n")
    return cfg


# ---------------------------------------------------------------------------
# Discovery: --list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_prints_catalog() -> None:
    """``mdk add --list`` exits 0 and names well-known templates."""
    result = runner.invoke(app, ["add", "--list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # A handful of stable templates should appear in the listing.
    for name in ("rag-qa", "sql-writer", "ticket-triager"):
        assert name in result.stdout, f"template {name!r} missing from catalog"


@pytest.mark.unit
def test_list_search_filters_by_name() -> None:
    """``--list --search sql`` shows sql-writer and filters out unrelated templates."""
    result_sql = runner.invoke(app, ["add", "--list", "--search", "sql"])
    result_all = runner.invoke(app, ["add", "--list"])
    assert result_sql.exit_code == 0, result_sql.stdout + result_sql.stderr
    assert result_all.exit_code == 0, result_all.stdout + result_all.stderr
    # sql-writer must survive the filter.
    assert "sql-writer" in result_sql.stdout
    # Filtered output should be shorter than unfiltered — some entries removed.
    assert len(result_sql.stdout) < len(result_all.stdout)


# ---------------------------------------------------------------------------
# Discovery: --preview
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("template", ["rag-qa", "sql-writer"])
def test_preview_prints_files(template: str) -> None:
    """``mdk add --preview <template>`` exits 0 and prints template files."""
    result = runner.invoke(app, ["add", "--preview", template])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Preview header includes the template name.
    assert template in result.stdout
    # Every template includes at least agent.yaml and prompt.md.
    assert "agent.yaml" in result.stdout
    assert "prompt.md" in result.stdout


@pytest.mark.unit
def test_preview_unknown_template_exits_two() -> None:
    """Unknown template for --preview → exit 2 with 'unknown template'."""
    result = runner.invoke(app, ["add", "--preview", "no-such-template"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "unknown template" in combined


@pytest.mark.unit
def test_preview_no_arg_exits_two() -> None:
    """``mdk add --preview`` with no template name → exit 2."""
    result = runner.invoke(app, ["add", "--preview"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_scaffolds_under_project_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mdk add rag-qa my-bot`` from a project root drops the agent at
    ``<root>/agents/my-bot/`` with the rag-qa template files."""
    project = tmp_path / "myproject"
    project.mkdir()
    _write_movate_yaml(project)
    (project / "agents").mkdir()
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["add", "rag-qa", "my-bot"])
    assert result.exit_code == 0, result.stdout + result.stderr

    dest = project / "agents" / "my-bot"
    assert dest.is_dir()
    assert (dest / "agent.yaml").is_file()
    assert (dest / "prompt.md").is_file()

    spec = yaml.safe_load((dest / "agent.yaml").read_text())
    assert spec["name"] == "my-bot"


@pytest.mark.unit
def test_add_name_flag_renames_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mdk add sql-writer --name my-sql-agent`` uses the explicit name."""
    project = tmp_path / "p"
    project.mkdir()
    _write_movate_yaml(project)
    (project / "agents").mkdir()
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["add", "sql-writer", "--name", "my-sql-agent"])
    assert result.exit_code == 0, result.stdout + result.stderr

    dest = project / "agents" / "my-sql-agent"
    assert dest.is_dir()
    spec = yaml.safe_load((dest / "agent.yaml").read_text())
    assert spec["name"] == "my-sql-agent"


@pytest.mark.unit
def test_add_template_name_is_default_agent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mdk add rag-qa`` with no rename uses the template name as the
    agent name and creates ``agents/rag-qa/``."""
    project = tmp_path / "p"
    project.mkdir()
    _write_movate_yaml(project)
    (project / "agents").mkdir()
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["add", "rag-qa"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (project / "agents" / "rag-qa" / "agent.yaml").is_file()


@pytest.mark.unit
def test_add_walks_up_to_find_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``mdk add`` from a subdirectory still lands the agent at the
    project root's ``agents/`` dir, not in the nested cwd."""
    project = tmp_path / "myproject"
    project.mkdir()
    _write_movate_yaml(project)
    (project / "agents").mkdir()
    nested = project / "subdir" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["add", "sql-writer"])
    assert result.exit_code == 0, result.stdout + result.stderr

    assert (project / "agents" / "sql-writer" / "agent.yaml").is_file()
    assert not (nested / "agents").exists()


@pytest.mark.unit
def test_add_rejects_existing_dest_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running ``mdk add`` over an existing agent without ``--force``
    must exit 2 — silently overwriting would lose edits."""
    project = tmp_path / "myproject"
    project.mkdir()
    _write_movate_yaml(project)
    (project / "agents").mkdir()
    monkeypatch.chdir(project)

    runner.invoke(app, ["add", "rag-qa"])
    prompt_path = project / "agents" / "rag-qa" / "prompt.md"
    prompt_path.write_text("# do not lose me\n")

    result = runner.invoke(app, ["add", "rag-qa"])
    assert result.exit_code == 2
    # Edit preserved.
    assert "do not lose me" in prompt_path.read_text()


@pytest.mark.unit
def test_add_force_overwrites_existing_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--force`` wipes the dir and re-scaffolds from the template."""
    project = tmp_path / "myproject"
    project.mkdir()
    _write_movate_yaml(project)
    (project / "agents").mkdir()
    monkeypatch.chdir(project)

    runner.invoke(app, ["add", "rag-qa"])
    (project / "agents" / "rag-qa" / "prompt.md").write_text("# user edit\n")

    result = runner.invoke(app, ["add", "rag-qa", "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    contents = (project / "agents" / "rag-qa" / "prompt.md").read_text()
    assert "user edit" not in contents


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_no_args_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk add`` with no arguments exits 2 with a pointer to --list."""
    project = tmp_path / "p"
    project.mkdir()
    _write_movate_yaml(project)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["add"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    # Should mention how to see options.
    assert "--list" in combined or "required" in combined.lower()


@pytest.mark.unit
def test_add_unknown_template_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown template name → exit 2 with 'unknown template'."""
    project = tmp_path / "p"
    project.mkdir()
    _write_movate_yaml(project)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["add", "no-such-template"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "unknown template" in combined


@pytest.mark.unit
def test_add_outside_project_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``mdk add`` outside any movate project exits 2 — no fallback
    to cwd in the current interface."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    result = runner.invoke(app, ["add", "rag-qa"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    # Should mention project or mdk init.
    assert "project" in combined.lower() or "init" in combined.lower()


@pytest.mark.unit
def test_add_project_flag_must_be_a_directory(tmp_path: Path) -> None:
    """``--target`` pointing at a non-directory (or non-existent path) exits 2
    or handles it gracefully — we don't create agents inside files."""
    not_a_dir = tmp_path / "not-a-dir.txt"
    not_a_dir.write_text("hi")
    result = runner.invoke(
        app,
        ["add", "rag-qa", "--target", str(not_a_dir)],
    )
    # Either exit 2 (preflight check) or 1 (scaffold error) — not 0.
    assert result.exit_code != 0


@pytest.mark.unit
def test_all_templates_are_known() -> None:
    """Sanity: the TEMPLATES registry must contain all names used in this
    test file so future renames don't silently skip tests."""
    for name in ("rag-qa", "sql-writer", "ticket-triager"):
        assert name in TEMPLATES, f"test uses {name!r} but it's not in TEMPLATES"
