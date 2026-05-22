"""Filesystem path conventions for movate projects (ADR 011).

The **project-level** runtime-state directory — snapshots, eval baselines,
``local.db``, the promotions log — is named ``.mdk/``. For backward
compatibility with projects created under the old ``.movate/`` name, the
resolver prefers ``.mdk/`` but falls back to an existing ``.movate/``; fresh
projects get ``.mdk/``.

This is the project STATE dir only. The machine-global ``~/.movate/``
(credentials / config / profiles / secrets) is a separate, unchanged
convention — do not route it through here.
"""

from __future__ import annotations

from pathlib import Path

#: Canonical project-state directory name (ADR 011).
STATE_DIR_NAME = ".mdk"
#: Legacy name, still read for backward compatibility.
LEGACY_STATE_DIR_NAME = ".movate"


def project_state_dir(root: Path) -> Path:
    """Return the project-state directory under ``root``.

    Resolution (ADR 011 D2 — read-compat, never moves data):

    * ``root/.mdk`` if it exists → use it.
    * else ``root/.movate`` if it exists → use it (legacy project).
    * else ``root/.mdk`` → the default for fresh projects (created on first
      write by the caller).

    ``root`` is a project root *or* an agent directory — per-agent eval
    baselines live under their own state dir, so the resolver works for both.
    """
    mdk = root / STATE_DIR_NAME
    if mdk.is_dir():
        return mdk
    legacy = root / LEGACY_STATE_DIR_NAME
    if legacy.is_dir():
        return legacy
    return mdk


def has_legacy_state_dir(root: Path) -> bool:
    """True when ``root`` carries a legacy ``.movate/`` and no ``.mdk/`` yet —
    i.e. a candidate for ``mdk migrate-state``."""
    return (root / LEGACY_STATE_DIR_NAME).is_dir() and not (root / STATE_DIR_NAME).is_dir()
