"""Tests for ``scripts/bump_version.py`` — the per-merge patch-bumper.

The script is invoked by ``.github/workflows/ci.yml`` on every push
to main so operators can tell from ``mdk --version`` whether their
installed binary reflects the latest merged code. Regressions in
the parsing or write-back logic would silently break that signal,
so pin the behavior here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"


def _run_script(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke bump_version.py with ``cwd`` rebased so it operates on
    a tmp_path fixture instead of the real repo. The script computes
    its paths from its own ``__file__`` location — copying it into
    ``cwd/scripts/`` keeps the relative-path resolution honest."""
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "bump_version.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _scaffold(tmp_path: Path, *, pyproject_version: str, init_version: str) -> None:
    """Mirror the repo layout the script expects: ``pyproject.toml``
    and ``src/movate/__init__.py`` at the root, with a ``scripts/``
    sibling that holds the script under test."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "movate-cli"\nversion = "{pyproject_version}"\n'
    )
    init_path = tmp_path / "src" / "movate" / "__init__.py"
    init_path.parent.mkdir(parents=True)
    init_path.write_text(f'__version__ = "{init_version}"\n')
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "bump_version.py").write_text(SCRIPT.read_text())


@pytest.mark.unit
def test_bump_increments_patch_and_writes_both_files(tmp_path: Path) -> None:
    """Happy path: 0.8.0 -> 0.8.1 in both pyproject.toml and __init__.py.
    The script must print the new version on stdout so the workflow
    can capture it for the commit message."""
    _scaffold(tmp_path, pyproject_version="0.8.0", init_version="0.8.0")

    result = _run_script(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.8.1"

    pyproject_after = (tmp_path / "pyproject.toml").read_text()
    init_after = (tmp_path / "src" / "movate" / "__init__.py").read_text()
    assert 'version = "0.8.1"' in pyproject_after
    assert '__version__ = "0.8.1"' in init_after


@pytest.mark.unit
def test_bump_handles_double_digit_patch_correctly(tmp_path: Path) -> None:
    """0.8.9 -> 0.8.10 — verify the bump does integer math, not
    string concat. A naive ``+ "1"`` would produce ``"0.8.91"``."""
    _scaffold(tmp_path, pyproject_version="0.8.9", init_version="0.8.9")

    result = _run_script(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "0.8.10"


@pytest.mark.unit
def test_check_mode_reports_current_version_without_bumping(tmp_path: Path) -> None:
    """``--check`` prints the current version + leaves files untouched.
    Useful for a pre-commit hook that wants to *report* the version
    without mutating it."""
    _scaffold(tmp_path, pyproject_version="0.8.0", init_version="0.8.0")

    result = _run_script(tmp_path, "--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.8.0"

    # Files should be byte-identical (no bump happened).
    assert 'version = "0.8.0"' in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "0.8.0"' in (
        tmp_path / "src" / "movate" / "__init__.py"
    ).read_text()


@pytest.mark.unit
def test_drift_between_pyproject_and_init_fails_loudly(tmp_path: Path) -> None:
    """If a PR somehow lands with the two version strings out of
    sync, the auto-bump must refuse to run. Silently fixing one would
    mask the real issue (a missed manual edit) and let drift propagate."""
    _scaffold(tmp_path, pyproject_version="0.8.0", init_version="0.7.0")

    result = _run_script(tmp_path)
    assert result.returncode != 0
    assert "drift detected" in result.stderr
    assert "0.8.0" in result.stderr
    assert "0.7.0" in result.stderr


@pytest.mark.unit
def test_drift_detection_also_runs_under_check_mode(tmp_path: Path) -> None:
    """``--check`` is the right thing to wire into a pre-commit hook
    — make sure it also flags drift, not just the bumping path."""
    _scaffold(tmp_path, pyproject_version="0.8.0", init_version="0.9.0")

    result = _run_script(tmp_path, "--check")
    assert result.returncode != 0
    assert "drift detected" in result.stderr


@pytest.mark.unit
def test_non_semver_version_string_rejected(tmp_path: Path) -> None:
    """Defense: a malformed version like ``0.8`` or ``0.8.0-rc1`` would
    blow up arithmetic in subtle ways. Refuse rather than guess."""
    _scaffold(tmp_path, pyproject_version="0.8", init_version="0.8")

    result = _run_script(tmp_path)
    assert result.returncode != 0
    assert "SemVer" in result.stderr or "shape" in result.stderr


@pytest.mark.unit
def test_repo_state_is_in_sync_today() -> None:
    """Real-repo sanity check: the committed pyproject.toml and
    __init__.py must agree on the version at all times. If this test
    fails, a manual reconcile is needed before the auto-bump can run
    again (it'll refuse on the next push)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"version drift in the actual repo: {result.stderr}"
    )
