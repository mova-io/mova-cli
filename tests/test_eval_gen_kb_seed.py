"""Tests for KB-corpus symptom seeding in mdk eval-gen.

Covers:
* _load_kb_seeds returns symptom strings for agents with KB skills
* _load_kb_seeds falls back to title when symptom is blank
* _load_kb_seeds returns [] for agents without KB skills
* _load_kb_seeds returns [] when corpus is absent
* _gen_user_message includes kb_seed in the prompt when provided
* _gen_user_message without kb_seed is unchanged
* --mock eval-gen with KB agent seeds advisory message in output
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.eval_gen_cmd import _gen_user_message, _load_kb_seeds
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _make_bundle(tmp_path: Path, *, has_kb_skill: bool = True) -> object:
    """Build a minimal AgentBundle-like object for unit tests."""
    from movate.cli.main import app  # noqa: PLC0415  — ensure app registered

    monkeypatch_path = tmp_path / "proj"
    monkeypatch_path.mkdir(exist_ok=True)
    (monkeypatch_path / "movate.yaml").write_text("agents_dir: ./agents\n")
    agents_dir = monkeypatch_path / "agents"
    agents_dir.mkdir(exist_ok=True)

    skill_name = "kb-lookup" if has_kb_skill else "echo"
    agent_yaml = (
        f"api_version: movate/v1\nkind: Agent\nname: test-agent\n"
        f"description: test\nmodel:\n  provider: openai\n  name: gpt-4o\n"
        f"skills:\n  - {skill_name}\n"
    )
    agent_dir = agents_dir / "test-agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(agent_yaml)
    return None  # we test _load_kb_seeds directly below


# ---------------------------------------------------------------------------
# _load_kb_seeds unit tests (pure function)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadKbSeeds:
    def _make_simple_bundle(self, skill_names: list[str]) -> object:
        """Minimal duck-typed bundle with just .skills."""
        from types import SimpleNamespace  # noqa: PLC0415
        return SimpleNamespace(
            skills=[
                SimpleNamespace(spec=SimpleNamespace(name=sn))
                for sn in skill_names
            ]
        )

    def test_returns_symptoms_for_kb_agent(self, tmp_path: Path) -> None:
        bundle = self._make_simple_bundle(["kb-lookup"])
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps([
            {"id": "1", "title": "Login fails", "symptom": "User cannot log in", "resolution": "x"},
            {"id": "2", "title": "Slow API", "symptom": "API response is slow", "resolution": "y"},
        ]))
        seeds = _load_kb_seeds(bundle, tmp_path)
        assert seeds == ["User cannot log in", "API response is slow"]

    def test_falls_back_to_title_when_symptom_blank(self, tmp_path: Path) -> None:
        bundle = self._make_simple_bundle(["kb-lookup"])
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps([
            {"id": "1", "title": "Login fails", "symptom": "", "resolution": "x"},
            {"id": "2", "title": "Billing error", "resolution": "y"},  # no symptom key
        ]))
        seeds = _load_kb_seeds(bundle, tmp_path)
        assert seeds == ["Login fails", "Billing error"]

    def test_returns_empty_for_non_kb_agent(self, tmp_path: Path) -> None:
        bundle = self._make_simple_bundle(["summarize", "translate"])
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps([
            {"id": "1", "title": "T", "symptom": "S", "resolution": "R"},
        ]))
        seeds = _load_kb_seeds(bundle, tmp_path)
        assert seeds == []

    def test_returns_empty_when_corpus_absent(self, tmp_path: Path) -> None:
        bundle = self._make_simple_bundle(["kb-lookup"])
        seeds = _load_kb_seeds(bundle, tmp_path)
        assert seeds == []

    def test_returns_empty_when_corpus_unreadable(self, tmp_path: Path) -> None:
        bundle = self._make_simple_bundle(["kb-lookup"])
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text("not json {{")
        seeds = _load_kb_seeds(bundle, tmp_path)
        assert seeds == []

    def test_skips_entries_with_no_symptom_or_title(self, tmp_path: Path) -> None:
        bundle = self._make_simple_bundle(["kb-lookup"])
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps([
            {"id": "1", "symptom": "Real symptom", "resolution": "x"},
            {"id": "2", "resolution": "no title no symptom"},  # skipped
            {"id": "3", "title": "", "symptom": "", "resolution": "empty strings"},  # skipped
        ]))
        seeds = _load_kb_seeds(bundle, tmp_path)
        assert seeds == ["Real symptom"]

    def test_mixed_symptom_and_title_fallback(self, tmp_path: Path) -> None:
        bundle = self._make_simple_bundle(["kb-lookup"])
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps([
            {"id": "1", "title": "A", "symptom": "Symptom A", "resolution": "x"},
            {"id": "2", "title": "Title B", "resolution": "y"},  # fallback to title
        ]))
        seeds = _load_kb_seeds(bundle, tmp_path)
        assert seeds == ["Symptom A", "Title B"]


# ---------------------------------------------------------------------------
# _gen_user_message unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenUserMessageKbSeed:
    def _make_bundle(self) -> object:
        from types import SimpleNamespace  # noqa: PLC0415
        return SimpleNamespace(
            spec=SimpleNamespace(name="triage", description="Triages tickets"),
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        )

    def test_kb_seed_appears_in_message(self) -> None:
        bundle = self._make_bundle()
        msg = _gen_user_message(bundle, index=0, sample_input=None, kb_seed="Login is broken")
        assert "Login is broken" in msg
        assert "knowledge base" in msg.lower() or "Scenario" in msg

    def test_kb_seed_none_produces_same_as_before(self) -> None:
        bundle = self._make_bundle()
        with_seed = _gen_user_message(bundle, index=0, sample_input=None, kb_seed="some symptom")
        without_seed = _gen_user_message(bundle, index=0, sample_input=None, kb_seed=None)
        assert "some symptom" in with_seed
        assert "some symptom" not in without_seed

    def test_both_kb_seed_and_sample_input_included(self) -> None:
        bundle = self._make_bundle()
        sample = {"text": "example query"}
        msg = _gen_user_message(
            bundle, index=0, sample_input=sample, kb_seed="API timeout errors"
        )
        assert "API timeout errors" in msg
        assert "example query" in msg


# ---------------------------------------------------------------------------
# Integration: mdk eval-gen --mock prints seed note when KB corpus present
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvalGenKbSeedIntegration:
    def _scaffold(
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

    def test_seed_note_in_output_when_corpus_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._scaffold(tmp_path, monkeypatch)
        kb_dir = project / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps([
            {"id": "1", "title": "Auth error", "symptom": "Cannot log in", "resolution": "reset"},
            {"id": "2", "title": "Slow API", "symptom": "Requests timing out", "resolution": "cache"},
        ]))
        result = runner.invoke(
            app,
            ["eval-gen", "ticket-triager", "--num", "2", "--mock", "--force"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "seeding from 2 KB symptom" in result.stdout

    def test_no_seed_note_when_no_corpus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._scaffold(tmp_path, monkeypatch)
        # Ensure corpus is absent (ticket-triager may scaffold a default)
        corpus = project / "kb" / "kb-lookup-corpus.json"
        if corpus.is_file():
            corpus.unlink()
        result = runner.invoke(
            app,
            ["eval-gen", "ticket-triager", "--num", "2", "--mock", "--force"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "seeding from" not in result.stdout
