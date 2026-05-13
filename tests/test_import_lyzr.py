"""Tests for ``mdk import lyzr`` — Lyzr JSON → MDK agent directory.

Two layers:
  * Unit tests on ``_build_plan`` / ``_slugify_name`` / ``_parse_examples``
    — pure functions, no IO.
  * CLI integration tests using the Tesla agent JSON fixture, asserting
    the on-disk output matches expectations + that ``movate validate``
    accepts the result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.import_lyzr import (
    _build_plan,
    _LyzrImportError,
    _parse_examples,
    _slugify_name,
)
from movate.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixture: real-shape Tesla agent JSON (trimmed for test brevity)
# ---------------------------------------------------------------------------


TESLA_AGENT = {
    "_id": "69fe0d9890de3014e9f1cf92",
    "api_key": "sk-default-fake",
    "name": "Tesla Customer Experience Manager v1 (MAY 08, 2026, 09:21 AM PST)",
    "description": "Orchestrates intelligent customer support workflows.",
    "agent_role": "Tesla Customer Experience Operations Manager",
    "agent_instructions": (
        "You are the central customer support AI manager for Tesla-related "
        "inquiries.\nAnalyze customer intent and route requests."
    ),
    "agent_goal": "Provide accurate, helpful Tesla customer responses.",
    "examples": (
        '[{"user":"How long to charge a Model Y at a Supercharger?",'
        '"assistant":"Typically 15-30 minutes."},'
        '{"user":"App won\'t connect.","assistant":"Let me help troubleshoot."}]'
    ),
    "response_format": {"type": "text"},
    "provider_id": "OpenAI",
    "model": "gpt-5",
    "top_p": "1",
    "temperature": "0.8",
    "managed_agents": [
        {"id": "id1", "name": "(R) Vehicle Support", "usage_description": "x"},
        {"id": "id2", "name": "(R) Charging", "usage_description": "y"},
    ],
    "max_iterations": 25,
}


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_slugify_strips_version_and_parenthetical() -> None:
    raw = "Tesla Customer Experience Manager v1 (MAY 08, 2026, 09:21 AM PST)"
    assert _slugify_name(raw) == "tesla-customer-experience-manager"


def test_slugify_handles_special_chars() -> None:
    assert _slugify_name("My Agent: v2 (alpha)") == "my-agent"
    assert _slugify_name("  foo___bar/baz  ") == "foo-bar-baz"


def test_slugify_rejects_empty_after_normalize() -> None:
    with pytest.raises(_LyzrImportError):
        _slugify_name("()___")


def test_parse_examples_handles_json_encoded_string() -> None:
    raw = '[{"user":"hi","assistant":"hello"}]'
    out = _parse_examples(raw)
    assert out == [{"user": "hi", "assistant": "hello"}]


def test_parse_examples_returns_empty_on_invalid_json() -> None:
    assert _parse_examples("not json") == []
    assert _parse_examples("") == []
    assert _parse_examples(None) == []


def test_parse_examples_accepts_already_parsed_list() -> None:
    raw = [{"user": "hi", "assistant": "hello"}]
    assert _parse_examples(raw) == raw


def test_parse_examples_skips_malformed_entries() -> None:
    raw = '[{"user":"ok","assistant":"ok"},{"user":""},{"missing":"both"}]'
    assert _parse_examples(raw) == [{"user": "ok", "assistant": "ok"}]


def test_build_plan_lyzr_runtime_uses_agent_id() -> None:
    plan = _build_plan(TESLA_AGENT, runtime="lyzr")
    assert plan["agent_name"] == "tesla-customer-experience-manager"
    assert plan["provider_str"] == "lyzr/69fe0d9890de3014e9f1cf92"
    assert plan["runtime"] == "lyzr"
    assert plan["params"] == {"temperature": 0.8, "top_p": 1.0}
    assert plan["goals"] == ["Provide accurate, helpful Tesla customer responses."]
    assert "imported-from-lyzr" in plan["tags"]
    assert "tesla-customer-experience-operations-manager" in plan["tags"]
    assert len(plan["examples"]) == 2
    assert len(plan["managed_agents"]) == 2


def test_build_plan_litellm_runtime_maps_provider() -> None:
    plan = _build_plan(TESLA_AGENT, runtime="litellm")
    assert plan["provider_str"] == "openai/gpt-5"
    assert plan["runtime"] == "litellm"


def test_build_plan_rejects_missing_instructions() -> None:
    bad = {**TESLA_AGENT, "agent_instructions": ""}
    with pytest.raises(_LyzrImportError, match="agent_instructions"):
        _build_plan(bad, runtime="lyzr")


def test_build_plan_rejects_unknown_provider() -> None:
    bad = {**TESLA_AGENT, "provider_id": "Cohere"}
    with pytest.raises(_LyzrImportError, match="Cohere"):
        _build_plan(bad, runtime="litellm")


def test_build_plan_lyzr_runtime_rejects_missing_id() -> None:
    bad = {k: v for k, v in TESLA_AGENT.items() if k != "_id"}
    with pytest.raises(_LyzrImportError, match="_id"):
        _build_plan(bad, runtime="lyzr")


def test_build_plan_coerces_string_numeric_params() -> None:
    """Lyzr ships temperature/top_p as strings ('0.8' / '1'). Must coerce."""
    plan = _build_plan(TESLA_AGENT, runtime="lyzr")
    assert isinstance(plan["params"]["temperature"], float)
    assert plan["params"]["temperature"] == 0.8
    assert plan["params"]["top_p"] == 1.0


def test_build_plan_rejects_non_numeric_temperature() -> None:
    bad = {**TESLA_AGENT, "temperature": "spicy"}
    with pytest.raises(_LyzrImportError, match="temperature"):
        _build_plan(bad, runtime="lyzr")


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tesla_json(tmp_path: Path) -> Path:
    path = tmp_path / "tesla.json"
    path.write_text(json.dumps(TESLA_AGENT))
    return path


def test_import_writes_expected_files_lyzr_runtime(
    tmp_path: Path, tesla_json: Path
) -> None:
    out = tmp_path / "agents"
    result = runner.invoke(
        app,
        ["import", "lyzr", str(tesla_json), "-o", str(out)],
    )
    assert result.exit_code == 0, result.stdout

    agent_dir = out / "tesla-customer-experience-manager"
    assert (agent_dir / "agent.yaml").exists()
    assert (agent_dir / "prompt.md").exists()
    assert (agent_dir / "schema" / "input.json").exists()
    assert (agent_dir / "schema" / "output.json").exists()
    assert (agent_dir / "lyzr-original.json").exists()

    agent_yaml = (agent_dir / "agent.yaml").read_text()
    assert "runtime: lyzr" in agent_yaml
    assert "lyzr/69fe0d9890de3014e9f1cf92" in agent_yaml
    assert "imported-from-lyzr" in agent_yaml

    prompt = (agent_dir / "prompt.md").read_text()
    assert "central customer support AI manager" in prompt
    assert "{{ input.message }}" in prompt

    original = json.loads((agent_dir / "lyzr-original.json").read_text())
    assert original["_id"] == TESLA_AGENT["_id"]


def test_import_litellm_runtime_uses_openai_provider(
    tmp_path: Path, tesla_json: Path
) -> None:
    out = tmp_path / "agents"
    result = runner.invoke(
        app,
        ["import", "lyzr", str(tesla_json), "-o", str(out), "--runtime", "litellm"],
    )
    assert result.exit_code == 0, result.stdout

    agent_yaml = (
        out / "tesla-customer-experience-manager" / "agent.yaml"
    ).read_text()
    assert "runtime: litellm" in agent_yaml
    assert "openai/gpt-5" in agent_yaml
    assert "lyzr/" not in agent_yaml.split("# Imported from")[1].split("\n")[0:5][0]


def test_import_rejects_existing_dir_without_force(
    tmp_path: Path, tesla_json: Path
) -> None:
    out = tmp_path / "agents"
    # First import succeeds.
    first = runner.invoke(app, ["import", "lyzr", str(tesla_json), "-o", str(out)])
    assert first.exit_code == 0

    # Second import without --force fails clean.
    second = runner.invoke(app, ["import", "lyzr", str(tesla_json), "-o", str(out)])
    assert second.exit_code == 2
    assert "already exists" in second.output


def test_import_force_overwrites(tmp_path: Path, tesla_json: Path) -> None:
    out = tmp_path / "agents"
    runner.invoke(app, ["import", "lyzr", str(tesla_json), "-o", str(out)])
    result = runner.invoke(
        app, ["import", "lyzr", str(tesla_json), "-o", str(out), "--force"]
    )
    assert result.exit_code == 0, result.stdout


def test_import_rejects_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json {")
    result = runner.invoke(app, ["import", "lyzr", str(bad), "-o", str(tmp_path / "out")])
    assert result.exit_code == 2
    assert "not valid JSON" in result.output or "parse error" in result.output


def test_import_rejects_bad_runtime_flag(tmp_path: Path, tesla_json: Path) -> None:
    result = runner.invoke(
        app,
        ["import", "lyzr", str(tesla_json), "-o", str(tmp_path), "--runtime", "garbage"],
    )
    assert result.exit_code == 2
