"""Tests for ``scripts/bump_version.py`` — the CalVer version bumper.

Versioning is CalVer ``YYYY.M.D.N`` (date-based; ``N`` = Nth commit of the
day on the branch). The script keeps three sinks in lockstep —
``pyproject.toml``, ``src/movate/__init__.py``, and the ``movate-cli`` stanza
in ``uv.lock`` — because drift would ship a wheel where ``pip show`` and
``mdk --version`` disagree, or fail CI's ``uv lock --check``.

Two layers of coverage:

* In-process unit tests of ``compute_version`` with injected ``today`` /
  ``commits_today`` (deterministic — no dependence on the wall clock or git).
* Subprocess integration tests that invoke the script against a scaffolded
  tmp repo, exercising the real file rewrites + ``--check`` + drift guard.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"

# Load the script as a module so we can unit-test its pure compute logic.
_spec = importlib.util.spec_from_file_location("bump_version", SCRIPT)
assert _spec and _spec.loader
bump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump)

_CALVER_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


# ---------------------------------------------------------------------------
# compute_version — pure logic, deterministic via injected inputs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeVersion:
    def test_first_commit_of_day_is_n_equals_one(self) -> None:
        assert bump.compute_version(today="2026-05-21", commits_today=0) == "2026.5.21.1"

    def test_n_increments_with_existing_commits(self) -> None:
        # 21 already today → this commit is the 22nd (the user's example).
        assert bump.compute_version(today="2026-05-21", commits_today=21) == "2026.5.21.22"

    def test_date_segments_are_unpadded(self) -> None:
        assert bump.compute_version(today="2026-05-05", commits_today=2) == "2026.5.5.3"
        assert bump.compute_version(today="2026-01-09", commits_today=0) == "2026.1.9.1"

    def test_output_is_pep440_canonical(self) -> None:
        # The literal we write MUST equal its own PEP 440 normalization so
        # `mdk --version` agrees with pip/uv metadata (no leading-zero drift).
        for today, n in [("2026-05-05", 0), ("2026-05-21", 21), ("2026-12-31", 99)]:
            v = bump.compute_version(today=today, commits_today=n)
            assert str(Version(v)) == v

    def test_new_day_resets_n(self) -> None:
        assert bump.compute_version(today="2026-05-22", commits_today=0) == "2026.5.22.1"


# ---------------------------------------------------------------------------
# Subprocess integration — real file rewrites against a scaffolded repo
# ---------------------------------------------------------------------------


def _run_script(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the scaffolded copy of bump_version.py. The script resolves its
    paths from its own ``__file__``, so we run the copy under ``cwd/scripts``
    to keep relative-path resolution honest."""
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "bump_version.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _scaffold(
    tmp_path: Path,
    *,
    pyproject_version: str,
    init_version: str,
    lock_version: str | None = None,
) -> None:
    """Mirror the repo layout the script expects: pyproject.toml +
    src/movate/__init__.py at the root, a scripts/ sibling holding the script
    under test, and (optionally) a uv.lock with a movate-cli stanza."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "movate-cli"\nversion = "{pyproject_version}"\n'
    )
    init_path = tmp_path / "src" / "movate" / "__init__.py"
    init_path.parent.mkdir(parents=True)
    init_path.write_text(f'__version__ = "{init_version}"\n')
    if lock_version is not None:
        (tmp_path / "uv.lock").write_text(
            '[[package]]\nname = "httpx"\nversion = "0.27.0"\n\n'
            f'[[package]]\nname = "movate-cli"\nversion = "{lock_version}"\n'
            'source = { editable = "." }\n'
        )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "bump_version.py").write_text(SCRIPT.read_text())


@pytest.mark.unit
def test_bump_writes_calver_to_all_sinks(tmp_path: Path) -> None:
    """A bump in a (non-git) scaffold writes a CalVer-shaped version — with
    N=1 since there's no git history — to all three sinks and prints it."""
    _scaffold(tmp_path, pyproject_version="0.0.0", init_version="0.0.0", lock_version="0.0.0")

    result = _run_script(tmp_path)
    assert result.returncode == 0, result.stderr
    new = result.stdout.strip()
    assert _CALVER_RE.match(new), f"expected CalVer YYYY.M.D.N, got {new!r}"
    assert new.endswith(".1"), f"no commits in scaffold → N must be 1, got {new!r}"

    assert f'version = "{new}"' in (tmp_path / "pyproject.toml").read_text()
    assert f'__version__ = "{new}"' in (tmp_path / "src" / "movate" / "__init__.py").read_text()
    lock = (tmp_path / "uv.lock").read_text()
    assert f'name = "movate-cli"\nversion = "{new}"' in lock
    # The unrelated dependency pin is untouched.
    assert 'name = "httpx"\nversion = "0.27.0"' in lock


@pytest.mark.unit
def test_print_mode_computes_without_writing(tmp_path: Path) -> None:
    """``--print`` emits the computed version but leaves files untouched."""
    _scaffold(tmp_path, pyproject_version="0.0.0", init_version="0.0.0")

    result = _run_script(tmp_path, "--print")
    assert result.returncode == 0, result.stderr
    assert _CALVER_RE.match(result.stdout.strip())
    # Nothing was rewritten.
    assert 'version = "0.0.0"' in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "0.0.0"' in (tmp_path / "src" / "movate" / "__init__.py").read_text()


@pytest.mark.unit
def test_check_mode_reports_current_version_without_bumping(tmp_path: Path) -> None:
    _scaffold(tmp_path, pyproject_version="2026.5.21.4", init_version="2026.5.21.4")

    result = _run_script(tmp_path, "--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2026.5.21.4"
    assert 'version = "2026.5.21.4"' in (tmp_path / "pyproject.toml").read_text()


@pytest.mark.unit
def test_drift_between_files_fails_loudly(tmp_path: Path) -> None:
    """If the sinks fall out of sync, the script refuses rather than silently
    fixing one — drift is a missed manual edit that should surface."""
    _scaffold(tmp_path, pyproject_version="2026.5.21.4", init_version="2026.5.20.9")

    result = _run_script(tmp_path)
    assert result.returncode != 0
    assert "drift detected" in result.stderr
    assert "2026.5.21.4" in result.stderr
    assert "2026.5.20.9" in result.stderr


@pytest.mark.unit
def test_drift_detection_also_runs_under_check_mode(tmp_path: Path) -> None:
    _scaffold(tmp_path, pyproject_version="2026.5.21.4", init_version="2026.5.21.5")

    result = _run_script(tmp_path, "--check")
    assert result.returncode != 0
    assert "drift detected" in result.stderr


@pytest.mark.unit
def test_uv_lock_drift_is_detected(tmp_path: Path) -> None:
    """uv.lock is part of the lockstep set — a stale lock pin must surface
    (CI's `uv lock --check` would otherwise fail on the bumped version)."""
    _scaffold(
        tmp_path,
        pyproject_version="2026.5.21.4",
        init_version="2026.5.21.4",
        lock_version="2026.5.21.3",
    )

    result = _run_script(tmp_path, "--check")
    assert result.returncode != 0
    assert "drift detected" in result.stderr
    assert "uv.lock" in result.stderr


@pytest.mark.unit
def test_repo_state_is_in_sync_today() -> None:
    """Real-repo sanity: the committed pyproject.toml, __init__.py, and
    uv.lock must agree on the version at all times."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"version drift in the actual repo: {result.stderr}"
