"""Internal IR: ``WorkflowGraph`` + nodes + edges + topology helpers.

The IR is intentionally richer than ``workflow.yaml`` exposes today. The
``NodeType`` and ``EdgeKind`` enums include variants that v0.3's compiler
will refuse to emit; they exist so v1.1's LangGraph compiler (and future
HITL / parallel / sub-workflow features) can reuse the same data structure
without a schema break.

The runner walks ``WorkflowGraph`` directly. Validators are layered on top
(see :mod:`movate.core.workflow.compiler`): the IR itself doesn't know
about "linear v0.3" or "DAG v0.4 with conditionals".
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class NodeType(StrEnum):
    AGENT = "agent"  # v0.3
    INTENT_ROUTER = "intent-router"  # v0.4 — classifier-driven intent branching
    JUDGE = "judge"  # v1.x (ADR 056) — runs a judge, gates on its verdict/score
    SUPERVISOR = "supervisor"  # ADR 092 D4 — bounded managerial delegation
    # Future variants — declared so the IR doesn't need a breaking change
    # when these phases ship. Compiler validators reject them today.
    TOOL = "tool"  # v1.1 — registered tool/function call
    HUMAN = "human"  # v1.1 — HITL, runner pauses + persists state
    FUNCTION = "function"  # v1.1 — inline Python callable
    SUB_WORKFLOW = "sub_workflow"  # v1.2 — nested WorkflowGraph by ref


class EdgeKind(StrEnum):
    SEQUENTIAL = "sequential"  # v0.3 — unconditional A→B
    CONDITIONAL = "conditional"  # v1.1 — fires only when `condition` evaluates truthy
    PARALLEL_FAN_OUT = "fan_out"  # v1.1 — concurrent siblings
    PARALLEL_FAN_IN = "fan_in"  # v1.1 — merge into one downstream node


@dataclass
class WorkflowNode:
    id: str
    type: NodeType
    ref: str
    """Reference resolved by the runner.

    For ``type == AGENT`` this is an absolute path to an agent directory.
    Other node types will reuse this field with their own resolution rules
    (e.g. tool registry key, human-task spec path).
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    """Compiler/runner annotations. v0.3 leaves this empty; later phases use
    it for LangGraph routing hints, retry overrides, etc."""


@dataclass
class WorkflowEdge:
    from_id: str
    to_id: str
    kind: EdgeKind = EdgeKind.SEQUENTIAL
    condition: str | None = None
    """For ``kind == CONDITIONAL``, an expression evaluated against the
    workflow state at runtime. Format reserved (likely a small subset of
    JSONPath); not parsed in v0.3 because v0.3 forbids conditional edges.
    """

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowGraph:
    """Compiled, topologically-sorted workflow definition.

    The graph is immutable from the runner's perspective; mutating after
    construction is undefined. Use the helper methods rather than walking
    ``edges`` directly.
    """

    name: str
    version: str
    description: str
    state_schema: dict[str, Any]
    """Parsed JSON Schema for the workflow state object. Validated at compile
    time; runner uses it to gate ``initial_state`` on every run."""

    entrypoint: str
    nodes: dict[str, WorkflowNode]
    edges: list[WorkflowEdge]
    workflow_dir: Path
    """Directory containing the source ``workflow.yaml``. Used by the runner
    to resolve any relative paths the compiler couldn't pre-resolve."""

    runtime: str = "auto"
    """The declared execution backend (ADR 055 D1, ADR 091), surfaced read-only
    from ``WorkflowSpec.runtime`` by the compiler. One of ``"auto"`` (default —
    Temporal when available + compilable, else native; resolved by
    ``resolve_effective_runtime``), ``"native"`` (the in-process
    :class:`~movate.core.workflow.runner.WorkflowRunner`), ``"temporal"``
    (ADR 054), or ``"langgraph"`` (ADR 030). The dispatch fork
    (and ``mdk show``) read this without re-parsing ``workflow.yaml``. A
    ``--runtime`` CLI override never mutates this — it is the workflow's
    *declared* home; the override is applied at the selection point only."""

    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ topology

    def successors(self, node_id: str) -> list[WorkflowEdge]:
        return [e for e in self.edges if e.from_id == node_id]

    def predecessors(self, node_id: str) -> list[WorkflowEdge]:
        return [e for e in self.edges if e.to_id == node_id]

    def sources(self) -> list[str]:
        """Node IDs with no inbound edges."""
        with_inbound = {e.to_id for e in self.edges}
        return [nid for nid in self.nodes if nid not in with_inbound]

    def sinks(self) -> list[str]:
        """Node IDs with no outbound edges."""
        with_outbound = {e.from_id for e in self.edges}
        return [nid for nid in self.nodes if nid not in with_outbound]

    def is_linear(self) -> bool:
        """True iff the graph is a single chain.

        Conditions:

        * exactly one source and exactly one sink
        * every node has at most one successor and at most one predecessor
        * no edges have a non-default ``kind``
        """
        if len(self.sources()) != 1 or len(self.sinks()) != 1:
            return False
        for nid in self.nodes:
            if len(self.successors(nid)) > 1 or len(self.predecessors(nid)) > 1:
                return False
        return all(e.kind is EdgeKind.SEQUENTIAL for e in self.edges)

    def topological_order(self) -> list[str]:
        """Kahn's-algorithm topological sort. Raises if the graph has a cycle.

        Stable for linear graphs (preserves the source-to-sink chain).
        """
        in_degree: dict[str, int] = defaultdict(int)
        for e in self.edges:
            in_degree[e.to_id] += 1

        queue: deque[str] = deque(nid for nid in self.nodes if in_degree[nid] == 0)
        order: list[str] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for edge in self.successors(nid):
                in_degree[edge.to_id] -= 1
                if in_degree[edge.to_id] == 0:
                    queue.append(edge.to_id)

        if len(order) != len(self.nodes):
            raise ValueError(f"graph has a cycle: {len(self.nodes) - len(order)} nodes unreachable")
        return order

    def find_back_edges(self) -> list[WorkflowEdge]:
        """Return the edges whose removal would make the graph acyclic.

        Cycle detection via DFS: an edge to a node currently on the DFS
        stack (a "grey" node) is a back-edge — it closes a loop. The set of
        back-edges is the minimal set the LangGraph compiler must treat as
        loop-back transitions (each needs a recursion guard so it can never
        run away — ADR 030 D2 / CLAUDE.md failure-mode rule).

        Deterministic: nodes are visited in insertion order and successors
        in declaration order, so the same graph always yields the same
        back-edge set. Pure read-only; does not mutate the graph.
        """
        white, grey, black = 0, 1, 2
        color: dict[str, int] = {nid: white for nid in self.nodes}
        back_edges: list[WorkflowEdge] = []

        # Iterative DFS so deep graphs don't blow the Python stack. We push
        # (node, successor-index) frames and resume where we left off.
        for root in self.nodes:
            if color[root] != white:
                continue
            stack: list[tuple[str, int]] = [(root, 0)]
            color[root] = grey
            while stack:
                nid, idx = stack[-1]
                succ = self.successors(nid)
                if idx < len(succ):
                    stack[-1] = (nid, idx + 1)
                    edge = succ[idx]
                    target = edge.to_id
                    if target not in color:  # dangling edge — ignore here
                        continue
                    if color[target] == grey:
                        back_edges.append(edge)
                    elif color[target] == white:
                        color[target] = grey
                        stack.append((target, 0))
                else:
                    color[nid] = black
                    stack.pop()
        return back_edges

    def has_cycle(self) -> bool:
        """True iff the graph contains at least one cycle."""
        return bool(self.find_back_edges())
