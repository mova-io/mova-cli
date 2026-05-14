"""``mdk export json-schema`` — emit MDK schemas in standalone form.

Coverage:
* Default ``--direction both`` emits a wrapper object with input/output
* ``--direction input`` / ``--direction output`` emit clean standalone
  JSON Schema (no wrapper) so downstream codegen tools parse directly
* ``--output <path>`` writes to disk + creates parent dirs
* ``--compact`` produces single-line JSON (pipe-friendly)
* Invalid agent path / direction exit 2
* Output is parseable JSON in every mode
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    """Scaffold a default-template agent for export tests."""
    dst = tmp_path / "demo"
    scaffold_agent(dst, name="demo", template="default")
    return dst


# ---------------------------------------------------------------------------
# json-schema — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_schema_both_emits_wrapper_object(agent_dir: Path) -> None:
    """Default direction=both returns a wrapper with input + output keys
    so the file is self-describing when committed."""
    result = runner.invoke(app, ["export", "json-schema", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "input" in payload
    assert "output" in payload
    assert "agent" in payload
    assert payload["agent"] == "demo"


@pytest.mark.unit
def test_json_schema_input_emits_standalone_schema(agent_dir: Path) -> None:
    """direction=input emits the input schema as-is (no wrapper) so
    downstream codegen tools (quicktype, datamodel-codegen) parse it
    directly without unwrapping."""
    result = runner.invoke(app, ["export", "json-schema", str(agent_dir), "--direction", "input"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    # Standalone schema — no wrapper agent/input/output keys
    assert "agent" not in payload
    # But IS a valid JSON Schema (has type + properties OR equivalent)
    assert "type" in payload or "$ref" in payload


@pytest.mark.unit
def test_json_schema_output_emits_standalone_schema(agent_dir: Path) -> None:
    result = runner.invoke(app, ["export", "json-schema", str(agent_dir), "--direction", "output"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "agent" not in payload


@pytest.mark.unit
def test_json_schema_writes_to_output_file(tmp_path: Path, agent_dir: Path) -> None:
    out_path = tmp_path / "out.schema.json"
    result = runner.invoke(
        app,
        [
            "export",
            "json-schema",
            str(agent_dir),
            "--direction",
            "input",
            "--output",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert out_path.is_file()
    # File contents are parseable JSON.
    payload = json.loads(out_path.read_text())
    assert isinstance(payload, dict)
    # Success message printed to stdout (not the JSON itself — that
    # went to the file).
    assert "wrote" in result.stdout


@pytest.mark.unit
def test_json_schema_output_creates_parent_dirs(tmp_path: Path, agent_dir: Path) -> None:
    """Writing to a nested path that doesn't exist works — mkdir(parents=True)."""
    out_path = tmp_path / "deep" / "nested" / "schema.json"
    result = runner.invoke(
        app,
        [
            "export",
            "json-schema",
            str(agent_dir),
            "--direction",
            "output",
            "--output",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert out_path.is_file()


@pytest.mark.unit
def test_json_schema_compact_is_single_line(agent_dir: Path) -> None:
    """--compact emits one-line JSON for piping to other tools."""
    result = runner.invoke(
        app,
        ["export", "json-schema", str(agent_dir), "--direction", "input", "--compact"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # No newline inside the JSON (the parser still accepts it).
    # We check by counting lines: should be 1 line (or 2 if there's a
    # trailing newline from rich; either way far less than the pretty form).
    line_count = sum(1 for line in result.stdout.split("\n") if line.strip())
    assert line_count <= 1, f"expected compact JSON, got {line_count} non-empty lines"
    # Still parseable.
    json.loads(result.stdout)


# ---------------------------------------------------------------------------
# json-schema — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_schema_invalid_direction_exits_two(agent_dir: Path) -> None:
    """Typo in --direction exits 2 with the valid set listed."""
    result = runner.invoke(
        app,
        ["export", "json-schema", str(agent_dir), "--direction", "sideways"],
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "input" in combined or "output" in combined  # valid set named


@pytest.mark.unit
def test_json_schema_bad_agent_path_exits_two(tmp_path: Path) -> None:
    """Non-existent agent path → exit 2 with load failure message."""
    nowhere = tmp_path / "does-not-exist"
    result = runner.invoke(app, ["export", "json-schema", str(nowhere)])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "load" in combined.lower() or "failed" in combined.lower()
