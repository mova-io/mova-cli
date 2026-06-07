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

HITL pause (ADR 017 D5, PR 1):

* When the walker reaches a ``NodeType.HUMAN`` node it executes NOTHING.
  It persists a durable checkpoint — a ``WorkflowRunRecord`` with
  ``status=PAUSED``, the gate's ``paused_node_id``, the ``paused_state``
  captured at the gate (the post-merge state of every node up to but not
  including the gate), and the gate's ``human_task`` spec — then returns a
  ``WorkflowResult(status=PAUSED, ...)``. No node after the gate runs.
HITL resume (ADR 017 D5, PR 2):

* :meth:`WorkflowRunner.resume` loads a PAUSED checkpoint (the signal
  endpoint has already merged the human's decision into ``paused_state``)
  and continues the walk from the **sequential successor** of
  ``paused_node_id`` with that merged state, reusing the SAME loop as
  :meth:`run` (:meth:`_walk`). ``paused_node_id`` + ``paused_state`` +
  ``human_task`` + ``workflow``/``workflow_version`` fully reconstruct where
  to resume. If the successor is itself a HUMAN node the walk re-pauses with
  a fresh checkpoint, so multi-gate workflows resume one gate at a time for
  free. The terminal/paused record is persisted under the SAME
  ``workflow_run_id`` (``save_workflow_run`` upserts on the id).

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
from movate.core.workflow.judge import (
    build_judge_state_value,
    derive_terminate,
    load_judge_bundle,
    verdict_from_response_data,
)
from movate.storage.base import StorageProvider
from movate.tracing.base import SpanCtx

# Absolute ceiling on per-node visits during a single walk (ADR 056 D4 +
# CLAUDE.md failure-mode rule). Bounded reflection loops re-visit nodes by
# design; this is the runaway backstop a JUDGE node's own ``max_iterations``
# rides under — even a misconfigured loop can never spin forever.
_MAX_NODE_VISITS = 50


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
    inspect what node N saw before crashing); on PAUSED this is the state
    captured at the human gate (same value as the checkpoint's
    ``paused_state``)."""

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

        # Validate initial state against the workflow's schema.
        try:
            Draft202012Validator(graph.state_schema).validate(initial_state)
        except JsonSchemaError as exc:
            raise WorkflowRunError(
                f"initial_state failed workflow state_schema: {exc.message}"
            ) from exc

        return await self._walk(
            graph,
            start_id=graph.entrypoint,
            state=dict(initial_state),
            initial_state=initial_state,
            wf_id=wf_id,
            mock=mock,
        )

    async def resume(
        self,
        graph: WorkflowGraph,
        record: WorkflowRunRecord,
    ) -> WorkflowResult:
        """Resume a workflow paused at a HUMAN gate (ADR 017 D5, PR 2).

        The signal endpoint has already validated the human's decision
        against the gate's ``output_contract`` and merged it into
        ``record.paused_state`` (decision wins), persisting the updated
        checkpoint. We continue the walk from the **sequential successor**
        of ``record.paused_node_id`` with that merged state, reusing the
        SAME loop as :meth:`run` via :meth:`_walk`. If the successor is
        itself a HUMAN node the walk re-pauses (a fresh PAUSED checkpoint),
        so multi-gate workflows resume one gate at a time for free.

        The terminal/paused :class:`WorkflowRunRecord` is persisted under
        the SAME ``workflow_run_id`` (resuming updates the existing run; it
        does not create a new one — ``save_workflow_run`` upserts on the id).
        ``initial_state`` is carried from the record so the resumed run's
        provenance stays intact.

        Guards (the endpoint maps these to 4xx):

        * ``record.status != PAUSED`` → not a resumable checkpoint (already
          resumed / terminal). Raises :class:`WorkflowRunError`.
        * ``record.paused_node_id is None`` → no gate to resume from (a
          malformed / non-checkpoint record). Raises :class:`WorkflowRunError`.
        """
        if record.status is not WorkflowStatus.PAUSED:
            raise WorkflowRunError(
                f"cannot resume workflow_run {record.workflow_run_id!r}: status is "
                f"{record.status.value!r}, expected {WorkflowStatus.PAUSED.value!r}"
            )
        if record.paused_node_id is None:
            raise WorkflowRunError(
                f"cannot resume workflow_run {record.workflow_run_id!r}: no "
                f"paused_node_id on the checkpoint"
            )

        # The merged state to resume with. ``paused_state`` is the post-merge
        # state captured at the gate, already merged with the human decision
        # by the signal endpoint. Fall back to an empty dict defensively (a
        # PAUSED record should always carry it).
        resume_state = dict(record.paused_state or {})

        # Resume from the gate's single sequential successor. The gate
        # executed nothing, so its successor is where execution continues.
        successor = self._sequential_successor(graph, record.paused_node_id)

        return await self._walk(
            graph,
            start_id=successor,
            state=resume_state,
            initial_state=record.initial_state,
            wf_id=record.workflow_run_id,
            mock=False,
        )

    async def _walk(
        self,
        graph: WorkflowGraph,
        *,
        start_id: str | None,
        state: dict[str, Any],
        initial_state: dict[str, Any],
        wf_id: str,
        mock: bool,
    ) -> WorkflowResult:
        """Walk the graph from ``start_id``, threading ``state``.

        The shared traversal loop for both :meth:`run` (start at the
        entrypoint with the validated initial state) and :meth:`resume`
        (start at a paused gate's successor with the merged checkpoint
        state). Persists a terminal/paused :class:`WorkflowRunRecord` keyed
        by ``wf_id`` (``save_workflow_run`` upserts) and returns a
        :class:`WorkflowResult`.

        ``start_id is None`` (a paused gate that is itself the sink — no
        successor) means "nothing left to run": the walk completes
        immediately as SUCCESS with ``state`` as the final state.

        Tracing (ADR 024 D4): opens ONE ``workflow.execute`` root span for the
        whole walk and threads its :class:`~movate.tracing.base.SpanCtx` into
        every node's ``Executor.execute(..., parent_span=...)`` — so each
        node's ``agent.execute`` nests under the workflow root and a multi-node
        workflow is one trace tree (Langfuse / OTel), not N disconnected roots.
        The span is opened on the executor's tracer (tracing stays wired at the
        edges) and is always closed exactly once via the ``finally`` — including
        on every early ``return`` (HITL pause, per-node error) — so a partial
        workflow still produces a complete root span. Offline correlation is
        unchanged: ``RunRecord.workflow_run_id`` + ``node_id`` already link the
        node rows. With tracing off (NullTracer) the span is a no-op.
        """
        started = time.monotonic()
        runs: list[RunRecord] = []

        # ADR 024 D4 — workflow-root span. One per walk; every node's
        # agent.execute nests under it via parent_span. Opened on the executor's
        # tracer so tracing stays at the edges (the runner is a pure
        # orchestrator). Closed exactly once in the finally below.
        wf_span = self._executor.tracer.start_span(
            "workflow.execute",
            {
                "workflow": graph.name,
                "workflow_version": graph.version,
                "workflow_run_id": wf_id,
                "tenant_id": self._tenant_id,
            },
        )
        try:
            return await self._walk_traced(
                graph,
                start_id=start_id,
                state=state,
                initial_state=initial_state,
                wf_id=wf_id,
                mock=mock,
                started=started,
                runs=runs,
                wf_span=wf_span,
            )
        finally:
            self._executor.tracer.end_span(wf_span)

    async def _walk_traced(
        self,
        graph: WorkflowGraph,
        *,
        start_id: str | None,
        state: dict[str, Any],
        initial_state: dict[str, Any],
        wf_id: str,
        mock: bool,
        started: float,
        runs: list[RunRecord],
        wf_span: SpanCtx,
    ) -> WorkflowResult:
        """Inner traversal loop — see :meth:`_walk`.

        Split out so :meth:`_walk` can own the workflow-root span lifecycle
        (open before, close in ``finally`` after) without threading the
        try/finally around every early return. ``wf_span`` is the workflow-root
        span every node's ``agent.execute`` nests under.
        """
        # Dynamic traversal: start at ``start_id``, follow each node's single
        # sequential successor (for agent nodes) or the router's chosen
        # branch (for intent-router nodes).  We track visited ids to guard
        # against pathological graph shapes that bypass the compiler's cycle
        # detector.
        current_id: str | None = start_id
        # Per-node visit counts (was a boolean set). A JUDGE-driven reflection
        # back-edge (ADR 056 D4) legitimately re-visits the producer + judge,
        # so a plain "seen once ⇒ cycle" guard is too strict. The JUDGE node
        # enforces its own ``max_iterations`` cap; this counter is the absolute
        # runaway backstop (``_MAX_NODE_VISITS``) for any other shape.
        visits: dict[str, int] = {}

        while current_id is not None:
            visits[current_id] = visits.get(current_id, 0) + 1
            if visits[current_id] > _MAX_NODE_VISITS:
                raise WorkflowRunError(
                    f"runaway loop: node {current_id!r} visited "
                    f"{visits[current_id]} times (cap {_MAX_NODE_VISITS})"
                )

            node = graph.nodes[current_id]
            node_id = current_id

            if node.type is NodeType.JUDGE:
                # --- JUDGE dispatch (ADR 056 D3) ------------------------------
                result = await self._run_judge(
                    node_id=node_id,
                    node=node,
                    state=state,
                    graph=graph,
                    wf_id=wf_id,
                    mock=mock,
                    wf_span=wf_span,
                    visits=visits,
                )
                if isinstance(result, WorkflowResult):
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
                chosen_next, judge_runs = result
                runs.extend(judge_runs)
                current_id = chosen_next
                continue

            if node.type is NodeType.INTENT_ROUTER:
                # --- intent-router dispatch -----------------------------------
                result = await self._run_intent_router(
                    node_id=node_id,
                    node=node,
                    state=state,
                    graph=graph,
                    wf_id=wf_id,
                    mock=mock,
                    wf_span=wf_span,
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

            if node.type is NodeType.HUMAN:
                # --- HITL gate: pause + persist a durable checkpoint --------
                # ADR 017 D5 (PR 1). Execute NOTHING at the gate. ``state`` is
                # already the post-merge state of every node up to (but not
                # including) this one — that is exactly the checkpoint PR 2
                # resumes from. We persist a PAUSED WorkflowRunRecord carrying
                # the gate id, that state, and the human-task spec, then return
                # a PAUSED WorkflowResult. PR 2's resume-on-signal path loads
                # this record, merges the human's decision into ``paused_state``,
                # and continues from this node's sequential successor.
                finished = time.monotonic()
                approvers = list(node.metadata.get("approvers", []))
                human_task = {
                    "prompt": node.metadata.get("prompt", ""),
                    "output_contract": list(node.metadata.get("output_contract", [])),
                    # Carry approvers on the pause record too (parity with the
                    # Temporal path) so the inventory + notification agree.
                    "approvers": approvers,
                }
                paused_state = dict(state)
                wf_record = WorkflowRunRecord(
                    workflow_run_id=wf_id,
                    tenant_id=self._tenant_id,
                    workflow=graph.name,
                    workflow_version=graph.version,
                    status=WorkflowStatus.PAUSED,
                    initial_state=initial_state,
                    final_state=paused_state,
                    paused_node_id=node_id,
                    paused_state=paused_state,
                    human_task=human_task,
                )
                await self._storage.save_workflow_run(wf_record)
                # Escalate to the approval channel (ADR 083). Fire-and-forget +
                # never raises — the pause is already persisted; notification is
                # best-effort. No-op until MOVATE_NOTIFIER is configured.
                from movate.core.notifier import (  # noqa: PLC0415
                    HumanPause,
                    notify_human_pause_safe,
                )

                await notify_human_pause_safe(
                    HumanPause(
                        run_id=wf_id,
                        workflow_name=graph.name,
                        workflow_version=graph.version,
                        node_id=node_id,
                        prompt=str(node.metadata.get("prompt", "")),
                        output_contract=list(node.metadata.get("output_contract", [])),
                        approvers=approvers,
                        tenant_id=self._tenant_id,
                        runtime="native",
                    )
                )
                return WorkflowResult(
                    workflow_run_id=wf_id,
                    status=WorkflowStatus.PAUSED,
                    initial_state=initial_state,
                    final_state=paused_state,
                    runs=runs,
                    started_at=started,
                    finished_at=finished,
                )

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
                parent_span=wf_span,
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
            current_id = self._sequential_successor(graph, node_id)

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

    @staticmethod
    def _sequential_successor(graph: WorkflowGraph, node_id: str) -> str | None:
        """The single sequential successor of ``node_id`` (or ``None`` at a sink).

        Filters out ``synthetic`` edges (compiler-added bookkeeping edges,
        e.g. an intent-router's fan-out) so only the real next-in-chain node
        is followed. Both the agent-node advance in :meth:`_walk` and
        :meth:`resume` (continuing from a paused gate's successor) use this so
        the "what runs next" rule lives in exactly one place.
        """
        seq = [e.to_id for e in graph.successors(node_id) if not e.metadata.get("synthetic")]
        return seq[0] if seq else None

    async def _run_judge(
        self,
        *,
        node_id: str,
        node: Any,
        state: dict[str, Any],
        graph: WorkflowGraph,
        wf_id: str,
        mock: bool,
        wf_span: SpanCtx,
        visits: dict[str, int],
    ) -> tuple[str | None, list[RunRecord]] | WorkflowResult:
        """Dispatch a JUDGE node (ADR 056 D3).

        Runs the judge (``judge_agent`` ref or inline ``criteria``) through the
        SAME :class:`Executor` every node uses, parses the canonical D2 verdict
        (``{verdict, score, feedback, terminate}``) via the shared
        ``core.workflow.judge`` helpers, stamps it into ``state[node_id]`` (and
        surfaces ``feedback`` at top level so the revise step can thread it),
        then resolves the next node:

        * **terminate** (accept, or ``score >= pass_threshold``) → route to
          ``on_accept`` if set, else the node's sequential successor (the
          eval-gate's continue / end-of-chain).
        * **revise** → if the bounded loop still has budget
          (``visits[node] < max_iterations``), follow the back-edge (the
          node's sequential successor — for a reflection loop that is the
          producer). Once the cap is hit, the loop terminates by routing to
          ``on_accept`` / the non-loop exit (mandatory cap, D4).

        Returns ``(next_node_id | None, [judge_run_record])`` on success, or a
        partial :class:`WorkflowResult` if the judge agent itself errors.

        Under ``mock=True`` the judge call is skipped and a deterministic
        *accept* verdict is produced so the whole pipeline runs under
        ``mdk run --mock`` without spend (matching the intent-router mock path).
        """
        meta = node.metadata
        criteria: str = meta.get("criteria", "") or ""
        input_field: str = meta.get("input_field", "text")
        pass_threshold: float | None = meta.get("pass_threshold")
        on_accept: str | None = meta.get("on_accept")
        on_revise: str | None = meta.get("on_revise")
        max_iterations: int = int(meta.get("max_iterations", 1) or 1)

        seq_next = self._sequential_successor(graph, node_id)

        if mock:
            # Deterministic accept so --mock exercises the accept path end to end.
            state[node_id] = build_judge_state_value(
                verdict="accept", score=None, feedback="", terminate=True
            )
            return (on_accept if on_accept is not None else seq_next), []

        # Load the judge bundle (ref or inline criteria) and run it through the
        # Executor — one execution path, tracing/metering/BYOK at the edges.
        try:
            judge_bundle = load_judge_bundle(judge_ref=node.ref, criteria=criteria)
        except (AgentLoadError, ValueError) as exc:
            raise WorkflowRunError(
                f"judge node {node_id!r}: judge agent failed to load: {exc}"
            ) from exc

        artifact = state.get(input_field, "")
        judge_input = _project_state({"text": str(artifact)}, judge_bundle)

        judge_response: RunResponse = await self._executor.execute(
            judge_bundle,
            RunRequest(agent=judge_bundle.spec.name, input=judge_input),
            workflow_run_id=wf_id,
            node_id=node_id,
            parent_span=wf_span,
            tenant_id_override=self._tenant_id,
        )

        judge_summary = _summarize_run(
            judge_response,
            tenant_id=self._tenant_id,
            bundle=judge_bundle,
            wf_id=wf_id,
            node_id=node_id,
        )
        judge_runs = [judge_summary]

        if judge_response.status != "success":
            await self._storage.save_run(judge_summary)
            return WorkflowResult(
                workflow_run_id=wf_id,
                status=WorkflowStatus.ERROR,
                initial_state=state,
                final_state=state,
                runs=judge_runs,
                error_node_id=node_id,
                error=judge_response.error,
                started_at=0.0,
                finished_at=time.monotonic(),
            )

        # Parse into the canonical verdict + derive terminate (ONE rule, shared
        # with the Temporal activity via core.workflow.judge).
        verdict, score, feedback = verdict_from_response_data(judge_response.data)
        terminate = derive_terminate(verdict=verdict, score=score, pass_threshold=pass_threshold)
        state[node_id] = build_judge_state_value(
            verdict=verdict, score=score, feedback=feedback, terminate=terminate
        )
        # Thread the judge's feedback to the next (revise) step (ADR 056 D4) so
        # the producer can fold it into its re-prompt. Top-level key, last-wins.
        state["feedback"] = feedback

        if terminate:
            # Accept routes to the explicit ``on_accept`` branch, else the
            # node's forward successor. A reflection workflow's only successor
            # is the back-edge to the producer — re-entering the loop on accept
            # is wrong (accept means done), so an accept whose only exit is the
            # back-edge ends the walk (None).
            if on_accept is not None:
                return on_accept, judge_runs
            if seq_next is not None and not self._is_back_edge(graph, node_id, seq_next):
                return seq_next, judge_runs
            return None, judge_runs

        # Revise. The revise target is the explicit ``on_revise`` branch if set,
        # else the sequential successor (which, for a reflection workflow, is
        # the back-edge to the producer).
        revise_target = on_revise if on_revise is not None else seq_next

        # The iteration cap (ADR 056 D4) only governs a revise target that
        # RE-ENTERS the loop (a back-edge). A forward revise branch (e.g. an
        # eval-gate routing to an ``escalate`` node) is a one-shot decision and
        # is taken regardless of the iteration count.
        if revise_target is not None and not self._is_back_edge(graph, node_id, revise_target):
            return revise_target, judge_runs

        # Looping revise: follow the back-edge only while iterations remain.
        if visits.get(node_id, 1) < max_iterations:
            return revise_target, judge_runs

        # Cap reached — terminate the loop deterministically (mandatory cap).
        # Prefer an explicit accept route; otherwise end the walk with the last
        # produced state rather than re-entering the loop.
        if on_accept is not None:
            return on_accept, judge_runs
        return None, judge_runs

    @staticmethod
    def _is_back_edge(graph: WorkflowGraph, from_id: str, to_id: str) -> bool:
        """True if ``from_id→to_id`` closes a loop (a detected back-edge).

        Used by the JUDGE cap logic to tell a reflection back-edge (re-enters
        the loop) from a genuine forward exit. Read-only.
        """
        return any(e.from_id == from_id and e.to_id == to_id for e in graph.find_back_edges())

    async def _run_intent_router(
        self,
        *,
        node_id: str,
        node: Any,
        state: dict[str, Any],
        graph: WorkflowGraph,
        wf_id: str,
        mock: bool,
        wf_span: SpanCtx,
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
            parent_span=wf_span,
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
