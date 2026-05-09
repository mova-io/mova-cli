"""``movate pricing`` CLI: table, JSON, and prefix filter behavior."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner()


@pytest.mark.unit
def test_pricing_table_default() -> None:
    """Default invocation prints a Rich table that mentions a known model."""
    result = runner.invoke(app, ["pricing"])
    assert result.exit_code == 0
    assert "movate pricing" in result.stdout
    assert "openai/gpt-4o-mini-2024-07-18" in result.stdout
    assert "anthropic/claude-haiku-4-5-20251001" in result.stdout
    # Header version + verification date should appear.
    assert "last verified" in result.stdout


@pytest.mark.unit
def test_pricing_json_output_is_machine_readable() -> None:
    result = runner.invoke(app, ["pricing", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "version" in payload
    assert "last_verified" in payload
    assert "models" in payload
    # Spot-check one known model
    key = "openai/gpt-4o-mini-2024-07-18"
    assert key in payload["models"]
    entry = payload["models"][key]
    assert entry["input_per_1k"] > 0
    assert entry["output_per_1k"] > 0


@pytest.mark.unit
def test_pricing_provider_prefix_filter_includes_matches() -> None:
    result = runner.invoke(app, ["pricing", "-p", "openai/"])
    assert result.exit_code == 0
    assert "openai/gpt-4o-mini-2024-07-18" in result.stdout
    assert "anthropic/claude-haiku-4-5-20251001" not in result.stdout


@pytest.mark.unit
def test_pricing_provider_prefix_filter_with_no_matches_exits_nonzero() -> None:
    result = runner.invoke(app, ["pricing", "-p", "no-such-provider/"])
    assert result.exit_code == 1


@pytest.mark.unit
def test_pricing_json_filter_yields_subset() -> None:
    result = runner.invoke(app, ["pricing", "-o", "json", "-p", "anthropic/"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["models"]
    for k in payload["models"]:
        assert k.startswith("anthropic/")
