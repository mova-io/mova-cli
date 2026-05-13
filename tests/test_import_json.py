"""Tests for ``mdk import json`` — generic JSON → MDK agent importer.

Two layers:
  * Unit tests on ``_build_plan`` / ``_normalize_name`` — pure functions,
    no IO.
  * CLI integration tests covering the happy path, --force, --name override,
    bad-shape errors, and roundtripping through ``mdk validate``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.import_json import _build_plan, _JsonImportError, _normalize_name
from movate.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_normalize_name_strips_special_chars() -> None:
    assert _normalize_name("Billing FAQ Agent") == "billing-faq-agent"
    assert _normalize_name("foo_bar/baz") == "foo-bar-baz"
    assert _normalize_name("  --Already-A-Slug--  ") == "already-a-slug"


def test_normalize_name_rejects_empty() -> None:
    with pytest.raises(_JsonImportError):
        _normalize_name("()___...")


def test_build_plan_minimal_valid_json() -> None:
    plan = _build_plan(
        {
            "name": "my-agent",
            "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
            "prompt": "You are helpful.",
        },
        name_override=None,
        fallback_name="fallback",
    )
    assert plan["agent_name"] == "my-agent"
    assert plan["version"] == "0.1.0"  # default
    assert plan["provider"] == "openai/gpt-4o-mini-2024-07-18"
    assert plan["prompt_raw"] == "You are helpful."
    assert plan["prompt_is_path"] is False
    assert plan["objectives"] == []


def test_build_plan_full_json_propagates_optional_fields() -> None:
    plan = _build_plan(
        {
            "name": "full-agent",
            "version": "1.2.3",
            "description": "Full example",
            "owner": "team-x",
            "tags": ["a", "b"],
            "runtime": "litellm",
            "model": {
                "provider": "anthropic/claude-haiku-4-5-20251001",
                "params": {"temperature": 0.0},
                "fallback": [{"provider": "openai/gpt-4o-mini-2024-07-18"}],
            },
            "prompt": "system prompt",
            "goals": ["goal 1"],
            "objectives": [{"id": "x", "threshold": 0.8}],
            "examples": [{"input": {"q": "hi"}, "output": {"a": "hello"}}],
            "budget": {"max_cost_usd_per_run": 0.50},
        },
        name_override=None,
        fallback_name="x",
    )
    assert plan["version"] == "1.2.3"
    assert plan["description"] == "Full example"
    assert plan["owner"] == "team-x"
    assert plan["tags"] == ["a", "b"]
    assert plan["runtime"] == "litellm"
    assert plan["params"] == {"temperature": 0.0}
    assert plan["fallback"] == [{"provider": "openai/gpt-4o-mini-2024-07-18"}]
    assert plan["goals"] == ["goal 1"]
    assert len(plan["objectives"]) == 1
    assert plan["objectives"][0]["id"] == "x"
    assert plan["budget"] == {"max_cost_usd_per_run": 0.50}


def test_build_plan_uses_name_override_when_provided() -> None:
    plan = _build_plan(
        {"name": "ignored", "model": {"provider": "openai/x"}, "prompt": "p"},
        name_override="Custom Name",
        fallback_name="x",
    )
    assert plan["agent_name"] == "custom-name"


def test_build_plan_uses_fallback_name_when_no_name_field() -> None:
    plan = _build_plan(
        {"model": {"provider": "openai/x"}, "prompt": "p"},
        name_override=None,
        fallback_name="exported-agent",
    )
    assert plan["agent_name"] == "exported-agent"


def test_build_plan_rejects_missing_model() -> None:
    with pytest.raises(_JsonImportError, match="model"):
        _build_plan(
            {"name": "x", "prompt": "p"},
            name_override=None,
            fallback_name="x",
        )


def test_build_plan_rejects_missing_provider() -> None:
    with pytest.raises(_JsonImportError, match="provider"):
        _build_plan(
            {"name": "x", "model": {"params": {}}, "prompt": "p"},
            name_override=None,
            fallback_name="x",
        )


def test_build_plan_rejects_missing_prompt() -> None:
    with pytest.raises(_JsonImportError, match="prompt"):
        _build_plan(
            {"name": "x", "model": {"provider": "openai/x"}},
            name_override=None,
            fallback_name="x",
        )


def test_build_plan_detects_path_prompt_reference() -> None:
    """Prompts starting with ``./`` or ending in ``.md`` are path refs;
    importer writes a placeholder file rather than the literal string."""
    plan = _build_plan(
        {"name": "x", "model": {"provider": "openai/x"}, "prompt": "./my-prompt.md"},
        name_override=None,
        fallback_name="x",
    )
    assert plan["prompt_is_path"] is True

    plan2 = _build_plan(
        {"name": "x", "model": {"provider": "openai/x"}, "prompt": "prompts/system.md"},
        name_override=None,
        fallback_name="x",
    )
    assert plan2["prompt_is_path"] is True


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_json(tmp_path: Path) -> Path:
    """A minimal valid JSON definition — enough for happy-path tests."""
    path = tmp_path / "minimal.json"
    path.write_text(
        json.dumps(
            {
                "name": "minimal-agent",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "You are minimal.\n\n{{ input.message }}",
            }
        )
    )
    return path


def test_import_writes_expected_files(tmp_path: Path, minimal_json: Path) -> None:
    out = tmp_path / "agents"
    result = runner.invoke(app, ["import", "json", str(minimal_json), "-o", str(out)])
    assert result.exit_code == 0, result.stdout

    agent_dir = out / "minimal-agent"
    assert (agent_dir / "agent.yaml").exists()
    assert (agent_dir / "prompt.md").exists()
    assert (agent_dir / "schema" / "input.json").exists()
    assert (agent_dir / "schema" / "output.json").exists()
    assert (agent_dir / "source.json").exists()

    # Inline prompt was written verbatim.
    prompt_body = (agent_dir / "prompt.md").read_text()
    assert "You are minimal." in prompt_body


def test_import_with_name_override(tmp_path: Path, minimal_json: Path) -> None:
    out = tmp_path / "agents"
    result = runner.invoke(
        app,
        ["import", "json", str(minimal_json), "-o", str(out), "--name", "Custom Agent"],
    )
    assert result.exit_code == 0
    assert (out / "custom-agent").exists()


def test_import_rejects_existing_dir_without_force(tmp_path: Path, minimal_json: Path) -> None:
    out = tmp_path / "agents"
    first = runner.invoke(app, ["import", "json", str(minimal_json), "-o", str(out)])
    assert first.exit_code == 0

    # mix_stderr=False so we can inspect the error stream separately.
    runner_stderr = CliRunner(mix_stderr=False)
    second = runner_stderr.invoke(app, ["import", "json", str(minimal_json), "-o", str(out)])
    assert second.exit_code == 2
    combined = second.stdout + (second.stderr or "")
    assert "already exists" in combined


def test_import_force_overwrites(tmp_path: Path, minimal_json: Path) -> None:
    out = tmp_path / "agents"
    runner.invoke(app, ["import", "json", str(minimal_json), "-o", str(out)])
    result = runner.invoke(app, ["import", "json", str(minimal_json), "-o", str(out), "--force"])
    assert result.exit_code == 0


def test_import_rejects_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json {")
    runner_stderr = CliRunner(mix_stderr=False)
    result = runner_stderr.invoke(app, ["import", "json", str(bad), "-o", str(tmp_path / "out")])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "parse error" in combined or "not valid JSON" in combined


def test_import_rejects_non_object_top_level(tmp_path: Path) -> None:
    """The JSON must be an object — a list or scalar at the top is wrong."""
    bad = tmp_path / "list.json"
    bad.write_text('["not", "an", "object"]')
    runner_stderr = CliRunner(mix_stderr=False)
    result = runner_stderr.invoke(app, ["import", "json", str(bad), "-o", str(tmp_path / "out")])
    assert result.exit_code == 2


def test_import_rejects_missing_required_fields(tmp_path: Path) -> None:
    no_model = tmp_path / "no-model.json"
    no_model.write_text(json.dumps({"name": "x", "prompt": "p"}))
    runner_stderr = CliRunner(mix_stderr=False)
    result = runner_stderr.invoke(
        app, ["import", "json", str(no_model), "-o", str(tmp_path / "out")]
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "model" in combined


def test_imported_agent_validates_via_mdk_validate(tmp_path: Path, minimal_json: Path) -> None:
    """Round-trip: import a JSON definition, then run `mdk validate` on
    the result. Catches any agent.yaml shape errors we generate."""
    out = tmp_path / "agents"
    result = runner.invoke(app, ["import", "json", str(minimal_json), "-o", str(out)])
    assert result.exit_code == 0

    validate_result = runner.invoke(app, ["validate", str(out / "minimal-agent")])
    assert validate_result.exit_code == 0, validate_result.stdout
    assert "minimal-agent" in validate_result.stdout
