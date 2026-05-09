"""Detect whether a path points to an agent or a workflow.

Single source of truth so ``movate run``, ``movate validate``, and
``movate show`` dispatch consistently. Agents have ``agent.yaml``;
workflows have ``workflow.yaml``. Both can be passed as the directory or
as the YAML file itself.
"""

from __future__ import annotations

from pathlib import Path


def is_workflow_path(path: Path) -> bool:
    """True iff ``path`` is a workflow directory (or its ``workflow.yaml`` file).

    A path with both ``workflow.yaml`` and ``agent.yaml`` is treated as a
    workflow — ``workflow.yaml`` wins. (We don't expect that combination
    in practice; calling it out here so the precedence is explicit.)
    """
    p = Path(path)
    if p.is_file():
        return p.name == "workflow.yaml"
    return p.is_dir() and (p / "workflow.yaml").exists()
