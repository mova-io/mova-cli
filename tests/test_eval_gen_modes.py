"""Tests for mdk eval-gen --mode adversarial/edge/refusal/standard.

Covers:
* --mode standard (default) — unchanged behaviour; no mode tag on entries
* --mode adversarial — entries tagged mode=adversarial; no refusal_expected
* --mode edge        — entries tagged mode=edge; no refusal_expected
* --mode refusal     — entries tagged mode=refusal AND refusal_expected=true
* Unknown mode value → exit code 2 with error message
* _mode_system_prompt() returns distinct prompts for each mode
* Mode is threaded through --all sweep (mdk_eval_gen_all_summary still fires)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.eval_gen_cmd import _VALID_MODES, _mode_system_prompt
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# _mode_system_prompt unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModeSystemPrompt:
    def test_standard_is_default(self) -> None:
        std = _mode_system_prompt("standard")
        assert "VARIED" in std or "realistic" in std.lower()

    def test_adversarial_prompt_differs_from_standard(self) -> None:
        assert _mode_system_prompt("adversarial") != _mode_system_prompt("standard")

    def test_adversarial_mentions_attack_intent(self) -> None:
        p = _mode_system_prompt("adversarial")
        assert "adversarial" in p.lower() or "injection" in p.lower() or "bypass" in p.lower()

    def test_edge_prompt_mentions_boundary(self) -> None:
        p = _mode_system_prompt("edge")
        assert "boundary" in p.lower() or "edge" in p.lower() or "empty" in p.lower()

    def test_refusal_prompt_mentions_decline(self) -> None:
        p = _mode_system_prompt("refusal")
        assert "refusal" in p.lower() or "decline" in p.lower() or "refuse" in p.lower()

    def test_all_four_modes_are_distinct(self) -> None:
        prompts = [_mode_system_prompt(m) for m in _VALID_MODES]
        assert len(set(prompts)) == len(prompts), "each mode should have a unique system prompt"

    def test_unknown_mode_falls_back_to_standard(self) -> None:
        fallback = _mode_system_prompt("totally-unknown-mode")
        std = _mode_system_prompt("standard")
        assert fallback == std


# ---------------------------------------------------------------------------
# Integration: mdk eval-gen --mock --mode <X>
# ---------------------------------------------------------------------------


def _scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


@pytest.mark.unit
class TestEvalGenModeFlag:
    def test_standard_mode_no_mode_tag_on_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["eval-gen", "ticket-triager", "--num", "2", "--mock", "--force"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        ds = project / "evals" / "ticket-triager" / "dataset.generated.jsonl"
        entries = [json.loads(line) for line in ds.read_text().splitlines() if line.strip()]
        for entry in entries:
            assert "mode" not in entry, "standard mode should not add a mode tag"
            assert "refusal_expected" not in entry

    def test_adversarial_mode_tags_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "eval-gen",
                "ticket-triager",
                "--num",
                "2",
                "--mock",
                "--force",
                "--mode",
                "adversarial",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        ds = project / "evals" / "ticket-triager" / "dataset.generated.jsonl"
        entries = [json.loads(line) for line in ds.read_text().splitlines() if line.strip()]
        assert entries, "should have generated entries"
        for entry in entries:
            assert entry.get("mode") == "adversarial"
            assert "refusal_expected" not in entry

    def test_edge_mode_tags_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = _scaffold(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["eval-gen", "ticket-triager", "--num", "2", "--mock", "--force", "--mode", "edge"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        ds = project / "evals" / "ticket-triager" / "dataset.generated.jsonl"
        entries = [json.loads(line) for line in ds.read_text().splitlines() if line.strip()]
        for entry in entries:
            assert entry.get("mode") == "edge"
            assert "refusal_expected" not in entry

    def test_refusal_mode_sets_refusal_expected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _scaffold(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["eval-gen", "ticket-triager", "--num", "2", "--mock", "--force", "--mode", "refusal"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        ds = project / "evals" / "ticket-triager" / "dataset.generated.jsonl"
        entries = [json.loads(line) for line in ds.read_text().splitlines() if line.strip()]
        assert entries
        for entry in entries:
            assert entry.get("mode") == "refusal"
            assert entry.get("refusal_expected") is True

    def test_unknown_mode_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scaffold(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "eval-gen",
                "ticket-triager",
                "--num",
                "1",
                "--mock",
                "--force",
                "--mode",
                "not-a-mode",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code != 0
        assert "not-a-mode" in result.stdout or "not-a-mode" in result.stderr

    def test_mode_note_in_progress_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scaffold(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "eval-gen",
                "ticket-triager",
                "--num",
                "1",
                "--mock",
                "--force",
                "--mode",
                "adversarial",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert "mode=adversarial" in result.stdout

    def test_standard_mode_no_note_in_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scaffold(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["eval-gen", "ticket-triager", "--num", "1", "--mock", "--force"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert "mode=" not in result.stdout

    def test_valid_modes_list(self) -> None:
        # ``domain`` joined the family in Phase 2 of the eval-scorecard
        # work — adds a KB-aware system prompt that pairs with the
        # kb_seeds plumbing already in place.
        assert set(_VALID_MODES) == {
            "standard",
            "adversarial",
            "edge",
            "refusal",
            "domain",
        }
