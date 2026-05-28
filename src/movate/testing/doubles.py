"""Test doubles: in-memory storage, null tracer, scripted judge provider.

These mirror the real implementations' protocols closely enough that they
satisfy mypy strict against ``StorageProvider`` / ``Tracer`` /
``BaseLLMProvider`` without copying production code into ``tests/``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from movate.core.dr_backup import ImportResult
from movate.core.events import Event
from movate.core.job_retry import ReclaimResult
from movate.core.models import (
    AgentBundleRecord,
    ApiKeyRecord,
    BatchRecord,
    BenchRecord,
    CanaryConfig,
    ConversationThread,
    Entity,
    EntityWithScore,
    EvalRecord,
    EvalSchedule,
    FailureRecord,
    FeedbackRecord,
    JobRecord,
    JobSchedule,
    JobStatus,
    KbChunk,
    KbChunkWithScore,
    Relation,
    RunRecord,
    Subgraph,
    TenantBudget,
    TenantProviderKey,
    Trigger,
    WorkflowRunRecord,
    WorkflowStatus,
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
        self.bench: list[BenchRecord] = []
        self.agent_bundles: list[AgentBundleRecord] = []
        self.workflow_runs: list[WorkflowRunRecord] = []
        self.jobs: list[JobRecord] = []
        self.batches: list[BatchRecord] = []
        self.api_keys: list[ApiKeyRecord] = []
        self.tenant_budgets: dict[str, TenantBudget] = {}
        self.feedback: list[FeedbackRecord] = []
        self.kb_chunks: list[KbChunk] = []
        self.entities: list[Entity] = []
        self.relations: list[Relation] = []
        self.conversation_threads: list[ConversationThread] = []
        self.eval_schedules: list[EvalSchedule] = []
        self.job_schedules: list[JobSchedule] = []
        self.triggers: list[Trigger] = []
        # ADR 018: per-tenant BYOK provider keys (encrypted-at-rest ciphertext),
        # keyed by (tenant_id, provider). Empty by default → no-config run path.
        self.tenant_provider_keys: list[TenantProviderKey] = []
        # item 23: trigger delivery dedup, keyed by (trigger_id, delivery_id)
        # → the job_id the first delivery enqueued.
        self.trigger_deliveries: dict[tuple[str, str], str] = {}
        # item 37: submission idempotency, keyed by (tenant_id, idempotency_key)
        # → the job_id the first async submit enqueued.
        self.run_submissions: dict[tuple[str, str], str] = {}
        self.canary_configs: list[CanaryConfig] = []
        # ADR 035 D1: events outbox. Appended in record_event order;
        # list_events sorts oldest-first by (created_at, id).
        self.events: list[Event] = []

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

    async def save_bench(self, b: BenchRecord) -> None:
        self.bench.append(b)

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None:
        # Upsert on workflow_run_id (the PRIMARY KEY in sqlite/postgres). A
        # resume (ADR 017 D5, PR 2) re-saves the SAME id when a paused run
        # continues, and the signal endpoint persists the merged checkpoint
        # back under the same id — replace-in-place so get_workflow_run reads
        # a single source of truth (no duplicate rows), matching the DB
        # providers' ON CONFLICT DO UPDATE.
        for i, existing in enumerate(self.workflow_runs):
            if existing.workflow_run_id == w.workflow_run_id:
                self.workflow_runs[i] = w
                return
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

    async def get_bench(self, bench_id: str, *, tenant_id: str) -> BenchRecord | None:
        return next(
            (b for b in self.bench if b.bench_id == bench_id and b.tenant_id == tenant_id),
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

    async def list_bench(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[BenchRecord]:
        rows = self.bench
        if tenant_id is not None:
            rows = [b for b in rows if b.tenant_id == tenant_id]
        if agent:
            rows = [b for b in rows if b.agent == agent]
        # Newest-first to match the SQL backends' ORDER BY.
        return sorted(rows, key=lambda b: b.created_at, reverse=True)[:limit]

    # ------------------------------------------------------------------
    # Eval schedules (ADR 016 D2)
    # ------------------------------------------------------------------

    async def save_eval_schedule(self, schedule: EvalSchedule) -> None:
        # Upsert keyed by (tenant_id, agent) — last write wins.
        self.eval_schedules = [
            s
            for s in self.eval_schedules
            if not (s.agent == schedule.agent and s.tenant_id == schedule.tenant_id)
        ]
        self.eval_schedules.append(schedule)

    async def get_eval_schedule(self, agent: str, *, tenant_id: str) -> EvalSchedule | None:
        return next(
            (s for s in self.eval_schedules if s.agent == agent and s.tenant_id == tenant_id),
            None,
        )

    async def list_eval_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[EvalSchedule]:
        rows = self.eval_schedules
        if tenant_id is not None:
            rows = [s for s in rows if s.tenant_id == tenant_id]
        return sorted(rows, key=lambda s: s.created_at, reverse=True)[:limit]

    async def delete_eval_schedule(self, agent: str, *, tenant_id: str) -> bool:
        before = len(self.eval_schedules)
        self.eval_schedules = [
            s for s in self.eval_schedules if not (s.agent == agent and s.tenant_id == tenant_id)
        ]
        return len(self.eval_schedules) < before

    async def touch_eval_schedule(
        self,
        agent: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        for i, s in enumerate(self.eval_schedules):
            if s.agent == agent and s.tenant_id == tenant_id:
                self.eval_schedules[i] = s.model_copy(update={"last_enqueued_at": last_enqueued_at})
                return

    # ------------------------------------------------------------------
    # Job schedules (ADR 017 D2)
    # ------------------------------------------------------------------

    async def save_job_schedule(self, schedule: JobSchedule) -> None:
        # Upsert keyed by (tenant_id, name) — last write wins.
        self.job_schedules = [
            s
            for s in self.job_schedules
            if not (s.name == schedule.name and s.tenant_id == schedule.tenant_id)
        ]
        self.job_schedules.append(schedule)

    async def get_job_schedule(self, name: str, *, tenant_id: str) -> JobSchedule | None:
        return next(
            (s for s in self.job_schedules if s.name == name and s.tenant_id == tenant_id),
            None,
        )

    async def list_job_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[JobSchedule]:
        rows = self.job_schedules
        if tenant_id is not None:
            rows = [s for s in rows if s.tenant_id == tenant_id]
        return sorted(rows, key=lambda s: s.created_at, reverse=True)[:limit]

    async def delete_job_schedule(self, name: str, *, tenant_id: str) -> bool:
        before = len(self.job_schedules)
        self.job_schedules = [
            s for s in self.job_schedules if not (s.name == name and s.tenant_id == tenant_id)
        ]
        return len(self.job_schedules) < before

    async def touch_job_schedule(
        self,
        name: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        for i, s in enumerate(self.job_schedules):
            if s.name == name and s.tenant_id == tenant_id:
                self.job_schedules[i] = s.model_copy(update={"last_enqueued_at": last_enqueued_at})
                return

    # ------------------------------------------------------------------
    # Triggers (ADR 017 D2 — inbound event/webhook → enqueue a job)
    # ------------------------------------------------------------------

    async def save_trigger(self, trigger: Trigger) -> None:
        # Upsert keyed by (tenant_id, name) — last write wins.
        self.triggers = [
            t
            for t in self.triggers
            if not (t.name == trigger.name and t.tenant_id == trigger.tenant_id)
        ]
        self.triggers.append(trigger)

    async def get_trigger(self, name: str, *, tenant_id: str) -> Trigger | None:
        return next(
            (t for t in self.triggers if t.name == name and t.tenant_id == tenant_id),
            None,
        )

    async def get_trigger_by_id(self, trigger_id: str) -> Trigger | None:
        return next((t for t in self.triggers if t.trigger_id == trigger_id), None)

    async def list_triggers(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[Trigger]:
        rows = self.triggers
        if tenant_id is not None:
            rows = [t for t in rows if t.tenant_id == tenant_id]
        return sorted(rows, key=lambda t: t.created_at, reverse=True)[:limit]

    async def delete_trigger(self, name: str, *, tenant_id: str) -> bool:
        before = len(self.triggers)
        self.triggers = [
            t for t in self.triggers if not (t.name == name and t.tenant_id == tenant_id)
        ]
        return len(self.triggers) < before

    async def touch_trigger(self, trigger_id: str, *, last_fired_at: datetime) -> None:
        for i, t in enumerate(self.triggers):
            if t.trigger_id == trigger_id:
                self.triggers[i] = t.model_copy(update={"last_fired_at": last_fired_at})
                return

    async def get_trigger_delivery(self, trigger_id: str, delivery_id: str) -> str | None:
        return self.trigger_deliveries.get((trigger_id, delivery_id))

    async def record_trigger_delivery(self, trigger_id: str, delivery_id: str, job_id: str) -> bool:
        # setdefault mirrors the DB's atomic INSERT-OR-IGNORE: only the first
        # write for a key lands; a later one finds the row and is a no-op.
        key = (trigger_id, delivery_id)
        if key in self.trigger_deliveries:
            return False
        self.trigger_deliveries[key] = job_id
        return True

    async def get_run_submission(self, tenant_id: str, idempotency_key: str) -> str | None:
        return self.run_submissions.get((tenant_id, idempotency_key))

    async def record_run_submission(
        self, tenant_id: str, idempotency_key: str, job_id: str
    ) -> bool:
        # Mirrors the DB's atomic INSERT-OR-IGNORE: only the first write for a
        # key lands; a later one finds the row and is a no-op.
        key = (tenant_id, idempotency_key)
        if key in self.run_submissions:
            return False
        self.run_submissions[key] = job_id
        return True

    # ------------------------------------------------------------------
    # Tenant provider keys (ADR 018 — per-tenant BYOK provider credentials)
    # ------------------------------------------------------------------

    async def save_tenant_provider_key(self, key: TenantProviderKey) -> None:
        # Upsert keyed by (tenant_id, provider) — last write wins (rotation).
        self.tenant_provider_keys = [
            k
            for k in self.tenant_provider_keys
            if not (k.provider == key.provider and k.tenant_id == key.tenant_id)
        ]
        self.tenant_provider_keys.append(key)

    async def get_tenant_provider_key(
        self, provider: str, *, tenant_id: str
    ) -> TenantProviderKey | None:
        return next(
            (
                k
                for k in self.tenant_provider_keys
                if k.provider == provider and k.tenant_id == tenant_id
            ),
            None,
        )

    async def list_tenant_provider_keys(self, *, tenant_id: str) -> list[TenantProviderKey]:
        rows = [k for k in self.tenant_provider_keys if k.tenant_id == tenant_id]
        return sorted(rows, key=lambda k: k.provider)

    async def list_all_tenant_provider_keys(
        self, *, limit: int = 100_000
    ) -> list[TenantProviderKey]:
        rows = sorted(self.tenant_provider_keys, key=lambda k: (k.tenant_id, k.provider))
        return rows[:limit]

    async def delete_tenant_provider_key(self, provider: str, *, tenant_id: str) -> bool:
        before = len(self.tenant_provider_keys)
        self.tenant_provider_keys = [
            k
            for k in self.tenant_provider_keys
            if not (k.provider == provider and k.tenant_id == tenant_id)
        ]
        return len(self.tenant_provider_keys) < before

    # ------------------------------------------------------------------
    # Canary configs (ADR 016 D3 — champion/challenger rollout)
    # ------------------------------------------------------------------

    async def save_canary_config(self, config: CanaryConfig) -> None:
        # Upsert keyed by (tenant_id, agent) — last write wins.
        self.canary_configs = [
            c
            for c in self.canary_configs
            if not (c.agent == config.agent and c.tenant_id == config.tenant_id)
        ]
        self.canary_configs.append(config)

    async def get_canary_config(self, agent: str, *, tenant_id: str) -> CanaryConfig | None:
        return next(
            (c for c in self.canary_configs if c.agent == agent and c.tenant_id == tenant_id),
            None,
        )

    async def list_canary_configs(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[CanaryConfig]:
        rows = self.canary_configs
        if tenant_id is not None:
            rows = [c for c in rows if c.tenant_id == tenant_id]
        return sorted(rows, key=lambda c: c.created_at, reverse=True)[:limit]

    async def delete_canary_config(self, agent: str, *, tenant_id: str) -> bool:
        before = len(self.canary_configs)
        self.canary_configs = [
            c for c in self.canary_configs if not (c.agent == agent and c.tenant_id == tenant_id)
        ]
        return len(self.canary_configs) < before

    # ------------------------------------------------------------------
    # Agent registry (ADR 014 D1)
    # ------------------------------------------------------------------

    async def save_agent_bundle(self, bundle: AgentBundleRecord) -> None:
        self.agent_bundles.append(bundle)

    async def get_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> AgentBundleRecord | None:
        matches = [b for b in self.agent_bundles if b.name == name and b.tenant_id == tenant_id]
        if version is not None:
            matches = [b for b in matches if b.version == version]
        if not matches:
            return None
        # version=None → latest by created_at (matches the SQL backends).
        return max(matches, key=lambda b: b.created_at)

    async def list_agents(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[AgentBundleRecord]:
        rows = [b for b in self.agent_bundles if b.tenant_id == tenant_id]
        # Latest version per name.
        latest: dict[str, AgentBundleRecord] = {}
        for b in rows:
            current = latest.get(b.name)
            if current is None or b.created_at > current.created_at:
                latest[b.name] = b
        ordered = sorted(latest.values(), key=lambda b: b.created_at, reverse=True)
        return ordered[:limit]

    async def list_agent_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[AgentBundleRecord]:
        rows = [b for b in self.agent_bundles if b.name == name and b.tenant_id == tenant_id]
        return sorted(rows, key=lambda b: b.created_at, reverse=True)[:limit]

    async def list_all_agent_bundles(self, *, limit: int = 100_000) -> list[AgentBundleRecord]:
        rows = sorted(self.agent_bundles, key=lambda b: (b.tenant_id, b.name, b.created_at))
        return rows[:limit]

    async def delete_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        def _matches(b: AgentBundleRecord) -> bool:
            if b.name != name or b.tenant_id != tenant_id:
                return False
            return version is None or b.version == version

        to_delete = [b for b in self.agent_bundles if _matches(b)]
        self.agent_bundles = [b for b in self.agent_bundles if not _matches(b)]
        return len(to_delete)

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        rows = self.workflow_runs
        if tenant_id is not None:
            rows = [w for w in rows if w.tenant_id == tenant_id]
        if workflow:
            rows = [w for w in rows if w.workflow == workflow]
        if status is not None:
            rows = [w for w in rows if w.status == status]
        return list(rows)[:limit]

    # ------------------------------------------------------------------
    # Batches (item 17 — batch inference)
    # ------------------------------------------------------------------

    async def save_batch(self, batch: BatchRecord) -> None:
        if any(b.batch_id == batch.batch_id for b in self.batches):
            raise ValueError(f"duplicate batch_id {batch.batch_id!r}")
        self.batches.append(batch)

    async def get_batch(self, batch_id: str, *, tenant_id: str) -> BatchRecord | None:
        return next(
            (b for b in self.batches if b.batch_id == batch_id and b.tenant_id == tenant_id),
            None,
        )

    async def list_batches(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 20,
    ) -> list[BatchRecord]:
        rows = self.batches
        if tenant_id is not None:
            rows = [b for b in rows if b.tenant_id == tenant_id]
        return sorted(rows, key=lambda b: b.created_at, reverse=True)[:limit]

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
        target: str | None = None,
        batch_id: str | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        rows = self.jobs
        if tenant_id:
            rows = [j for j in rows if j.tenant_id == tenant_id]
        if status:
            rows = [j for j in rows if j.status == status]
        if target:
            rows = [j for j in rows if j.target == target]
        if batch_id:
            rows = [j for j in rows if j.batch_id == batch_id]
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
            JobStatus.CANCELLED,
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

    async def reclaim_stale_jobs(
        self,
        *,
        older_than: datetime,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> ReclaimResult:
        """Reclaim orphaned ``RUNNING`` jobs — cross-tenant, in-memory.

        Iterates the jobs list applying the same rules as the SQL
        backends: budget-exhausted stale rows → ``DEAD_LETTER``; the rest
        → ``QUEUED`` with ``attempt_count`` bumped and immediate re-claim
        eligibility (``next_retry_at = now``). Single event loop means
        this is atomic by construction.
        """
        from movate.core.models import ErrorInfo  # noqa: PLC0415

        effective_now = now if now is not None else datetime.now(UTC)
        requeued = 0
        dead_lettered = 0
        for i, j in enumerate(self.jobs):
            if (
                j.status != JobStatus.RUNNING
                or j.claimed_at is None
                or not (j.claimed_at < older_than)
            ):
                continue
            if j.attempt_count + 1 >= max_attempts:
                self.jobs[i] = j.model_copy(
                    update={
                        "status": JobStatus.DEAD_LETTER,
                        "completed_at": effective_now,
                        "error": ErrorInfo.model_validate(
                            {
                                "type": "reaper_dead_letter",
                                "message": (
                                    "orphaned in running past visibility timeout; "
                                    "retry budget exhausted"
                                ),
                            }
                        ),
                    }
                )
                dead_lettered += 1
            else:
                self.jobs[i] = j.model_copy(
                    update={
                        "status": JobStatus.QUEUED,
                        "claimed_at": None,
                        "attempt_count": j.attempt_count + 1,
                        "next_retry_at": effective_now,
                    }
                )
                requeued += 1
        return ReclaimResult(requeued=requeued, dead_lettered=dead_lettered)

    async def request_job_cancel(self, job_id: str, *, tenant_id: str) -> JobStatus | None:
        """Cooperatively cancel a job — same state machine as the SQL backends.

        Single event loop → atomic by construction (no lock needed):

        * ``QUEUED`` → ``CANCELLED`` (+ ``completed_at = now``); the
          claim path only takes ``QUEUED`` rows, so it's never picked up.
        * ``RUNNING`` → set ``cancel_requested = True`` (status stays
          ``RUNNING``); the worker finalizes it at its checkpoint.
        * already terminal → no-op; return the unchanged status.

        Tenant-scoped: a missing / cross-tenant id returns ``None`` (the
        same shape as ``get_job`` → 404, never 403).
        """
        for i, j in enumerate(self.jobs):
            if j.job_id == job_id and j.tenant_id == tenant_id:
                if j.status == JobStatus.QUEUED:
                    self.jobs[i] = j.model_copy(
                        update={
                            "status": JobStatus.CANCELLED,
                            "completed_at": datetime.now(UTC),
                        }
                    )
                    return JobStatus.CANCELLED
                if j.status == JobStatus.RUNNING:
                    self.jobs[i] = j.model_copy(update={"cancel_requested": True})
                    return JobStatus.RUNNING
                # Already terminal — no-op, report the unchanged status.
                return j.status
        # Missing or cross-tenant — matches get_job's None shape.
        return None

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

    async def set_api_key_expiry(
        self, key_id: str, *, tenant_id: str, expires_at: datetime
    ) -> None:
        # Grace window on a rotated key (ADR 013 D5). Tenant-scoped; never
        # re-arm a revoked key. No-op on missing / cross-tenant / revoked.
        for i, k in enumerate(self.api_keys):
            if k.key_id == key_id and k.tenant_id == tenant_id and k.revoked_at is None:
                self.api_keys[i] = k.model_copy(update={"expires_at": expires_at})
                return

    async def update_api_key_scopes(self, key_id: str, *, scopes: list[str]) -> None:
        # Bootstrap-key self-heal: rewrite ONLY the scopes column, preserving
        # secret_hash/salt/tenant_id/env/created_at. Not tenant-scoped. No-op
        # on missing.
        for i, k in enumerate(self.api_keys):
            if k.key_id == key_id:
                self.api_keys[i] = k.model_copy(update={"scopes": list(scopes)})
                return

    async def revoke_all_api_keys(self, *, tenant_id: str, except_key_id: str | None = None) -> int:
        # Compromise-response bulk revoke (ADR 013 D5). Spares
        # ``except_key_id`` so the operator isn't locked out. Returns the
        # number actually revoked (already-revoked + spared are skipped).
        now = datetime.now(UTC)
        revoked = 0
        for i, k in enumerate(self.api_keys):
            if k.tenant_id == tenant_id and k.revoked_at is None and k.key_id != except_key_id:
                self.api_keys[i] = k.model_copy(update={"revoked_at": now})
                revoked += 1
        return revoked

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

    async def save_feedback(self, feedback: FeedbackRecord) -> None:
        # In-memory upsert: replace any existing row with the same
        # feedback_id (matches Postgres ON CONFLICT and sqlite INSERT
        # OR REPLACE semantics).
        self.feedback = [f for f in self.feedback if f.feedback_id != feedback.feedback_id]
        self.feedback.append(feedback)

    async def save_kb_chunk(self, chunk: KbChunk) -> None:
        # Upsert on (agent, tenant_id, content_hash). Preserve
        # ``chunk_id`` when updating so cached references stay valid
        # — matches the Postgres + sqlite contract.
        key = (chunk.agent, chunk.tenant_id, chunk.content_hash)
        for i, existing in enumerate(self.kb_chunks):
            if (existing.agent, existing.tenant_id, existing.content_hash) == key:
                # Replace in place, keep the old chunk_id.
                self.kb_chunks[i] = chunk.model_copy(update={"chunk_id": existing.chunk_id})
                return
        self.kb_chunks.append(chunk)

    async def search_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        from movate.storage.postgres import (  # type: ignore[attr-defined]  # noqa: PLC0415
            _rank_chunks_by_cosine,
        )

        chunks = [c for c in self.kb_chunks if c.agent == agent and c.tenant_id == tenant_id]
        return _rank_chunks_by_cosine(chunks, query_embedding, limit)

    async def search_kb_chunks_lexical(
        self,
        *,
        agent: str,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """Python BM25 fallback — InMemory has no native FTS index."""
        from movate.kb.lexical import bm25_search  # noqa: PLC0415

        chunks = [c for c in self.kb_chunks if c.agent == agent and c.tenant_id == tenant_id]
        return bm25_search(chunks, query, limit=limit)

    async def list_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
        limit: int = 1000,
    ) -> list[KbChunk]:
        rows = [c for c in self.kb_chunks if c.agent == agent and c.tenant_id == tenant_id]
        if source is not None:
            rows = [c for c in rows if c.source == source]
        rows = sorted(rows, key=lambda c: c.created_at, reverse=True)
        return rows[: int(limit)]

    async def delete_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        before = len(self.kb_chunks)
        self.kb_chunks = [
            c
            for c in self.kb_chunks
            if not (
                c.agent == agent
                and c.tenant_id == tenant_id
                and (source is None or c.source == source)
            )
        ]
        return before - len(self.kb_chunks)

    async def reindex_kb(self, *, agent: str, tenant_id: str) -> int:
        # In-memory search is brute-force cosine — no vector index to
        # rebuild. Graceful no-op returning the chunk count, mirroring
        # the sqlite backend. NEVER raises.
        return sum(1 for c in self.kb_chunks if c.agent == agent and c.tenant_id == tenant_id)

    # ------------------------------------------------------------------
    # Knowledge graph (GraphRAG) — entities + relations. BFS expansion
    # in Python; the SQL backends use a recursive CTE for the same result.
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> None:
        key = (entity.agent, entity.tenant_id, entity.content_hash)
        for i, existing in enumerate(self.entities):
            if (existing.agent, existing.tenant_id, existing.content_hash) == key:
                merged = sorted(set(existing.source_chunk_ids) | set(entity.source_chunk_ids))
                self.entities[i] = entity.model_copy(
                    update={"entity_id": existing.entity_id, "source_chunk_ids": merged}
                )
                return
        self.entities.append(entity)

    async def upsert_relation(self, relation: Relation) -> None:
        key = (relation.agent, relation.tenant_id, relation.content_hash)
        for i, existing in enumerate(self.relations):
            if (existing.agent, existing.tenant_id, existing.content_hash) == key:
                merged = sorted(set(existing.source_chunk_ids) | set(relation.source_chunk_ids))
                self.relations[i] = relation.model_copy(
                    update={"relation_id": existing.relation_id, "source_chunk_ids": merged}
                )
                return
        self.relations.append(relation)

    async def search_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[EntityWithScore]:
        from movate.storage._cosine import rank_entities_by_cosine  # noqa: PLC0415

        ents = [e for e in self.entities if e.agent == agent and e.tenant_id == tenant_id]
        return rank_entities_by_cosine(ents, query_embedding, limit)

    async def expand_neighbors(
        self,
        *,
        agent: str,
        tenant_id: str,
        entity_ids: list[str],
        hops: int = 1,
        limit: int = 50,
    ) -> Subgraph:
        if not entity_ids:
            return Subgraph(entities=[], relations=[])
        rels = [r for r in self.relations if r.agent == agent and r.tenant_id == tenant_id]
        # Breadth-first reachability bounded by ``hops`` (undirected for
        # reachability; edge direction preserved in the returned rows).
        reachable: set[str] = set(entity_ids)
        frontier: set[str] = set(entity_ids)
        for _ in range(max(0, hops)):
            nxt: set[str] = set()
            for r in rels:
                if r.src_entity_id in frontier and r.dst_entity_id not in reachable:
                    nxt.add(r.dst_entity_id)
                if r.dst_entity_id in frontier and r.src_entity_id not in reachable:
                    nxt.add(r.src_entity_id)
            if not nxt:
                break
            reachable |= nxt
            frontier = nxt
        # Edges with both endpoints reachable, strongest first, budget-capped.
        internal = [
            r for r in rels if r.src_entity_id in reachable and r.dst_entity_id in reachable
        ]
        internal.sort(key=lambda r: r.weight, reverse=True)
        returned = internal[: int(limit)]
        keep_ids = (
            set(entity_ids)
            | {r.src_entity_id for r in returned}
            | {r.dst_entity_id for r in returned}
        )
        ents = [
            e
            for e in self.entities
            if e.agent == agent and e.tenant_id == tenant_id and e.entity_id in keep_ids
        ]
        return Subgraph(entities=ents, relations=returned)

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        return next(
            (e for e in self.entities if e.entity_id == entity_id and e.tenant_id == tenant_id),
            None,
        )

    async def list_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        source_chunk_id: str | None = None,
        limit: int = 1000,
    ) -> list[Entity]:
        rows = [e for e in self.entities if e.agent == agent and e.tenant_id == tenant_id]
        if source_chunk_id is not None:
            rows = [e for e in rows if source_chunk_id in e.source_chunk_ids]
        rows = sorted(rows, key=lambda e: e.created_at, reverse=True)
        return rows[: int(limit)]

    async def list_relations(
        self,
        *,
        agent: str,
        tenant_id: str,
        limit: int = 1000,
    ) -> list[Relation]:
        rows = [r for r in self.relations if r.agent == agent and r.tenant_id == tenant_id]
        rows = sorted(rows, key=lambda r: r.created_at, reverse=True)
        return rows[: int(limit)]

    async def delete_graph(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        if source is None:
            before = len(self.entities) + len(self.relations)
            self.entities = [
                e for e in self.entities if not (e.agent == agent and e.tenant_id == tenant_id)
            ]
            self.relations = [
                r for r in self.relations if not (r.agent == agent and r.tenant_id == tenant_id)
            ]
            return before - len(self.entities) - len(self.relations)
        # Per-source delete: drop graph rows whose provenance is SOLELY the
        # given source (subset of that source's chunks). Multi-source rows
        # survive — matches the SQL backends.
        chunk_ids = {
            c.chunk_id
            for c in self.kb_chunks
            if c.agent == agent and c.tenant_id == tenant_id and c.source == source
        }

        def solely_from_source(ids: list[str]) -> bool:
            return bool(ids) and set(ids) <= chunk_ids

        before = len(self.entities) + len(self.relations)
        self.entities = [
            e
            for e in self.entities
            if not (
                e.agent == agent
                and e.tenant_id == tenant_id
                and solely_from_source(e.source_chunk_ids)
            )
        ]
        self.relations = [
            r
            for r in self.relations
            if not (
                r.agent == agent
                and r.tenant_id == tenant_id
                and solely_from_source(r.source_chunk_ids)
            )
        ]
        return before - len(self.entities) - len(self.relations)

    # ------------------------------------------------------------------
    # Conversation threads (PR-N) — multi-turn agent foundation.
    # ------------------------------------------------------------------

    async def save_conversation_thread(self, thread: ConversationThread) -> None:
        # In-memory upsert on thread_id — matches Postgres ON CONFLICT
        # + sqlite INSERT OR REPLACE semantics.
        self.conversation_threads = [
            t for t in self.conversation_threads if t.thread_id != thread.thread_id
        ]
        self.conversation_threads.append(thread)

    async def get_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> ConversationThread | None:
        # Tenant-scoped — cross-tenant lookup returns None (mirrors
        # the contract on every single-record getter).
        for t in self.conversation_threads:
            if t.thread_id == thread_id and t.tenant_id == tenant_id:
                return t
        return None

    async def list_conversation_threads(
        self,
        *,
        tenant_id: str,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[ConversationThread]:
        rows = [t for t in self.conversation_threads if t.tenant_id == tenant_id]
        if agent is not None:
            rows = [t for t in rows if t.agent == agent]
        rows = sorted(rows, key=lambda t: t.updated_at, reverse=True)
        return rows[: int(limit)]

    async def list_runs_for_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[RunRecord]:
        # Tenant-scoped: cross-tenant thread id returns [] rather than
        # raising or leaking.
        rows = [r for r in self.runs if r.thread_id == thread_id and r.tenant_id == tenant_id]
        # ASC by created_at — chronological order, earliest turn first,
        # so the runtime can render conversation history without
        # reversing.
        rows = sorted(rows, key=lambda r: r.created_at)
        return rows[: int(limit)]

    async def delete_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        # Tenant-scoped delete: a thread row for a different tenant
        # is invisible to this call (returns False), mirroring the
        # 404-not-403 contract on cross-tenant reads.
        before = len(self.conversation_threads)
        self.conversation_threads = [
            t
            for t in self.conversation_threads
            if not (t.thread_id == thread_id and t.tenant_id == tenant_id)
        ]
        return len(self.conversation_threads) < before

    async def list_feedback(
        self,
        *,
        run_id: str | None = None,
        agent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        rows = self.feedback
        if run_id is not None:
            rows = [f for f in rows if f.run_id == run_id]
        if agent is not None:
            rows = [f for f in rows if f.agent == agent]
        if tenant_id is not None:
            rows = [f for f in rows if f.tenant_id == tenant_id]
        if user_id is not None:
            rows = [f for f in rows if f.user_id == user_id]
        rows = sorted(rows, key=lambda f: f.created_at, reverse=True)
        return rows[: int(limit)]

    # ------------------------------------------------------------------
    # DR backup/restore (item 26) — delegate to the same backend-agnostic
    # orchestration the real providers use, so the in-memory double exercises
    # the identical export/import path under test.
    # ------------------------------------------------------------------

    async def export_state(self) -> dict[str, object]:
        from movate.core.dr_backup import export_state  # noqa: PLC0415

        return await export_state(self)

    async def import_state(
        self, snapshot: dict[str, object], *, mode: str = "skip-existing"
    ) -> ImportResult:
        from movate.core.dr_backup import import_state  # noqa: PLC0415

        return await import_state(self, snapshot, mode=mode)

    # ------------------------------------------------------------------
    # Events outbox (ADR 035 D1 — durable lifecycle events).
    # ------------------------------------------------------------------

    async def record_event(self, event: Event) -> None:
        self.events.append(event)

    async def list_events(
        self,
        tenant_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        kind: str | None = None,
        subject: str | None = None,
        limit: int = 200,
        after_id: str | None = None,
    ) -> list[Event]:
        rows = [e for e in self.events if e.tenant_id == tenant_id]
        if since is not None:
            rows = [e for e in rows if e.created_at >= since]
        if until is not None:
            rows = [e for e in rows if e.created_at < until]
        if kind is not None:
            rows = [e for e in rows if e.kind == kind]
        if subject is not None:
            rows = [e for e in rows if e.subject == subject]
        # Oldest-first with id as a stable tie-breaker (matches the DB
        # backends' ORDER BY created_at ASC, id ASC).
        rows = sorted(rows, key=lambda e: (e.created_at, e.id))
        if after_id is not None:
            # Skip up to and including the cursor row's position. An
            # unknown / cross-tenant id falls back to "from the
            # beginning" (no existence leak).
            cursor = next((e for e in rows if e.id == after_id), None)
            if cursor is not None:
                rows = [e for e in rows if (e.created_at, e.id) > (cursor.created_at, cursor.id)]
        return rows[:limit]

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
        # Detect any judge/specialist call by looking for the common score-response
        # contract phrase or any specialist evaluator pattern.
        _judge_signals = (
            "Rubric:",
            "specialist evaluator",
            "Return ONLY a JSON object",
        )
        if any(sig in body for sig in _judge_signals):
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
