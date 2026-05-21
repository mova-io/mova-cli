"""Compute + write movate-cli's CalVer version: ``YYYY.M.D.N``.

Two files hold the version string in lockstep:

* ``pyproject.toml`` — distribution metadata + what uv / hatchling read
  when building wheels.
* ``src/movate/__init__.py`` — ``__version__`` exposed to runtime callers
  (e.g. ``mdk --version``, ``GET /healthz``).

Versioning scheme (2026-05 onward): **CalVer** ``YYYY.M.D.N`` where ``N`` is
the 1-based ordinal of the commit being made among commits whose
committer-date is *today* and that are reachable from ``HEAD`` (so the first
commit of a day is ``.1``, the next ``.2``, …). Date segments are UNPADDED
(``2026.5.21.7``, not ``2026.05.21.7``) so the string is PEP 440-canonical —
pip / uv / hatchling strip leading zeros anyway, and an unpadded literal
keeps ``mdk --version`` in agreement with the installed-package metadata.

Auto-bumped on every commit by ``.githooks/pre-commit`` (enable once per
clone with ``scripts/install-hooks.sh``, which sets ``core.hooksPath``). The
hook runs this script and re-stages the two files. Also runnable by hand::

    python scripts/bump_version.py            # write the files, print version
    python scripts/bump_version.py --print    # print computed version, write nothing
    python scripts/bump_version.py --check    # exit nonzero if the two files drift

The version is a static literal rewritten per commit (not computed at
import/build time) because the installed wheel has no ``.git`` to count
against — the value must be baked in when the commit is made.

(Until 2026-05 this bumped the SemVer patch component; the project moved to
date-based versions so the version itself records when each build was cut.)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "movate" / "__init__.py"
UV_LOCK = REPO_ROOT / "uv.lock"

# Anchored to line start; ``count=1`` on substitution rewrites the project
# version in pyproject.toml (the first ``version = "..."``), not any dep pin.
_PYPROJECT_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_INIT_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
# uv.lock lists every package; match the version that belongs to OUR
# package by anchoring to its ``name = "movate-cli"`` stanza. The package
# is an editable source (no wheel/sdist hash tied to the version), so a
# surgical one-line replace is equivalent to a full ``uv lock`` and keeps
# CI's ``uv lock --check`` green without re-resolving dependencies.
_UV_LOCK_RE = re.compile(r'(name = "movate-cli"\nversion = ")([^"]+)(")')


def _read_version(path: Path, pattern: re.Pattern[str]) -> str:
    text = path.read_text()
    match = pattern.search(text)
    if not match:
        raise SystemExit(f"could not find version in {path}")
    return match.group(1)


def _commits_today(today: str) -> int:
    """Count commits reachable from HEAD whose committer-date is ``today``.

    ``today`` is an ISO ``YYYY-MM-DD`` string in local time. Returns 0 when
    there's no git history (fresh repo) or git isn't available — so the
    first commit of a day always lands on ``.1``.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--pretty=%cd", "--date=format:%Y-%m-%d"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0
    return sum(1 for line in out.splitlines() if line.strip() == today)


def compute_version(today: str | None = None, commits_today: int | None = None) -> str:
    """Return the ``YYYY.M.D.N`` version for a commit made ``today``.

    ``today`` (ISO ``YYYY-MM-DD``) and ``commits_today`` are injectable for
    tests; in normal use they default to the system date and a ``git log``
    count. ``N = commits_today + 1`` — the 1-based ordinal of the commit
    being made.
    """
    day = _dt.date.fromisoformat(today) if today else _dt.date.today()
    iso = day.isoformat()
    existing = commits_today if commits_today is not None else _commits_today(iso)
    return f"{day.year}.{day.month}.{day.day}.{existing + 1}"


def _read_uv_lock_version() -> str | None:
    """Return movate-cli's pinned version in uv.lock, or None if absent."""
    if not UV_LOCK.is_file():
        return None
    match = _UV_LOCK_RE.search(UV_LOCK.read_text())
    return match.group(2) if match else None


def _check_in_sync() -> None:
    pyproject_v = _read_version(PYPROJECT, _PYPROJECT_RE)
    init_v = _read_version(INIT_PY, _INIT_RE)
    lock_v = _read_uv_lock_version()
    sources = {"pyproject.toml": pyproject_v, "src/movate/__init__.py": init_v}
    if lock_v is not None:
        sources["uv.lock"] = lock_v
    if len(set(sources.values())) > 1:
        detail = ", ".join(f"{name}={v!r}" for name, v in sources.items())
        raise SystemExit(f"version drift detected: {detail}. Reconcile manually.")


def _write_version(new: str) -> None:
    PYPROJECT.write_text(_PYPROJECT_RE.sub(f'version = "{new}"', PYPROJECT.read_text(), count=1))
    INIT_PY.write_text(_INIT_RE.sub(f'__version__ = "{new}"', INIT_PY.read_text(), count=1))
    # Keep uv.lock's own-package pin in lockstep so CI's `uv lock --check`
    # stays green without a (slow) dependency re-resolve.
    if UV_LOCK.is_file():
        UV_LOCK.write_text(_UV_LOCK_RE.sub(rf'\g<1>{new}\g<3>', UV_LOCK.read_text(), count=1))


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the CalVer version into the source tree.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify pyproject.toml and __init__.py versions match; don't bump.",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="Print the computed CalVer version without writing any files.",
    )
    args = parser.parse_args()

    _check_in_sync()
    if args.check:
        print(_read_version(PYPROJECT, _PYPROJECT_RE))
        return

    new = compute_version()
    if args.print_only:
        print(new)
        return

    _write_version(new)
    print(new, file=sys.stdout)


if __name__ == "__main__":
    main()
