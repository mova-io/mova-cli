"""Small unified-diff helpers for action plan previews (ADR 025 D2).

Actions render their intended file change as a unified diff in the
:class:`~movate.authoring.models.ActionPlan` so a human / planner sees exactly
what ``apply`` will write — without anything being written. Kept tiny and
dependency-free (stdlib ``difflib``).
"""

from __future__ import annotations

import difflib
from pathlib import Path


def unified(old: str, new: str, *, rel_path: str) -> str:
    """Return a unified diff between ``old`` and ``new`` for ``rel_path``.

    Returns an empty string when the content is unchanged. Line endings are
    normalized to ``\\n`` so the diff renders predictably across platforms.
    """
    if old == new:
        return ""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
    )
    return "\n".join(diff)


def diff_for_file(path: Path, new: str, *, rel_path: str) -> str:
    """Unified diff for replacing ``path``'s content with ``new``.

    Treats a missing file as empty (so a create shows as all-additions).
    Reads the current content but writes nothing.
    """
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    return unified(old, new, rel_path=rel_path)
