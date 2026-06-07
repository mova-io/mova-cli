"""CalVer is derived from git at build time (ADR 066).

Covers the pure computor (`scripts/calver_version.calver_from_git_data`) against
fixed git data — no wall clock, no dependence on the real repo's history — plus
that the legacy `scripts/bump_version.py` is now a side-effect-free printer that
no longer edits any file.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


calver = _load("calver_version")


def _epoch(y: int, m: int, d: int, h: int = 12) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp())


# --------------------------------------------------------------------------- #
# Pure CalVer computation
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_counter_counts_commits_sharing_heads_utc_day() -> None:
    head = _epoch(2026, 6, 5)
    # 3 commits on 2026-06-05, 2 on prior days → N == 3.
    epochs = [
        head,
        _epoch(2026, 6, 5, 9),
        _epoch(2026, 6, 5, 23),
        _epoch(2026, 6, 4),
        _epoch(2026, 6, 1),
    ]
    assert calver.calver_from_git_data(head, epochs) == "2026.6.5.3"


@pytest.mark.unit
def test_segments_are_unpadded_pep440_canonical() -> None:
    head = _epoch(2026, 1, 3)
    assert calver.calver_from_git_data(head, [head]) == "2026.1.3.1"  # not 2026.01.03


@pytest.mark.unit
def test_dirty_tree_appends_local_segment() -> None:
    head = _epoch(2026, 6, 5)
    v = calver.calver_from_git_data(head, [head], short_sha="abc1234", dirty=True)
    assert v == "2026.6.5.1+gabc1234.dirty"


@pytest.mark.unit
def test_clean_tree_has_no_local_segment() -> None:
    head = _epoch(2026, 6, 5)
    v = calver.calver_from_git_data(head, [head], short_sha="abc1234", dirty=False)
    assert v == "2026.6.5.1"  # release artifacts are clean PEP 440


@pytest.mark.unit
def test_counter_is_never_below_one() -> None:
    head = _epoch(2026, 6, 5)
    # Degenerate input (head not in the list) still yields N>=1.
    assert calver.calver_from_git_data(head, []).endswith(".1")


# --------------------------------------------------------------------------- #
# compute_calver against the real repo + the legacy printer
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_compute_calver_reads_this_repo() -> None:
    v = calver.compute_calver(str(_SCRIPTS.parent))
    # Real repo → a CalVer like 2026.M.D.N (optionally + a local dev segment).
    assert v[0].isdigit()
    assert v.split("+")[0].count(".") == 3


@pytest.mark.unit
def test_bump_version_script_only_prints_and_writes_nothing() -> None:
    """ADR 066 D4: bump_version.py no longer edits files — it just prints."""
    repo = _SCRIPTS.parent
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "bump_version.py")],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip()  # printed a version
    # The committed pyproject carries no `version =` line (it's dynamic now).
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert "\nversion = " not in pyproject
    assert 'dynamic = ["version"]' in pyproject
