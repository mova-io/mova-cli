"""movate — declarative platform for building, evaluating, and deploying AI agents."""

# Bumped per shipped PR — keep in sync with `pyproject.toml: version`.
# We bump the PATCH level for every merged user-facing PR (the
# bump-version-on-each-PR practice from the May 2026 retrospective);
# MINOR bumps mark a coherent release window (0.7 → 0.8 etc.).
__version__ = "0.7.1"

# Release date for `mdk --version`. Format: YYYY-MM-DD. Updated each
# time `__version__` is bumped — that way `mdk --version` always shows
# WHEN this build was cut, not just which number it has. Cheap +
# greppable; no external metadata system needed.
__release_date__ = "2026-05-15"
