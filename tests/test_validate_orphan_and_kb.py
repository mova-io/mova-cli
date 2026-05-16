"""Tests for mdk validate orphan-asset and KB-corpus checks.

Three warnings surface in mdk validate when files exist but nothing uses them:

1. contexts/<name>.md that no agent declares → yellow advisory in --all output.
2. skills/<name>/ that no agent declares → yellow advisory in --all output.
3. A kb-skill is declared but kb/kb-lookup-corpus.json is absent → per-agent advisory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _scaffold_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agents: str = "rag-qa",
) -> Path:
    """Init a project with the given agent(s), chdir in."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "--project", "proj", "--skip-snapshot", "--with-agents", agents],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    project = tmp_path / "proj"
    monkeypatch.chdir(project)
    return project


@pytest.mark.unit
class TestOrphanedContext:
    def test_orphaned_context_warns_in_validate_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch)
        # Drop an extra context file that no agent declares.
        (project / "contexts" / "orphan-guide.md").write_text("# Orphan")
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert "orphan-guide.md" in result.stdout
        assert "not declared by any agent" in result.stdout

    def test_declared_context_no_orphan_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # rag-qa declares grounded-qa-rubric AND scaffolds the file —
        # validate --all should not flag it as orphaned.
        _scaffold_project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        # The only context on disk is the declared one; no orphan message expected.
        assert "not declared by any agent" not in result.stdout


@pytest.mark.unit
class TestOrphanedSkill:
    def test_orphaned_skill_warns_in_validate_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch)
        # Create a skill directory that no agent references.
        skill_dir = project / "skills" / "unused-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text(
            "api_version: movate/v1\nkind: Skill\nname: unused-skill\n"
            "version: 0.1.0\ndescription: test\n"
            "schema:\n  input: {}\n  output: {}\n"
            "implementation:\n  kind: python\n  entry: unused_skill.impl:run\n"
            "side_effects: read-only\n"
        )
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert "unused-skill" in result.stdout
        assert "not declared by any agent" in result.stdout


@pytest.mark.unit
class TestKBCorpusMissing:
    def test_kb_skill_without_corpus_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ticket-triager declares kb-lookup, which reads kb/kb-lookup-corpus.json.
        project = _scaffold_project(tmp_path, monkeypatch, agents="ticket-triager")
        corpus = project / "kb" / "kb-lookup-corpus.json"
        if corpus.is_file():
            corpus.unlink()
        result = runner.invoke(
            app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"}
        )
        assert "kb-lookup-corpus.json" in result.stdout
        assert "demo corpus" in result.stdout

    def test_kb_skill_with_corpus_no_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch, agents="ticket-triager")
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps([
                {"id": "1", "title": "T", "tags": [], "symptom": "", "resolution": ""}
            ])
        )
        result = runner.invoke(
            app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"}
        )
        assert "demo corpus" not in result.stdout
