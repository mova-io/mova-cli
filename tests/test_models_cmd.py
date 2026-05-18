"""Tests for ``mdk models list`` and ``mdk models show``.

Coverage matrix:

* ``list`` — renders all models, --provider filter, --has-tools filter,
  --has-vision filter, --output json is stable and machine-readable.
* ``show`` — detail panel for a known model, --output json, exits 1 with
  "model not found" message for an unknown model ID.
"""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for tolerant string matching."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


# ---------------------------------------------------------------------------
# ``mdk models list``
# ---------------------------------------------------------------------------


def test_list_renders_all_models() -> None:
    """Default invocation — all models from the pricing table appear in output.

    Uses JSON output to check model IDs reliably (table truncates long IDs
    when the CliRunner terminal is narrow).
    """
    result = runner.invoke(app, ["models", "list", "-o", "json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    ids = {r["model_id"] for r in payload["models"]}
    # Spot-check a few known model IDs from each provider.
    assert any("gpt-4o-2024-08-06" in mid for mid in ids)
    assert any("claude-sonnet-4-6" in mid for mid in ids)
    assert any("claude-haiku" in mid for mid in ids)
    # Provider column should be present.
    providers = {r["provider"] for r in payload["models"]}
    assert "anthropic" in providers
    assert "openai" in providers


def test_list_json_output_is_valid_and_stable() -> None:
    """``--output json`` produces parseable JSON with the expected top-level keys."""
    result = runner.invoke(app, ["models", "list", "--output", "json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    assert "version" in payload
    assert "last_verified" in payload
    assert "models" in payload
    assert isinstance(payload["models"], list)
    assert len(payload["models"]) > 0
    # Each row should have the standard fields.
    row = payload["models"][0]
    for key in (
        "model_id",
        "provider",
        "context_window",
        "input_per_1m",
        "output_per_1m",
        "supports_tools",
        "supports_vision",
    ):
        assert key in row, f"missing key {key!r} in JSON row"


def test_list_json_models_sorted_by_provider_then_model_id() -> None:
    """Models in JSON output are sorted: provider ascending, then model ID ascending."""
    result = runner.invoke(app, ["models", "list", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    ids = [r["model_id"] for r in payload["models"]]
    # Verify the list is sorted by (provider, model_id).
    sorted_ids = sorted(ids, key=lambda x: (x.split("/")[0] if "/" in x else x, x))
    assert ids == sorted_ids


def test_list_provider_filter_returns_only_matching_provider() -> None:
    """``--provider anthropic`` returns only anthropic/* models."""
    result = runner.invoke(app, ["models", "list", "--provider", "anthropic", "-o", "json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    providers = {r["provider"] for r in payload["models"]}
    assert providers == {"anthropic"}
    # Must include at least one anthropic model.
    assert len(payload["models"]) > 0


def test_list_provider_filter_openai() -> None:
    """``--provider openai`` returns only openai/* models (no azure, no anthropic)."""
    result = runner.invoke(app, ["models", "list", "--provider", "openai", "-o", "json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    providers = {r["provider"] for r in payload["models"]}
    assert providers == {"openai"}


def test_list_provider_filter_unknown_exits_1() -> None:
    """Unknown provider with no matches → exit code 1."""
    result = runner.invoke(app, ["models", "list", "--provider", "nonexistent-provider"])
    assert result.exit_code == 1


def test_list_has_tools_filter() -> None:
    """``--has-tools`` returns only models that support tool / function calling."""
    result = runner.invoke(app, ["models", "list", "--has-tools", "-o", "json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    for row in payload["models"]:
        assert row["supports_tools"] is True, f"{row['model_id']} missing tool support"


def test_list_has_vision_filter() -> None:
    """``--has-vision`` returns only models that accept image inputs."""
    result = runner.invoke(app, ["models", "list", "--has-vision", "-o", "json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    assert len(payload["models"]) > 0
    for row in payload["models"]:
        assert row["supports_vision"] is True, f"{row['model_id']} missing vision support"


def test_list_combined_filters() -> None:
    """``--provider anthropic --has-vision`` narrows to anthropic vision models."""
    result = runner.invoke(
        app,
        ["models", "list", "--provider", "anthropic", "--has-vision", "-o", "json"],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    for row in payload["models"]:
        assert row["provider"] == "anthropic"
        assert row["supports_vision"] is True


def test_list_table_output_includes_column_headers() -> None:
    """Rich table output contains the column header names."""
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cleaned = _strip_ansi(result.stdout)
    for header in ("Provider", "Model ID", "Context", "Input", "Output"):
        assert header in cleaned, f"expected column header {header!r} missing from table"


# ---------------------------------------------------------------------------
# ``mdk models show``
# ---------------------------------------------------------------------------


def test_show_known_model_exits_0() -> None:
    """``mdk models show`` for a known model exits 0."""
    result = runner.invoke(app, ["models", "show", "anthropic/claude-sonnet-4-6"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


def test_show_known_model_displays_detail() -> None:
    """Detail panel contains pricing, context window, and capability fields."""
    result = runner.invoke(app, ["models", "show", "openai/gpt-4o-2024-08-06"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cleaned = _strip_ansi(result.stdout)
    assert "openai/gpt-4o-2024-08-06" in cleaned
    assert "openai" in cleaned
    # Context window present.
    assert "128k" in cleaned or "128" in cleaned
    # Pricing section present.
    assert "Input" in cleaned
    assert "Output" in cleaned


def test_show_json_output_is_valid() -> None:
    """``--output json`` for a known model yields parseable JSON."""
    result = runner.invoke(
        app,
        ["models", "show", "anthropic/claude-haiku-4-5-20251001", "-o", "json"],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    row = json.loads(result.stdout)
    assert row["model_id"] == "anthropic/claude-haiku-4-5-20251001"
    assert row["provider"] == "anthropic"
    assert row["in_pricing_table"] is True
    assert isinstance(row["input_per_1m"], float)
    assert isinstance(row["output_per_1m"], float)
    assert isinstance(row["context_window"], int)
    assert isinstance(row["supports_tools"], bool)
    assert isinstance(row["supports_vision"], bool)


def test_show_json_output_has_cached_pricing() -> None:
    """Models with a cached_input price surface it in JSON output."""
    result = runner.invoke(app, ["models", "show", "openai/gpt-4o-2024-08-06", "-o", "json"])
    assert result.exit_code == 0
    row = json.loads(result.stdout)
    assert row["cached_input_per_1m"] is not None
    assert row["cached_input_per_1m"] > 0


def test_show_all_known_models_exit_0() -> None:
    """Every model in the pricing table is retrievable via ``models show``."""
    # Get all model IDs from the JSON list first.
    list_result = runner.invoke(app, ["models", "list", "-o", "json"])
    assert list_result.exit_code == 0
    payload = json.loads(list_result.stdout)
    for entry in payload["models"]:
        mid = entry["model_id"]
        show_result = runner.invoke(app, ["models", "show", mid])
        assert show_result.exit_code == 0, (
            f"models show {mid!r} failed:\n{show_result.stdout}{show_result.stderr or ''}"
        )


def test_show_unknown_model_exits_1() -> None:
    """Unknown model ID → exit 1 with 'model not found' in stderr."""
    result = runner.invoke(app, ["models", "show", "openai/does-not-exist-9999"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "model not found" in combined or "not found" in combined.lower()


def test_show_unknown_model_hints_list() -> None:
    """Unknown model hint mentions ``mdk models list``."""
    result = runner.invoke(app, ["models", "show", "anthropic/fake-model"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "models list" in combined or "list" in combined.lower()
