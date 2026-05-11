"""Compile :class:`WorkflowGraph` onto a LangGraph ``StateGraph``.

v1.0 of the compiler: **linear AGENT workflows only.** Mirrors the
contract of :class:`movate.core.workflow.runner.WorkflowRunner` — same
``WorkflowResult`` shape, same per-node ``RunRecord`` persistence, same
tenant_id propagation, same first-failure-stops semantics — but the
topology walk runs through LangGraph's ``CompiledStateGraph.ainvoke``
instead of our hand-rolled loop.

Why ship this now if it does nothing the homegrown runner doesn't:

* **It's the seam.** Conditional edges (v1.1), parallel fan-out (v1.1),
  HITL pauses (v1.1), and the checkpointer ecosystem all plug in here
  by removing the linear-only validator and emitting the additional
  LangGraph constructs. Without the seam in production code, those
  features remain ahead of their integration path.
* **It validates the seam against a real workload.** The
  ``langgraph_prototype.py`` spike used mock node callables. This
  compiler wraps the actual ``Executor.execute`` — proving that retry,
  fallback, cost tracking, schema validation, and storage persistence
  compose with LangGraph's node-fn lifecycle without surprises.

Error semantics (v1.0):

* Linear walk; first node that returns a non-success
  :class:`movate.core.models.RunResponse` stops the workflow. State
  AT THE POINT OF FAILURE is preserved (not merged with the failing
  node's partial output) — same as the homegrown runner.
* Schema-validation errors on initial_state raise
  :class:`movate.core.workflow.runner.WorkflowRunError` before the
  graph is compiled.
* Agent-load errors at a node raise ``WorkflowRunError`` before that
  node's runner fn is invoked.

What's deferred (v1.1+):

* Conditional edges → ``add_conditional_edges`` mapping. See
  ``docs/langgraph-seam.md`` §B.
* Parallel fan-out → state-schema reducer annotations.
  See ``docs/langgraph-seam.md`` §A.
* HITL → ``interrupt_before`` + checkpointer.
  See ``docs/langgraph-seam.md`` §D-§E.
* Branch-level failure invalidation (sibling branches survive failure).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaError

from movate.core.executor import Executor
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import (
    RunRecord,
    RunRequest,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.workflow.ir import EdgeKind, NodeType, WorkflowGraph
from movate.storage.base import StorageProvider

if TYPE_CHECKING:
    # Forward-declared so the type annotation resolves without a runtime
    # circular import (runner.py imports this module via its dispatch).
    from movate.core.workflow.runner import WorkflowResult


class LangGraphCompileError(Exception):
    """Raised when the IR can't be compiled to a LangGraph StateGraph.

    Distinct from :class:`WorkflowRunError` (runtime / per-node failures)
    and :class:`WorkflowCompileError` (IR validation failures). Callers
    typically catch all three and map to exit-code 2.
    """


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------


def can_compile(graph: WorkflowGraph) -> tuple[bool, str | None]:
    """Return ``(supported, reason)`` for the v1.0 compiler.

    Used by the runner to surface a clean error before attempting the
    compile, rather than letting the build fail halfway through.
    Returning ``(False, reason)`` carries the operator-facing message
    explaining WHY this graph can't go through LangGraph yet — usually
    pointing at the v1.1 feature that would unlock it.
    """
    if not graph.is_linear():
        return (
            False,
            "langgraph runtime currently supports linear workflows only — "
            "conditional / parallel topologies land in v1.1 (see "
            "docs/langgraph-seam.md).",
        )
    for nid, node in graph.nodes.items():
        if node.type is not NodeType.AGENT:
            return (
                False,
                f"node {nid!r} has type {node.type.value!r}; langgraph compiler "
                "currently handles AGENT nodes only. TOOL / HUMAN / FUNCTION / "
                "SUB_WORKFLOW support lands in v1.1.",
            )
    for e in graph.edges:
        if e.kind is not EdgeKind.SEQUENTIAL:
            return (
                False,
                f"edge {e.from_id}→{e.to_id} has kind {e.kind.value!r}; "
                "langgraph compiler currently handles SEQUENTIAL edges only.",
            )
    return (True, None)


def import_langgraph() -> tuple[Any, Any, Any]:
    """Lazy LangGraph import.

    Done in a helper rather than at module top so ``movate validate`` and
    other commands that touch this module (via the compilers package
    import chain) don't pay the LangGraph startup cost. Raises
    :class:`LangGraphCompileError` with an install hint when LangGraph
    isn't on the system Python — operators see a friendly pointer
    instead of a raw ImportError.
    """
    try:
        from langgraph.graph import END, START, StateGraph  # noqa: PLC0415 — optional dep
    except ImportError as exc:
        raise LangGraphCompileError(
            "workflow.yaml declares 'runtime: langgraph' but the langgraph "
            "package isn't installed. Install with: "
            "uv pip install 'movate-cli[langgraph]'"
        ) from exc
    return StateGraph, START, END


# ---------------------------------------------------------------------------
# Runner entry point — async; matches WorkflowRunner.run's surface
# ---------------------------------------------------------------------------


async def run_via_langgraph(
    graph: WorkflowGraph,
    initial_state: dict[str, Any],
    *,
    executor: Executor,
    storage: StorageProvider,
    tenant_id: str,
    workflow_run_id: str | None = None,
) -> WorkflowResult:
    """Execute ``graph`` under the LangGraph runtime.

    Drop-in replacement for :meth:`WorkflowRunner.run` when
    ``graph.runtime == "langgraph"``. Returns the same ``WorkflowResult``
    shape so downstream code (CLI render, storage queries, replay)
    doesn't branch on runtime.
    """
    # Local imports for the WorkflowResult + WorkflowRunError types and
    # the _summarize_run helper. runner.py imports THIS module at dispatch
    # time, so importing runner at module-level here would cycle.
    from movate.core.workflow.runner import (  # noqa: PLC0415 — circular import
        WorkflowResult,
        WorkflowRunError,
        _summarize_run,
    )

    supported, reason = can_compile(graph)
    if not supported:
        raise LangGraphCompileError(reason or "unsupported graph shape")

    StateGraph, START, END = import_langgraph()  # noqa: N806 — LangGraph public names

    wf_id = workflow_run_id or str(uuid4())
    started = time.monotonic()

    # Validate initial state up front — same as the homegrown runner.
    try:
        Draft202012Validator(graph.state_schema).validate(initial_state)
    except JsonSchemaError as exc:
        raise WorkflowRunError(
            f"initial_state failed workflow state_schema: {exc.message}"
        ) from exc

    # Pre-load every AGENT bundle so failures surface before we build
    # the graph. Mirrors the homegrown runner's per-node load step.
    bundles: dict[str, AgentBundle] = {}
    for nid, node in graph.nodes.items():
        try:
            bundles[nid] = load_agent(node.ref)
        except AgentLoadError as exc:
            raise WorkflowRunError(
                f"node {nid!r}: agent at {node.ref} failed to load: {exc}"
            ) from exc

    # Shared closure state — node fns append RunRecords here and mark
    # workflow-level errors. Reading these after `ainvoke` reconstructs
    # the WorkflowResult.
    captured_runs: list[RunRecord] = []
    error_state: dict[str, Any] = {}  # {"node_id": ..., "error": ErrorInfo, "state_before": dict}

    def _make_node_fn(
        node_id: str,
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        bundle = bundles[node_id]

        async def node_fn(state: dict[str, Any]) -> dict[str, Any]:
            # If a previous node errored, this one is a no-op pass-through.
            # LangGraph still walks downstream nodes in topological order;
            # we short-circuit by ignoring them, then the post-invoke
            # logic builds the WorkflowResult from `error_state`.
            if error_state:
                return state

            # Project state onto the agent's input schema (same rule as
            # the homegrown runner).
            agent_input = _project_state(state, bundle)

            response = await executor.execute(
                bundle,
                RunRequest(agent=bundle.spec.name, input=agent_input),
                workflow_run_id=wf_id,
                node_id=node_id,
            )

            # Per-node RunRecord summary for the WorkflowResult.runs view.
            # `_summarize_run` was imported once at the top of run_via_langgraph.
            summary = _summarize_run(
                response,
                tenant_id=tenant_id,
                bundle=bundle,
                wf_id=wf_id,
                node_id=node_id,
            )
            captured_runs.append(summary)

            if response.status != "success":
                # Same as homegrown: persist an ERROR-status RunRecord
                # for queries that join on workflow_run_id+node_id. The
                # executor only writes a FailureRecord on its side.
                # Reads `storage` from the enclosing closure (loop var).
                await storage.save_run(summary)
                error_state["node_id"] = node_id
                error_state["error"] = response.error
                error_state["state_before"] = dict(state)
                # Return state unchanged so partial-state semantics match
                # the homegrown runner.
                return state

            # Success: shallow-merge response into state. Returning the
            # FULL merged state (not just delta) so `StateGraph(dict)`'s
            # replace-on-update merge preserves all keys.
            new_state = dict(state)
            new_state.update(response.data)
            return new_state

        return node_fn

    state_graph = StateGraph(dict)
    for nid in graph.nodes:
        state_graph.add_node(nid, _make_node_fn(nid))

    state_graph.add_edge(START, graph.entrypoint)
    for edge in graph.edges:
        state_graph.add_edge(edge.from_id, edge.to_id)
    for sink in graph.sinks():
        state_graph.add_edge(sink, END)

    compiled = state_graph.compile()

    # Invoke. LangGraph will walk every node; our short-circuit logic
    # ensures downstream-of-error nodes are pass-throughs.
    final_state = await compiled.ainvoke(dict(initial_state))

    finished = time.monotonic()

    if error_state:
        # Workflow halted mid-walk. Persist the WorkflowRunRecord with
        # error_node_id + the pre-failure state, then build the result.
        wf_record = WorkflowRunRecord(
            workflow_run_id=wf_id,
            tenant_id=tenant_id,
            workflow=graph.name,
            workflow_version=graph.version,
            status=WorkflowStatus.ERROR,
            initial_state=initial_state,
            final_state=error_state["state_before"],
            error_node_id=error_state["node_id"],
            error=error_state["error"],
        )
        await storage.save_workflow_run(wf_record)
        return WorkflowResult(
            workflow_run_id=wf_id,
            status=WorkflowStatus.ERROR,
            initial_state=initial_state,
            final_state=error_state["state_before"],
            runs=captured_runs,
            error_node_id=error_state["node_id"],
            error=error_state["error"],
            started_at=started,
            finished_at=finished,
        )

    # Happy path.
    wf_record = WorkflowRunRecord(
        workflow_run_id=wf_id,
        tenant_id=tenant_id,
        workflow=graph.name,
        workflow_version=graph.version,
        status=WorkflowStatus.SUCCESS,
        initial_state=initial_state,
        final_state=final_state,
    )
    await storage.save_workflow_run(wf_record)
    return WorkflowResult(
        workflow_run_id=wf_id,
        status=WorkflowStatus.SUCCESS,
        initial_state=initial_state,
        final_state=final_state,
        runs=captured_runs,
        started_at=started,
        finished_at=finished,
    )


def _project_state(state: dict[str, Any], bundle: AgentBundle) -> dict[str, Any]:
    """Same rule as :func:`movate.core.workflow.runner._project_state` —
    duplicated here (not imported) so this module stays a true alternative
    compiler: a future variant might project differently (e.g. typed-state
    extraction) without touching the homegrown runner."""
    props = bundle.input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return dict(state)
    return {k: state[k] for k in props if k in state}


# Type-only re-export so callers can `from ...compilers.langgraph import LangGraphCompileError`
# without importing this whole module's heavy machinery.
__all__ = ["LangGraphCompileError", "can_compile", "import_langgraph", "run_via_langgraph"]
