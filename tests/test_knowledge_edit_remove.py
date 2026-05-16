"""Tests for `mdk knowledge remove`, `mdk knowledge edit`, and empty-corpus validate check.

Covers:
* mdk knowledge remove <id> — deletes entry by id, requires --yes in non-TTY
* mdk knowledge edit <id> --set '{...}' — patches named fields, preserves others
* mdk validate corpus `[]` empty-array warning
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


def _write_corpus(proj: Path, entries: list[dict]) -> Path:
    kb = proj / "kb"
    kb.mkdir(exist_ok=True)
    p = kb / "kb-lookup-corpus.json"
    p.write_text(json.dumps(entries, indent=2))
    return p


# ---------------------------------------------------------------------------
# mdk knowledge remove
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgeRemove:
    def _corpus_with_two(self, proj: Path) -> Path:
        return _write_corpus(proj, [
            {"id": "KB-1", "title": "First", "resolution": "Fix 1"},
            {"id": "KB-2", "title": "Second", "resolution": "Fix 2"},
        ])

    def test_removes_entry_with_yes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        corpus = self._corpus_with_two(proj)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "remove", "KB-1", "--yes"])
        assert result.exit_code == 0, result.stdout + result.stderr
        remaining = json.loads(corpus.read_text())
        assert len(remaining) == 1
        assert remaining[0]["id"] == "KB-2"

    def test_output_confirms_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        self._corpus_with_two(proj)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "remove", "KB-1", "--yes"])
        assert "KB-1" in result.stdout
        assert "removed" in result.stdout.lower() or "✓" in result.stdout

    def test_errors_on_missing_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        self._corpus_with_two(proj)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "remove", "KB-99", "--yes"])
        assert result.exit_code == 2
        assert "KB-99" in result.stderr or "KB-99" in result.stdout

    def test_errors_when_no_yes_in_non_tty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        self._corpus_with_two(proj)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "remove", "KB-1"])
        assert result.exit_code != 0

    def test_errors_when_corpus_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "remove", "KB-1", "--yes"])
        assert result.exit_code == 2

    def test_corpus_has_correct_count_after_remove(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        corpus = self._corpus_with_two(proj)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["knowledge", "remove", "KB-1", "--yes"])
        data = json.loads(corpus.read_text())
        assert len(data) == 1


# ---------------------------------------------------------------------------
# mdk knowledge edit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgeEdit:
    def _corpus(self, proj: Path) -> Path:
        return _write_corpus(proj, [
            {"id": "KB-1", "title": "Old title", "resolution": "Old fix", "tags": ["x"]},
        ])

    def test_patches_named_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        corpus = self._corpus(proj)
        monkeypatch.chdir(proj)
        patch = json.dumps({"resolution": "New fix"})
        result = runner.invoke(app, ["knowledge", "edit", "KB-1", "--set", patch])
        assert result.exit_code == 0, result.stdout + result.stderr
        data = json.loads(corpus.read_text())
        assert data[0]["resolution"] == "New fix"

    def test_preserves_other_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        corpus = self._corpus(proj)
        monkeypatch.chdir(proj)
        patch = json.dumps({"resolution": "Updated"})
        runner.invoke(app, ["knowledge", "edit", "KB-1", "--set", patch])
        data = json.loads(corpus.read_text())
        assert data[0]["title"] == "Old title"
        assert data[0]["tags"] == ["x"]

    def test_can_rename_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        corpus = self._corpus(proj)
        monkeypatch.chdir(proj)
        patch = json.dumps({"id": "KB-999"})
        result = runner.invoke(app, ["knowledge", "edit", "KB-1", "--set", patch])
        assert result.exit_code == 0, result.stdout + result.stderr
        data = json.loads(corpus.read_text())
        assert data[0]["id"] == "KB-999"

    def test_errors_on_missing_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        self._corpus(proj)
        monkeypatch.chdir(proj)
        patch = json.dumps({"resolution": "x"})
        result = runner.invoke(app, ["knowledge", "edit", "KB-99", "--set", patch])
        assert result.exit_code == 2

    def test_errors_on_invalid_json_patch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        self._corpus(proj)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "edit", "KB-1", "--set", "{not json}"])
        assert result.exit_code == 2

    def test_output_confirms_fields_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        self._corpus(proj)
        monkeypatch.chdir(proj)
        patch = json.dumps({"resolution": "Better answer"})
        result = runner.invoke(app, ["knowledge", "edit", "KB-1", "--set", patch])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "resolution" in result.stdout

    def test_errors_when_corpus_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        monkeypatch.chdir(proj)
        patch = json.dumps({"resolution": "x"})
        result = runner.invoke(app, ["knowledge", "edit", "KB-1", "--set", patch])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# mdk validate: empty corpus [] warning
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateEmptyCorpus:
    def _scaffold_kb_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "proj", "--skip-snapshot", "--with-agents", "ticket-triager"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        project = tmp_path / "proj"
        monkeypatch.chdir(project)
        return project

    def test_empty_corpus_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._scaffold_kb_agent(tmp_path, monkeypatch)
        (project / "kb").mkdir(exist_ok=True)
        (project / "kb" / "kb-lookup-corpus.json").write_text("[]")
        result = runner.invoke(
            app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"}
        )
        assert "empty" in result.stdout
        assert "mdk knowledge add" in result.stdout

    def test_nonempty_corpus_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._scaffold_kb_agent(tmp_path, monkeypatch)
        (project / "kb").mkdir(exist_ok=True)
        (project / "kb" / "kb-lookup-corpus.json").write_text(
            json.dumps([{"id": "KB-1", "title": "T", "resolution": "R", "tags": []}])
        )
        result = runner.invoke(
            app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"}
        )
        assert "empty" not in result.stdout
