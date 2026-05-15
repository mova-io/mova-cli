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
"""

from __future__ import annotations

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


def _walk_up_for_project_root() -> Path | None:
    """Walk up from cwd looking for ``movate.yaml`` — same convention
    used by every other project-aware command (``mdk add``,
    ``mdk doctor agent``, ``mdk snapshot``, ``mdk diff``)."""
    current = Path.cwd().resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent
