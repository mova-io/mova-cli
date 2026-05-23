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
from movate.core.notify import NotificationDispatcher
from movate.core.workflow import WorkflowGraph, WorkflowRunner
from movate.runtime.agent_resolver import resolve_agent_bundle
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
        notifier: NotificationDispatcher | None = None,
    ) -> None:
        self._storage = storage
        self._executor = executor
        self._agents: dict[str, AgentBundle] = {b.spec.name: b for b in (agents or [])}
        # The filesystem-scanned bundles double as the resolver's fallback
        # list — kept as a plain list so resolve_agent_bundle can scan it
        # when the durable registry has no row for the job's tenant.
        self._agents_fallback: list[AgentBundle] = list(agents or [])
        self._workflows: dict[str, WorkflowGraph] = workflows or {}
        self._use_mock_for_eval = use_mock_for_eval
        self._notifier = notifier
        """Optional :class:`NotificationDispatcher` for drift alerts (ADR
        016 D2). When an eval job completes, the dispatch compares the new
        EvalRecord to a baseline; on regression it logs ``eval_drift_detected``
        and (if a notifier is wired) dispatches an alert. ``None`` → the
        structured log still fires, no email/console alert. Backwards
        compatible: existing callers that don't pass a notifier are
        unaffected, and non-scheduled evals without a baseline never drift."""
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
        if job.kind == JobKind.BENCH:
            return await self._execute_bench(job)
        return _error(
            "unknown_kind",
            f"unsupported JobKind {job.kind!r}",
            retryable=False,
        )

    async def _resolve_bundle(self, job: JobRecord) -> AgentBundle | None:
        """Resolve ``job.target`` to a runnable bundle for ``job.tenant_id``.

        Registry-first (so the worker sees what the API published —
        the #109 fix), with the filesystem-scanned bundles as the
        fallback (local ``mdk worker --agents ./dir`` + the existing
        tests, which carry no durable registry row). The job's
        ``tenant_id`` scopes the registry read so the worker reads the
        same tenant the API published under.
        """
        return await resolve_agent_bundle(
            self._storage,
            job.target,
            tenant_id=job.tenant_id,
            fallback=self._agents_fallback,
        )

    async def _execute_agent(self, job: JobRecord) -> DispatchOutcome:
        bundle = await self._resolve_bundle(job)
        if bundle is None:
            # The agent resolved in neither the durable registry (for
            # this job's tenant) nor the worker's local filesystem
            # fallback — so it genuinely isn't published. ADR 014 D2
            # closed the old cross-pod sync gap (BACKLOG #109): an agent
            # created via POST /api/v1/agents now lands in the shared
            # registry and the worker resolves it directly. A miss here
            # means the name/tenant is wrong or the agent was never
            # published, not a sync lag.
            return _error(
                "unknown_agent",
                f"agent {job.target!r} not registered for tenant {job.tenant_id!r}",
                retryable=False,
                hint=(
                    "no bundle for this agent in the durable registry "
                    "(scoped to the job's tenant) nor on the worker's "
                    "filesystem. confirm the agent was published via "
                    "POST /api/v1/agents under the same tenant, and that "
                    "the name matches."
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
                # Propagate the thread linkage onto the spawned run
                # so multi-turn agents can later list this turn via
                # list_runs_for_thread. ``None`` (the common case for
                # standalone runs) is a no-op.
                thread_id=job.thread_id,
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
        from movate.providers.pricing import load_pricing  # noqa: PLC0415

        bundle = await self._resolve_bundle(job)
        if bundle is None:
            return _error(
                "unknown_agent",
                f"agent {job.target!r} not registered for tenant {job.tenant_id!r}",
                retryable=False,
            )

        cfg = job.input
        use_mock: bool = self._use_mock_for_eval or bool(cfg.get("mock", False))

        if use_mock:
            from movate.providers.mock import MockProvider  # noqa: PLC0415

            provider: Any = MockProvider()
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

        # ADR 016 D2 — continuous-eval drift check. Runs when the eval job
        # asks for it: either a scheduled eval (``scheduled=True``, set by
        # the scheduler tick) or any eval that pinned a ``baseline_id``.
        # Ad-hoc evals with neither carry no baseline intent → no drift
        # check, byte-for-byte the old behaviour. Best-effort: a drift /
        # alert failure never changes the job's SUCCESS outcome.
        await self._maybe_check_drift(job, record)

        # Store eval_id in result_run_id — field is a generic "result
        # identifier"; the API contract documents this mapping for EVAL jobs.
        return DispatchOutcome(
            status=JobStatus.SUCCESS,
            result_run_id=record.eval_id,
            error=None,
        )

    async def _maybe_check_drift(self, job: JobRecord, record: Any) -> None:
        """Compare a fresh EvalRecord to a baseline and alert on regression.

        Wrapped in a broad try/except: drift detection + alerting are a
        courtesy layer over a SUCCESS eval; nothing here may flip the job's
        outcome or raise into the worker loop.
        """
        from movate.core.drift import alert_on_drift, detect_drift, select_baseline  # noqa: PLC0415

        cfg = job.input
        scheduled = bool(cfg.get("scheduled", False))
        baseline_id = cfg.get("baseline_id") or None
        if not scheduled and baseline_id is None:
            return  # ad-hoc eval, no baseline intent — nothing to diff

        try:
            tolerance = float(cfg.get("regression_tolerance", 0.05))
            # The agent's eval history (newest-first); includes the row we
            # just saved, which select_baseline excludes by eval_id.
            history = await self._storage.list_evals(
                tenant_id=job.tenant_id,
                agent=record.agent,
                limit=50,
            )
            baseline = select_baseline(
                current=record,
                candidates=history,
                baseline_id=baseline_id,
            )
            result = detect_drift(record, baseline, tolerance=tolerance)
            logger.info("eval_drift_check %s", result.summary())
            await alert_on_drift(
                result,
                notifier=self._notifier,
                notify_email=cfg.get("notify_email") or job.notify_email,
            )
        except Exception:
            logger.warning(
                "eval_drift_check_failed job_id=%s agent=%s — eval result is "
                "unchanged; this is the drift/alert path only",
                job.job_id,
                record.agent,
                exc_info=True,
            )

    async def _execute_bench(self, job: JobRecord) -> DispatchOutcome:
        """Run an async multi-model bench job (BACKLOG #64).

        ``job.input`` carries the bench config (the same fields as
        :class:`movate.runtime.schemas.BenchSubmission`): ``models``,
        ``judge``, ``rubric``, ``mock``, ``runs``, ``gate_mode``,
        ``input``. The completed :class:`BenchRecord` is persisted via
        storage; ``result_run_id`` is set to the ``bench_id`` so callers
        can retrieve it via ``GET /api/v1/bench/{bench_id}`` — mirrors
        the EVAL job's eval_id mapping.
        """
        from movate.core.bench import BenchEngine  # noqa: PLC0415
        from movate.core.eval import EvalConfigError  # noqa: PLC0415
        from movate.core.models import JudgeConfig, JudgeMethod, ModelConfig  # noqa: PLC0415
        from movate.providers.pricing import load_pricing  # noqa: PLC0415

        bundle = await self._resolve_bundle(job)
        if bundle is None:
            return _error(
                "unknown_agent",
                f"agent {job.target!r} not registered for tenant {job.tenant_id!r}",
                retryable=False,
            )

        cfg = job.input
        models = [str(m) for m in (cfg.get("models") or [])]
        if not models:
            return _error("bench_config", "bench requires at least one model", retryable=False)

        # Reuse the EVAL job's mock policy: the server-level
        # use_mock_for_eval flag forces mock for both eval + bench jobs in
        # test environments; otherwise the per-job ``mock`` flag decides.
        use_mock: bool = self._use_mock_for_eval or bool(cfg.get("mock", False))
        if use_mock:
            from movate.providers.mock import MockProvider  # noqa: PLC0415

            provider: Any = MockProvider()
        else:
            from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

            provider = LiteLLMProvider()

        from movate.tracing import build_tracer  # noqa: PLC0415

        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=self._storage,
            tracer=build_tracer(),
            tenant_id=job.tenant_id,
        )

        # Quality scoring is opt-in: only when the submission carried both
        # a judge provider and an inline rubric (matches the CLI's
        # resolution). Otherwise the bench reports cost + latency only.
        judge_provider = cfg.get("judge")
        rubric = cfg.get("rubric")
        judge: JudgeConfig | None = None
        if judge_provider and rubric:
            judge = JudgeConfig(
                method=JudgeMethod.LLM_JUDGE,
                model=ModelConfig(provider=str(judge_provider)),
                rubric=str(rubric),
            )

        input_payload = cfg.get("input")
        if not isinstance(input_payload, dict):
            return _error("bench_config", "bench requires an 'input' object", retryable=False)

        try:
            engine = BenchEngine(
                executor=executor,
                provider=provider,
                runs_per_model=int(cfg.get("runs", 1)),
                gate_mode=str(cfg.get("gate_mode", "mean")),
                judge=judge,
                rubric=str(rubric) if rubric else None,
            )
            summary = await engine.run(bundle, input_payload=input_payload, providers=models)
        except EvalConfigError as exc:
            return _error("bench_config", str(exc), retryable=False)
        except Exception as exc:
            logger.exception("bench_execute_unhandled job_id=%s", job.job_id)
            return _error("internal", str(exc), retryable=True)

        # Honor the API-pre-generated bench_id when present (so the
        # caller's GET /bench/{bench_id} hits the persisted row); fall
        # back to a fresh uuid for direct/local callers.
        pregenerated = cfg.get("bench_id")
        record = summary.to_record(
            tenant_id=job.tenant_id,
            bench_id=str(pregenerated) if pregenerated else None,
        )
        await self._storage.save_bench(record)

        # bench_id rides result_run_id (generic "result identifier"); the
        # API contract documents this mapping for BENCH jobs.
        return DispatchOutcome(
            status=JobStatus.SUCCESS,
            result_run_id=record.bench_id,
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
