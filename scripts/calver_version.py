"""Compute movate-cli's CalVer (``YYYY.M.D.N``) from git history (ADR 066).

The version is **derived from git at build time**, never stored in a committed
file — so PRs never touch a version line, there's nothing to conflict on, and no
bot writes a version back to ``main`` (the two failure modes ADR 066 closes).

Used by ``hatch_build.py`` (the hatchling metadata hook) at build/install time.
The CalVer *format + counter semantics are unchanged* from the old
``bump_version.py``:

* ``YYYY.M.D`` — the committer date (UTC) of ``HEAD``, segments unpadded
  (PEP 440-canonical: ``2026.6.5``, not ``2026.06.05``).
* ``N``       — the count of commits sharing that UTC calendar day (the same
  monotonic per-day counter the per-commit hook produced).
* A **dirty / uncommitted** tree appends a PEP 440 local segment
  ``+g<shortsha>.dirty`` so dev builds are distinguishable and never collide with
  a clean release artifact (which omits the local segment).

The parsing core (:func:`calver_from_git_data`) is pure + takes injected git
data, so it's unit-tested against fixtures without depending on the real repo's
history. :func:`compute_calver` is the git-reading wrapper; it degrades to
``0+unknown`` rather than failing a build if git is unavailable (e.g. a source
tree with no ``.git``).
"""

from __future__ import annotations

# NOTE: ``timezone.utc`` (not ``datetime.UTC``) keeps this 3.9-safe — the script
# may run under a bare system python; the UP017 noqas below suppress ruff's 3.11 alias.
import os
import subprocess
from datetime import datetime, timezone

#: Env override (highest priority): an explicit version to use verbatim, e.g.
#: passed as a Docker build-arg so the image gets the right CalVer even though
#: ``.git`` is excluded from the build context. Mirrors SETUPTOOLS_SCM_PRETEND_VERSION.
_OVERRIDE_ENV = "MOVATE_BUILD_VERSION"


def _utc_day(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")  # noqa: UP017


def calver_from_git_data(
    head_epoch: int,
    all_epochs: list[int],
    *,
    short_sha: str = "",
    dirty: bool = False,
) -> str:
    """Pure CalVer computation from git data (testable, no subprocess).

    ``head_epoch`` is HEAD's committer unix timestamp; ``all_epochs`` is every
    commit's committer timestamp (order-independent). ``N`` is how many commits
    share HEAD's UTC day. A ``dirty`` tree appends ``+g<short_sha>.dirty``.
    """
    head = datetime.fromtimestamp(head_epoch, tz=timezone.utc)  # noqa: UP017
    target_day = _utc_day(head_epoch)
    n = sum(1 for e in all_epochs if _utc_day(e) == target_day)
    base = f"{head.year}.{head.month}.{head.day}.{max(n, 1)}"
    if dirty:
        return f"{base}+{('g' + short_sha + '.dirty') if short_sha else 'dirty'}"
    return base


def _git(root: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", root, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def compute_calver(root: str = ".") -> str:
    """Resolve the CalVer for the checkout at ``root``. Degrades, never raises.

    Precedence: an explicit ``MOVATE_BUILD_VERSION`` env override wins (used to
    inject the host-computed version into a Docker build whose context excludes
    ``.git``); otherwise it's derived from git history.
    """
    override = os.environ.get(_OVERRIDE_ENV, "").strip()
    if override:
        return override
    try:
        head_epoch = int(_git(root, "log", "-1", "--format=%ct").strip())
        all_epochs = [int(x) for x in _git(root, "log", "--format=%ct").split()]
        short_sha = _git(root, "rev-parse", "--short", "HEAD").strip()
        dirty = bool(_git(root, "status", "--porcelain").strip())
        return calver_from_git_data(head_epoch, all_epochs, short_sha=short_sha, dirty=dirty)
    except Exception:  # pragma: no cover - git absent / not a repo → safe sentinel
        return "0+unknown"


if __name__ == "__main__":
    # Thin debug printer (ADR 066 D4 — bump_version.py no longer EDITS files;
    # this prints the computed version for CI/operators).
    print(compute_calver())
