"""Print movate-cli's CalVer — version is git-derived now, NOT committed (ADR 066).

As of ADR 066 the version is computed from git history at build time
(:mod:`scripts.calver_version` via ``hatch_build.py``) and is **not** stored in
any committed file. So this script no longer *edits* anything — there is no
version line to bump, no files to keep in lockstep, and no per-PR bump gate.

It is retained only as a thin, side-effect-free **printer** of the computed
version for CI / operators / debugging. Every invocation just prints the CalVer
and exits 0; the legacy ``--print`` / ``--check`` flags are accepted as no-ops so
old call sites don't break.

    python scripts/bump_version.py            # print the git-derived CalVer
    python scripts/bump_version.py --print    # same (legacy flag)
    python scripts/bump_version.py --check     # same (nothing can drift now)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# scripts/ isn't an importable package; import the sibling computor directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from calver_version import compute_calver


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Accepted for backward compatibility; both are no-ops — the version is
    # derived from git, so there is nothing to write and nothing to drift.
    parser.add_argument("--print", action="store_true", help="(legacy, no-op) print the version")
    parser.add_argument("--check", action="store_true", help="(legacy, no-op) always succeeds")
    parser.parse_args()
    print(compute_calver(str(Path(__file__).resolve().parent.parent)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
