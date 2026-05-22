"""Tests for ``mdk migrate-state`` — legacy `.movate/` → `.mdk/` (ADR 011)."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from movate.cli.migrate_state_cmd import _rewrite_gitignore, migrate_state


def _legacy_project(root: Path) -> Path:
    state = root / ".movate"
    (state / "snapshots" / "abc").mkdir(parents=True)
    (state / "local.db").write_text("x")
    (state / "agentX").mkdir()
    (state / "agentX" / "baseline.json").write_text("{}")
    return state


@pytest.mark.unit
def test_migrate_moves_movate_to_mdk(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    migrate_state(path=tmp_path, dry_run=False)
    assert not (tmp_path / ".movate").exists()
    assert (tmp_path / ".mdk" / "snapshots" / "abc").is_dir()
    assert (tmp_path / ".mdk" / "agentX" / "baseline.json").read_text() == "{}"


@pytest.mark.unit
def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    migrate_state(path=tmp_path, dry_run=True)
    assert (tmp_path / ".movate").is_dir()
    assert not (tmp_path / ".mdk").exists()


@pytest.mark.unit
def test_noop_when_no_legacy_dir(tmp_path: Path) -> None:
    # No .movate/ → no-op, no .mdk/ created, no error.
    migrate_state(path=tmp_path, dry_run=False)
    assert not (tmp_path / ".mdk").exists()


@pytest.mark.unit
def test_refuses_to_clobber_nonempty_mdk(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    (tmp_path / ".mdk").mkdir()
    (tmp_path / ".mdk" / "keep.txt").write_text("important")
    with pytest.raises(typer.Exit):
        migrate_state(path=tmp_path, dry_run=False)
    # Both left intact.
    assert (tmp_path / ".movate").is_dir()
    assert (tmp_path / ".mdk" / "keep.txt").read_text() == "important"


@pytest.mark.unit
def test_gitignore_rewrite_swaps_project_patterns_only(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        "~/.movate/\n.movate/*\n!.movate/*/\n.movate/*/*\n!.movate/*/baseline.json\n*.db\n"
    )
    assert _rewrite_gitignore(tmp_path) is True
    body = (tmp_path / ".gitignore").read_text()
    # Project patterns migrated…
    assert ".mdk/*" in body
    assert "!.mdk/*/baseline.json" in body
    assert ".movate/*" not in body
    # …but the machine-global line is untouched.
    assert "~/.movate/" in body
