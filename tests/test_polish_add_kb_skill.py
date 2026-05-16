"""Tests for `mdk add kb`, `mdk add skill`, and `mdk knowledge add` (polish sweep items 23-25).

Covers:
* mdk add kb — scaffolds kb/kb-lookup-corpus.json with one example entry
* mdk add skill <name> — scaffolds skills/<name>/skill.yaml + impl.py
* mdk knowledge add — appends a single corpus entry interactively (--json mode)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


# ---------------------------------------------------------------------------
# mdk add kb
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAddKb:
    def test_creates_corpus_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "kb"])
        assert result.exit_code == 0, result.stdout + result.stderr
        corpus = proj / "kb" / "kb-lookup-corpus.json"
        assert corpus.is_file()
        data = json.loads(corpus.read_text())
        assert isinstance(data, list)
        assert len(data) >= 1
        entry = data[0]
        assert "id" in entry
        assert "resolution" in entry

    def test_creates_kb_dir_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        assert not (proj / "kb").exists()
        result = runner.invoke(app, ["add", "kb"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (proj / "kb").is_dir()

    def test_errors_if_corpus_already_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        kb_dir = proj / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text("[]")
        result = runner.invoke(app, ["add", "kb"])
        assert result.exit_code == 2
        assert "already exists" in result.stderr

    def test_errors_outside_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["add", "kb"])
        assert result.exit_code == 2
        assert "not inside a movate project" in result.stderr

    def test_greppable_summary_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "kb"])
        assert result.exit_code == 0, result.stdout
        assert "mdk_add_kb_summary:" in result.stdout
        assert "ok=true" in result.stdout


# ---------------------------------------------------------------------------
# mdk add skill <name>
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAddSkillBare:
    def test_creates_skill_yaml_and_impl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "skill", "my-skill"])
        assert result.exit_code == 0, result.stdout + result.stderr
        skill_dir = proj / "skills" / "my-skill"
        assert (skill_dir / "skill.yaml").is_file()
        assert (skill_dir / "impl.py").is_file()

    def test_skill_yaml_has_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "skill", "my-skill"])
        content = (proj / "skills" / "my-skill" / "skill.yaml").read_text()
        assert "my-skill" in content

    def test_impl_has_run_function(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "skill", "my-skill"])
        impl = (proj / "skills" / "my-skill" / "impl.py").read_text()
        assert "async def run" in impl

    def test_errors_if_skill_already_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        existing = proj / "skills" / "my-skill"
        existing.mkdir(parents=True)
        result = runner.invoke(app, ["add", "skill", "my-skill"])
        assert result.exit_code == 2
        assert "already exists" in result.stderr

    def test_errors_on_invalid_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "skill", "Bad_Name"])
        assert result.exit_code == 2
        assert "invalid" in result.stderr

    def test_errors_on_missing_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "skill"])
        assert result.exit_code == 2
        assert "skill name required" in result.stderr

    def test_errors_outside_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["add", "skill", "my-skill"])
        assert result.exit_code == 2
        assert "not inside a movate project" in result.stderr

    def test_greppable_summary_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "skill", "my-skill"])
        assert result.exit_code == 0, result.stdout
        assert "mdk_add_skill_summary:" in result.stdout
        assert "ok=true" in result.stdout

    def test_hyphen_in_name_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "skill", "send-email"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (proj / "skills" / "send-email" / "skill.yaml").is_file()


# ---------------------------------------------------------------------------
# mdk knowledge add (--json mode — non-TTY)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgeAdd:
    def _kb_corpus(self, proj: Path) -> Path:
        return proj / "kb" / "kb-lookup-corpus.json"

    def test_creates_corpus_if_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        entry = json.dumps({"id": "KB-1", "title": "T", "symptom": "S", "resolution": "R"})
        result = runner.invoke(app, ["knowledge", "add", "--json", entry])
        assert result.exit_code == 0, result.stdout + result.stderr
        corpus = self._kb_corpus(proj)
        assert corpus.is_file()
        data = json.loads(corpus.read_text())
        assert len(data) == 1
        assert data[0]["id"] == "KB-1"

    def test_appends_to_existing_corpus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        kb_dir = proj / "kb"
        kb_dir.mkdir()
        existing = [{"id": "KB-0", "title": "Old", "resolution": "Fix it"}]
        (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps(existing))
        entry = json.dumps({"id": "KB-1", "title": "New", "resolution": "New fix"})
        result = runner.invoke(app, ["knowledge", "add", "--json", entry])
        assert result.exit_code == 0, result.stdout + result.stderr
        data = json.loads(self._kb_corpus(proj).read_text())
        assert len(data) == 2
        assert data[1]["id"] == "KB-1"

    def test_errors_on_missing_required_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        entry = json.dumps({"title": "No id", "resolution": "R"})
        result = runner.invoke(app, ["knowledge", "add", "--json", entry])
        assert result.exit_code == 2
        assert "id" in result.stderr.lower() or "required" in result.stderr.lower()

    def test_errors_on_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "add", "--json", "{not valid json}"])
        assert result.exit_code == 2

    def test_prints_entry_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        entry = json.dumps({"id": "KB-1", "title": "T", "resolution": "R"})
        result = runner.invoke(app, ["knowledge", "add", "--json", entry])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "1" in result.stdout  # new entry count surfaced
