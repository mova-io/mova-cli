"""Bump the patch component of movate-cli's version.

Two files hold the canonical version string in lockstep:

* ``pyproject.toml`` — distribution metadata + the version uv reads
  when building wheels.
* ``src/movate/__init__.py`` — ``__version__`` exposed to runtime
  callers (e.g. ``mdk --version``).

Invoked manually by a maintainer cutting a release — see step 6 of
``RELEASING.md``. Prints the new version on stdout. (Until 2026-05
this was wired into a per-merge CI auto-bump job; that path was
removed because the mova-io org policy blocks GitHub Actions from
opening PRs, leaving the bot pushing dangling bump branches with no
PR ever created. The release-tag model is more honest anyway.)

Usage::

    python scripts/bump_version.py            # bump patch
    python scripts/bump_version.py --check    # exit nonzero if files drift

The ``--check`` mode is a safety net: if a PR somehow lands with
pyproject.toml's version != src/movate/__init__.py's __version__,
the bump script would just silently fix one — better to fail loudly
and force a manual reconcile.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "movate" / "__init__.py"

_PYPROJECT_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_INIT_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def _read_version(path: Path, pattern: re.Pattern[str]) -> str:
    text = path.read_text()
    match = pattern.search(text)
    if not match:
        raise SystemExit(f"could not find version in {path}")
    return match.group(1)


_SEMVER_PARTS = 3  # major.minor.patch


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) != _SEMVER_PARTS or not all(p.isdigit() for p in parts):
        raise SystemExit(f"unexpected SemVer shape: {version!r} (want N.N.N)")
    major, minor, patch = (int(p) for p in parts)
    return f"{major}.{minor}.{patch + 1}"


def _check_in_sync() -> None:
    pyproject_v = _read_version(PYPROJECT, _PYPROJECT_RE)
    init_v = _read_version(INIT_PY, _INIT_RE)
    if pyproject_v != init_v:
        raise SystemExit(
            f"version drift detected: pyproject.toml={pyproject_v!r} but "
            f"src/movate/__init__.py={init_v!r}. Reconcile manually before "
            "the auto-bump can run."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify pyproject.toml and __init__.py versions match; don't bump.",
    )
    args = parser.parse_args()

    _check_in_sync()
    if args.check:
        print(_read_version(PYPROJECT, _PYPROJECT_RE))
        return

    old = _read_version(PYPROJECT, _PYPROJECT_RE)
    new = _bump_patch(old)

    pyproject_text = PYPROJECT.read_text()
    PYPROJECT.write_text(_PYPROJECT_RE.sub(f'version = "{new}"', pyproject_text, count=1))

    init_text = INIT_PY.read_text()
    INIT_PY.write_text(_INIT_RE.sub(f'__version__ = "{new}"', init_text, count=1))

    print(new, file=sys.stdout)


if __name__ == "__main__":
    main()
