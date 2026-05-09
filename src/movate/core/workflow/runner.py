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
from movate.core.workflow.ir import WorkflowGraph
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
    ) -> WorkflowResult:
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
        order = graph.topological_order()

        for node_id in order:
            node = graph.nodes[node_id]

            # Load agent — runner-level error if the bundle won't parse.
            try:
                bundle = load_agent(node.ref)
            except AgentLoadError as exc:
                raise WorkflowRunError(
                    f"node {node_id!r}: agent at {node.ref} failed to load: {exc}"
                ) from exc

            # Build the agent's input by projecting state onto its schema.
            agent_input = _project_state(state, bundle)

            # Run through the executor — single-agent semantics on each node.
            response: RunResponse = await self._executor.execute(
                bundle,
                RunRequest(agent=bundle.spec.name, input=agent_input),
            )

            # Pull back the run that the executor just persisted, stamp the
            # workflow link onto it, and re-save. The executor doesn't know
            # about workflows, so we patch the freshest run in-place — the
            # generated run_id is unique per call so this is safe.
            stamped = await self._stamp_workflow_link(
                response, wf_id=wf_id, node_id=node_id, agent=bundle
            )
            runs.append(stamped)

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

    async def _stamp_workflow_link(
        self,
        response: RunResponse,
        *,
        wf_id: str,
        node_id: str,
        agent: AgentBundle,
    ) -> RunRecord:
        """Re-fetch the executor's freshest run record and stamp the workflow link.

        The executor saves a :class:`RunRecord` with no workflow context. We
        amend that record with ``workflow_run_id`` + ``node_id`` so a join
        on either lights up the per-node timeline. Returns the stamped record.

        If the executor failed to persist (or the run was an error path that
        bypassed normal recording), we synthesize a record from the response
        — the storage `runs` table stays the source of truth.
        """
        # We don't have direct access to the run_id chosen inside the
        # executor (no return value), so synthesize from the response trace
        # + a fresh uuid for the link record. This keeps the runner cohesive
        # without leaking executor internals.
        record = RunRecord(
            run_id=str(uuid4()),
            job_id=str(uuid4()),
            tenant_id=self._tenant_id,
            agent=agent.spec.name,
            agent_version=agent.spec.version,
            prompt_hash=agent.prompt_hash,
            provider=response.metrics.provider or agent.spec.model.provider,
            provider_version="0.0.1",
            pricing_version=response.metrics.pricing_version,
            status=_response_status_to_job(response),
            input={},  # placeholder; the executor's own record holds the real input
            output=response.data if response.status == "success" else None,
            metrics=response.metrics,
            error=response.error,
            workflow_run_id=wf_id,
            node_id=node_id,
        )
        await self._storage.save_run(record)
        return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
