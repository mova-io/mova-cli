"""``MDK_*`` ↔ ``MOVATE_*`` env-var aliasing.

Part of the MDK rename (Sprint A, May 2026). ``MDK_*`` is the canonical
prefix going forward; ``MOVATE_*`` stays as a transitional alias. Both
must keep working through v1.x.

This module runs once at CLI startup, AFTER ``load_dotenv()`` but
BEFORE any other code reads ``os.environ``. It bridges the two prefixes
in both directions:

* ``MDK_X`` set, ``MOVATE_X`` unset → copy MDK → MOVATE so existing
  read sites (every ``os.environ.get("MOVATE_X")`` line) see the new
  value transparently.
* ``MOVATE_X`` set, ``MDK_X`` unset → copy MOVATE → MDK so new code
  reading ``MDK_X`` works on legacy configs unchanged. Emits a one-shot
  deprecation warning per process listing the legacy vars in use.
* Both set → ``MDK_X`` wins (canonical), no copy. No warning — the
  operator clearly intended the canonical setting.

Result: every existing ``MOVATE_*`` env var keeps working with zero
code changes elsewhere; new code can read ``MDK_*`` for clarity; the
deprecation warning gives operators a nudge to rename their CI/k8s
manifests when they're ready.
"""

from __future__ import annotations

import os
import sys

_LEGACY_PREFIX = "MOVATE_"
_CANONICAL_PREFIX = "MDK_"
_WARN_FIRED = False

# How many legacy var names to print verbatim in the deprecation
# warning before condensing into "+N more". Three keeps the line
# short on screens; more would push the message over a typical
# 100-char terminal width.
_MAX_LEGACY_VARS_IN_WARNING = 3


def sync_env_aliases() -> None:
    """Bridge ``MDK_*`` and ``MOVATE_*`` env vars in both directions.

    Idempotent — safe to call multiple times. The deprecation warning
    only fires on the first call where legacy vars are present.
    """
    legacy_in_use: list[str] = []

    # Snapshot the keys before mutating (mutating os.environ during
    # iteration is fine in CPython but the snapshot is clearer).
    keys = list(os.environ.keys())

    for key in keys:
        if key.startswith(_CANONICAL_PREFIX):
            # MDK_X set — copy down to MOVATE_X if unset, so legacy
            # readers see the value transparently.
            legacy_key = _LEGACY_PREFIX + key[len(_CANONICAL_PREFIX) :]
            if legacy_key not in os.environ:
                os.environ[legacy_key] = os.environ[key]
        elif key.startswith(_LEGACY_PREFIX):
            # MOVATE_X set — copy up to MDK_X if unset, so new readers
            # see the value. Note the legacy var for the warning.
            canonical_key = _CANONICAL_PREFIX + key[len(_LEGACY_PREFIX) :]
            if canonical_key not in os.environ:
                os.environ[canonical_key] = os.environ[key]
                legacy_in_use.append(key)

    if legacy_in_use:
        _warn_legacy_vars_once(legacy_in_use)


def _warn_legacy_vars_once(legacy_vars: list[str]) -> None:
    """Print a one-shot deprecation warning listing legacy MOVATE_* vars.

    Stays terse — operators with 10+ env vars don't want 10 warning
    lines, and the list is a sufficient hint for a sed/find-replace
    on their CI config.
    """
    global _WARN_FIRED  # noqa: PLW0603 — single-process one-shot warning state
    if _WARN_FIRED:
        return
    _WARN_FIRED = True

    sample = sorted(legacy_vars)
    if len(sample) > _MAX_LEGACY_VARS_IN_WARNING:
        head = ", ".join(sample[:_MAX_LEGACY_VARS_IN_WARNING])
        rendered = f"{head}, … (+{len(sample) - _MAX_LEGACY_VARS_IN_WARNING} more)"
    else:
        rendered = ", ".join(sample)

    print(
        f"⚠ MOVATE_* env vars are deprecated — rename to MDK_*. "
        f"Currently in use: {rendered}. Both prefixes work through v1.x.",
        file=sys.stderr,
    )
