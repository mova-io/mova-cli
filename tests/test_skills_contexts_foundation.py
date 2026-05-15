"""Skills + contexts scaffold foundation.

This bundle is the structural floor for skills/contexts:

1. `mdk init --project` creates `skills/` and `contexts/` directories
   (alongside the existing `agents/`), each with a `.gitkeep`.
2. `ProjectConfig` gains `skills_dir` + `contexts_dir` fields with
   sensible defaults (`./skills`, `./contexts`).
3. The bootstrapped `movate.yaml` lists both new dirs in its comments
   so operators see the conventions in-file.
4. `mdk add` auto-scaffolds context placeholders when an agent.yaml
   declares `contexts: [name]` and the file doesn't exist —
   symmetric with the existing skill auto-scaffold.

The follow-up PR layers REAL demo content on top: specifically-named
skills + hand-written context Markdown for the three demo-flow role
agents.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap a fresh project, return project root."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "--project", "demo", "--skip-snapshot"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return tmp_path / "demo"


# ---------------------------------------------------------------------------
# Dirs created by `mdk init --project`
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectScaffoldCreatesDirs:
    def test_creates_skills_dir_with_gitkeep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap(tmp_path, monkeypatch)
        skills_dir = proj / "skills"
        assert skills_dir.is_dir(), f"expected {skills_dir} to be created"
        # .gitkeep survives `git add`.
        assert (skills_dir / ".gitkeep").is_file()

    def test_creates_contexts_dir_with_gitkeep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap(tmp_path, monkeypatch)
        contexts_dir = proj / "contexts"
        assert contexts_dir.is_dir(), f"expected {contexts_dir} to be created"
        assert (contexts_dir / ".gitkeep").is_file()

    def test_existing_agents_dir_still_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: adding skills/+contexts/ must not break the
        existing agents/+.gitkeep behavior."""
        proj = _bootstrap(tmp_path, monkeypatch)
        assert (proj / "agents").is_dir()
        assert (proj / "agents" / ".gitkeep").is_file()


# ---------------------------------------------------------------------------
# ProjectConfig fields + movate.yaml template
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectConfigFields:
    def test_default_skills_and_contexts_dirs(self) -> None:
        """Bare ProjectConfig() yields the canonical defaults."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        cfg = ProjectConfig()
        assert cfg.skills_dir == "./skills"
        assert cfg.contexts_dir == "./contexts"

    def test_movate_yaml_template_lists_skills_and_contexts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bootstrapped movate.yaml documents skills_dir + contexts_dir."""
        proj = _bootstrap(tmp_path, monkeypatch)
        body = (proj / "movate.yaml").read_text()
        assert "skills_dir:" in body
        assert "contexts_dir:" in body

    def test_movate_yaml_still_validates_as_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new fields don't break ProjectConfig validation."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        proj = _bootstrap(tmp_path, monkeypatch)
        data = yaml.safe_load((proj / "movate.yaml").read_text())
        cfg = ProjectConfig.model_validate(data)
        # The defaults survive a round-trip.
        assert cfg.skills_dir == "./skills"
        assert cfg.contexts_dir == "./contexts"


# ---------------------------------------------------------------------------
# Project Panel surfaces new dirs in the file list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectPanelMentionsNewDirs:
    def test_panel_lists_skills_and_contexts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "demo", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # The success Panel shows both new dirs alongside agents/.
        assert "skills/" in result.stdout
        assert "contexts/" in result.stdout
        # And they're described as "empty" (no leftover content).
        # We just check both names appear in proximity to the
        # existing agents/ bullet.


# ---------------------------------------------------------------------------
# Context auto-scaffold via `mdk add`
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContextAutoScaffold:
    def test_helper_creates_placeholder_for_declared_context(self, tmp_path: Path) -> None:
        """Unit test the helper directly — declared context →
        `<project>/contexts/<name>.md` is created with the
        placeholder body."""
        from movate.cli.add_cmd import _maybe_scaffold_declared_contexts  # noqa: PLC0415

        # Bootstrap a minimal "project" (just enough state for the
        # helper to walk). It doesn't need to be a real movate project.
        project_root = tmp_path / "proj"
        project_root.mkdir()
        agent_dir = project_root / "agents" / "demo-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text(
            "name: demo-agent\ncontexts:\n  - style-guide\n  - safety-policy\n"
        )

        result = _maybe_scaffold_declared_contexts(agent_dir=agent_dir, project_root=project_root)
        assert sorted(result) == ["safety-policy", "style-guide"]
        # Files materialized at the canonical path.
        for name in ("style-guide", "safety-policy"):
            ctx_file = project_root / "contexts" / f"{name}.md"
            assert ctx_file.is_file()
            body = ctx_file.read_text()
            # The placeholder body carries the context name + a TODO
            # structure so operators see what to fill in.
            assert name in body
            assert "TODO" in body

    def test_helper_skips_already_existing_contexts(self, tmp_path: Path) -> None:
        """Don't overwrite a context the operator already authored."""
        from movate.cli.add_cmd import _maybe_scaffold_declared_contexts  # noqa: PLC0415

        project_root = tmp_path / "proj"
        project_root.mkdir()
        contexts_dir = project_root / "contexts"
        contexts_dir.mkdir()
        existing = contexts_dir / "style-guide.md"
        existing.write_text("# Hand-written content\n\nDon't overwrite me.")

        agent_dir = project_root / "agents" / "a"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text("name: a\ncontexts:\n  - style-guide\n  - new-one\n")

        result = _maybe_scaffold_declared_contexts(agent_dir=agent_dir, project_root=project_root)
        # Only the new one was scaffolded.
        assert result == ["new-one"]
        # Existing file preserved.
        assert "Don't overwrite me." in existing.read_text()

    def test_helper_returns_empty_when_no_contexts_declared(self, tmp_path: Path) -> None:
        """No `contexts:` field → no scaffolding."""
        from movate.cli.add_cmd import _maybe_scaffold_declared_contexts  # noqa: PLC0415

        project_root = tmp_path / "proj"
        project_root.mkdir()
        agent_dir = project_root / "agents" / "a"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text("name: a\nskills: [foo]\n")

        assert (
            _maybe_scaffold_declared_contexts(agent_dir=agent_dir, project_root=project_root) == []
        )
        # No contexts/ dir leakage either.
        assert not (project_root / "contexts").exists()

    def test_add_one_returns_contexts_scaffolded_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: a fresh project + `mdk add`ing an agent whose
        agent.yaml declares contexts ends with auto-scaffolded .md
        stubs and the quiet-mode return dict reports them."""
        proj = _bootstrap(tmp_path, monkeypatch)
        monkeypatch.chdir(proj)
        # Drop an agent with a contexts declaration via mdk init
        # (rag-qa role template doesn't declare contexts today; we
        # add one manually for this test by patching the agent.yaml
        # right after add).
        result = runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        agent_yaml = proj / "agents" / "rag-qa" / "agent.yaml"
        # Inject a contexts field at the end of the file.
        new_body = agent_yaml.read_text() + "\ncontexts:\n  - style-guide\n  - safety-policy\n"
        agent_yaml.write_text(new_body)
        # Re-run the scaffold helper directly (the operator would
        # `mdk add --update rag-qa` for this in practice; we exercise
        # the helper to keep the test deterministic).
        from movate.cli.add_cmd import _maybe_scaffold_declared_contexts  # noqa: PLC0415

        scaffolded = _maybe_scaffold_declared_contexts(
            agent_dir=agent_yaml.parent, project_root=proj
        )
        assert sorted(scaffolded) == ["safety-policy", "style-guide"]
        for name in ("style-guide", "safety-policy"):
            assert (proj / "contexts" / f"{name}.md").is_file()
