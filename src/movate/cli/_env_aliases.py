"""``MDK_*`` ↔ ``MOVATE_*`` env-var aliasing (compat re-export).

The implementation moved to :mod:`movate.core.env_aliases` so that the
execution plane (``movate.runtime``) can call ``sync_env_aliases()`` at
lifespan startup without importing the control plane (``movate.cli``) —
that import would violate the control-plane ⊥ execution-plane boundary
(see ``docs/architecture-principles.md``: "``runtime`` does not import
``cli``"). ``movate.core`` is the neutral layer both planes already
depend on.

This module is kept as a thin re-export so the existing public import
path — ``from movate.cli._env_aliases import sync_env_aliases``, used by
``movate.cli.main`` — keeps working unchanged. New callers should import
from :mod:`movate.core.env_aliases` directly.
"""

from __future__ import annotations

from movate.core.env_aliases import (
    _MAX_LEGACY_VARS_IN_WARNING,
    _warn_legacy_vars_once,
    sync_env_aliases,
)

__all__ = [
    "_MAX_LEGACY_VARS_IN_WARNING",
    "_warn_legacy_vars_once",
    "sync_env_aliases",
]
