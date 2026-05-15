"""Shared path-resolution helpers for CLI commands that accept a
positional ``<path>`` argument referring to an agent or workflow.

The motivating use case: when inside a movate project, operators want
to type a bare name (``mdk run rag-qa``) instead of the full path
(``mdk run ./agents/rag-qa``). This module's
:func:`resolve_agent_or_workflow_arg` does that resolution for any
command that opts in.

Resolution rules (in order):

1. URL → leave unchanged (``http://`` / ``https://`` for remote eval).
2. Path that exists on disk → leave unchanged (operator passed full path).
3. Bare name + we're inside a movate project (walk-up finds movate.yaml):
   look for ``./agents/<name>/agent.yaml`` first, then
   ``./workflows/<name>/workflow.yaml``. First hit wins.
4. Otherwise → leave unchanged (caller's error path surfaces a clear
   "not found" message).

Companion: :func:`suggest_similar_agent` surfaces a typo-distance
suggestion when the operator's bare name doesn't resolve. Used by
caller error paths to render "did you mean rag-qa?" hints.
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
    project_root = _walk_up_for_project_root()
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
    root = _walk_up_for_project_root()
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


def _walk_up_for_project_root() -> Path | None:
    """Walk up from cwd looking for a project-root marker. Same set of
    accepted names as :data:`movate.core.config.PROJECT_MARKER_FILES`
    (``project.yaml`` / ``policy.yaml`` / ``movate.yaml``)."""
    from movate.core.config import is_project_root  # noqa: PLC0415

    current = Path.cwd().resolve()
    while True:
        if is_project_root(current):
            return current
        if current.parent == current:
            return None
        current = current.parent
