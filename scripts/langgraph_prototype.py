"""Throwaway prototype: ``WorkflowGraph`` IR → LangGraph ``StateGraph``.

Purpose
-------

The original implementation roadmap flagged **workflow-IR design lock-in**
as the top risk for v1.1: a bad IR makes LangGraph swap-in expensive
months from now. This script is the cheap insurance — it actually compiles
our IR onto LangGraph constructs *today*, proving the seam works (or
surfacing gaps before we commit to the IR shape).

Read alongside [docs/langgraph-seam.md](../docs/langgraph-seam.md), which
records the findings + recommended IR additions for v1.1.

**Throwaway.** This file is not imported by anything in ``src/movate/``,
not exercised by the test suite, and does not ship in any wheel. Delete
it (and add ``movate/core/workflow/compilers/langgraph.py`` instead) when
v1.1 lands.

Running
-------

LangGraph is intentionally NOT a movate-cli runtime dependency yet. To
exercise the prototype, install it in your dev env:

    uv pip install langgraph

Then::

    uv run python scripts/langgraph_prototype.py

If ``langgraph`` is missing, the script falls back to printing the IR
mapping plan as text — useful for the "sketch but don't run" use case.

Scope
-----

The prototype covers four mappings, in order of phase:

1. **Linear (v0.3)** — ``AGENT`` nodes + ``SEQUENTIAL`` edges. Fully
   runnable end-to-end with a mock executor.
2. **Conditional (v1.1)** — ``CONDITIONAL`` edges with a string predicate.
3. **Parallel fan-out (v1.1)** — multiple outbound edges from one node,
   merged with a state reducer.
4. **HITL (v1.1)** — ``HUMAN`` node, ``interrupt_before`` on compile,
   checkpointer for resume.

Each section ends with a ``FINDINGS`` comment summarizing what the IR
needs to gain (if anything) for v1.1's real compiler.
"""

from __future__ import annotations

import operator
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from typing_extensions import TypedDict

# Import our IR. Run from repo root so this resolves.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from movate.core.workflow.ir import (  # noqa: E402
    EdgeKind,
    NodeType,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)

# ---------------------------------------------------------------------------
# LangGraph import guard. The prototype is best-effort; if LangGraph isn't
# installed we print the mapping as a sketch and exit 0 (no failure — this
# is exploratory tooling, not a test).
# ---------------------------------------------------------------------------

try:
    from langgraph.checkpoint.memory import MemorySaver  # type: ignore[import-not-found]
    from langgraph.graph import END, START, StateGraph  # type: ignore[import-not-found]

    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False
    StateGraph = None  # type: ignore[assignment,misc]
    MemorySaver = None  # type: ignore[assignment,misc]
    START = "__start__"  # type: ignore[assignment]
    END = "__end__"  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Mock executor — stands in for ``movate.core.executor.Executor.execute()``.
# In a real compiler each AGENT node would call into Executor with the
# resolved AgentBundle. For the prototype we just stamp the state with the
# node id so we can verify the topology fired the right nodes in the right
# order.
# ---------------------------------------------------------------------------


def make_agent_runner(node_id: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a fn that simulates an AGENT node call.

    Real compiler will:
      * resolve ``node.ref`` (agent dir path) → AgentBundle via the loader
      * project ``state`` onto the node's input schema (already done in our runner)
      * call ``Executor.execute(bundle, projected_input)``
      * shallow-merge the response.data back into state

    Returns a FULL state dict rather than deltas because ``StateGraph(dict)``
    treats the state as opaque and replaces it wholesale on each step —
    only TypedDict states get LangGraph's per-key shallow-merge. The real
    compiler will materialise a TypedDict from ``WorkflowGraph.state_schema``
    so this preservation work happens at the graph layer, not the runner.
    See FINDING under "State merging" in docs/langgraph-seam.md.
    """

    def runner(state: dict[str, Any]) -> dict[str, Any]:
        new_state = dict(state)
        history = list(new_state.get("history", []))
        history.append(node_id)
        new_state["history"] = history
        new_state[f"{node_id}_visited"] = True
        return new_state

    return runner


# ---------------------------------------------------------------------------
# 1. Linear (v0.3) — fully supported by current IR + LangGraph trivially.
# ---------------------------------------------------------------------------


def build_linear_ir() -> WorkflowGraph:
    return WorkflowGraph(
        name="linear-demo",
        version="0.1.0",
        description="A → B → C, all AGENT nodes, all SEQUENTIAL edges.",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="agents/a"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="agents/b"),
            "c": WorkflowNode(id="c", type=NodeType.AGENT, ref="agents/c"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="b"),
            WorkflowEdge(from_id="b", to_id="c"),
        ],
        workflow_dir=Path("/tmp/linear-demo"),
    )


def compile_linear(ir: WorkflowGraph) -> Any:
    """v0.3 mapping. Trivial — every AGENT node becomes a LangGraph node;
    SEQUENTIAL edges become ``add_edge`` calls; source → ``START``; sink → ``END``.
    """
    assert ir.is_linear(), "linear compiler requires a linear graph"
    assert HAS_LANGGRAPH

    graph: Any = StateGraph(dict)
    for nid, node in ir.nodes.items():
        assert node.type is NodeType.AGENT, "v0.3 compiler only handles AGENT nodes"
        graph.add_node(nid, make_agent_runner(nid))

    graph.add_edge(START, ir.entrypoint)
    for edge in ir.edges:
        assert edge.kind is EdgeKind.SEQUENTIAL
        graph.add_edge(edge.from_id, edge.to_id)
    for sink in ir.sinks():
        graph.add_edge(sink, END)

    return graph.compile()


# FINDINGS — linear (v0.3):
#
# * Direct mapping. IR is sufficient as-is. No additions needed.
# * State projection (state → node input schema → state merge) is OUR
#   runner's job today; with LangGraph it'd live in the node fn wrapper
#   instead. Either works.


# ---------------------------------------------------------------------------
# 2. Conditional (v1.1) — IR has `condition: str | None` but doesn't
#    define the syntax. Prototype assumes Python expressions against
#    ``state`` (safe-eval via a restricted namespace).
# ---------------------------------------------------------------------------


def _eval_condition(expr: str, state: dict[str, Any]) -> bool:
    """Toy predicate evaluator. Real impl needs sandboxing (asteval /
    simpleeval) or a custom mini-language (JSONPath-like).

    Reading `expr` here as plain Python — DO NOT ship this. Hand-rolled
    eval against operator-authored YAML is a code-injection vector.
    """
    return bool(eval(expr, {"__builtins__": {}}, {"state": state}))


def build_conditional_ir() -> WorkflowGraph:
    return WorkflowGraph(
        name="conditional-demo",
        version="0.1.0",
        description="A → (B if score > 0.7 else C) → D",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="agents/a"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="agents/b"),
            "c": WorkflowNode(id="c", type=NodeType.AGENT, ref="agents/c"),
            "d": WorkflowNode(id="d", type=NodeType.AGENT, ref="agents/d"),
        },
        edges=[
            WorkflowEdge(
                from_id="a",
                to_id="b",
                kind=EdgeKind.CONDITIONAL,
                condition="state.get('score', 0) > 0.7",
            ),
            WorkflowEdge(
                from_id="a",
                to_id="c",
                kind=EdgeKind.CONDITIONAL,
                condition="state.get('score', 0) <= 0.7",
            ),
            WorkflowEdge(from_id="b", to_id="d"),
            WorkflowEdge(from_id="c", to_id="d"),
        ],
        workflow_dir=Path("/tmp/conditional-demo"),
    )


def compile_conditional(ir: WorkflowGraph) -> Any:
    """v1.1 mapping. CONDITIONAL edges from one source compile to a
    ``add_conditional_edges`` call with a router fn picking the first
    matching branch.
    """
    assert HAS_LANGGRAPH

    graph: Any = StateGraph(dict)
    for nid, _node in ir.nodes.items():
        graph.add_node(nid, make_agent_runner(nid))
    graph.add_edge(START, ir.entrypoint)

    # Group edges by source. For each source with CONDITIONAL outbound,
    # build a router. SEQUENTIAL stays as add_edge.
    for src_id in ir.nodes:
        outbound = ir.successors(src_id)
        if not outbound:
            graph.add_edge(src_id, END)
            continue

        if all(e.kind is EdgeKind.SEQUENTIAL for e in outbound):
            for e in outbound:
                graph.add_edge(e.from_id, e.to_id)
            continue

        if all(e.kind is EdgeKind.CONDITIONAL for e in outbound):
            # Build a router that picks the first edge whose condition fires.
            condition_pairs = [(e.condition or "False", e.to_id) for e in outbound]

            def router(state: dict[str, Any], pairs: Any = condition_pairs) -> str:
                for expr, target in pairs:
                    if _eval_condition(expr, state):
                        return target
                # Fall-through: workflow author should add an explicit else.
                raise RuntimeError(f"no conditional matched for state: {state}")

            mapping = {e.to_id: e.to_id for e in outbound}
            graph.add_conditional_edges(src_id, router, mapping)
            continue

        raise NotImplementedError(f"mixed edge kinds from {src_id} not yet handled")

    return graph.compile()


# FINDINGS — conditional (v1.1):
#
# * `WorkflowEdge.condition: str` is too open-ended. Decision point:
#     (a) safe-eval Python expressions (asteval / simpleeval),
#     (b) restricted DSL (JSONPath-like: `$.score > 0.7`),
#     (c) registered Python callable name + bound args.
#   Recommend (b) — cheapest to validate at compile time, no sandbox-escape
#   surface, matches what users expect from YAML config.
# * Need a way to express the "else" branch deterministically. One option:
#   require the LAST CONDITIONAL edge from a node to have `condition: null`
#   meaning "default". Compiler enforces.
# * IR gap: no field for the router's "no match" semantics. Add
#   `default_target: str | None` to nodes that emit conditional edges,
#   OR enforce the null-condition-as-default convention above.


# ---------------------------------------------------------------------------
# 3. Parallel fan-out (v1.1) — multiple PARALLEL_FAN_OUT edges from one
#    node. LangGraph runs these concurrently. The hard part is the merge:
#    state from parallel branches needs a per-key reducer.
# ---------------------------------------------------------------------------


def build_parallel_ir() -> WorkflowGraph:
    return WorkflowGraph(
        name="parallel-demo",
        version="0.1.0",
        description="A fans out to {B, C, D}, all merge at E.",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="agents/a"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="agents/b"),
            "c": WorkflowNode(id="c", type=NodeType.AGENT, ref="agents/c"),
            "d": WorkflowNode(id="d", type=NodeType.AGENT, ref="agents/d"),
            "e": WorkflowNode(id="e", type=NodeType.AGENT, ref="agents/e"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="b", kind=EdgeKind.PARALLEL_FAN_OUT),
            WorkflowEdge(from_id="a", to_id="c", kind=EdgeKind.PARALLEL_FAN_OUT),
            WorkflowEdge(from_id="a", to_id="d", kind=EdgeKind.PARALLEL_FAN_OUT),
            WorkflowEdge(from_id="b", to_id="e", kind=EdgeKind.PARALLEL_FAN_IN),
            WorkflowEdge(from_id="c", to_id="e", kind=EdgeKind.PARALLEL_FAN_IN),
            WorkflowEdge(from_id="d", to_id="e", kind=EdgeKind.PARALLEL_FAN_IN),
        ],
        workflow_dir=Path("/tmp/parallel-demo"),
    )


def compile_parallel(ir: WorkflowGraph) -> Any:
    """v1.1 mapping. LangGraph runs concurrent siblings natively when
    multiple edges leave the same source. The state schema needs reducers
    on any key written by parallel branches — otherwise LangGraph throws
    InvalidUpdateError ("multiple values for same key without reducer").
    """
    assert HAS_LANGGRAPH

    # `history` is written by every node including parallel branches, so it
    # needs a reducer (operator.add concatenates lists). Real compiler reads
    # the reducer from a movate-specific schema annotation (see findings
    # below). Note: nodes in this graph return **deltas only** (just their
    # own id), not full state — otherwise operator.add would double-count
    # the upstream history. That's a meaningful constraint for the real
    # AGENT runner wrapper at v1.1.
    class ParallelState(TypedDict, total=False):
        history: Annotated[list[str], operator.add]

    def delta_runner(node_id: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def fn(_state: dict[str, Any]) -> dict[str, Any]:
            return {"history": [node_id]}

        return fn

    graph: Any = StateGraph(ParallelState)
    for nid, _node in ir.nodes.items():
        graph.add_node(nid, delta_runner(nid))
    graph.add_edge(START, ir.entrypoint)
    for e in ir.edges:
        graph.add_edge(e.from_id, e.to_id)
    for sink in ir.sinks():
        graph.add_edge(sink, END)

    return graph.compile()


# FINDINGS — parallel (v1.1):
#
# * IR gap: state_schema today is plain JSON Schema. LangGraph requires
#   per-key reducers for fields written by parallel branches. Need to
#   extend the schema with a movate-specific annotation, e.g.::
#
#       state_schema:
#         type: object
#         properties:
#           history:
#             type: array
#             items: {type: string}
#             x-movate-reducer: append   # known reducer names
#
#   Compiler maps `append` → operator.add, `union` → set-like merge, etc.
# * IR gap (smaller): FAN_OUT vs FAN_IN tag on edges is informational;
#   LangGraph derives the concurrency from topology. Worth keeping for
#   error messages ("you intended fan-out here but only one branch exists"),
#   but the compiler ignores it for routing.


# ---------------------------------------------------------------------------
# 4. HITL (v1.1) — HUMAN node. LangGraph supports this via
#    `interrupt_before=[node_name]` on compile + a checkpointer.
# ---------------------------------------------------------------------------


def build_hitl_ir() -> WorkflowGraph:
    return WorkflowGraph(
        name="hitl-demo",
        version="0.1.0",
        description="A (agent) → H (human approval) → B (agent).",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="agents/a"),
            "h": WorkflowNode(
                id="h",
                type=NodeType.HUMAN,
                ref="approval-task",
                metadata={
                    # v1.1 should formalise this: what payload schema does
                    # the human submit on resume?
                    "resume_payload_schema": {
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                    },
                },
            ),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="agents/b"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="h"),
            WorkflowEdge(from_id="h", to_id="b"),
        ],
        workflow_dir=Path("/tmp/hitl-demo"),
    )


def compile_hitl(ir: WorkflowGraph) -> Any:
    """v1.1 mapping. HUMAN nodes don't have a runner — they pause the
    graph. LangGraph's ``interrupt_before`` + checkpointer is the standard
    pattern; the external system resumes via ``invoke(state, config={...})``
    after merging the human's response into state.
    """
    assert HAS_LANGGRAPH

    graph: Any = StateGraph(dict)
    human_node_ids: list[str] = []
    for nid, node in ir.nodes.items():
        if node.type is NodeType.HUMAN:
            # No runner — the node is a placeholder the graph pauses BEFORE.
            # We still need add_node so edges resolve. The "runner" is a
            # passthrough that just echoes state.
            graph.add_node(nid, lambda s: s)
            human_node_ids.append(nid)
        else:
            graph.add_node(nid, make_agent_runner(nid))

    graph.add_edge(START, ir.entrypoint)
    for e in ir.edges:
        graph.add_edge(e.from_id, e.to_id)
    for sink in ir.sinks():
        graph.add_edge(sink, END)

    return graph.compile(
        checkpointer=MemorySaver(),
        interrupt_before=human_node_ids,
    )


# FINDINGS — HITL (v1.1):
#
# * IR gap: HUMAN nodes need a `resume_payload_schema` (what JSON the
#   external system supplies to resume). Today's `metadata: dict[str, Any]`
#   is enough as a stash; recommend formalising as a typed field on
#   `WorkflowNode` for HUMAN type.
# * IR gap: checkpointer choice is a workflow-level concern (memory for
#   tests, postgres for prod). Add `WorkflowGraph.checkpointer_config` or
#   leave it as a runner CLI flag — undecided.
# * Resume API surface (movate side): need a new endpoint, e.g.
#   ``POST /workflows/{run_id}/resume`` with the human's payload. Outside
#   the IR but on the v1.1 PRD's critical path.


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    if not HAS_LANGGRAPH:
        print("langgraph not installed. To run the prototype:")
        print()
        print("    uv pip install langgraph")
        print()
        print("This script otherwise documents the IR→LangGraph mapping in")
        print("comments + docs/langgraph-seam.md. Returning 0 (informational).")
        return 0

    print("=" * 72)
    print("1. Linear (v0.3) — runnable on current IR")
    print("=" * 72)
    linear = compile_linear(build_linear_ir())
    result = linear.invoke({"history": []})
    print(f"  result: history={result['history']}")
    assert result["history"] == ["a", "b", "c"]
    print("  PASS: linear A→B→C executed in topological order")
    print()

    print("=" * 72)
    print("2. Conditional (v1.1) — runs but needs IR additions for production")
    print("=" * 72)
    conditional = compile_conditional(build_conditional_ir())

    high = conditional.invoke({"history": [], "score": 0.9})
    print(f"  score=0.9 → history={high['history']}")
    assert high["history"] == ["a", "b", "d"], high["history"]

    low = conditional.invoke({"history": [], "score": 0.3})
    print(f"  score=0.3 → history={low['history']}")
    assert low["history"] == ["a", "c", "d"], low["history"]
    print("  PASS: routing picked the right branch in each case")
    print()

    print("=" * 72)
    print("3. Parallel (v1.1) — requires state-schema reducer annotation")
    print("=" * 72)
    parallel = compile_parallel(build_parallel_ir())
    pr = parallel.invoke({"history": []})
    print(f"  result: history={pr['history']}")
    assert set(pr["history"]) == {"a", "b", "c", "d", "e"}, pr["history"]
    assert pr["history"][0] == "a" and pr["history"][-1] == "e"
    print("  PASS: a ran first, e ran last, b/c/d interleaved")
    print()

    print("=" * 72)
    print("4. HITL (v1.1) — pauses at HUMAN node")
    print("=" * 72)
    hitl = compile_hitl(build_hitl_ir())
    config = {"configurable": {"thread_id": "demo"}}
    pre = hitl.invoke({"history": []}, config=config)
    print(f"  paused at HUMAN. history so far: {pre['history']}")
    assert pre["history"] == ["a"], pre["history"]

    # Merge human's payload into checkpointed state, then resume by
    # invoking with `None` — that tells LangGraph to continue from the
    # checkpoint without supplying new input. Passing a dict to invoke()
    # on a dict-typed StateGraph would *replace* state, blowing away the
    # checkpointed history.
    hitl.update_state(config, {"approved": True})
    post = hitl.invoke(None, config=config)
    print(f"  resumed → history={post['history']}")
    assert "b" in post["history"], post["history"]
    assert post.get("approved") is True
    print("  PASS: graph paused before HUMAN, resumed cleanly")
    print()

    print("=" * 72)
    print("All four seams validated.")
    print("See docs/langgraph-seam.md for the recommended IR additions.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
