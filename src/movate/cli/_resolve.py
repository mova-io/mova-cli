"""Shared path-resolution helpers for CLI commands.

Exports:

* :func:`walk_up_for_project_root` — walk up from cwd to find a project
  root (``project.yaml`` / ``policy.yaml`` / ``movate.yaml``). Returns
  ``None`` when not found. Used by every command that needs the project root.

* :func:`resolve_agent_or_workflow_arg` — resolve a bare name to its
  ``agents/`` or ``workflows/`` path when inside a project, falling
  through unchanged for URLs and paths that already exist on disk.

* :func:`resolve_agent_arg` — the ONE shared agent-name→path resolver
  backing ``mdk run`` / ``validate`` / ``dev`` (ADR 026 D2). Deterministic
  order: existing path → name in the discovered project → friendly error.

* :func:`suggest_similar_agent` — typo-distance hint for "did you mean X?"
"""

from __future__ import annotations

import difflib
from pathlib import Path

# Cap on how many agent names the friendly not-found message inlines —
# beyond this the list is noise, so we drop it (the operator can run
# `mdk validate --all` / list the dir).
_MAX_AGENTS_TO_LIST = 12


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


def _looks_like_path(arg: str) -> bool:
    """True when ``arg`` is path-shaped (has a separator, is ``.``/``..``,
    or starts with ``~``) — i.e. NOT a bare agent name.

    Bare names (``sitebot``) resolve under ``agents/``; path-shaped args
    (``.``, ``./agents/x``, ``/abs/x``) are taken literally. Used to decide
    whether the friendly not-found message (ADR 026 D2) should suggest the
    by-name vs by-path forms.
    """
    return arg in (".", "..") or "/" in arg or "\\" in arg or arg.startswith("~")


def agent_not_found_message(arg: str) -> str:
    """Friendly "no agent here" message for ``mdk run/validate/dev`` (ADR 026 D2).

    Replaces the raw ``agent path is not a directory`` failure. Offers the
    two things the operator most likely meant: resolve a NAME from the
    project root, or run the agent in the CURRENT folder by path (``.``).
    Appends a did-you-mean when a similarly-named agent exists in the
    project, and lists the available agents when there are a handful.
    """
    name = Path(arg).name or arg
    lines = [f"no agent '{name}' here."]

    suggestion = suggest_similar_agent(name)
    if suggestion:
        lines.append(f"  → did you mean '{suggestion}'?")

    in_project = walk_up_for_project_root() is not None
    cwd_is_agent = (Path.cwd() / "agent.yaml").is_file()

    if in_project:
        lines.append(f"  → run it by name from the project root:  mdk run {name}")
        agents = list_project_agents()
        if agents and len(agents) <= _MAX_AGENTS_TO_LIST:
            lines.append(f"  → agents in this project: {', '.join(agents)}")
    if cwd_is_agent:
        lines.append("  → or run the agent in THIS folder by path:  mdk run .")
    elif not in_project:
        # Neither a project nor a standalone agent dir — point both ways.
        lines.append(
            "  → not inside a project. Pass a path to an agent dir "
            "(mdk run ./path/to/agent) or `cd` into a project first."
        )
    return "\n".join(lines)


def resolve_agent_arg(arg: str) -> Path:
    """Resolve an agent NAME or PATH to its directory (ADR 026 D2).

    The ONE shared resolver backing ``mdk run`` / ``validate`` / ``dev``.
    Deterministic order:

    1. ``arg`` is an existing path (``.``, ``./agents/x``, ``/abs/x``) →
       use it verbatim (existing path ALWAYS wins, even if a same-named
       agent exists in the project — no ambiguity surprise).
    2. else treat ``arg`` as a NAME and resolve under the discovered
       project's ``agents/<name>/`` (or ``workflows/<name>/``).
    3. else raise :class:`FileNotFoundError` carrying the friendly
       :func:`agent_not_found_message` — callers turn it into an exit-2.

    A STANDALONE agent dir is first-class: ``mdk run .`` (step 1) and a
    by-path arg both resolve WITHOUT requiring a ``project.yaml`` marker —
    the loader's ``_resolve_project_root`` falls back to the agent's parent.

    Returns the resolved :class:`Path`. URLs (remote runtimes) are out of
    scope here — callers handle ``--target`` / ``http(s)://`` before calling.
    """
    candidate = Path(arg)
    # 1. Existing path wins outright.
    if candidate.exists():
        return candidate
    # 2. Bare name → project agents/ or workflows/.
    if not _looks_like_path(arg):
        project_root = walk_up_for_project_root()
        if project_root is not None:
            agent_path = project_root / "agents" / arg
            if (agent_path / "agent.yaml").is_file():
                return agent_path
            workflow_path = project_root / "workflows" / arg
            if (workflow_path / "workflow.yaml").is_file():
                return workflow_path
    # 3. Nothing resolved → friendly error.
    raise FileNotFoundError(agent_not_found_message(arg))


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
