"""Tests for the per-PR version-bump gate (scripts/bump_version.py).

The gate fails a PR whose CalVer version isn't strictly newer than the base
branch's — enforcing "increment the version on every merge" and catching the
collision class where two PRs off the same base compute the same next N (which
is how 2026.5.22.19 once landed on two consecutive merges).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bump_version", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bv = _load()


@pytest.mark.unit
class TestVersionOrdering:
    def test_same_day_increment_is_ahead(self) -> None:
        assert bv.is_strictly_ahead("2026.5.22.20", "2026.5.22.19")

    def test_equal_is_not_ahead(self) -> None:
        # The exact bug we're guarding against: a merge that didn't bump.
        assert not bv.is_strictly_ahead("2026.5.22.19", "2026.5.22.19")

    def test_older_is_not_ahead(self) -> None:
        assert not bv.is_strictly_ahead("2026.5.22.18", "2026.5.22.19")

    def test_new_day_is_ahead(self) -> None:
        assert bv.is_strictly_ahead("2026.5.23.1", "2026.5.22.99")

    def test_numeric_not_lexical_comparison(self) -> None:
        # Lexically "100" < "99"; numerically 100 > 99. Must be numeric.
        assert bv.is_strictly_ahead("2026.5.22.100", "2026.5.22.99")

    def test_version_tuple_parses_calver(self) -> None:
        assert bv._version_tuple("2026.5.22.19") == (2026, 5, 22, 19)
