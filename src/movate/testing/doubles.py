"""Test doubles: in-memory storage, null tracer, scripted judge provider.

These mirror the real implementations' protocols closely enough that they
satisfy mypy strict against ``StorageProvider`` / ``Tracer`` /
``BaseLLMProvider`` without copying production code into ``tests/``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from movate.core.models import (
    ApiKeyRecord,
    EvalRecord,
    FailureRecord,
    JobRecord,
    JobStatus,
    RunRecord,
    TenantBudget,
    WorkflowRunRecord,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from movate.tracing.base import SpanCtx, Tracer


class InMemoryStorage:
    """In-memory implementation of :class:`movate.storage.base.StorageProvider`.

    Records are kept in plain lists for direct assertion in tests
    (``assert len(storage.runs) == 1``). ``init`` and ``close`` are no-ops.
    """

    name = "in_memory"

    def __init__(self) -> None:
        self.runs: list[RunRecord] = []
        self.failures: list[FailureRecord] = []
        self.evals: list[EvalRecord] = []
        self.workflow_runs: list[WorkflowRunRecord] = []
        self.jobs: list[JobRecord] = []
        self.api_keys: list[ApiKeyRecord] = []
        self.tenant_budgets: dict[str, TenantBudget] = {}

    async def init(self) -> None:
        return None

    async def ping(self) -> None:
        """No-op for the in-memory double — there's no backend to
        check. Tests that exercise the ``/ready`` failure path use a
        custom subclass that overrides this to raise."""
        return None

    async def save_run(self, run: RunRecord) -> None:
        self.runs.append(run)

    async def save_failure(self, f: FailureRecord) -> None:
        self.failures.append(f)

    async def save_eval(self, e: EvalRecord) -> None:
        self.evals.append(e)

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None:
        self.workflow_runs.append(w)

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        return next(
            (r for r in self.runs if r.run_id == run_id and r.tenant_id == tenant_id),
            None,
        )

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        return next(
            (
                w
                for w in self.workflow_runs
                if w.workflow_run_id == workflow_run_id and w.tenant_id == tenant_id
            ),
            None,
        )

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        return next(
            (e for e in self.evals if e.eval_id == eval_id and e.tenant_id == tenant_id),
            None,
        )

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]:
        rows = self.runs
        if agent:
            rows = [r for r in rows if r.agent == agent]
        if tenant_id:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        if status:
            rows = [r for r in rows if r.status.value == status]
        if workflow_run_id:
            rows = [r for r in rows if r.workflow_run_id == workflow_run_id]
        return list(rows)[:limit]

    async def list_evals(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        rows = self.evals
        if tenant_id is not None:
            rows = [e for e in rows if e.tenant_id == tenant_id]
        if agent:
            rows = [e for e in rows if e.agent == agent]
        return list(rows)[:limit]

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        rows = self.workflow_runs
        if tenant_id is not None:
            rows = [w for w in rows if w.tenant_id == tenant_id]
        if workflow:
            rows = [w for w in rows if w.workflow == workflow]
        return list(rows)[:limit]

    # ------------------------------------------------------------------
    # Jobs (v0.5)
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        if any(j.job_id == job.job_id for j in self.jobs):
            raise ValueError(f"duplicate job_id {job.job_id!r}")
        self.jobs.append(job)

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        return next(
            (j for j in self.jobs if j.job_id == job_id and j.tenant_id == tenant_id),
            None,
        )

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        rows = self.jobs
        if tenant_id:
            rows = [j for j in rows if j.tenant_id == tenant_id]
        if status:
            rows = [j for j in rows if j.status == status]
        # Newest-first to match SqliteProvider's ORDER BY.
        return sorted(rows, key=lambda j: j.created_at, reverse=True)[:limit]

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """In-memory claim: oldest queued, optionally tenant-scoped.

        Async coroutines on a single event loop don't preempt mid-method,
        so the SELECT-then-UPDATE pair here is atomic by construction —
        no lock needed. The Sqlite/Postgres providers carry the actual
        concurrency story; this double exists to test calling code.

        Retry-aware: skips jobs whose ``next_retry_at`` is in the
        future. Matches the sqlite/postgres claim semantics.
        """
        now = datetime.now(UTC)
        candidates = [
            j
            for j in self.jobs
            if j.status == JobStatus.QUEUED
            and (tenant_id is None or j.tenant_id == tenant_id)
            and (j.next_retry_at is None or j.next_retry_at <= now)
        ]
        if not candidates:
            return None
        oldest = min(candidates, key=lambda j: j.created_at)
        # Mutate via Pydantic.copy so we don't lose `extra="forbid"` enforcement.
        idx = self.jobs.index(oldest)
        claimed = oldest.model_copy(update={"status": JobStatus.RUNNING, "claimed_at": now})
        self.jobs[idx] = claimed
        return claimed

    async def update_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        status: JobStatus,
        result_run_id: str | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        if status not in (
            JobStatus.SUCCESS,
            JobStatus.ERROR,
            JobStatus.SAFETY_BLOCKED,
            JobStatus.DEAD_LETTER,
        ):
            raise ValueError(f"update_job only accepts terminal statuses; got {status!r}")
        for i, j in enumerate(self.jobs):
            if j.job_id == job_id and j.tenant_id == tenant_id:
                from movate.core.models import ErrorInfo  # noqa: PLC0415

                self.jobs[i] = j.model_copy(
                    update={
                        "status": status,
                        "result_run_id": result_run_id,
                        "error": ErrorInfo.model_validate(error) if error else None,
                        "completed_at": datetime.now(UTC),
                    }
                )
                return
        # Silently no-op on tenant mismatch — matches sqlite/postgres
        # behavior where the WHERE clause filters out the row. (We
        # used to raise on "no job found"; that left a side channel
        # for cross-tenant id probing.)
        return

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Re-queue a job for retry after a transient failure.

        Matches the storage Protocol — flips RUNNING → QUEUED, clears
        claimed_at, stamps the new attempt_count + next_retry_at.
        Silently no-ops on tenant mismatch (same rationale as
        update_job).
        """
        for i, j in enumerate(self.jobs):
            if j.job_id == job_id and j.tenant_id == tenant_id:
                self.jobs[i] = j.model_copy(
                    update={
                        "status": JobStatus.QUEUED,
                        "claimed_at": None,
                        "attempt_count": attempt_count,
                        "next_retry_at": next_retry_at,
                    }
                )
                return
        return

    # ------------------------------------------------------------------
    # API keys (v0.5 stage 2)
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        if any(k.key_id == key.key_id for k in self.api_keys):
            raise ValueError(f"duplicate key_id {key.key_id!r}")
        self.api_keys.append(key)

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        return next((k for k in self.api_keys if k.key_id == key_id), None)

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        rows = self.api_keys
        if tenant_id is not None:
            rows = [k for k in rows if k.tenant_id == tenant_id]
        if not include_revoked:
            rows = [k for k in rows if k.revoked_at is None]
        return sorted(rows, key=lambda k: k.created_at, reverse=True)

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        for i, k in enumerate(self.api_keys):
            if k.key_id == key_id and k.tenant_id == tenant_id and k.revoked_at is None:
                self.api_keys[i] = k.model_copy(update={"revoked_at": datetime.now(UTC)})
                return
        # Idempotent + tenant-scoped: silently no-op on missing,
        # cross-tenant, or already-revoked.

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        for i, k in enumerate(self.api_keys):
            if k.key_id == key_id and k.tenant_id == tenant_id:
                self.api_keys[i] = k.model_copy(update={"last_used_at": datetime.now(UTC)})
                return

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        return self.tenant_budgets.get(tenant_id)

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        # Preserve created_at on update (mirrors sqlite/postgres
        # ``ON CONFLICT DO UPDATE`` semantics).
        existing = self.tenant_budgets.get(budget.tenant_id)
        if existing is not None:
            self.tenant_budgets[budget.tenant_id] = budget.model_copy(
                update={"created_at": existing.created_at, "updated_at": datetime.now(UTC)}
            )
        else:
            self.tenant_budgets[budget.tenant_id] = budget

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        return sorted(self.tenant_budgets.values(), key=lambda b: b.created_at)

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for run in self.runs:
            if run.tenant_id != tenant_id:
                continue
            if run.created_at < month_start:
                continue
            total += run.metrics.cost_usd
        return total

    async def close(self) -> None:
        return None


class NullTracer(Tracer):
    """Tracer that captures spans + events in lists for assertion.

    Use ``tracer.events`` to assert observability hooks fired (e.g.
    ``fallback_triggered``, ``cost_drift``).
    """

    name = "null"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.ended_status: list[str] = []

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        return SpanCtx(
            trace_id="trace-x",
            name=name,
            attributes=dict(attrs or {}),
            parent_id=parent.span_id if parent else None,
        )

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        self.ended_status.append(status)

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        self.events.append(event)

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value


class JudgeStubProvider(BaseLLMProvider):
    """Provider double that splits behavior by prompt content.

    * If the prompt contains ``Rubric:`` (i.e. an LLM-as-judge call), returns
      a JSON object with the configured ``judge_score`` + a ``"stub"`` rationale.
    * Otherwise returns the configured ``agent_response`` verbatim.

    Captures every provider string seen in ``calls`` and every judge prompt
    body in ``judge_prompts`` so tests can assert which path ran and what
    rubric was used.
    """

    name = "judge_stub"
    version = "0.0.1"

    def __init__(self, *, agent_response: str, judge_score: float) -> None:
        self._agent_response = agent_response
        self._judge_score = judge_score
        self.calls: list[str] = []
        self.judge_prompts: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request.provider)
        body = request.messages[0].content if request.messages else ""
        if "Rubric:" in body:
            self.judge_prompts.append(body)
            return CompletionResponse(
                text=f'{{"score": {self._judge_score}, "rationale": "stub"}}',
            )
        return CompletionResponse(text=self._agent_response)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream by yielding the same response as :meth:`complete`
        in two slices, so tests that exercise the executor's
        streaming branch see ≥ 1 mid-stream chunk plus a final
        usage chunk."""
        resp = await self.complete(request)
        # Mid-stream chunk: the whole text in one slice.
        yield StreamChunk(text=resp.text)
        # Final chunk: zero text, populated tokens (mirrors LiteLLM
        # include_usage=True behaviour).
        yield StreamChunk(text="", tokens=resp.tokens)

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError
