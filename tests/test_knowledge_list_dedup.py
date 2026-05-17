"""Tests for `mdk knowledge list` and duplicate-id KB corpus validation.

Covers:
* mdk knowledge list — renders corpus entries as a table
* mdk validate duplicate-id warning in kb/kb-lookup-corpus.json
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


def _scaffold_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agents: str = "ticket-triager",
) -> Path:
    """Init a real project with the given agent(s)."""
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


# ---------------------------------------------------------------------------
# mdk knowledge list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgeList:
    def test_list_shows_table_with_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        _write_corpus(
            proj,
            [
                {
                    "id": "KB-1",
                    "title": "Login fails",
                    "tags": ["auth"],
                    "resolution": "Reset password",
                },
                {
                    "id": "KB-2",
                    "title": "Slow query",
                    "tags": ["db", "perf"],
                    "resolution": "Add index",
                },
            ],
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "KB-1" in result.stdout
        assert "Login fails" in result.stdout
        assert "KB-2" in result.stdout
        assert "Slow query" in result.stdout

    def test_list_shows_tags_comma_joined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        _write_corpus(
            proj,
            [
                {
                    "id": "KB-3",
                    "title": "Multi tag",
                    "tags": ["billing", "refunds"],
                    "resolution": "Fix",
                },
            ],
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list"])
        assert result.exit_code == 0
        assert "billing" in result.stdout
        assert "refunds" in result.stdout

    def test_list_truncates_long_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        long_res = "A" * 200
        _write_corpus(
            proj,
            [
                {"id": "KB-4", "title": "Long one", "tags": [], "resolution": long_res},
            ],
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list"])
        assert result.exit_code == 0
        # Should be truncated — full 200-char string won't appear verbatim
        assert long_res not in result.stdout
        assert "…" in result.stdout

    def test_list_empty_corpus_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _project(tmp_path)
        _write_corpus(proj, [])
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list"])
        assert result.exit_code == 0
        assert "empty" in result.stdout or "mdk knowledge add" in result.stdout

    def test_list_missing_corpus_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list"])
        assert result.exit_code != 0
        assert "not found" in result.stderr or "not found" in result.stdout

    def test_list_limit_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _project(tmp_path)
        entries = [
            {"id": f"KB-{i}", "title": f"Entry {i}", "tags": [], "resolution": f"Fix {i}"}
            for i in range(10)
        ]
        _write_corpus(proj, entries)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list", "--limit", "3"])
        assert result.exit_code == 0
        # Shows only the first 3 entries
        assert "KB-0" in result.stdout
        assert "KB-1" in result.stdout
        assert "KB-2" in result.stdout
        assert "KB-9" not in result.stdout
        assert "3 of 10" in result.stdout

    def test_list_entry_count_shown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _project(tmp_path)
        _write_corpus(
            proj,
            [
                {"id": "KB-A", "title": "Alpha", "tags": [], "resolution": "Fix A"},
            ],
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list"])
        assert result.exit_code == 0
        assert "1 entry" in result.stdout or "1 entr" in result.stdout

    def test_list_custom_corpus_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _project(tmp_path)
        custom = proj / "custom.json"
        custom.write_text(
            json.dumps([{"id": "CX-1", "title": "Custom", "tags": [], "resolution": "Works"}])
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["knowledge", "list", "--corpus", str(custom)])
        assert result.exit_code == 0
        assert "CX-1" in result.stdout


# ---------------------------------------------------------------------------
# mdk validate duplicate-id warning
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDuplicateKBId:
    def test_duplicate_id_warns_in_validate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch)
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps(
                [
                    {
                        "id": "DUP-001",
                        "title": "First",
                        "tags": [],
                        "symptom": "",
                        "resolution": "Fix A",
                    },
                    {
                        "id": "DUP-001",
                        "title": "Dupe",
                        "tags": [],
                        "symptom": "",
                        "resolution": "Fix B",
                    },
                ]
            )
        )
        result = runner.invoke(app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"})
        assert "DUP-001" in result.stdout
        assert "duplicate" in result.stdout

    def test_duplicate_id_hint_mentions_remove(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch)
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps(
                [
                    {"id": "X-1", "title": "A", "tags": [], "symptom": "", "resolution": "a"},
                    {"id": "X-1", "title": "B", "tags": [], "symptom": "", "resolution": "b"},
                ]
            )
        )
        result = runner.invoke(app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"})
        assert "mdk knowledge remove" in result.stdout or "mdk knowledge list" in result.stdout

    def test_unique_ids_no_duplicate_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch)
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps(
                [
                    {
                        "id": "OK-1",
                        "title": "One",
                        "tags": [],
                        "symptom": "",
                        "resolution": "fix 1",
                    },
                    {
                        "id": "OK-2",
                        "title": "Two",
                        "tags": [],
                        "symptom": "",
                        "resolution": "fix 2",
                    },
                ]
            )
        )
        result = runner.invoke(app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"})
        assert "duplicate" not in result.stdout

    def test_multiple_duplicates_all_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold_project(tmp_path, monkeypatch)
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps(
                [
                    {"id": "A", "title": "A1", "tags": [], "symptom": "", "resolution": "a"},
                    {"id": "B", "title": "B1", "tags": [], "symptom": "", "resolution": "b"},
                    {"id": "A", "title": "A2", "tags": [], "symptom": "", "resolution": "aa"},
                    {"id": "B", "title": "B2", "tags": [], "symptom": "", "resolution": "bb"},
                ]
            )
        )
        result = runner.invoke(app, ["validate", "agents/ticket-triager"], env={"COLUMNS": "200"})
        assert "'A'" in result.stdout or '"A"' in result.stdout
        assert "'B'" in result.stdout or '"B"' in result.stdout
