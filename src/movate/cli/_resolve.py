"""Shared path-resolution helpers for CLI commands.

Exports:

* :func:`walk_up_for_project_root` — walk up from cwd to find a project
  root (``project.yaml`` / ``policy.yaml`` / ``movate.yaml``). Returns
  ``None`` when not found. Used by every command that needs the project root.

* :func:`resolve_agent_or_workflow_arg` — resolve a bare name to its
  ``agents/`` or ``workflows/`` path when inside a project, falling
  through unchanged for URLs and paths that already exist on disk.

* :func:`suggest_similar_agent` — typo-distance hint for "did you mean X?"
"""

from __future__ import annotations

import difflib
from pathlib import Path


def resolve_agent_or_workflow_arg(arg: str) -> str:
    """Resolve a bare name to its agents/workflows path if applicable.

    Returns the resolved path as a string (so the caller doesn't need
    to change its signature from ``str`` to ``Path``). Falls through
    to the original arg for anything ambiguous — the caller's
    existing error handling deals with the not-found case.
    """
    # URL — remote eval against a deployed runtime.
    if arg.startswith(("http://", "https://")):
        return arg

    # Already a path that exists.
    candidate = Path(arg)
    if candidate.exists():
        return arg

    # Bare name. Need a project root to resolve under.
    project_root = walk_up_for_project_root()
    if project_root is None:
        return arg

    # Try agents/<name>/agent.yaml first (the common case), then
    # workflows/<name>/workflow.yaml.
    agent_path = project_root / "agents" / arg
    if (agent_path / "agent.yaml").is_file():
        return str(agent_path)
    workflow_path = project_root / "workflows" / arg
    if (workflow_path / "workflow.yaml").is_file():
        return str(workflow_path)

    return arg


def list_project_agents() -> list[str]:
    """Return the names of every agent in the current project's
    ``agents/`` directory.

    Used by :func:`suggest_similar_agent` to fuzzy-match typo'd
    names against the actual project state. Returns an empty list
    when called outside a project — caller falls through to its
    own error.
    """
    root = walk_up_for_project_root()
    if root is None:
        return []
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return []
    return sorted(
        candidate.name
        for candidate in agents_dir.iterdir()
        if candidate.is_dir() and (candidate / "agent.yaml").is_file()
    )


def suggest_similar_agent(name: str, *, cutoff: float = 0.6) -> str | None:
    """Return the closest agent name in the project, or ``None`` if
    nothing is within the cutoff similarity.

    Wraps :func:`difflib.get_close_matches` against the project's
    actual agent directory listing — operators typing ``ragqa`` see
    ``Did you mean rag-qa?`` instead of a bare "not found." The
    ``cutoff`` (0.6 default) balances "obvious typo" against
    "unrelated word that happens to share letters."
    """
    candidates = list_project_agents()
    if not candidates:
        return None
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def walk_up_for_project_root() -> Path | None:
    """Walk up from cwd looking for a project-root marker.

    Checks ``project.yaml``, ``policy.yaml``, and ``movate.yaml`` —
    the same set as :data:`movate.core.config.PROJECT_MARKER_FILES`.
    Returns ``None`` when the filesystem root is reached without finding
    any marker, so callers can distinguish "no project" from "cwd is root".
    """
    from movate.core.config import is_project_root  # noqa: PLC0415

    current = Path.cwd().resolve()
    while True:
        if is_project_root(current):
            return current
        if current.parent == current:
            return None
        current = current.parent
