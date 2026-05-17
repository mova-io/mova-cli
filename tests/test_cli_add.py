"""``mdk add <name> --template <role>`` — project-aware role scaffolding.

Companion to :mod:`tests.test_cli_scaffold` (which covers
``mdk scaffold tool``) and :mod:`tests.test_roles` (which covers the
role *catalog*). This file exercises the CLI surface of ``mdk add``:

* **Scaffolding** — drops the agent under ``<project>/agents/<name>/``,
  resolved by walking up from cwd for ``movate.yaml`` or honoring
  ``--project``.
* **Discovery** — ``--list-roles`` (table + JSON) and
  ``--describe <role>`` print without scaffolding.
* **Failure modes** — missing template, unknown template, missing name,
  existing dest, ``--force`` overwrite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import ROLE_TEMPLATES

runner = CliRunner(mix_stderr=False)


def _write_movate_yaml(parent: Path) -> Path:
    """Drop a minimal ``movate.yaml`` so ``mdk add`` recognises ``parent``
    as a project root via walk-up."""
    cfg = parent / "movate.yaml"
    cfg.write_text("# test project marker\n")
    return cfg


# ---------------------------------------------------------------------------
# Discovery: --list-roles
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_roles_prints_catalog() -> None:
    """``mdk add --list-roles`` exits 0 + names every role in the
    catalog. The Mova iO wizard depends on this list staying stable."""
    result = runner.invoke(app, ["add", "--list-roles"])
    assert result.exit_code == 0, result.stdout + result.stderr
    for name in ROLE_TEMPLATES:
        assert name in result.stdout, f"role {name!r} missing from catalog"


@pytest.mark.unit
def test_list_roles_json_is_machine_parseable() -> None:
    """``mdk add --list-roles --json`` emits a JSON array of role
    objects with the fields the wizard reads (``name``, ``description``,
    ``role``, ``tags``)."""
    result = runner.invoke(app, ["add", "--list-roles", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == len(ROLE_TEMPLATES)
    by_name = {entry["name"]: entry for entry in payload}
    for name in ROLE_TEMPLATES:
        assert name in by_name, f"role {name!r} missing from JSON catalog"
        entry = by_name[name]
        # The four wizard-facing fields.
        assert "description" in entry
        assert "role" in entry
        assert "tags" in entry
        assert isinstance(entry["tags"], list)


# ---------------------------------------------------------------------------
# Discovery: --describe
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("role", sorted(ROLE_TEMPLATES.keys()))
def test_describe_role_prints_metadata(role: str) -> None:
    """``mdk add --describe <role>`` prints the role's metadata and
    the files it would scaffold; exits 0; does not create anything."""
    result = runner.invoke(app, ["add", "--describe", role])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert role in result.stdout
    # The describe block lists scaffolded files including ROLE.md.
    assert "ROLE.md" in result.stdout
    assert "agent.yaml" in result.stdout


@pytest.mark.unit
def test_describe_unknown_role_exits_two() -> None:
    """Unknown role → exit 2 with a clear ``unknown template`` message."""
    result = runner.invoke(app, ["add", "--describe", "no-such-role"])
    assert result.exit_code == 2
    assert "unknown template" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_scaffolds_under_project_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mdk add foo --template support-triage`` from a project root
    lands the agent at ``<root>/agents/foo/`` with the placeholder
    substituted and the role template's files present."""
    project = tmp_path / "myproject"
    project.mkdir()
    _write_movate_yaml(project)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["add", "ticket-triage", "--template", "support-triage"])
    assert result.exit_code == 0, result.stdout + result.stderr

    dest = project / "agents" / "ticket-triage"
    assert dest.is_dir()
    assert (dest / "agent.yaml").is_file()
    assert (dest / "prompt.md").is_file()
    assert (dest / "evals" / "dataset.jsonl").is_file()
    assert (dest / "ROLE.md").is_file()

    spec = yaml.safe_load((dest / "agent.yaml").read_text())
    assert spec["name"] == "ticket-triage"
    assert spec["role"] == "support-triage"
    assert spec["api_version"] == "movate/v1"


@pytest.mark.unit
def test_add_walks_up_to_find_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running ``mdk add`` from a subdirectory of the project still
    lands the agent at the project's ``agents/`` dir, not the cwd's."""
    project = tmp_path / "myproject"
    project.mkdir()
    _write_movate_yaml(project)
    nested = project / "subdir" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["add", "nested-agent", "--template", "sql-writer"])
    assert result.exit_code == 0, result.stdout + result.stderr

    # Agent landed at the project root, not in the nested cwd.
    assert (project / "agents" / "nested-agent" / "agent.yaml").is_file()
    assert not (nested / "agents").exists()


@pytest.mark.unit
def test_add_project_flag_overrides_walk_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--project <path>`` wins over cwd walk-up. Used by Mova iO's
    wizard which knows the target project ID up front."""
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    _write_movate_yaml(project_a)
    _write_movate_yaml(project_b)
    # cwd would resolve to project_a if walk-up ran, but --project
    # forces project_b.
    monkeypatch.chdir(project_a)

    result = runner.invoke(
        app,
        ["add", "x", "--template", "reply-drafter", "--project", str(project_b)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (project_b / "agents" / "x" / "agent.yaml").is_file()
    assert not (project_a / "agents").exists()


@pytest.mark.unit
def test_add_falls_back_to_cwd_when_no_movate_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``movate.yaml`` anywhere → use cwd (with a warning). Lets
    a user run ``mdk add`` in a fresh dir without first scaffolding
    a project."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    result = runner.invoke(app, ["add", "loose", "--template", "text-classifier"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (bare / "agents" / "loose" / "agent.yaml").is_file()
    # User-visible warning so they know the fallback fired.
    assert "no" in result.stdout.lower() and "movate.yaml" in result.stdout


@pytest.mark.unit
def test_add_rejects_existing_dest_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running ``mdk add`` over an existing agent dir without
    ``--force`` must exit 2 — silently overwriting would lose edits."""
    project = tmp_path / "myproject"
    project.mkdir()
    _write_movate_yaml(project)
    monkeypatch.chdir(project)

    runner.invoke(app, ["add", "dup", "--template", "support-triage"])
    # User edits the prompt.
    prompt_path = project / "agents" / "dup" / "prompt.md"
    prompt_path.write_text("# do not lose me\n")

    result = runner.invoke(app, ["add", "dup", "--template", "support-triage"])
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
    monkeypatch.chdir(project)

    runner.invoke(app, ["add", "dup", "--template", "support-triage"])
    (project / "agents" / "dup" / "prompt.md").write_text("# user edit\n")

    result = runner.invoke(app, ["add", "dup", "--template", "support-triage", "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    contents = (project / "agents" / "dup" / "prompt.md").read_text()
    assert "user edit" not in contents


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_missing_name_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``mdk add`` with no name and no discovery flag is a
    user error (exit 2). Hint mentions ``--list-roles`` / ``--describe``."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["add", "--template", "support-triage"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "name" in combined.lower()


@pytest.mark.unit
def test_add_missing_template_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk add foo`` with no ``--template`` is a user error. The
    error lists available roles so the user can pick one without
    consulting docs."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["add", "foo"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "--template" in combined


@pytest.mark.unit
def test_add_unknown_template_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown template name → exit 2 with the list of valid names."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["add", "foo", "--template", "no-such-thing"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "unknown template" in combined


@pytest.mark.unit
def test_add_project_flag_must_be_a_directory(tmp_path: Path) -> None:
    """``--project`` pointing at a non-directory (or non-existent
    path) exits 2 — we'd otherwise create a weird tree under a file."""
    not_a_dir = tmp_path / "not-a-dir.txt"
    not_a_dir.write_text("hi")
    result = runner.invoke(
        app,
        ["add", "x", "--template", "support-triage", "--project", str(not_a_dir)],
    )
    assert result.exit_code == 2
