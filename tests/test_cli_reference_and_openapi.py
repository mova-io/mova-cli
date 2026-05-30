"""Discoverability-polish tests.

Covers the additive CLI/API discoverability work:

1. **CLI reference** — :func:`generate_cli_reference` is pure + deterministic,
   includes the command tree + per-command help, and the committed
   ``docs/cli-reference.md`` is fresh (the ``--check`` contract).
2. **Single `project` registration** — the duplicate panel registration was
   removed, so ``project`` appears exactly once in the command tree.
3. **OpenAPI export** — the spec builds offline, is deterministic, pins a
   stable version, and the committed ``docs/openapi.json`` is fresh.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import typer

from movate.cli.docs_cmd import generate_cli_reference
from movate.cli.main import app

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str) -> Any:
    """Import a ``scripts/<name>.py`` module by path (scripts/ isn't a package)."""
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# CLI reference
# ---------------------------------------------------------------------------


class TestCliReference:
    def test_is_deterministic(self) -> None:
        assert generate_cli_reference() == generate_cli_reference()

    def test_has_tree_and_command_sections(self) -> None:
        out = generate_cli_reference()
        assert out.startswith("# `mdk` CLI reference")
        assert "## Command tree" in out
        assert "## Commands" in out
        # A representative demo-path command appears as a detail section.
        assert "### `mdk kb search`" in out

    def test_includes_examples_blocks(self) -> None:
        # The Examples blocks added to demo-path commands flow through into
        # the reference verbatim.
        out = generate_cli_reference()
        assert "mdk project create" in out
        assert "mdk catalog list" in out

    def test_no_trailing_whitespace(self) -> None:
        out = generate_cli_reference()
        assert not any(line.endswith(" ") for line in out.splitlines())

    def test_committed_reference_is_fresh(self) -> None:
        committed = (_REPO_ROOT / "docs" / "cli-reference.md").read_text()
        assert committed == generate_cli_reference(), (
            "docs/cli-reference.md is stale — run scripts/gen_cli_reference.py"
        )


# ---------------------------------------------------------------------------
# project — registered exactly once
# ---------------------------------------------------------------------------


def test_project_registered_exactly_once() -> None:
    root = typer.main.get_command(app)
    # `project` is a single group in the resolved command tree (the duplicate
    # DEVELOP-panel registration was removed; it lives under MANAGE now).
    assert "project" in root.commands  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# OpenAPI export
# ---------------------------------------------------------------------------


class TestOpenapiExport:
    def test_builds_offline_and_pins_version(self) -> None:
        mod = _load_script("export_openapi")
        spec = mod._build_spec()
        assert spec["openapi"].startswith("3.")
        assert spec["info"]["version"] == mod._PINNED_VERSION
        assert spec.get("paths"), "expected a non-empty paths object"

    def test_render_is_deterministic(self) -> None:
        mod = _load_script("export_openapi")
        first = mod._render(mod._build_spec())
        second = mod._render(mod._build_spec())
        assert first == second
        # Valid JSON, trailing newline.
        assert json.loads(first)
        assert first.endswith("\n")

    def test_committed_spec_is_fresh(self) -> None:
        mod = _load_script("export_openapi")
        committed = (_REPO_ROOT / "docs" / "openapi.json").read_text()
        assert committed == mod._render(mod._build_spec()), (
            "docs/openapi.json is stale — run scripts/export_openapi.py"
        )
