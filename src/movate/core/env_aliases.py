"""``MDK_*`` ↔ ``MOVATE_*`` env-var aliasing.

Part of the MDK rename (Sprint A, May 2026). ``MDK_*`` is the canonical
prefix going forward; ``MOVATE_*`` stays as a transitional alias. Both
must keep working through v1.x.

This helper bridges the two prefixes in both directions. It runs once at
CLI startup (``movate.cli.main``, AFTER ``load_dotenv()`` but BEFORE any
other code reads ``os.environ``) and again at the very start of the
FastAPI runtime lifespan (``movate.runtime.app``), so the bridge holds
regardless of entry path — Typer CLI, direct ASGI/uvicorn factory,
embedded, or tests. It is stdlib-only and lives in ``movate.core`` so
that BOTH the control plane (``cli``) and the execution plane
(``runtime``) can import it without violating the
control-plane ⊥ execution-plane boundary (``runtime`` must never import
``cli`` — see ``docs/architecture-principles.md``).

Bridging rules (a *destination* counts as "needs filling" when it is
absent OR present-but-empty/whitespace-only — an empty value is treated
as unset; this is the deployed-Azure case where ``MOVATE_X=""`` shadowed
a real ``MDK_X``):

* ``MDK_X`` set (non-empty), ``MOVATE_X`` unset-or-empty → copy
  MDK → MOVATE so existing read sites (every
  ``os.environ.get("MOVATE_X")`` line) see the new value transparently.
* ``MOVATE_X`` set (non-empty), ``MDK_X`` unset-or-empty → copy
  MOVATE → MDK so new code reading ``MDK_X`` works on legacy configs
  unchanged. Emits a one-shot deprecation warning per process listing
  the legacy vars that actually carried a value and were bridged up.
* Both set (non-empty) → ``MDK_X`` wins (canonical), no copy, no
  warning — the operator clearly intended the canonical setting.

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


def _needs_filling(key: str) -> bool:
    """A destination var "needs filling" when it is absent OR present
    but empty/whitespace-only.

    The empty-string case is the deployed-Azure bug (#67): bicep set the
    canonical ``MDK_X`` to a real value while the legacy ``MOVATE_X`` was
    present as ``""``. A pure presence check (``key not in os.environ``)
    left the empty legacy var in place, so downstream
    ``os.environ.get("MOVATE_X")`` readers saw ``""`` and fell back to
    ephemeral SQLite. Treating empty-or-blank as unset lets the canonical
    value flow down to the legacy name.
    """
    return os.environ.get(key, "").strip() == ""


def sync_env_aliases() -> None:
    """Bridge ``MDK_*`` and ``MOVATE_*`` env vars in both directions.

    Idempotent — safe to call multiple times. The deprecation warning
    only fires on the first call where legacy vars are bridged up.
    """
    legacy_in_use: list[str] = []

    # Snapshot the key→value pairs before mutating. We read source
    # values from this snapshot (not live ``os.environ``) so the result
    # is independent of iteration order: a copy-down that fills a legacy
    # var must not then be re-read as a "legacy var in use" on the same
    # pass. Mutating os.environ during iteration is fine in CPython, but
    # the snapshot keeps the logic order-independent and clear.
    snapshot = dict(os.environ)

    for key, value in snapshot.items():
        if key.startswith(_CANONICAL_PREFIX):
            # MDK_X set — copy down to MOVATE_X if the legacy name needs
            # filling (absent or empty), so legacy readers see the value.
            if value.strip() == "":
                continue
            legacy_key = _LEGACY_PREFIX + key[len(_CANONICAL_PREFIX) :]
            if _needs_filling(legacy_key):
                os.environ[legacy_key] = value
        elif key.startswith(_LEGACY_PREFIX):
            # MOVATE_X set — copy up to MDK_X if the canonical name needs
            # filling (absent or empty). Only a legacy var that actually
            # carried a non-empty value and was bridged up counts as
            # "in use" for the deprecation warning — an empty shadow var
            # does not nag the operator.
            if value.strip() == "":
                continue
            canonical_key = _CANONICAL_PREFIX + key[len(_LEGACY_PREFIX) :]
            if _needs_filling(canonical_key):
                os.environ[canonical_key] = value
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
