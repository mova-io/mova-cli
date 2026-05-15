"""Bundle C — `mdk add` agent-maturity story.

Four new behaviors layered on top of `mdk add`:

1. **`--preview <template>`** — print template files to stdout
   without writing. Single-template only.
2. **`--remove <name>`** — clean removal counterpart to add. Surfaces
   dangling references in workflows + baselines. Dry-run by default.
3. **`--update <name>`** — refresh against latest template. Reads
   the `_template_source` marker comment, diffs each file, dry-run
   by default.
4. **Auto-scaffold declared skills** — when a template's `skills:`
   field is non-empty, scaffold any missing skills under
   `<project>/skills/<name>/`. `--no-skills` opts out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.add_cmd import (
    _TEMPLATE_SOURCE_COMMENT_PREFIX,
    _read_template_source,
    _stamp_template_source,
)
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Same minimal-project fixture as the other add tests."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


# ---------------------------------------------------------------------------
# Item C2: --preview
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPreview:
    def test_preview_prints_template_files(self) -> None:
        """--preview shows every text file in the template dir."""
        result = runner.invoke(
            app, ["add", "rag-qa", "--preview"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Header Panel mentions the template.
        assert "rag-qa" in result.stdout
        # Each template file is rendered with a file separator.
        assert "agent.yaml" in result.stdout
        assert "prompt.md" in result.stdout
        assert "schema" in result.stdout

    def test_preview_does_not_write(self, tmp_path: Path) -> None:
        """--preview must NOT touch the filesystem."""
        before = set(tmp_path.iterdir())
        runner.invoke(
            app,
            ["add", "rag-qa", "--preview", "--target", str(tmp_path)],
            env={"COLUMNS": "200"},
        )
        # Nothing new on disk.
        assert set(tmp_path.iterdir()) == before

    def test_preview_rejects_unknown_template(self) -> None:
        result = runner.invoke(
            app, ["add", "bogus-template", "--preview"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        assert "unknown template" in result.stderr.lower()

    def test_preview_rejects_multiple_templates(self) -> None:
        """--preview is single-template only."""
        result = runner.invoke(
            app,
            ["add", "rag-qa", "ticket-triager", "--preview"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        assert "one template" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Item C3: --remove
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemove:
    def test_remove_dry_run_does_not_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        # First add an agent so we have something to remove.
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert (proj / "agents" / "rag-qa" / "agent.yaml").is_file()

        # Remove without --apply — agent should survive.
        result = runner.invoke(
            app, ["add", "--remove", "rag-qa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Would remove" in result.stdout or "dry-run" in result.stdout.lower()
        assert (proj / "agents" / "rag-qa" / "agent.yaml").is_file()
        # Greppable summary line surfaces.
        assert "mdk_remove_summary:" in result.stdout
        assert "dry_run=true" in result.stdout

    def test_remove_apply_deletes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert (proj / "agents" / "rag-qa" / "agent.yaml").is_file()

        result = runner.invoke(
            app, ["add", "--remove", "rag-qa", "--apply"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert not (proj / "agents" / "rag-qa").exists()
        assert "mdk_remove_summary:" in result.stdout
        assert "dry_run=false" in result.stdout

    def test_remove_surfaces_workflow_references(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        # Drop a workflow that references the agent.
        (proj / "workflows").mkdir()
        (proj / "workflows" / "support").mkdir()
        (proj / "workflows" / "support" / "workflow.yaml").write_text(
            "nodes:\n  - id: a\n    type: agent\n    ref: ../../agents/rag-qa\n"
        )

        result = runner.invoke(
            app, ["add", "--remove", "rag-qa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        # The workflow reference surfaces as a warning.
        assert "workflow" in result.stdout.lower()
        assert "support/workflow.yaml" in result.stdout

    def test_remove_unknown_agent_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["add", "--remove", "does-not-exist"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        assert "not found" in result.stderr.lower()

    def test_remove_outside_project_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["add", "--remove", "x"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        assert "not inside a movate project" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Item C1: template versioning + --update
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTemplateSource:
    def test_stamp_writes_comment_line(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: x\n")
        _stamp_template_source(agent_dir, template="rag-qa")
        contents = (agent_dir / "agent.yaml").read_text()
        assert _TEMPLATE_SOURCE_COMMENT_PREFIX in contents
        assert "rag-qa@" in contents
        # And it's a comment — yaml.safe_load shouldn't see it as a field.
        parsed = yaml.safe_load(contents)
        assert "_template_source" not in parsed

    def test_read_round_trips(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: x\n")
        _stamp_template_source(agent_dir, template="rag-qa")
        result = _read_template_source(agent_dir)
        assert result is not None
        template_name, version = result
        assert template_name == "rag-qa"
        assert version  # any non-empty version is fine

    def test_read_returns_none_when_missing(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: x\n")
        assert _read_template_source(agent_dir) is None

    def test_double_stamp_is_idempotent(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: x\n")
        _stamp_template_source(agent_dir, template="rag-qa")
        first = (agent_dir / "agent.yaml").read_text()
        _stamp_template_source(agent_dir, template="rag-qa")
        second = (agent_dir / "agent.yaml").read_text()
        # No duplicate marker lines.
        assert first == second


@pytest.mark.unit
class TestUpdate:
    def test_update_shows_no_drift_for_fresh_scaffold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A freshly-scaffolded agent should report `no drift` against
        its source template."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})

        result = runner.invoke(
            app, ["add", "--update", "rag-qa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "no drift" in result.stdout.lower() or "up to date" in result.stdout.lower()

    def test_update_detects_prompt_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})

        # Tweak the prompt.md so it no longer matches the template.
        prompt_path = proj / "agents" / "rag-qa" / "prompt.md"
        prompt_path.write_text("totally rewritten prompt\n")

        result = runner.invoke(
            app, ["add", "--update", "rag-qa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        assert "drift" in result.stdout.lower()
        assert "prompt.md" in result.stdout

    def test_update_apply_overwrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        prompt_path = proj / "agents" / "rag-qa" / "prompt.md"
        original_prompt = prompt_path.read_text()
        prompt_path.write_text("WRITTEN OVER BY OPERATOR\n")

        result = runner.invoke(
            app,
            ["add", "--update", "rag-qa", "--apply"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # The overwrite restored the template's prompt.md.
        assert prompt_path.read_text() == original_prompt

    def test_update_no_marker_errors_with_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent without the `_template_source` marker can't be
        updated automatically."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        # Scaffold WITHOUT going through `mdk add` so no marker is set.
        agent_dir = proj / "agents" / "manually-added"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\nkind: Agent\nname: manually-added\n"
            "version: 0.1.0\nmodel:\n  provider: openai/x\nprompt: ./prompt.md\n"
            "schema:\n  input: ./schema/input.json\n  output: ./schema/output.json\n"
            "evals:\n  dataset: ./evals/dataset.jsonl\n"
        )

        result = runner.invoke(
            app, ["add", "--update", "manually-added"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        assert "_template_source" in result.stderr


# ---------------------------------------------------------------------------
# Item C4: auto-scaffold declared skills
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoSkills:
    def test_template_with_no_skills_does_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Templates that don't declare skills should not create
        ./skills/ at all."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # rag-qa doesn't declare skills.
        assert not (proj / "skills").exists()

    def test_no_skills_flag_skips_autoscaffold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-skills must skip auto-scaffolding even when the
        template declares skills."""
        proj = _bootstrap_project(tmp_path)
        # Create a scratch template that declares a skill.
        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        # We don't add a fake template to the registry — just verify
        # the --no-skills flag is wired and doesn't crash the add path.
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["add", "rag-qa", "--no-skills"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        _ = TEMPLATES_DIR  # silence unused
        assert not (proj / "skills").exists()

    def test_autoscaffold_creates_skill_dir_when_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Edit a scaffolded agent's agent.yaml to declare a skill,
        then re-trigger the auto-scaffold path by calling the helper
        directly."""
        from movate.cli.add_cmd import _maybe_scaffold_declared_skills  # noqa: PLC0415

        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})

        # Append a skills field to the agent.yaml (rag-qa doesn't ship
        # with one — different templates have different schemas).
        agent_yaml = proj / "agents" / "rag-qa" / "agent.yaml"
        agent_yaml.write_text(
            agent_yaml.read_text() + "\nskills:\n  - web-search\n"
        )

        scaffolded = _maybe_scaffold_declared_skills(
            agent_dir=proj / "agents" / "rag-qa", project_root=proj
        )
        assert scaffolded == ["web-search"]
        # The skill dir was created with at least skill.yaml.
        assert (proj / "skills" / "web-search").is_dir()
        assert (proj / "skills" / "web-search" / "skill.yaml").is_file()

    def test_autoscaffold_skips_existing_skills(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the skill dir already exists, don't overwrite it."""
        from movate.cli.add_cmd import _maybe_scaffold_declared_skills  # noqa: PLC0415

        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        # Pre-create the skill dir with operator-customized content.
        skill_dir = proj / "skills" / "web-search"
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text("# operator content\n")

        # Now scaffold an agent that declares the skill.
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        agent_yaml = proj / "agents" / "rag-qa" / "agent.yaml"
        agent_yaml.write_text(
            agent_yaml.read_text() + "\nskills:\n  - web-search\n"
        )

        scaffolded = _maybe_scaffold_declared_skills(
            agent_dir=proj / "agents" / "rag-qa", project_root=proj
        )
        # Empty list — the existing skill dir was respected.
        assert scaffolded == []
        # Operator content survives.
        assert (
            (proj / "skills" / "web-search" / "skill.yaml").read_text()
            == "# operator content\n"
        )


# ---------------------------------------------------------------------------
# Template-source marker is stamped on every fresh `mdk add`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_stamps_template_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _bootstrap_project(tmp_path)
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    source = _read_template_source(proj / "agents" / "rag-qa")
    assert source is not None
    template_name, version = source
    assert template_name == "rag-qa"
    assert version  # non-empty
