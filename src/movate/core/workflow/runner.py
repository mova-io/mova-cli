"""Workflow runner — walks a :class:`WorkflowGraph` linearly, threads state.

Stage-2 deliverable. Calls into the existing :class:`Executor` per node so
cost / tracing / retries / fallback all work identically to single-agent
runs. Each node's :class:`RunRecord` is stamped with the parent
``workflow_run_id`` so the per-node history can be reconstructed by joining
on that id.

State plumbing rules (v0.3, may evolve):

* ``initial_state`` is validated against ``graph.state_schema`` at entry.
* For each node the runner builds the agent's ``input`` by *filtering*
  state to the keys present in the agent's input schema's ``properties``.
  If ``properties`` is empty / absent, the entire state is passed.
  This keeps node contracts narrow even as state grows across nodes.
* The agent's output dict is shallow-merged back into state at top level.
  Output keys overwrite same-named state keys (most-recent-wins).
* On node failure the runner stops and returns the partial state plus the
  failed node's ``RunRecord``. No subsequent nodes execute.

Explicit ``inputs:`` / ``outputs:`` mappings are deliberately deferred to
v0.4 — easy to add when real workflows demand finer control.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaError

from movate.core.executor import Executor
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import (
    ErrorInfo,
    JobStatus,
    RunRecord,
    RunRequest,
    RunResponse,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.storage.base import StorageProvider


class WorkflowRunError(Exception):
    """Raised for runner-level errors that aren't agent failures.

    Examples: initial_state fails schema, agent dir at node.ref won't load.
    Per-node agent failures are *not* exceptions — they're recorded on the
    :class:`WorkflowResult` and the workflow status is set to ``ERROR``.
    """


@dataclass
class WorkflowResult:
    """Output of one :func:`WorkflowRunner.run` call."""

    workflow_run_id: str
    status: WorkflowStatus
    initial_state: dict[str, Any]
    final_state: dict[str, Any]
    """The state dict at the moment the workflow halted. On success this is
    the post-merge state after the sink node ran; on partial failure this is
    the state captured *before* the failing node executed (so the user can
    inspect what node N saw before crashing)."""

    runs: list[RunRecord] = field(default_factory=list)
    error_node_id: str | None = None
    error: ErrorInfo | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at) * 1000))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class WorkflowRunner:
    """Walk a :class:`WorkflowGraph` in topological order.

    The runner is a *pure orchestrator*: every actual model call goes
    through the wrapped :class:`Executor`. That means the per-node retry,
    fallback, cost-drift, and budget logic is identical to a standalone
    ``movate run`` on the same agent.
    """

    def __init__(
        self,
        *,
        executor: Executor,
        storage: StorageProvider,
        tenant_id: str = "local",
    ) -> None:
        self._executor = executor
        self._storage = storage
        self._tenant_id = tenant_id

    async def run(
        self,
        graph: WorkflowGraph,
        initial_state: dict[str, Any],
        *,
        workflow_run_id: str | None = None,
        mock: bool = False,
    ) -> WorkflowResult:
        """Run the workflow from the entrypoint.

        ``mock`` skips real classifier calls in ``intent-router`` nodes —
        the first route key is chosen deterministically so the whole
        workflow can be exercised under ``mdk run --mock``.
        """
        wf_id = workflow_run_id or str(uuid4())
        started = time.monotonic()

        # 1. Validate initial state against the workflow's schema.
        try:
            Draft202012Validator(graph.state_schema).validate(initial_state)
        except JsonSchemaError as exc:
            raise WorkflowRunError(
                f"initial_state failed workflow state_schema: {exc.message}"
            ) from exc

        state: dict[str, Any] = dict(initial_state)
        runs: list[RunRecord] = []

        # Dynamic traversal: start at entrypoint, follow each node's single
        # sequential successor (for agent nodes) or the router's chosen
        # branch (for intent-router nodes).  We track visited ids to guard
        # against pathological graph shapes that bypass the compiler's cycle
        # detector.
        current_id: str | None = graph.entrypoint
        visited: set[str] = set()

        while current_id is not None:
            if current_id in visited:
                raise WorkflowRunError(
                    f"cycle detected at node {current_id!r} during execution"
                )
            visited.add(current_id)

            node = graph.nodes[current_id]
            node_id = current_id

            if node.type is NodeType.INTENT_ROUTER:
                # --- intent-router dispatch -----------------------------------
                result = await self._run_intent_router(
                    node_id=node_id,
                    node=node,
                    state=state,
                    graph=graph,
                    wf_id=wf_id,
                    mock=mock,
                )
                if isinstance(result, WorkflowResult):
                    # Propagate partial failure from within the router.
                    await self._storage.save_workflow_run(
                        WorkflowRunRecord(
                            workflow_run_id=wf_id,
                            tenant_id=self._tenant_id,
                            workflow=graph.name,
                            workflow_version=graph.version,
                            status=WorkflowStatus.ERROR,
                            initial_state=initial_state,
                            final_state=state,
                            error_node_id=result.error_node_id,
                            error=result.error,
                        )
                    )
                    return WorkflowResult(
                        workflow_run_id=wf_id,
                        status=WorkflowStatus.ERROR,
                        initial_state=initial_state,
                        final_state=state,
                        runs=runs + result.runs,
                        error_node_id=result.error_node_id,
                        error=result.error,
                        started_at=started,
                        finished_at=time.monotonic(),
                    )
                # result is (chosen_node_id, classifier_run_records)
                chosen_next, router_runs = result
                runs.extend(router_runs)
                current_id = chosen_next
                continue

            # --- agent node --------------------------------------------------
            # Load agent — runner-level error if the bundle won't parse.
            try:
                bundle = load_agent(node.ref)
            except AgentLoadError as exc:
                raise WorkflowRunError(
                    f"node {node_id!r}: agent at {node.ref} failed to load: {exc}"
                ) from exc

            # Build the agent's input by projecting state onto its schema.
            agent_input = _project_state(state, bundle)

            # Run through the executor with the workflow context so the
            # persisted RunRecord carries workflow_run_id + node_id without
            # the runner needing a second save. Pass tenant_id_override so
            # multi-tenant workers stamp the right tenant on each node's
            # RunRecord (executor default may be a different tenant).
            response: RunResponse = await self._executor.execute(
                bundle,
                RunRequest(agent=bundle.spec.name, input=agent_input),
                workflow_run_id=wf_id,
                node_id=node_id,
                tenant_id_override=self._tenant_id,
            )

            # Python-level summary for the WorkflowResult.runs view.
            # NOT persisted on success — the executor already wrote a row
            # with workflow_run_id+node_id stamped on it. On failure the
            # executor only writes a FailureRecord, so we save one
            # ERROR-status RunRecord here so per-node failures show up in
            # ``list_runs(workflow_run_id=…)`` joins.
            summary = _summarize_run(
                response,
                tenant_id=self._tenant_id,
                bundle=bundle,
                wf_id=wf_id,
                node_id=node_id,
            )
            runs.append(summary)
            if response.status != "success":
                await self._storage.save_run(summary)

            if response.status != "success":
                # Stop — partial state retained. The user sees what node N saw
                # before it crashed (state has NOT been merged with response).
                finished = time.monotonic()
                wf_record = WorkflowRunRecord(
                    workflow_run_id=wf_id,
                    tenant_id=self._tenant_id,
                    workflow=graph.name,
                    workflow_version=graph.version,
                    status=WorkflowStatus.ERROR,
                    initial_state=initial_state,
                    final_state=state,
                    error_node_id=node_id,
                    error=response.error,
                )
                await self._storage.save_workflow_run(wf_record)
                return WorkflowResult(
                    workflow_run_id=wf_id,
                    status=WorkflowStatus.ERROR,
                    initial_state=initial_state,
                    final_state=state,
                    runs=runs,
                    error_node_id=node_id,
                    error=response.error,
                    started_at=started,
                    finished_at=finished,
                )

            # Merge agent output into state.
            state.update(response.data)

            # Advance: follow the single sequential successor (or None at sink).
            seq_successors = [
                e.to_id for e in graph.successors(node_id) if not e.metadata.get("synthetic")
            ]
            current_id = seq_successors[0] if seq_successors else None

        finished = time.monotonic()
        wf_record = WorkflowRunRecord(
            workflow_run_id=wf_id,
            tenant_id=self._tenant_id,
            workflow=graph.name,
            workflow_version=graph.version,
            status=WorkflowStatus.SUCCESS,
            initial_state=initial_state,
            final_state=state,
        )
        await self._storage.save_workflow_run(wf_record)

        return WorkflowResult(
            workflow_run_id=wf_id,
            status=WorkflowStatus.SUCCESS,
            initial_state=initial_state,
            final_state=state,
            runs=runs,
            started_at=started,
            finished_at=finished,
        )

    async def _run_intent_router(
        self,
        *,
        node_id: str,
        node: Any,
        state: dict[str, Any],
        graph: WorkflowGraph,
        wf_id: str,
        mock: bool,
    ) -> tuple[str | None, list[RunRecord]] | WorkflowResult:
        """Dispatch an ``intent-router`` node.

        Returns either:
        - ``(chosen_node_id, [classifier_run_records])`` on success, where
          ``chosen_node_id`` is the next node to execute (or ``None`` if the
          chosen branch has no further successors — treated as end-of-chain).
        - A partial :class:`WorkflowResult` on classifier failure (the caller
          propagates this as a workflow-level error).

        Under ``mock=True`` the real classifier call is skipped; the first
        route key is chosen deterministically so the whole pipeline can be
        exercised with ``mdk run --mock``.
        """
        routes: dict[str, str] = node.metadata["routes"]
        fallback: str = node.metadata["fallback"]
        classifier_agent_name: str = node.metadata["classifier_agent"]
        input_field: str = node.metadata["input_field"]

        if mock:
            # Pick the first route key deterministically (sorted for
            # stability) so --mock gives a predictable path through the
            # workflow regardless of key insertion order.
            chosen_label = next(iter(sorted(routes))) if routes else None
            chosen_node = routes.get(chosen_label, fallback) if chosen_label else fallback
            return chosen_node, []

        # Real path: resolve the classifier agent and call it.
        # The classifier agent may be a bare name (looked up relative to the
        # workflow dir) or an absolute path.
        from pathlib import Path as _Path  # noqa: PLC0415

        clf_path = _Path(classifier_agent_name)
        if not clf_path.is_absolute():
            clf_path = (graph.workflow_dir / classifier_agent_name).resolve()

        try:
            clf_bundle = load_agent(clf_path)
        except AgentLoadError as exc:
            raise WorkflowRunError(
                f"intent-router {node_id!r}: classifier agent {classifier_agent_name!r} "
                f"failed to load: {exc}"
            ) from exc

        # Build classifier input: {text: <state[input_field]>, labels: [<route keys>]}
        text_value = state.get(input_field, "")
        labels = list(routes.keys())
        clf_input = {"text": str(text_value), "labels": labels}

        clf_response: RunResponse = await self._executor.execute(
            clf_bundle,
            RunRequest(agent=clf_bundle.spec.name, input=clf_input),
            workflow_run_id=wf_id,
            node_id=node_id,
            tenant_id_override=self._tenant_id,
        )

        clf_summary = _summarize_run(
            clf_response,
            tenant_id=self._tenant_id,
            bundle=clf_bundle,
            wf_id=wf_id,
            node_id=node_id,
        )
        clf_runs = [clf_summary]

        if clf_response.status != "success":
            await self._storage.save_run(clf_summary)
            # Return a partial WorkflowResult-like object the caller will wrap.
            return WorkflowResult(
                workflow_run_id=wf_id,
                status=WorkflowStatus.ERROR,
                initial_state=state,
                final_state=state,
                runs=clf_runs,
                error_node_id=node_id,
                error=clf_response.error,
                started_at=0.0,
                finished_at=time.monotonic(),
            )

        # Extract the label from the classifier's output.
        chosen_label = clf_response.data.get("label", "")
        chosen_node = routes.get(str(chosen_label), fallback)

        return chosen_node, clf_runs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize_run(
    response: RunResponse,
    *,
    tenant_id: str,
    bundle: AgentBundle,
    wf_id: str,
    node_id: str,
) -> RunRecord:
    """Build a per-node :class:`RunRecord` for the runner's in-memory view.

    Caller decides whether to persist. ``provider_version`` is "" because the
    executor's own persisted row holds the canonical value; the executor also
    holds the canonical input dict, so this synthesis leaves ``input`` empty.
    """
    return RunRecord(
        run_id=str(uuid4()),
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        agent=bundle.spec.name,
        agent_version=bundle.spec.version,
        prompt_hash=bundle.prompt_hash,
        provider=response.metrics.provider or bundle.spec.model.provider,
        provider_version="",
        pricing_version=response.metrics.pricing_version,
        status=_response_status_to_job(response),
        input={},
        output=response.data if response.status == "success" else None,
        metrics=response.metrics,
        error=response.error,
        workflow_run_id=wf_id,
        node_id=node_id,
    )


def _project_state(state: dict[str, Any], bundle: AgentBundle) -> dict[str, Any]:
    """Filter ``state`` to keys the agent's input schema names.

    If the schema lists no ``properties`` (or the schema is permissive),
    pass the whole state. Otherwise pick exactly the listed keys.
    """
    props = bundle.input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return dict(state)
    return {k: state[k] for k in props if k in state}


def _response_status_to_job(response: RunResponse) -> JobStatus:
    if response.status == "success":
        return JobStatus.SUCCESS
    if response.status == "safety_blocked":
        return JobStatus.SAFETY_BLOCKED
    return JobStatus.ERROR
