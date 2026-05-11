"""Shell-completion helpers for CLI arguments.

Typer/Click handle option + subcommand completion out of the box once
the user runs ``movate --install-completion``. Argument *values* —
"complete the agent name after ``movate run ``" — need a per-argument
``shell_complete=`` callable. This module supplies the two we use:

* :func:`complete_agent_path` — used by every local command that takes
  a ``Path`` to an agent directory (``run``, ``validate``, ``show``,
  ``watch``, ``bench``, ``eval``). Suggests both bare names from the
  agents root (``faq-agent``) AND the relative path form
  (``agents/faq-agent``) since both are valid args to those commands.

* :func:`complete_agent_name` — used by ``movate submit`` whose first
  arg is a *name*, not a path. We only complete from the local
  ``agents/`` tree on disk; querying the deployed runtime for the
  real registry would require a sync HTTP call on every TAB, which
  is the wrong tradeoff. (Future: cache the runtime's /agents
  response on disk and refresh it via an explicit subcommand.)

Click's ``shell_complete=`` signature is ``(ctx, param, incomplete)``;
the underscore-named parameters mark the two we don't use. We also
expose ``_complete_agent_path_impl(incomplete)`` / ``_..._name_impl``
as the bare single-arg implementations so tests + reuse don't have
to fabricate Click Context objects.

Both functions are deliberately sync and never raise — completion
runs on every keystroke that triggers shell expansion, so any latency
or error spirals user-visible. On any unexpected condition we return
``[]`` so completion silently does nothing rather than spewing a
traceback into the user's prompt.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any


def _agents_root() -> Path:
    """Resolve the directory we scan for agent dirs.

    Same precedence as ``movate serve`` / ``movate worker``:
    ``MOVATE_AGENTS_PATH`` env var wins; otherwise ``./agents`` from
    the current working directory.
    """
    env = os.environ.get("MOVATE_AGENTS_PATH")
    if env:
        return Path(env)
    return Path("./agents")


def _agent_dirs(root: Path) -> list[Path]:
    """Direct children of ``root`` that look like agent directories.

    Same "looks like an agent" check as the runtime registry: directory
    that contains an ``agent.yaml``. Cheap stat-based filter; no YAML
    parsing — completion has to stay snappy.
    """
    if not root.exists() or not root.is_dir():
        return []
    out: list[Path] = []
    with contextlib.suppress(OSError):
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if (entry / "agent.yaml").exists():
                out.append(entry)
    return sorted(out)


def _complete_agent_path_impl(incomplete: str) -> list[str]:
    """Inner impl — see :func:`complete_agent_path` for the contract."""
    try:
        root = _agents_root()
        candidates: list[str] = []
        for d in _agent_dirs(root):
            # Bare name AND prefixed form — let the user have either.
            candidates.append(d.name)
            candidates.append(str(d))
        # De-dupe while preserving order, then filter by prefix.
        seen: set[str] = set()
        unique: list[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return [c for c in unique if c.startswith(incomplete)]
    except Exception:
        return []


def _complete_agent_name_impl(incomplete: str) -> list[str]:
    """Inner impl — see :func:`complete_agent_name` for the contract."""
    try:
        root = _agents_root()
        return [d.name for d in _agent_dirs(root) if d.name.startswith(incomplete)]
    except Exception:
        return []


def complete_agent_path(_ctx: Any, _param: Any, incomplete: str) -> list[str]:
    """Suggest agent-directory paths whose name starts with ``incomplete``.

    Returns both bare names and the resolved-path form, since both are
    valid to commands like ``movate run`` (which accept a relative or
    absolute path to an agent directory).
    """
    return _complete_agent_path_impl(incomplete)


def complete_agent_name(_ctx: Any, _param: Any, incomplete: str) -> list[str]:
    """Suggest agent *names* (not paths) for ``movate submit``.

    Scans the local ``agents/`` tree only — does NOT call the deployed
    runtime's ``/agents`` endpoint. Rationale: completion must be
    instant (sub-50ms) and not require auth/network state. The
    local-vs-remote drift is acceptable because the typical workflow
    is "develop locally, deploy, submit" — names match in practice.
    """
    return _complete_agent_name_impl(incomplete)


__all__ = [
    "_complete_agent_name_impl",
    "_complete_agent_path_impl",
    "complete_agent_name",
    "complete_agent_path",
]
