"""Filesystem registries — scan paths for ``agent.yaml`` / ``workflow.yaml``.

The agent registry feeds ``GET /agents`` (advertises what's runnable)
and the worker dispatch (resolves a ``JobRecord.target`` to a runnable
``AgentBundle``). The workflow registry similarly feeds the worker for
``JobKind.WORKFLOW`` jobs.

Both scans happen **once** at app/worker build time so request
handling and job claims are constant-time lookups, not fresh disk
walks.

Robustness invariant: a single broken yaml MUST NOT prevent startup.
Log a warning and skip — the operator sees what was rejected, the
rest of the catalog still loads.
"""

from __future__ import annotations

import logging
from pathlib import Path

from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowGraph,
    compile_workflow,
    load_workflow_spec,
    validate_for_runtime,
)
from movate.core.workflow.spec import WorkflowSpecLoadError

logger = logging.getLogger(__name__)


def scan_agents(root: Path) -> list[AgentBundle]:
    """Walk ``root`` for directories containing an ``agent.yaml``.

    Returns the list of successfully-loaded :class:`AgentBundle`s,
    sorted by spec name for stable ordering. Missing or non-directory
    ``root`` returns an empty list (operator running ``movate serve``
    without any agents on disk shouldn't crash — they just have an
    empty catalog).

    Walks **only one level deep** by design: agent layouts are flat
    (``agents/<name>/agent.yaml``). Recursing arbitrarily would pick
    up nested test fixtures and dev scratch dirs.
    """
    if not root.exists() or not root.is_dir():
        logger.info("agents_root_missing path=%s", root)
        return []

    bundles: list[AgentBundle] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "agent.yaml").exists():
            # Not every subdirectory is an agent — skip silently.
            # Could be a `.git`, an `evals/` shared dataset, etc.
            continue
        try:
            bundle = load_agent(entry)
        except AgentLoadError as exc:
            # One bad agent.yaml shouldn't blackhole the catalog.
            logger.warning("agent_load_skipped path=%s reason=%s", entry, exc)
            continue
        bundles.append(bundle)

    bundles.sort(key=lambda b: b.spec.name)
    return bundles


def scan_workflows(root: Path) -> dict[str, WorkflowGraph]:
    """Walk ``root`` for directories containing a ``workflow.yaml``.

    Returns a name → :class:`WorkflowGraph` mapping. Workers index by
    name to dispatch ``JobKind.WORKFLOW`` jobs.

    Same robustness invariant as :func:`scan_agents`: invalid
    workflow definitions (broken YAML, compile errors, branched
    graphs that fail :func:`validate_linear` in v0.3) are skipped
    with a warning rather than crashing. The valid catalog still
    loads.

    Walks **only one level deep** — same convention as
    :func:`scan_agents`. v0.5 ships only linear workflows; the
    compiler will reject non-linear shapes so they never appear in
    the registry, even if their YAML loads.
    """
    if not root.exists() or not root.is_dir():
        logger.info("workflows_root_missing path=%s", root)
        return {}

    graphs: dict[str, WorkflowGraph] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "workflow.yaml").exists():
            continue
        try:
            spec, parent = load_workflow_spec(entry)
            graph = compile_workflow(spec, parent)
            validate_for_runtime(graph)
        except (WorkflowSpecLoadError, WorkflowCompileError) as exc:
            logger.warning("workflow_load_skipped path=%s reason=%s", entry, exc)
            continue
        if spec.name in graphs:
            logger.warning(
                "workflow_duplicate_name name=%s path=%s (keeping first)",
                spec.name,
                entry,
            )
            continue
        graphs[spec.name] = graph

    return graphs


__all__ = ["scan_agents", "scan_workflows"]
