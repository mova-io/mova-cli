"""``movate scaffold tool <name>`` — generate boilerplate for a tool.

Tests cover the contract:

* Default target ``./tools/<name>/`` gets four files: ``tool.yaml``,
  ``handler.py``, ``schema/input.json``, ``schema/output.json``.
* Each file is valid YAML / JSON / Python (we lint by importlib).
* Name placeholder is substituted everywhere.
* Custom ``--target`` works.
* Re-running without ``--force`` exits 2 (no surprise overwrite).
* Re-running with ``--force`` replaces the directory.
* Invalid names (uppercase / underscores) rejected at parse time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app

runner = CliRunner(mix_stderr=False)


@pytest.mark.unit
def test_scaffold_tool_creates_complete_layout(tmp_path: Path) -> None:
    """Default scaffold drops a valid tool dir under ``./tools/<name>/``."""
    target = tmp_path / "tools"
    result = runner.invoke(
        cli_app,
        ["scaffold", "tool", "web-search", "--target", str(target)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    tool_dir = target / "web-search"
    assert tool_dir.is_dir()
    # All four expected files present.
    assert (tool_dir / "tool.yaml").is_file()
    assert (tool_dir / "handler.py").is_file()
    assert (tool_dir / "schema" / "input.json").is_file()
    assert (tool_dir / "schema" / "output.json").is_file()


@pytest.mark.unit
def test_scaffold_tool_substitutes_name_placeholder(tmp_path: Path) -> None:
    """``__TOOL_NAME__`` token in every templated file is replaced
    with the user-provided name. Belt-and-braces: assert there's
    NO un-substituted placeholder left anywhere."""
    target = tmp_path / "tools"
    result = runner.invoke(
        cli_app,
        ["scaffold", "tool", "sql-query", "--target", str(target)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    tool_dir = target / "sql-query"
    spec = yaml.safe_load((tool_dir / "tool.yaml").read_text())
    assert spec["name"] == "sql-query"
    assert spec["kind"] == "Tool"
    assert spec["api_version"] == "movate/v1"

    input_schema = json.loads((tool_dir / "schema" / "input.json").read_text())
    assert "sql-query" in input_schema["title"]

    output_schema = json.loads((tool_dir / "schema" / "output.json").read_text())
    assert "sql-query" in output_schema["title"]

    handler = (tool_dir / "handler.py").read_text()
    assert "sql-query" in handler
    # No leftover placeholder tokens anywhere.
    for path in tool_dir.rglob("*"):
        if path.is_file():
            assert "__TOOL_NAME__" not in path.read_text(), f"placeholder leaked in {path}"


@pytest.mark.unit
def test_scaffold_tool_handler_is_importable_python(tmp_path: Path) -> None:
    """The handler stub must be syntactically valid Python (and
    expose an ``async def handler``) — otherwise the user's IDE
    chokes immediately."""
    import ast  # noqa: PLC0415

    target = tmp_path / "tools"
    runner.invoke(cli_app, ["scaffold", "tool", "demo", "--target", str(target)])

    handler_src = (target / "demo" / "handler.py").read_text()
    tree = ast.parse(handler_src)
    # At least one async function named `handler` defined at module scope.
    async_defs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    assert any(fn.name == "handler" for fn in async_defs)


@pytest.mark.unit
def test_scaffold_tool_default_target_is_dot_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``--target`` → drops into ``./tools/<name>/`` from cwd."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_app, ["scaffold", "tool", "default-target-test"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "tools" / "default-target-test" / "tool.yaml").is_file()


@pytest.mark.unit
def test_scaffold_tool_existing_dir_fails_without_force(tmp_path: Path) -> None:
    """Re-running over an existing tool dir without ``--force`` must
    exit 2 — silently overwriting would lose user edits."""
    target = tmp_path / "tools"
    runner.invoke(cli_app, ["scaffold", "tool", "demo", "--target", str(target)])
    # User edits handler.py.
    (target / "demo" / "handler.py").write_text("# do not lose me\n")

    result = runner.invoke(cli_app, ["scaffold", "tool", "demo", "--target", str(target)])
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    # Edit preserved.
    assert "do not lose me" in (target / "demo" / "handler.py").read_text()


@pytest.mark.unit
def test_scaffold_tool_force_overwrites(tmp_path: Path) -> None:
    """``--force`` wipes the dir and re-scaffolds from the template."""
    target = tmp_path / "tools"
    runner.invoke(cli_app, ["scaffold", "tool", "demo", "--target", str(target)])
    (target / "demo" / "handler.py").write_text("# user edit\n")

    result = runner.invoke(
        cli_app,
        ["scaffold", "tool", "demo", "--target", str(target), "--force"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # User edit is gone — replaced by fresh template.
    handler = (target / "demo" / "handler.py").read_text()
    assert "user edit" not in handler
    assert "async def handler" in handler


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_name",
    ["WebSearch", "web_search", "Web-Search", "1numeric-start", "", "with spaces"],
)
def test_scaffold_tool_rejects_invalid_name(tmp_path: Path, bad_name: str) -> None:
    """Only lowercase + hyphens. Anything else exits 2 with a clear hint."""
    result = runner.invoke(
        cli_app,
        ["scaffold", "tool", bad_name, "--target", str(tmp_path / "tools")],
    )
    assert result.exit_code == 2, f"unexpectedly accepted {bad_name!r}"
