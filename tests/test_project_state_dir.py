"""Tests for the project-state-dir resolver (ADR 011).

`.mdk/` is the canonical name; `.movate/` is read for backward compatibility.
Fresh projects default to `.mdk/`; existing `.movate/` projects keep working.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.paths import (
    LEGACY_STATE_DIR_NAME,
    STATE_DIR_NAME,
    has_legacy_state_dir,
    project_state_dir,
)


@pytest.mark.unit
def test_fresh_project_defaults_to_mdk(tmp_path: Path) -> None:
    # Neither dir exists → default to .mdk (created on first write by caller).
    assert project_state_dir(tmp_path) == tmp_path / ".mdk"
    assert STATE_DIR_NAME == ".mdk"


@pytest.mark.unit
def test_legacy_movate_is_used_when_present(tmp_path: Path) -> None:
    (tmp_path / ".movate").mkdir()
    assert project_state_dir(tmp_path) == tmp_path / ".movate"
    assert LEGACY_STATE_DIR_NAME == ".movate"


@pytest.mark.unit
def test_mdk_preferred_over_legacy_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / ".movate").mkdir()
    (tmp_path / ".mdk").mkdir()
    assert project_state_dir(tmp_path) == tmp_path / ".mdk"


@pytest.mark.unit
def test_mdk_used_when_only_mdk_exists(tmp_path: Path) -> None:
    (tmp_path / ".mdk").mkdir()
    assert project_state_dir(tmp_path) == tmp_path / ".mdk"


@pytest.mark.unit
def test_has_legacy_state_dir(tmp_path: Path) -> None:
    assert has_legacy_state_dir(tmp_path) is False  # nothing yet
    (tmp_path / ".movate").mkdir()
    assert has_legacy_state_dir(tmp_path) is True  # legacy only → migratable
    (tmp_path / ".mdk").mkdir()
    assert has_legacy_state_dir(tmp_path) is False  # already has .mdk → not flagged
