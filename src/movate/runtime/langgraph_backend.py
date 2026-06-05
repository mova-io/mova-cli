"""LangGraph in-process execution backend (ADR 030 D1).

Programmatically builds a ``langgraph.graph.StateGraph`` from the mdk
workflow IR (``WorkflowGraph``) and executes it in-process. Each agent
node is an async closure that calls the mdk ``Executor.execute()`` —
the SAME execution path the native runner and Temporal activities use
(ADR 054 D3 reuse). The result is a ``WorkflowResult`` with the same
shape the native runner produces (conformance contract, ADR 055 D7).

Import safety: the ``langgraph`` SDK is imported LAZILY inside the public
functions — never at module scope. A runtime without ``mdk[langgraph]``
installed can still import this module (type annotations use strings).
``require_backend_available("langgraph")`` in ``workflow_backend.py``
probes availability BEFORE any call reaches here.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from movate.core.executor import Executor
    from movate.core.loader import AgentBundle
    from movate.core.workflow.ir import WorkflowGraph, WorkflowNode
    from movate.storage.base import StorageProvider
    from movate.tracing.base import SpanCtx, Tracer

log = logging.getLogger(__name__)

# LangGraph's default recursion limit is 25; we match it. Operators can
# override via workflow.yaml metadata or a future CLI flag.
DEFAULT_RECURSION_LIMIT = 25


class LangGraphBackendError(Exception):
    """Raised when LangGraph graph construction or invocation fails."""


async def run_langgraph_workflow(  # noqa: PLR0912
    graph: WorkflowGraph,
    initial_state: dict[str, Any],
    *,
    executor: Executor,
    tracer: Tracer,
    storage: StorageProvider,
    tenant_id: str = "local",
    workflow_run_id: str | None = None,
    mock: bool = False,
    defaults: Any = None,
    on_node_token: Any | None = None,
) -> Any:
    """Compile ``graph`` to LangGraph and execute it in-process.

    Returns a :class:`~movate.core.workflow.runner.WorkflowResult` with the
    same shape the native runner produces (ADR 055 D7 conformance).
    """
    from langgraph.checkpoint.memory import MemorySaver  # noqa: PLC0415
    from langgraph.graph import END, START, StateGraph  # noqa: PLC0415

    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.core.models import (  # noqa: PLC0415
        RunRequest,
        WorkflowStatus,
    )
    from movate.core.workflow.ir import EdgeKind, NodeType  # noqa: PLC0415
    from movate.core.workflow.runner import WorkflowResult  # noqa: PLC0415
    from movate.runtime.langgraph_checkpointer import (  # noqa: PLC0415
        MdkCheckpointSaver,
    )

    wf_id = workflow_run_id or str(uuid4())
    started = time.monotonic()
    runs: list[Any] = []
    error_node_id: str | None = None
    error: Any = None

    # ── Tracing: open root span ──────────────────────────────────────────
    wf_span: SpanCtx | None = None
    with contextlib.suppress(Exception):
        wf_span = tracer.start_span(
            "workflow.execute",
            {
                "workflow.name": graph.name,
                "workflow.version": graph.version,
                "workflow.runtime": "langgraph",
                "workflow.run_id": wf_id,
                "workflow.node_count": len(graph.nodes),
            },
        )

    def _project_state(state: dict[str, Any], bundle: AgentBundle) -> dict[str, Any]:
        """Filter ``state`` to keys the agent's input schema names."""
        props = bundle.input_schema.get("properties")
        if not isinstance(props, dict) or not props:
            return dict(state)
        return {k: state[k] for k in props if k in state}

    def _make_agent_node(node: WorkflowNode) -> Any:
        """Build an async node function that calls ``Executor.execute()``."""

        async def agent_fn(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal error_node_id, error
            node_span: SpanCtx | None = None
            with contextlib.suppress(Exception):
                node_span = tracer.start_span(
                    "workflow.node",
                    {
                        "node.id": node.id,
                        "node.type": str(node.type),
                        "node.agent_ref": node.ref,
                    },
                    parent=wf_span,
                )

            try:
                bundle = load_agent(node.ref)
                agent_input = _project_state(state, bundle)
                # on_node_token enables streaming: tokens from each node
                # are forwarded to the caller as they're generated (e.g.
                # for voice pipeline sentence-streaming TTS).
                _token_cb = None
                if on_node_token is not None:

                    def _token_cb(token: str) -> None:  # type: ignore[misc]
                        on_node_token(node.id, token)

                response = await executor.execute(
                    bundle,
                    RunRequest(agent=bundle.spec.name, input=agent_input),
                    workflow_run_id=wf_id,
                    node_id=node.id,
                    parent_span=node_span or wf_span,
                    tenant_id_override=tenant_id,
                    on_token=_token_cb,
                )
                if response.status != "success":
                    error_node_id = node.id
                    error = response.error
                    raise LangGraphBackendError(
                        f"node {node.id!r} failed: "
                        f"{getattr(response.error, 'message', response.status)}"
                    )
                # Merge agent output into state.
                merged = {**state, **response.data}
                log.debug(
                    "langgraph node %s completed: keys=%s",
                    node.id,
                    list(response.data.keys()),
                )
                return merged
            except LangGraphBackendError:
                raise  # re-raise our own errors
            except Exception as exc:
                error_node_id = node.id
                error = exc
                raise LangGraphBackendError(f"node {node.id!r} raised: {exc}") from exc
            finally:
                if node_span is not None:
                    with contextlib.suppress(Exception):
                        tracer.end_span(node_span)

        return agent_fn

    def _make_router_node(node: WorkflowNode) -> Any:
        """Build a node function for intent-routers.

        The node runs the classifier and writes the routing decision into
        state under a ``__route_{node_id}`` key. A conditional-edge router
        function reads this key to determine the next node.
        """

        async def router_fn(state: dict[str, Any]) -> dict[str, Any]:
            # Intent-router metadata carries the routes config.
            routes = node.metadata.get("routes", {})
            fallback = node.metadata.get("fallback")
            classifier_ref = node.metadata.get("classifier_agent")
            input_field = node.metadata.get("input_field", "input")

            # Run the classifier agent to get a label.
            if classifier_ref:
                bundle = load_agent(classifier_ref)
                classifier_input = {input_field: state.get(input_field, "")}
                response = await executor.execute(
                    bundle,
                    RunRequest(agent=bundle.spec.name, input=classifier_input),
                    workflow_run_id=wf_id,
                    node_id=node.id,
                    parent_span=wf_span,
                    tenant_id_override=tenant_id,
                )
                label = str(response.data.get("label", "")).strip().lower()
            else:
                label = ""

            # Resolve label → target node.
            target = routes.get(label, fallback or "")
            decision_key = f"__route_{node.id}"
            log.debug(
                "langgraph router %s: label=%r target=%r",
                node.id,
                label,
                target,
            )
            return {**state, decision_key: target}

        return router_fn

    # ── Build the StateGraph ─────────────────────────────────────────────
    try:
        builder = StateGraph(dict)  # type: ignore[type-var]

        for node_id, node in graph.nodes.items():
            if node.type == NodeType.AGENT:
                builder.add_node(node_id, _make_agent_node(node))
            elif node.type == NodeType.INTENT_ROUTER:
                builder.add_node(node_id, _make_router_node(node))
            elif node.type == NodeType.HUMAN:
                # HUMAN gates are not yet supported on the LangGraph backend.
                # Fail clearly so the operator knows to use native or temporal.
                raise LangGraphBackendError(
                    f"HUMAN node {node_id!r} is not yet supported on the "
                    "LangGraph backend. Use runtime 'native' or 'temporal'."
                )
            else:
                raise LangGraphBackendError(
                    f"unsupported node type {node.type!r} for node {node_id!r}"
                )

        # Wire START → entrypoint.
        builder.add_edge(START, graph.entrypoint)

        # Collect which nodes have conditional out-edges (intent-routers).
        conditional_sources: set[str] = set()
        for edge in graph.edges:
            if edge.kind == EdgeKind.CONDITIONAL:
                conditional_sources.add(edge.from_id)

        # Group conditional edges by source for add_conditional_edges().
        cond_edge_map: dict[str, dict[str, str]] = {}
        for edge in graph.edges:
            if edge.kind == EdgeKind.CONDITIONAL:
                cond_edge_map.setdefault(edge.from_id, {})[edge.condition or edge.to_id] = (
                    edge.to_id
                )

        # Wire edges.
        for edge in graph.edges:
            if edge.from_id in conditional_sources:
                continue  # handled below via add_conditional_edges
            if edge.metadata.get("synthetic"):
                continue  # synthetic edges from compiler, not real graph edges
            builder.add_edge(edge.from_id, edge.to_id)

        # Wire conditional edges (intent-routers).
        for source_id, mapping in cond_edge_map.items():
            decision_key = f"__route_{source_id}"

            def _make_router(key: str, route_map: dict[str, str]) -> Any:
                def route_fn(state: dict[str, Any]) -> str:
                    return str(state.get(key, ""))

                return route_fn

            builder.add_conditional_edges(  # type: ignore[arg-type]
                source_id,
                _make_router(decision_key, mapping),
                mapping,
            )

        # Wire sinks → END.
        for sink_id in graph.sinks():
            if sink_id not in conditional_sources:
                builder.add_edge(sink_id, END)

        # Use durable MdkCheckpointSaver when the storage backend supports
        # it (SQLite / Postgres); fall back to MemorySaver for InMemory or
        # when setup fails (e.g. missing langgraph extras).
        checkpointer: Any
        try:
            checkpointer = await MdkCheckpointSaver.from_storage(storage)
            log.debug("langgraph: using MdkCheckpointSaver (%s)", storage.name)
        except (TypeError, Exception):
            log.debug("langgraph: falling back to MemorySaver")
            checkpointer = MemorySaver()

        compiled = builder.compile(checkpointer=checkpointer)

    except LangGraphBackendError:
        raise
    except Exception as exc:
        raise LangGraphBackendError(f"failed to build LangGraph StateGraph: {exc}") from exc

    # ── Execute ──────────────────────────────────────────────────────────
    recursion_limit = int(graph.metadata.get("recursion_limit", DEFAULT_RECURSION_LIMIT))

    try:
        result_state = await compiled.ainvoke(
            initial_state,
            {
                "recursion_limit": recursion_limit,
                "configurable": {"thread_id": wf_id},
            },
        )
        finished = time.monotonic()

        return WorkflowResult(
            workflow_run_id=wf_id,
            status=WorkflowStatus.SUCCESS,
            initial_state=initial_state,
            final_state=dict(result_state),
            runs=runs,
            started_at=started,
            finished_at=finished,
        )

    except LangGraphBackendError:
        # Node failure — already captured error_node_id and error above.
        finished = time.monotonic()
        from movate.core.models import ErrorInfo  # noqa: PLC0415

        return WorkflowResult(
            workflow_run_id=wf_id,
            status=WorkflowStatus.ERROR,
            initial_state=initial_state,
            final_state=initial_state,  # pre-failure state
            runs=runs,
            error_node_id=error_node_id,
            error=(
                error
                if isinstance(error, ErrorInfo)
                else ErrorInfo(
                    type="langgraph_node_error",
                    message=str(error),
                    retryable=False,
                )
            ),
            started_at=started,
            finished_at=finished,
        )

    except Exception as exc:
        # Catch LangGraph's GraphRecursionError and any other unexpected errors.
        finished = time.monotonic()
        from movate.core.models import ErrorInfo  # noqa: PLC0415

        is_recursion = "recursion" in type(exc).__name__.lower()
        error_type = "recursion_limit_exceeded" if is_recursion else "langgraph_error"
        log.warning("langgraph workflow %s failed: %s", wf_id, exc)

        return WorkflowResult(
            workflow_run_id=wf_id,
            status=WorkflowStatus.ERROR,
            initial_state=initial_state,
            final_state=initial_state,
            runs=runs,
            error_node_id=error_node_id,
            error=ErrorInfo(
                type=error_type,
                message=str(exc),
                retryable=False,
            ),
            started_at=started,
            finished_at=finished,
        )

    finally:
        if wf_span is not None:
            with contextlib.suppress(Exception):
                tracer.end_span(wf_span)
