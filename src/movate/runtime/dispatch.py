"""Worker dispatch — translate a ``JobRecord`` into the right execution path.

Pure logic, no async loop. The :class:`WorkerDispatch` takes the
collaborators (executor, agent registry, optional workflow registry)
once and returns a :class:`DispatchOutcome` per job. The actual claim
loop lives in :mod:`runtime.worker`.

Splitting the loop from the dispatch makes both pieces tractable to
test: dispatch is asserted with a single ``execute_job`` call against
``InMemoryStorage``; the loop is asserted by feeding a stop event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from movate.core.executor import Executor
from movate.core.loader import AgentBundle
from movate.core.models import (
    ErrorInfo,
    JobKind,
    JobRecord,
    JobStatus,
    RunRequest,
    WorkflowStatus,
)
from movate.core.workflow import WorkflowGraph, WorkflowRunner
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchOutcome:
    """What to write back into the ``JobRecord`` after dispatch.

    The worker calls ``storage.update_job(job_id, status=...,
    result_run_id=..., error=...)`` with these fields directly.
    """

    status: JobStatus
    result_run_id: str | None
    error: dict[str, Any] | None


class WorkerDispatch:
    """Routes a claimed ``JobRecord`` to the right execution path.

    Agent jobs go through :class:`Executor`. Workflow jobs go through
    :class:`WorkflowRunner`. Targets that don't resolve in either
    registry → terminal ``ERROR`` with a structured message; the
    caller (the worker loop) updates the job. Never raises for
    *user-facing* failures (unknown agent, bad input, runtime
    exception inside Executor) — those become DispatchOutcome ERRORs.
    Programming errors (storage Provider crash, etc.) propagate so
    the worker's outer try/except can record them as INTERNAL.
    """

    def __init__(
        self,
        *,
        storage: StorageProvider,
        executor: Executor,
        agents: list[AgentBundle] | None = None,
        workflows: dict[str, WorkflowGraph] | None = None,
        use_mock_for_eval: bool = False,
    ) -> None:
        self._storage = storage
        self._executor = executor
        self._agents: dict[str, AgentBundle] = {b.spec.name: b for b in (agents or [])}
        self._workflows: dict[str, WorkflowGraph] = workflows or {}
        self._use_mock_for_eval = use_mock_for_eval
        """When True, eval jobs always use MockProvider regardless of the
        job's ``mock`` flag. Useful in test environments where real LLM
        calls would be expensive. The app sets this from the server's
        startup config; individual eval jobs can override per-job via
        ``job.input["mock"]``."""

    async def execute_job(self, job: JobRecord) -> DispatchOutcome:
        """Execute one job. Returns a :class:`DispatchOutcome` regardless
        of success or user-facing failure."""
        if job.kind == JobKind.AGENT:
            return await self._execute_agent(job)
        if job.kind == JobKind.WORKFLOW:
            return await self._execute_workflow(job)
        if job.kind == JobKind.EVAL:
            return await self._execute_eval(job)
        return _error(
            "unknown_kind",
            f"unsupported JobKind {job.kind!r}",
            retryable=False,
        )

    async def _execute_agent(self, job: JobRecord) -> DispatchOutcome:
        bundle = self._agents.get(job.target)
        if bundle is None:
            # Known recurring failure: in multi-pod ACA deploys the
            # worker pod's filesystem doesn't see agents created via
            # `POST /api/v1/agents` on the API pod (cross-pod sync gap,
            # tracked as BACKLOG item 109). The runbook workaround is
            # `?wait=true` on the run endpoint (item 110), which
            # sidesteps the worker entirely. Surface that pointer in
            # the error so callers stop debugging "did my agent
            # actually get created?" — the agent IS there, just not on
            # this pod.
            return _error(
                "unknown_agent",
                f"agent {job.target!r} not registered on this worker",
                retryable=False,
                hint=(
                    "this worker pod has no bundle for the requested agent. "
                    "if you just created it via POST /api/v1/agents, the "
                    "bundle is on the API pod's filesystem but not yet "
                    "synced to workers (cross-pod sync gap — see BACKLOG "
                    "item 109). until that lands, retry the run with "
                    "`?wait=true` to execute inline on the API pod (item 110)."
                ),
            )
        request = RunRequest(agent=job.target, input=job.input)
        try:
            # The Executor is constructed once per worker process with a
            # default tenant_id (typically "local" or the worker's pool
            # tenant). Pass the JOB's tenant_id explicitly so the
            # persisted RunRecord + budget queries use the right tenant —
            # otherwise GET /runs/<id> from the API key's tenant context
            # returns 404 because the stored row is scoped to the wrong
            # tenant.
            response = await self._executor.execute(
                bundle,
                request,
                job_id=job.job_id,
                tenant_id_override=job.tenant_id,
            )
        except Exception as exc:
            # Executor is expected to swallow MovateError into a
            # status='error' RunResponse, so an unhandled exception
            # here is a real bug or an external failure (storage
            # write, tracer, etc.). Record as a retryable error so
            # operators can decide whether to requeue.
            logger.exception("agent_execute_unhandled job_id=%s", job.job_id)
            return _error("internal", str(exc), retryable=True)

        if response.status == "success":
            return DispatchOutcome(
                status=JobStatus.SUCCESS,
                result_run_id=response.run_id,
                error=None,
            )
        if response.status == "safety_blocked":
            return DispatchOutcome(
                status=JobStatus.SAFETY_BLOCKED,
                result_run_id=response.run_id,
                error=response.error.model_dump() if response.error else None,
            )
        # status == "error"
        return DispatchOutcome(
            status=JobStatus.ERROR,
            result_run_id=response.run_id,
            error=response.error.model_dump() if response.error else None,
        )

    async def _execute_eval(self, job: JobRecord) -> DispatchOutcome:
        """Run an async eval job.

        ``job.input`` carries the eval configuration dict (the same
        fields as :class:`movate.runtime.schemas.EvalSubmission`):
        ``mock``, ``runs``, ``gate_mode``, ``objective``,
        ``skill_responses``. The completed :class:`EvalRecord` is
        persisted via storage; ``result_run_id`` is set to the
        ``eval_id`` so callers can retrieve it via
        ``GET /api/v1/evals/{eval_id}``.
        """
        from movate.core.eval import EvalConfigError, EvalEngine  # noqa: PLC0415
        from movate.providers.mock import MockProvider  # noqa: PLC0415
        from movate.providers.pricing import load_pricing  # noqa: PLC0415

        bundle = self._agents.get(job.target)
        if bundle is None:
            return _error(
                "unknown_agent",
                f"agent {job.target!r} not registered on this worker",
                retryable=False,
            )

        cfg = job.input
        use_mock: bool = self._use_mock_for_eval or bool(cfg.get("mock", False))

        if use_mock:
            from movate.providers.mock import MockProvider as _Mock  # noqa: PLC0415

            provider: Any = _Mock()
        else:
            from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

            provider = LiteLLMProvider()

        from movate.tracing import build_tracer  # noqa: PLC0415

        pricing = load_pricing()
        executor = Executor(
            provider=provider,
            pricing=pricing,
            storage=self._storage,
            tracer=build_tracer(),
            tenant_id=job.tenant_id,
        )

        try:
            engine = EvalEngine(
                executor=executor,
                provider=provider,
                runs_per_case=int(cfg.get("runs", 1)),
                gate_mode=str(cfg.get("gate_mode", "mean")),
                objective_filter=cfg.get("objective") or None,
            )
            summary = await engine.run(bundle)
        except EvalConfigError as exc:
            return _error("eval_config", str(exc), retryable=False)
        except Exception as exc:
            logger.exception("eval_execute_unhandled job_id=%s", job.job_id)
            return _error("internal", str(exc), retryable=True)

        record = summary.to_record(tenant_id=job.tenant_id)
        await self._storage.save_eval(record)

        # Store eval_id in result_run_id — field is a generic "result
        # identifier"; the API contract documents this mapping for EVAL jobs.
        return DispatchOutcome(
            status=JobStatus.SUCCESS,
            result_run_id=record.eval_id,
            error=None,
        )

    async def _execute_workflow(self, job: JobRecord) -> DispatchOutcome:
        graph = self._workflows.get(job.target)
        if graph is None:
            return _error(
                "unknown_workflow",
                f"workflow {job.target!r} not registered on this worker",
                retryable=False,
            )
        # Same tenant-scoping fix as the agent path: workers run jobs from
        # many tenants through one Executor; the workflow runner must stamp
        # the job's tenant on every node's RunRecord, not the executor's
        # construction-time default.
        runner = WorkflowRunner(
            executor=self._executor,
            storage=self._storage,
            tenant_id=job.tenant_id,
        )
        try:
            result = await runner.run(graph, initial_state=job.input)
        except Exception as exc:
            logger.exception("workflow_execute_unhandled job_id=%s", job.job_id)
            return _error("internal", str(exc), retryable=True)

        if result.status == WorkflowStatus.SUCCESS:
            return DispatchOutcome(
                status=JobStatus.SUCCESS,
                result_run_id=result.workflow_run_id,
                error=None,
            )
        # Workflow can only land in SUCCESS or ERROR per WorkflowStatus.
        return DispatchOutcome(
            status=JobStatus.ERROR,
            result_run_id=result.workflow_run_id,
            error=result.error.model_dump() if result.error else None,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(
    kind: str,
    message: str,
    *,
    retryable: bool,
    hint: str | None = None,
) -> DispatchOutcome:
    """Build an ``ERROR`` outcome from a structured failure tuple.

    ``hint`` is surfaced through to the persisted JobRecord (and any
    poller staring at ``GET /api/v1/jobs/{id}``). Used to point callers
    at the runbook for known-recurring failure classes — currently the
    cross-pod bundle-sync gap (``unknown_agent``)."""
    return DispatchOutcome(
        status=JobStatus.ERROR,
        result_run_id=None,
        error=ErrorInfo(
            type=kind,
            message=message,
            retryable=retryable,
            hint=hint,
        ).model_dump(),
    )


__all__ = ["DispatchOutcome", "WorkerDispatch"]
