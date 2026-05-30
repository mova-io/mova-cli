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
from movate.core.workflow.spec import (
    AgentNodeSpec,
    HumanNodeSpec,
    IntentRouterNodeSpec,
    JudgeNodeSpec,
    WorkflowSpec,
)


class WorkflowCompileError(Exception):
    """Raised when a WorkflowSpec is structurally invalid (bad refs, missing
    entrypoint, cycles, dangling edges, …) or fails a validator pass.
    """


# ---------------------------------------------------------------------------
# Pass 1 — compile
# ---------------------------------------------------------------------------


def compile_workflow(
    spec: WorkflowSpec,
    workflow_dir: Path,
    *,
    allow_cycles: bool = False,
) -> WorkflowGraph:
    """Build a :class:`WorkflowGraph` from a parsed spec.

    Performs structural checks that always apply (regardless of phase):
    duplicate node ids, unknown edge endpoints, unknown entrypoint, missing
    state schema file, malformed state schema, cycles. Raises
    :class:`WorkflowCompileError` on failure.

    Does *not* enforce phase-specific shape constraints; pair with
    :func:`validate_linear` (v0.3) or future ``validate_dag`` for that.

    ``allow_cycles`` (ADR 030 D2, default ``False`` — preserves the native
    runner contract): when ``True`` the acyclic requirement is relaxed so a
    workflow with intentional loops (ReAct / reflection / retry-until)
    compiles into the IR for the LangGraph *export* path. The LangGraph
    compiler emits those loops with a mandatory recursion guard. The native
    runner never sets this — it walks a DAG only.
    """
    workflow_dir = workflow_dir.resolve()

    # 1. Nodes — duplicate id check + ref resolution.
    nodes: dict[str, WorkflowNode] = {}
    for ns in spec.nodes:
        if ns.id in nodes:
            raise WorkflowCompileError(f"duplicate node id: {ns.id!r}")
        if isinstance(ns, AgentNodeSpec):
            resolved_ref = (workflow_dir / ns.ref).resolve()
            # We don't load the agent here — that's the runner's job. But we
            # at least make sure the path exists so a typo in workflow.yaml
            # fails loud at compile time.
            if not resolved_ref.exists():
                raise WorkflowCompileError(
                    f"node {ns.id!r}: ref path does not exist: {resolved_ref}"
                )
            nodes[ns.id] = WorkflowNode(
                id=ns.id,
                type=NodeType.AGENT,
                ref=str(resolved_ref),
            )
        elif isinstance(ns, IntentRouterNodeSpec):
            # intent-router nodes don't have a file-system ref — they carry
            # their config in ``metadata`` so the runner can dispatch them.
            nodes[ns.id] = WorkflowNode(
                id=ns.id,
                type=NodeType.INTENT_ROUTER,
                ref="",  # unused for intent-router
                metadata={
                    "routes": ns.routes,
                    "fallback": ns.fallback,
                    "classifier_agent": ns.classifier_agent,
                    "input_field": ns.input_field,
                },
            )
        elif isinstance(ns, HumanNodeSpec):
            # HUMAN gate (ADR 017 D5). No file-system ref — the node carries
            # its human-task spec in ``metadata``. The runner does NOT execute
            # it: it pauses + persists a checkpoint there (PR 1) and PR 2
            # resumes on an external signal. Validate the spec here so a
            # malformed gate fails loud at compile time, mirroring how
            # agent/intent-router config is checked.
            prompt = ns.prompt.strip()
            if not prompt:
                raise WorkflowCompileError(
                    f"human node {ns.id!r}: 'prompt' must be a non-empty string"
                )
            if not all(isinstance(k, str) and k for k in ns.output_contract):
                raise WorkflowCompileError(
                    f"human node {ns.id!r}: 'output_contract' must be a list of "
                    f"non-empty state-key strings"
                )
            nodes[ns.id] = WorkflowNode(
                id=ns.id,
                type=NodeType.HUMAN,
                ref="",  # unused for human gates
                metadata={
                    "prompt": prompt,
                    "output_contract": list(ns.output_contract),
                },
            )
        elif isinstance(ns, JudgeNodeSpec):
            # JUDGE node (ADR 056 D1). When a ``judge_agent`` ref is supplied
            # it resolves to an absolute path (like an agent ref) so a typo
            # fails loud at compile time; the inline-``criteria`` form carries
            # no file-system ref. Routing + threshold live in metadata so the
            # native runner (D3) and the Temporal activity (D5) read the SAME
            # shape. Route-target validation happens below (step 3b).
            judge_ref = ""
            if ns.judge_agent and ns.judge_agent.strip():
                resolved_judge = (workflow_dir / ns.judge_agent).resolve()
                if not resolved_judge.exists():
                    raise WorkflowCompileError(
                        f"judge node {ns.id!r}: judge_agent ref path does not exist: "
                        f"{resolved_judge}"
                    )
                judge_ref = str(resolved_judge)
            nodes[ns.id] = WorkflowNode(
                id=ns.id,
                type=NodeType.JUDGE,
                ref=judge_ref,  # absolute judge-agent path, or "" for inline criteria
                metadata={
                    "criteria": ns.criteria or "",
                    "input_field": ns.input_field,
                    "pass_threshold": ns.pass_threshold,
                    "on_accept": ns.on_accept,
                    "on_revise": ns.on_revise,
                    "max_iterations": ns.max_iterations,
                },
            )
        else:
            raise WorkflowCompileError(f"node {ns.id!r}: unknown node type {ns.type!r}")

    # 2. Entrypoint must exist.
    if spec.entrypoint not in nodes:
        raise WorkflowCompileError(
            f"entrypoint {spec.entrypoint!r} not in nodes (available: {', '.join(sorted(nodes))})"
        )

    # 3. Validate intent-router route targets. All route values + fallback must
    # name valid node ids so we can catch typos at compile time rather than
    # at run time.
    for ns in spec.nodes:
        if not isinstance(ns, IntentRouterNodeSpec):
            continue
        all_targets = [*ns.routes.values(), ns.fallback]
        for target in all_targets:
            if target not in nodes:
                raise WorkflowCompileError(
                    f"intent-router node {ns.id!r}: route target {target!r} "
                    f"is not a valid node id (known: {', '.join(sorted(nodes))})"
                )

    # 3b. Validate JUDGE routing targets (ADR 056 D1). ``on_accept`` /
    # ``on_revise``, when set, must name valid node ids — caught here, not at
    # run time. Unset routing legs fall through to the sequential successor
    # (the eval-gate's default-continue / the reflection loop's back-edge).
    for ns in spec.nodes:
        if not isinstance(ns, JudgeNodeSpec):
            continue
        judge_legs: list[tuple[str, str | None]] = [
            ("on_accept", ns.on_accept),
            ("on_revise", ns.on_revise),
        ]
        for leg, leg_target in judge_legs:
            if leg_target is not None and leg_target not in nodes:
                raise WorkflowCompileError(
                    f"judge node {ns.id!r}: {leg} target {leg_target!r} is not a valid "
                    f"node id (known: {', '.join(sorted(nodes))})"
                )

    # 4. Edges — explicit edges must exist + no self-loops; then inject synthetic
    # CONDITIONAL edges from each intent-router to its route targets so that the
    # IR graph correctly models reachability and topological order.
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
        # A self-loop is only a legitimate construct on the cycle-tolerant
        # export path (e.g. a single-node ReAct loop). The native runner walks
        # a DAG, so it stays forbidden there.
        if es.from_id == es.to_id and not allow_cycles:
            raise WorkflowCompileError(f"self-loop on node {es.from_id!r} not allowed")
        # Lower the spec edge kind (ADR 030 D2) into the IR's EdgeKind. Old
        # bare {from,to} edges resolve to SEQUENTIAL → identical IR to before.
        try:
            edge_kind = EdgeKind(es.resolved_kind)
        except ValueError:  # pragma: no cover — guarded by the spec Literal
            raise WorkflowCompileError(
                f"edge {es.from_id!r}→{es.to_id!r}: unknown kind {es.resolved_kind!r}"
            ) from None
        edges.append(
            WorkflowEdge(
                from_id=es.from_id,
                to_id=es.to_id,
                kind=edge_kind,
                condition=es.when,
            )
        )

    # Inject synthetic edges for intent-router route targets so the graph
    # correctly reflects reachability. We use CONDITIONAL kind to distinguish
    # them from user-declared sequential edges (validate_linear skips routers).
    seen_router_edges: set[tuple[str, str]] = set()
    for ns in spec.nodes:
        if not isinstance(ns, IntentRouterNodeSpec):
            continue
        all_targets = [*ns.routes.values(), ns.fallback]
        for target in all_targets:
            pair = (ns.id, target)
            if pair in seen_router_edges:
                continue
            seen_router_edges.add(pair)
            edges.append(
                WorkflowEdge(
                    from_id=ns.id,
                    to_id=target,
                    kind=EdgeKind.CONDITIONAL,
                    metadata={"synthetic": True, "source": "intent-router"},
                )
            )

    # Inject synthetic edges for JUDGE branch targets (ADR 056 D1) so the
    # graph reflects reachability. Like intent-router targets these are
    # CONDITIONAL+synthetic (exempt from validate_linear's sequential check)
    # and the native runner skips them when computing the sequential
    # successor (so an unset routing leg still falls through correctly).
    seen_judge_edges: set[tuple[str, str]] = set()
    for ns in spec.nodes:
        if not isinstance(ns, JudgeNodeSpec):
            continue
        for branch_target in (ns.on_accept, ns.on_revise):
            if branch_target is None:
                continue
            pair = (ns.id, branch_target)
            if pair in seen_judge_edges:
                continue
            seen_judge_edges.add(pair)
            edges.append(
                WorkflowEdge(
                    from_id=ns.id,
                    to_id=branch_target,
                    kind=EdgeKind.CONDITIONAL,
                    metadata={"synthetic": True, "source": "judge"},
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
        # ADR 055 D1 — surface the declared backend read-only on the IR so the
        # dispatch fork + `mdk show` see it without re-parsing workflow.yaml.
        # Default "native" preserves every existing workflow's behavior.
        runtime=spec.runtime,
    )

    # 5. Cycle detection — fail fast at compile time, UNLESS the caller opted
    # into the cycle-tolerant export path (ADR 030 D2). The native runner
    # always rejects cycles (it walks a DAG); the LangGraph export compiler
    # turns back-edges into recursion-guarded loops.
    if not allow_cycles:
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

    Exception: ``intent-router`` nodes (``NodeType.INTENT_ROUTER``) are
    explicitly permitted — they are the branching primitive for v0.4.
    Synthetic CONDITIONAL edges injected by the compiler for intent-router
    route targets are also exempt from the sequential-only edge check.

    ADR 017 D5 (PR 1): ``human`` (HITL gate) nodes (``NodeType.HUMAN``) are
    now also permitted — the runner pauses + persists a durable checkpoint
    at a human gate rather than executing it. TOOL / FUNCTION / sub-workflow
    node types remain rejected (they land in later phases).

    ADR 056: ``judge`` nodes (``NodeType.JUDGE``) are permitted — like
    ``intent-router`` they are a verdict-driven branching primitive, so they
    may branch (``on_accept``/``on_revise``) and their workflows may have
    multiple sinks. The bounded *reflection* loop (a JUDGE on a back-edge) is
    cyclic and therefore not a linear-phase workflow; it compiles with
    ``allow_cycles=True`` (the export/cycle-tolerant path) — this validator
    only governs the acyclic eval-gate/branch form.

    Replaceable: v0.4+ phases can call a different validator (or none)
    against the same :class:`WorkflowGraph` without modifying the IR or
    the structural compiler.
    """
    # Node types — agent + intent-router + human (HITL gate) + judge. Tools/
    # functions/sub-workflows are still rejected. Most specific failure first.
    _allowed_types = {
        NodeType.AGENT,
        NodeType.INTENT_ROUTER,
        NodeType.HUMAN,
        NodeType.JUDGE,
    }
    bad_types = sorted(n.id for n in graph.nodes.values() if n.type not in _allowed_types)
    if bad_types:
        raise WorkflowCompileError(
            f"v0.3 supports only type=agent, type=intent-router, type=human, and "
            f"type=judge nodes; offenders: {', '.join(bad_types)}. "
            f"Tools/sub-workflows land in v1.1+."
        )

    # Edge kinds — sequential only, EXCEPT synthetic conditional edges from
    # intent-router nodes (those are injected by compile_workflow).
    bad_edges = [
        e
        for e in graph.edges
        if e.kind is not EdgeKind.SEQUENTIAL
        and not (e.kind is EdgeKind.CONDITIONAL and e.metadata.get("synthetic"))
    ]
    if bad_edges:
        raise WorkflowCompileError(
            f"v0.3 supports only sequential edges; got {len(bad_edges)} non-sequential. "
            f"Conditional / parallel edges land in v1.1+."
        )

    # Branching / joining — intent-router and judge nodes are allowed to branch
    # (that is their whole purpose). We only flag plain agent nodes that branch.
    _branch_types = {NodeType.INTENT_ROUTER, NodeType.JUDGE}
    branching = sorted(
        nid
        for nid in graph.nodes
        if len(graph.successors(nid)) > 1 and graph.nodes[nid].type not in _branch_types
    )
    if branching:
        raise WorkflowCompileError(
            f"v0.3 forbids branches (>1 successor) on non-router nodes; "
            f"offenders: {', '.join(branching)}. "
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

    # Sink — for linear workflows exactly one; intent-router / judge workflows
    # may have multiple sinks (each branch target that has no successor). We
    # only enforce single-sink on pure-linear (no router / no judge) workflows.
    router_nodes = {
        nid for nid, n in graph.nodes.items() if n.type in {NodeType.INTENT_ROUTER, NodeType.JUDGE}
    }
    if not router_nodes:
        sinks = graph.sinks()
        if len(sinks) != 1:
            raise WorkflowCompileError(
                f"v0.3 workflows must have exactly one sink node; got {len(sinks)}: "
                f"{', '.join(sinks) or '(none)'}"
            )
