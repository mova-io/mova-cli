"""Tests for mdk validate orphan-asset and KB-corpus checks.

Warnings / errors surface in mdk validate when:

1. contexts/<name>.md that no agent declares → yellow advisory in --all output.
2. skills/<name>/ that no agent declares → yellow advisory in --all output.
3. A kb-skill is declared but kb/kb-lookup-corpus.json is absent → per-agent advisory.
4. kb-lookup-corpus.json entries are missing required fields → per-agent advisory.
5. A Python skill directory has no impl.py → per-agent error.
6. A context body exceeds _CTX_ADVISORY_BYTES → advisory; _CTX_ERROR_BYTES → error.
7. An orphaned skill has a malformed skill.yaml → --all error.
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
class TestOrphanedSkillMalformed:
    def test_malformed_skill_yaml_warns_in_validate_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch)
        skill_dir = project / "skills" / "bad-skill"
        skill_dir.mkdir(parents=True)
        # Intentionally missing required fields (no `schema`, no `implementation`).
        (skill_dir / "skill.yaml").write_text(
            "api_version: movate/v1\nkind: Skill\nname: bad-skill\nversion: 0.1.0\n"
            "description: test\n"
        )
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        # Malformed skill.yaml surfaces via the skills registry load, which
        # now includes the dir name in the error ("bad-skill/skill.yaml
        # validation failed: ..."). The agent that triggers the registry load
        # fails and the project validate exits non-zero.
        assert "bad-skill" in result.stdout
        assert result.exit_code != 0


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

    def test_kb_corpus_missing_required_fields_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch, agents="ticket-triager")
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        # Entry is missing 'resolution' — a required field.
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps([{"id": "BAD-001", "title": "Missing resolution"}])
        )
        result = runner.invoke(
            app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"}
        )
        assert "BAD-001" in result.stdout
        assert "missing" in result.stdout

    def test_kb_corpus_valid_fields_no_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch, agents="ticket-triager")
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps([
                {"id": "OK-001", "title": "Good entry", "resolution": "All good",
                 "tags": [], "symptom": ""}
            ])
        )
        result = runner.invoke(
            app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"}
        )
        assert "missing" not in result.stdout
        assert "required fields" not in result.stdout


def _declared_context_file(project: Path, agent_name: str) -> Path:
    """Return the resolved path to the first context declared by an agent.

    Mirrors the two-tier resolution: agent-local
    ``agents/<name>/contexts/<ctx>.md`` takes priority over
    ``contexts/<ctx>.md`` at the project level.
    """
    import yaml  # noqa: PLC0415

    spec = yaml.safe_load((project / "agents" / agent_name / "agent.yaml").read_text())
    ctx_names: list[str] = spec.get("contexts") or []
    assert ctx_names, f"{agent_name} declares no contexts"
    ctx_name = ctx_names[0]
    # Agent-local override wins if it exists.
    agent_local = project / "agents" / agent_name / "contexts" / f"{ctx_name}.md"
    if agent_local.is_file():
        return agent_local
    return project / "contexts" / f"{ctx_name}.md"


@pytest.mark.unit
class TestContextSize:
    def test_large_context_advisory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from movate.cli.validate import _CTX_ADVISORY_BYTES  # noqa: PLC0415

        project = _scaffold_project(tmp_path, monkeypatch)
        ctx_file = _declared_context_file(project, "rag-qa")
        ctx_file.write_text("x" * (_CTX_ADVISORY_BYTES + 1))
        result = runner.invoke(
            app, ["validate", "agents/rag-qa"], env={"COLUMNS": "200"}
        )
        assert "large" in result.stdout or "bytes" in result.stdout

    def test_oversized_context_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from movate.cli.validate import _CTX_ERROR_BYTES  # noqa: PLC0415

        project = _scaffold_project(tmp_path, monkeypatch)
        ctx_file = _declared_context_file(project, "rag-qa")
        ctx_file.write_text("x" * (_CTX_ERROR_BYTES + 1))
        result = runner.invoke(
            app, ["validate", "agents/rag-qa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code != 0
        assert "exceeds" in result.stdout or "limit" in result.stdout


@pytest.mark.unit
class TestPythonSkillImplMissing:
    def test_missing_impl_py_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A declared Python skill without impl.py should fail validate."""
        project = _scaffold_project(tmp_path, monkeypatch)
        # Find the first declared skill's dir and remove impl.py.
        skill_dirs = [
            d for d in (project / "skills").iterdir()
            if d.is_dir() and (d / "skill.yaml").is_file()
        ]
        if not skill_dirs:
            pytest.skip("rag-qa has no local skills")
        impl = skill_dirs[0] / "impl.py"
        if impl.is_file():
            impl.unlink()
        result = runner.invoke(
            app, ["validate", "agents/rag-qa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code != 0
        assert "impl.py" in result.stdout
