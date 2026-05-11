"""WorkflowSpec → WorkflowGraph compiler + v0.3 linear-only validator.

Two passes:

1. :func:`compile_workflow` — pure structural compile. Builds the IR,
   resolves agent ``ref`` paths to absolute, loads + validates the
   ``state_schema``, checks that the entrypoint and edge endpoints exist,
   and detects cycles. Output is a syntactically valid :class:`WorkflowGraph`.
2. :func:`validate_linear` — semantic gate for v0.3. Rejects branches,
   joins, conditional edges, and non-agent node types. Lives in its own
   function so v1.1 can substitute richer validators without touching
   the compiler.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from movate.core.workflow.ir import (
    EdgeKind,
    NodeType,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)
from movate.core.workflow.spec import WorkflowSpec


class WorkflowCompileError(Exception):
    """Raised when a WorkflowSpec is structurally invalid (bad refs, missing
    entrypoint, cycles, dangling edges, …) or fails a validator pass.
    """


# ---------------------------------------------------------------------------
# Pass 1 — compile
# ---------------------------------------------------------------------------


def compile_workflow(spec: WorkflowSpec, workflow_dir: Path) -> WorkflowGraph:
    """Build a :class:`WorkflowGraph` from a parsed spec.

    Performs structural checks that always apply (regardless of phase):
    duplicate node ids, unknown edge endpoints, unknown entrypoint, missing
    state schema file, malformed state schema, cycles. Raises
    :class:`WorkflowCompileError` on failure.

    Does *not* enforce phase-specific shape constraints; pair with
    :func:`validate_linear` (v0.3) or future ``validate_dag`` for that.
    """
    workflow_dir = workflow_dir.resolve()

    # 1. Nodes — duplicate id check + ref resolution.
    nodes: dict[str, WorkflowNode] = {}
    for ns in spec.nodes:
        if ns.id in nodes:
            raise WorkflowCompileError(f"duplicate node id: {ns.id!r}")
        resolved_ref = (workflow_dir / ns.ref).resolve()
        # We don't load the agent here — that's the runner's job. But we
        # at least make sure the path exists so a typo in workflow.yaml
        # fails loud at compile time.
        if not resolved_ref.exists():
            raise WorkflowCompileError(f"node {ns.id!r}: ref path does not exist: {resolved_ref}")
        nodes[ns.id] = WorkflowNode(
            id=ns.id,
            type=NodeType(ns.type),
            ref=str(resolved_ref),
        )

    # 2. Entrypoint must exist.
    if spec.entrypoint not in nodes:
        raise WorkflowCompileError(
            f"entrypoint {spec.entrypoint!r} not in nodes (available: {', '.join(sorted(nodes))})"
        )

    # 3. Edges — endpoints must exist + no self-loops in v0.3.
    edges: list[WorkflowEdge] = []
    for es in spec.edges:
        if es.from_id not in nodes:
            raise WorkflowCompileError(
                f"edge from {es.from_id!r} → {es.to_id!r}: source node missing"
            )
        if es.to_id not in nodes:
            raise WorkflowCompileError(
                f"edge from {es.from_id!r} → {es.to_id!r}: target node missing"
            )
        if es.from_id == es.to_id:
            raise WorkflowCompileError(f"self-loop on node {es.from_id!r} not allowed")
        edges.append(
            WorkflowEdge(
                from_id=es.from_id,
                to_id=es.to_id,
                kind=EdgeKind.SEQUENTIAL,
            )
        )

    # 4. State schema — load + validate.
    schema_path = (workflow_dir / spec.state_schema).resolve()
    if not schema_path.exists():
        raise WorkflowCompileError(f"state_schema not found: {schema_path}")
    try:
        state_schema: Any = json.loads(schema_path.read_text())
    except json.JSONDecodeError as exc:
        raise WorkflowCompileError(f"invalid JSON in state_schema {schema_path}: {exc}") from exc
    if not isinstance(state_schema, dict):
        raise WorkflowCompileError(f"state_schema {schema_path} must be a JSON object")
    try:
        Draft202012Validator.check_schema(state_schema)
    except Exception as exc:
        raise WorkflowCompileError(f"invalid state_schema: {exc}") from exc

    graph = WorkflowGraph(
        name=spec.name,
        version=spec.version,
        description=spec.description,
        state_schema=state_schema,
        entrypoint=spec.entrypoint,
        nodes=nodes,
        edges=edges,
        workflow_dir=workflow_dir,
        runtime=spec.runtime.value,
    )

    # 5. Cycle detection — fail fast at compile time.
    try:
        graph.topological_order()
    except ValueError as exc:
        raise WorkflowCompileError(str(exc)) from exc

    # 6. Reachability — every non-entrypoint node must be reachable from entrypoint.
    reachable = _reachable(graph, graph.entrypoint)
    orphans = sorted(set(nodes) - reachable)
    if orphans:
        raise WorkflowCompileError(
            f"unreachable from entrypoint {graph.entrypoint!r}: {', '.join(orphans)}"
        )

    return graph


def _reachable(graph: WorkflowGraph, start: str) -> set[str]:
    seen: set[str] = {start}
    stack = [start]
    while stack:
        nid = stack.pop()
        for edge in graph.successors(nid):
            if edge.to_id not in seen:
                seen.add(edge.to_id)
                stack.append(edge.to_id)
    return seen


# ---------------------------------------------------------------------------
# Pass 2 — v0.3 phase gate
# ---------------------------------------------------------------------------


def validate_linear(graph: WorkflowGraph) -> None:
    """Reject anything more permissive than a strict linear chain.

    v0.3 only ships linear pipelines. This is the firewall: branches,
    joins, conditional edges, parallel fan-out/in, and non-agent node
    types all fail here with a phase-aware message pointing the user at
    when the feature is expected to land.

    Replaceable: v0.4+ phases can call a different validator (or none)
    against the same :class:`WorkflowGraph` without modifying the IR or
    the structural compiler.
    """
    # Node types — agent only. Most specific user-facing failure first.
    bad_types = sorted(n.id for n in graph.nodes.values() if n.type is not NodeType.AGENT)
    if bad_types:
        raise WorkflowCompileError(
            f"v0.3 supports only type=agent nodes; offenders: {', '.join(bad_types)}. "
            f"Tools/HITL/sub-workflows land in v1.1+."
        )

    # Edge kinds — sequential only.
    bad_edges = [e for e in graph.edges if e.kind is not EdgeKind.SEQUENTIAL]
    if bad_edges:
        raise WorkflowCompileError(
            f"v0.3 supports only sequential edges; got {len(bad_edges)} non-sequential. "
            f"Conditional / parallel edges land in v1.1+."
        )

    # Branching / joining — checked before source/sink count so the user gets
    # the most pointed message (a branch implicitly creates >1 sink; we'd rather
    # say "no branches" than "exactly one sink").
    branching = sorted(nid for nid in graph.nodes if len(graph.successors(nid)) > 1)
    if branching:
        raise WorkflowCompileError(
            f"v0.3 forbids branches (>1 successor); offenders: {', '.join(branching)}. "
            f"Parallel fan-out lands in v1.1+."
        )
    joining = sorted(nid for nid in graph.nodes if len(graph.predecessors(nid)) > 1)
    if joining:
        raise WorkflowCompileError(
            f"v0.3 forbids joins (>1 predecessor); offenders: {', '.join(joining)}. "
            f"Parallel fan-in lands in v1.1+."
        )

    # Source — exactly one, must be the entrypoint. (Only reachable now if the
    # graph has zero edges or two truly disconnected single-node sub-graphs.)
    sources = graph.sources()
    if len(sources) != 1:
        raise WorkflowCompileError(
            f"v0.3 workflows must have exactly one source node; got {len(sources)}: "
            f"{', '.join(sources) or '(none)'}"
        )
    if sources[0] != graph.entrypoint:
        raise WorkflowCompileError(
            f"the source node {sources[0]!r} must be the declared entrypoint {graph.entrypoint!r}"
        )

    # Sink — exactly one.
    sinks = graph.sinks()
    if len(sinks) != 1:
        raise WorkflowCompileError(
            f"v0.3 workflows must have exactly one sink node; got {len(sinks)}: "
            f"{', '.join(sinks) or '(none)'}"
        )
