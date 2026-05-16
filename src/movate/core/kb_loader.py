"""Knowledge-base file resolution for skills.

Skills that need data files (JSON corpora, vector indices, document
snippets, etc.) at runtime look up their dependencies via this
module. The canonical convention since the May-2026 MVP rename is:

    <project_root>/kb/<filename>

Where ``<project_root>`` is the directory containing a project marker
file (``project.yaml`` / ``policy.yaml`` / ``movate.yaml``). Operators
drop knowledge assets here; skills read them.

Pattern of use inside a skill's ``impl.py``::

    from movate.core.kb_loader import resolve_kb_file

    def run(input, ctx):
        corpus_path = resolve_kb_file(
            "kb-lookup-corpus.json",
            start=Path(__file__).parent,
        )
        if corpus_path is None:
            # Project doesn't have a kb/ override — fall back to a
            # bundled default that ships with the skill.
            corpus_path = Path(__file__).parent / "corpus.json"
        ...

This two-tier lookup (project ``kb/`` first, bundled fallback) lets
the skill ship with a working demo corpus while making customization
a single drop-in: operators put their real data at
``<project>/kb/kb-lookup-corpus.json`` and the skill picks it up
automatically on next run.

The resolver returns ``None`` (not raises) on the no-project /
no-file cases so callers can implement their own fallback logic
without try/except gymnastics.
"""

from __future__ import annotations

from pathlib import Path

from movate.core.config import PROJECT_MARKER_FILES


def resolve_kb_file(name: str, *, start: Path | None = None) -> Path | None:
    """Resolve a knowledge-base file at ``<project_root>/kb/<name>``.

    Walks up from ``start`` (default: cwd) looking for the project
    root, then checks ``<project_root>/kb/<name>``. Returns the
    resolved :class:`Path` if it exists, ``None`` otherwise.

    Same walk-up convention as :func:`movate.core.config.is_project_root`
    — the three accepted marker filenames are ``project.yaml`` /
    ``policy.yaml`` / ``movate.yaml``.

    Pass ``start=Path(__file__).parent`` from a skill's ``impl.py`` so
    the lookup is relative to the skill's installation location (the
    skill lives at ``<project>/skills/<name>/impl.py``; walking up
    finds the project root three levels above).

    Returns ``None`` in two cases:

    * No project marker file found anywhere up the tree (skill is
      running outside a project — e.g. during ``mdk skills test``
      against a bare scaffold).
    * Project found, but ``<project_root>/kb/<name>`` doesn't exist.

    Both cases are valid; the caller decides whether to error or
    fall back to a bundled default.
    """
    base = (start or Path.cwd()).resolve()
    # Walk up from `base` including base itself — the project marker
    # might be at the starting directory (rare but possible when a
    # skill is invoked from the project root in tests).
    #
    # Two-tier resolution order:
    # 1. Agent-local: <agent_dir>/kb/<name> — populated by `mdk deploy`
    #    when it bundles the project's kb/ alongside the agent. An agent
    #    boundary is detected by the presence of agent.yaml (same file
    #    the loader requires). When found, we check for the kb file but
    #    keep walking so the project-root tier can still win if the
    #    agent-local kb/ is absent (e.g. during local dev where skills
    #    live at <project>/skills/, not inside an agent dir).
    # 2. Project root: <project_root>/kb/<name> — the canonical local-dev
    #    location; found via the PROJECT_MARKER_FILES walk.
    for parent in (base, *base.parents):
        if (parent / "agent.yaml").is_file():
            agent_kb = parent / "kb" / name
            if agent_kb.is_file():
                return agent_kb
            # Found agent boundary but kb/<name> absent; keep walking
            # so the project-root tier can still resolve it.
        if any((parent / marker).is_file() for marker in PROJECT_MARKER_FILES):
            kb_file = parent / "kb" / name
            return kb_file if kb_file.is_file() else None
    return None


__all__ = ["resolve_kb_file"]
