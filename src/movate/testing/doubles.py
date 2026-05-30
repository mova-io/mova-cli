"""Test doubles: in-memory storage, null tracer, scripted judge provider.

These mirror the real implementations' protocols closely enough that they
satisfy mypy strict against ``StorageProvider`` / ``Tracer`` /
``BaseLLMProvider`` without copying production code into ``tests/``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

from movate.core.dr_backup import ImportResult
from movate.core.events import Event
from movate.core.job_retry import ReclaimResult
from movate.core.models import (
    _DEFAULT_PROJECT_DESCRIPTION,
    _DEFAULT_PROJECT_NAME,
    _TENANT_SYSTEM_PRINCIPAL,
    AgentBundleRecord,
    ApiKeyRecord,
    AuditRecord,
    BatchRecord,
    BenchRecord,
    CanaryConfig,
    CatalogEntry,
    CatalogEntryRating,
    CatalogEntryVersion,
    CatalogRatingsSummary,
    CatalogSource,
    ConversationThread,
    DiagnosisRecord,
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
    Project,
    ProjectAgent,
    ProjectKb,
    ProjectKbMode,
    ProjectMember,
    ProjectMemberRole,
    ProjectWorkflow,
    Relation,
    RunRecord,
    Session,
    SessionMessage,
    Subgraph,
    TenantBudget,
    TenantProviderKey,
    Trigger,
    WorkflowBundleRecord,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.observability.models import ObservabilityInsight
from movate.core.webhooks import WebhookAttempt, WebhookSubscription
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from movate.tracing.base import SpanCtx, Tracer

_RATING_MIN = 1
_RATING_MAX = 5


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
        # ADR 037 D1: workflow analogue of agent_bundles. Same shape; the
        # registry doubles as the version history.
        self.workflow_bundles: list[WorkflowBundleRecord] = []
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
        # ADR 045 D10: stateful sessions + their messages. Lists for
        # direct test assertion (matches the other entities here).
        self.sessions: list[Session] = []
        self.session_messages: list[SessionMessage] = []
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
        # ADR 040 — projects + members + M:N junctions for agents/workflows/KBs.
        # Lists for direct test assertion (matches the other entities here).
        self.projects: list[Project] = []
        self.project_members: list[ProjectMember] = []
        self.project_agents: list[ProjectAgent] = []
        self.project_workflows: list[ProjectWorkflow] = []
        self.project_kbs: list[ProjectKb] = []
        # ADR 041 — agent catalog. Three namespaces in one bucket, keyed by
        # (slug, source, tenant_id) so the uniqueness contract is preserved
        # without sentinels (the in-memory double doesn't care about NULL-in-PK
        # semantics — that quirk only matters in sqlite/postgres).
        self.catalog_entries: list[CatalogEntry] = []
        self.catalog_entry_versions: list[CatalogEntryVersion] = []
        self.catalog_entry_ratings: list[CatalogEntryRating] = []
        self.catalog_sync_watermarks: dict[str, datetime] = {}
        # ADR 047: append-only observability insights. A re-run of the analyst
        # for a day appends another row; reads take the latest per
        # (tenant, project, date).
        self.insights: list[ObservabilityInsight] = []
        # ADR 035 D1: events outbox. Appended in record_event order;
        # list_events sorts oldest-first by (created_at, id).
        self.events: list[Event] = []
        # ADR 035 D2: webhook subscriptions + per-attempt delivery log
        # + per-webhook cursor. Default-off — no rows unless created.
        self.webhooks: list[WebhookSubscription] = []
        self.webhook_attempts: list[WebhookAttempt] = []
        # Cursor map: webhook_id -> last delivered event id. Tenant
        # bound is preserved by always reading via the tenant-scoped
        # get_webhook (we never look up cursors for cross-tenant ids).
        self.webhook_cursors: dict[str, str] = {}
        # Eval-generation jobs (``mdk eval generate``). Keyed by job_id;
        # value is a :class:`movate.core.eval_generator.EvalGenerationJob`.
        # Tenant scoping is enforced inline in the getter, not as a
        # secondary index — same as the other ``get_*`` methods.
        self._eval_generation_jobs: dict[str, Any] = {}
        # ADR 043 D1: persisted Failure Pattern Diagnoser outputs. Keyed
        # by diagnosis_id; upserted in place so the runtime background
        # task can transition a row from ``running`` to ``completed``
        # without a separate update method.
        self.diagnoses: dict[str, DiagnosisRecord] = {}
        # Claude-orchestrated audit records (read-only audit pipeline).
        # Empty by default — populated only when an audit job completes.
        self.audits: list[AuditRecord] = []

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

    async def save_audit(self, a: AuditRecord) -> None:
        self.audits.append(a)

    async def get_audit(self, audit_id: str, *, tenant_id: str) -> AuditRecord | None:
        return next(
            (au for au in self.audits if au.audit_id == audit_id and au.tenant_id == tenant_id),
            None,
        )

    async def list_audits(
        self,
        *,
        tenant_id: str | None = None,
        scope_id: str | None = None,
        limit: int = 20,
    ) -> list[AuditRecord]:
        rows = self.audits
        if tenant_id is not None:
            rows = [a for a in rows if a.tenant_id == tenant_id]
        if scope_id:
            rows = [a for a in rows if a.scope_id == scope_id]
        return sorted(rows, key=lambda a: a.created_at, reverse=True)[:limit]

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

    # ------------------------------------------------------------------
    # Projects (ADR 040)
    # ------------------------------------------------------------------

    async def create_project(self, project: Project) -> Project:
        # Unique (tenant_id, name) — matches the SQL backends' invariant.
        for existing in self.projects:
            if existing.tenant_id == project.tenant_id and existing.name == project.name:
                raise ValueError(
                    f"project ({project.tenant_id!r}, {project.name!r}) already exists"
                )
            if existing.project_id == project.project_id:
                raise ValueError(f"project_id {project.project_id!r} already exists")
        self.projects.append(project)
        return project

    async def get_project(self, tenant_id: str, project_id: str) -> Project | None:
        return next(
            (p for p in self.projects if p.project_id == project_id and p.tenant_id == tenant_id),
            None,
        )

    async def get_project_by_name(self, tenant_id: str, name: str) -> Project | None:
        return next(
            (p for p in self.projects if p.tenant_id == tenant_id and p.name == name),
            None,
        )

    async def list_projects(
        self,
        tenant_id: str,
        *,
        include_archived: bool = False,
        limit: int = 100,
        after_id: str | None = None,
    ) -> list[Project]:
        rows = [p for p in self.projects if p.tenant_id == tenant_id]
        if not include_archived:
            rows = [p for p in rows if p.archived_at is None]
        # Stable ordering: newest-first, tie-broken by project_id DESC.
        rows.sort(key=lambda p: (p.created_at, p.project_id), reverse=True)
        if after_id is not None:
            cursor = next((p for p in self.projects if p.project_id == after_id), None)
            if cursor is not None:
                key = (cursor.created_at, cursor.project_id)
                rows = [p for p in rows if (p.created_at, p.project_id) < key]
        return rows[:limit]

    async def update_project(
        self,
        tenant_id: str,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Project | None:
        target_idx = next(
            (
                i
                for i, p in enumerate(self.projects)
                if p.project_id == project_id and p.tenant_id == tenant_id
            ),
            None,
        )
        if target_idx is None:
            return None
        target = self.projects[target_idx]
        if name is None and description is None:
            return target
        # Rename collision guard matches the SQL UNIQUE(tenant_id, name).
        if name is not None and name != target.name:
            for p in self.projects:
                if p.tenant_id == tenant_id and p.name == name:
                    raise ValueError(
                        f"project name {name!r} already exists in tenant {tenant_id!r}"
                    )
        updated = target.model_copy(
            update={
                "name": name if name is not None else target.name,
                "description": description if description is not None else target.description,
                "updated_at": datetime.now(UTC),
            }
        )
        self.projects[target_idx] = updated
        return updated

    async def archive_project(self, tenant_id: str, project_id: str) -> bool:
        target_idx = next(
            (
                i
                for i, p in enumerate(self.projects)
                if p.project_id == project_id and p.tenant_id == tenant_id
            ),
            None,
        )
        if target_idx is None:
            return False
        target = self.projects[target_idx]
        if target.name == _DEFAULT_PROJECT_NAME:
            raise ValueError(f"default project for tenant {tenant_id!r} cannot be archived")
        if target.archived_at is not None:
            return False
        now = datetime.now(UTC)
        self.projects[target_idx] = target.model_copy(
            update={"archived_at": now, "updated_at": now}
        )
        return True

    async def add_project_member(
        self,
        project_id: str,
        principal_id: str,
        role: ProjectMemberRole,
        added_by: str,
    ) -> None:
        for m in self.project_members:
            if m.project_id == project_id and m.principal_id == principal_id:
                raise ValueError(f"member {principal_id!r} already on project {project_id!r}")
        self.project_members.append(
            ProjectMember(
                project_id=project_id,
                principal_id=principal_id,
                role=role,
                added_by=added_by,
            )
        )

    async def list_project_members(self, project_id: str) -> list[ProjectMember]:
        rows = [m for m in self.project_members if m.project_id == project_id]
        return sorted(rows, key=lambda m: (m.added_at, m.principal_id))

    async def update_project_member(
        self,
        project_id: str,
        principal_id: str,
        *,
        role: ProjectMemberRole,
    ) -> ProjectMember | None:
        for i, m in enumerate(self.project_members):
            if m.project_id == project_id and m.principal_id == principal_id:
                updated = m.model_copy(update={"role": role})
                self.project_members[i] = updated
                return updated
        return None

    async def remove_project_member(self, project_id: str, principal_id: str) -> bool:
        before = len(self.project_members)
        self.project_members = [
            m
            for m in self.project_members
            if not (m.project_id == project_id and m.principal_id == principal_id)
        ]
        return len(self.project_members) < before

    async def get_project_member(
        self,
        project_id: str,
        principal_id: str,
    ) -> ProjectMember | None:
        return next(
            (
                m
                for m in self.project_members
                if m.project_id == project_id and m.principal_id == principal_id
            ),
            None,
        )

    # ------------------------------------------------------------------
    # Agent catalog (ADR 041)
    # ------------------------------------------------------------------

    @staticmethod
    def _catalog_namespace_ok(source: CatalogSource, tenant_id: str | None) -> None:
        if source is CatalogSource.PRIVATE:
            if not tenant_id:
                raise ValueError("catalog 'private' entries require tenant_id")
        elif tenant_id is not None:
            raise ValueError(f"catalog '{source.value}' entries must have tenant_id=None")

    async def upsert_catalog_entry(self, entry: CatalogEntry) -> None:
        self._catalog_namespace_ok(entry.source, entry.tenant_id)
        self.catalog_entries = [
            e
            for e in self.catalog_entries
            if not (
                e.slug == entry.slug and e.source == entry.source and e.tenant_id == entry.tenant_id
            )
        ]
        self.catalog_entries.append(entry)

    async def get_catalog_entry(
        self,
        slug: str,
        *,
        source: CatalogSource,
        tenant_id: str | None = None,
    ) -> CatalogEntry | None:
        if source is CatalogSource.PRIVATE and tenant_id is None:
            return None
        wanted = tenant_id if source is CatalogSource.PRIVATE else None
        return next(
            (
                e
                for e in self.catalog_entries
                if e.slug == slug and e.source == source and e.tenant_id == wanted
            ),
            None,
        )

    async def list_catalog_entries(
        self,
        tenant_id: str,
        *,
        source_filter: CatalogSource | None = None,
        tag_filter: str | None = None,
        shape_filter: str | None = None,
        q: str | None = None,
        limit: int = 100,
        after_slug: str | None = None,
    ) -> list[CatalogEntry]:
        def _visible(e: CatalogEntry) -> bool:
            if e.source is CatalogSource.MOVATE:
                return True
            if e.source is CatalogSource.COMMUNITY:
                return True
            return e.source is CatalogSource.PRIVATE and e.tenant_id == tenant_id

        rows = [e for e in self.catalog_entries if _visible(e)]
        if source_filter is not None:
            rows = [e for e in rows if e.source == source_filter]
        if shape_filter is not None:
            rows = [e for e in rows if e.shape == shape_filter]
        if tag_filter is not None:
            rows = [e for e in rows if tag_filter in e.tags]
        if q:
            needle = q.lower()
            rows = [
                e
                for e in rows
                if needle in e.name.lower()
                or needle in e.title.lower()
                or needle in e.description.lower()
            ]
        if after_slug is not None:
            rows = [e for e in rows if e.slug > after_slug]
        rows.sort(key=lambda e: e.slug)
        return rows[:limit]

    async def get_catalog_entry_versions(
        self,
        slug: str,
        *,
        source: CatalogSource,
        tenant_id: str | None = None,
    ) -> list[CatalogEntryVersion]:
        if source is CatalogSource.PRIVATE and tenant_id is None:
            return []
        wanted = tenant_id if source is CatalogSource.PRIVATE else None
        rows = [
            v
            for v in self.catalog_entry_versions
            if v.slug == slug and v.source == source and v.tenant_id == wanted
        ]
        return sorted(rows, key=lambda v: v.published_at, reverse=True)

    async def get_catalog_entry_version(
        self,
        slug: str,
        *,
        source: CatalogSource,
        version: str,
        tenant_id: str | None = None,
    ) -> CatalogEntryVersion | None:
        if source is CatalogSource.PRIVATE and tenant_id is None:
            return None
        wanted = tenant_id if source is CatalogSource.PRIVATE else None
        return next(
            (
                v
                for v in self.catalog_entry_versions
                if v.slug == slug
                and v.source == source
                and v.version == version
                and v.tenant_id == wanted
            ),
            None,
        )

    async def attach_agent_to_project(self, project_id: str, agent_name: str) -> None:
        for a in self.project_agents:
            if a.project_id == project_id and a.agent_name == agent_name:
                return  # idempotent
        self.project_agents.append(ProjectAgent(project_id=project_id, agent_name=agent_name))

    async def detach_agent_from_project(self, project_id: str, agent_name: str) -> bool:
        before = len(self.project_agents)
        self.project_agents = [
            a
            for a in self.project_agents
            if not (a.project_id == project_id and a.agent_name == agent_name)
        ]
        return len(self.project_agents) < before

    async def list_project_agents(self, project_id: str) -> list[str]:
        rows = sorted(
            (a for a in self.project_agents if a.project_id == project_id),
            key=lambda a: (a.added_at, a.agent_name),
        )
        return [a.agent_name for a in rows]

    async def list_projects_for_agent(self, tenant_id: str, agent_name: str) -> list[str]:
        # Filter via the project's tenant — junction has no tenant column,
        # tenant-isolation is single-sourced through the project row.
        project_ids_in_tenant = {p.project_id for p in self.projects if p.tenant_id == tenant_id}
        rows = sorted(
            (
                a
                for a in self.project_agents
                if a.agent_name == agent_name and a.project_id in project_ids_in_tenant
            ),
            key=lambda a: (a.added_at, a.project_id),
        )
        if rows:
            return [a.project_id for a in rows]
        default_project = await self.get_or_create_default_project(tenant_id)
        return [default_project.project_id]

    async def attach_workflow_to_project(self, project_id: str, workflow_name: str) -> None:
        for w in self.project_workflows:
            if w.project_id == project_id and w.workflow_name == workflow_name:
                return
        self.project_workflows.append(
            ProjectWorkflow(project_id=project_id, workflow_name=workflow_name)
        )

    async def detach_workflow_from_project(self, project_id: str, workflow_name: str) -> bool:
        before = len(self.project_workflows)
        self.project_workflows = [
            w
            for w in self.project_workflows
            if not (w.project_id == project_id and w.workflow_name == workflow_name)
        ]
        return len(self.project_workflows) < before

    async def list_project_workflows(self, project_id: str) -> list[str]:
        rows = sorted(
            (w for w in self.project_workflows if w.project_id == project_id),
            key=lambda w: (w.added_at, w.workflow_name),
        )
        return [w.workflow_name for w in rows]

    async def list_projects_for_workflow(self, tenant_id: str, workflow_name: str) -> list[str]:
        project_ids_in_tenant = {p.project_id for p in self.projects if p.tenant_id == tenant_id}
        rows = sorted(
            (
                w
                for w in self.project_workflows
                if w.workflow_name == workflow_name and w.project_id in project_ids_in_tenant
            ),
            key=lambda w: (w.added_at, w.project_id),
        )
        if rows:
            return [w.project_id for w in rows]
        default_project = await self.get_or_create_default_project(tenant_id)
        return [default_project.project_id]

    async def attach_kb_to_project(
        self,
        project_id: str,
        kb_id: str,
        mode: ProjectKbMode,
    ) -> None:
        for i, k in enumerate(self.project_kbs):
            if k.project_id == project_id and k.kb_id == kb_id:
                # Upsert mode in place (mirror SQL backends' ON CONFLICT path).
                self.project_kbs[i] = k.model_copy(update={"mode": mode})
                return
        self.project_kbs.append(ProjectKb(project_id=project_id, kb_id=kb_id, mode=mode))

    async def detach_kb_from_project(self, project_id: str, kb_id: str) -> bool:
        before = len(self.project_kbs)
        self.project_kbs = [
            k for k in self.project_kbs if not (k.project_id == project_id and k.kb_id == kb_id)
        ]
        return len(self.project_kbs) < before

    async def list_project_kbs(self, project_id: str) -> list[tuple[str, ProjectKbMode]]:
        rows = sorted(
            (k for k in self.project_kbs if k.project_id == project_id),
            key=lambda k: (k.added_at, k.kb_id),
        )
        return [(k.kb_id, k.mode) for k in rows]

    async def list_projects_for_kb(self, tenant_id: str, kb_id: str) -> list[str]:
        project_ids_in_tenant = {p.project_id for p in self.projects if p.tenant_id == tenant_id}
        rows = sorted(
            (
                k
                for k in self.project_kbs
                if k.kb_id == kb_id and k.project_id in project_ids_in_tenant
            ),
            key=lambda k: (k.added_at, k.project_id),
        )
        return [k.project_id for k in rows]

    async def get_or_create_default_project(self, tenant_id: str) -> Project:
        existing = await self.get_project_by_name(tenant_id, _DEFAULT_PROJECT_NAME)
        if existing is not None:
            return existing
        project = Project(
            tenant_id=tenant_id,
            name=_DEFAULT_PROJECT_NAME,
            description=_DEFAULT_PROJECT_DESCRIPTION,
            owner_principal_id=_TENANT_SYSTEM_PRINCIPAL,
        )
        try:
            return await self.create_project(project)
        except ValueError:
            row = await self.get_project_by_name(tenant_id, _DEFAULT_PROJECT_NAME)
            assert row is not None  # invariant after race
            return row

    async def upsert_catalog_entry_version(
        self,
        slug: str,
        *,
        source: CatalogSource,
        version: str,
        bundle_tar: bytes,
        digest: str,
        tenant_id: str | None = None,
    ) -> CatalogEntryVersion:
        self._catalog_namespace_ok(source, tenant_id)
        existing = next(
            (
                v
                for v in self.catalog_entry_versions
                if v.slug == slug
                and v.source == source
                and v.version == version
                and v.tenant_id == tenant_id
            ),
            None,
        )
        if existing is not None:
            updated = existing.model_copy(update={"bundle_tar": bundle_tar, "digest": digest})
            self.catalog_entry_versions = [
                updated if v is existing else v for v in self.catalog_entry_versions
            ]
            return updated
        record = CatalogEntryVersion(
            slug=slug,
            version=version,
            source=source,
            tenant_id=tenant_id,
            bundle_tar=bundle_tar,
            digest=digest,
        )
        self.catalog_entry_versions.append(record)
        return record

    async def record_catalog_rating(
        self,
        slug: str,
        *,
        tenant_id: str,
        source: CatalogSource = CatalogSource.MOVATE,
        rating: int,
        comment: str | None = None,
    ) -> CatalogRatingsSummary:
        if not (_RATING_MIN <= rating <= _RATING_MAX):
            raise ValueError("rating must be between 1 and 5")
        # Overwrite any prior rating from this tenant for this entry.
        self.catalog_entry_ratings = [
            r
            for r in self.catalog_entry_ratings
            if not (r.slug == slug and r.source == source and r.tenant_id == tenant_id)
        ]
        self.catalog_entry_ratings.append(
            CatalogEntryRating(
                slug=slug,
                source=source,
                tenant_id=tenant_id,
                rating=int(rating),
                comment=comment,
            )
        )
        rows = [r for r in self.catalog_entry_ratings if r.slug == slug and r.source == source]
        count = len(rows)
        avg = sum(r.rating for r in rows) / count if count else 0.0
        summary = CatalogRatingsSummary(count=count, avg=avg)
        # Update every visible entry row for (slug, source) so a list view
        # reflects the new aggregate.
        updated_entries: list[CatalogEntry] = []
        for entry in self.catalog_entries:
            if entry.slug == slug and entry.source == source:
                updated_entries.append(entry.model_copy(update={"ratings_summary": summary}))
            else:
                updated_entries.append(entry)
        self.catalog_entries = updated_entries
        return summary

    async def get_catalog_sync_watermark(self, source: CatalogSource) -> datetime | None:
        return self.catalog_sync_watermarks.get(source.value)

    async def set_catalog_sync_watermark(self, source: CatalogSource, ts: datetime) -> None:
        self.catalog_sync_watermarks[source.value] = ts

    # ------------------------------------------------------------------
    # Durable workflow registry (ADR 037 D1)
    # ------------------------------------------------------------------

    async def save_workflow_bundle(self, bundle: WorkflowBundleRecord) -> None:
        self.workflow_bundles.append(bundle)

    async def get_workflow_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> WorkflowBundleRecord | None:
        matches = [b for b in self.workflow_bundles if b.name == name and b.tenant_id == tenant_id]
        if version is not None:
            matches = [b for b in matches if b.version == version]
        if not matches:
            return None
        return max(matches, key=lambda b: b.created_at)

    async def list_workflows(
        self,
        *,
        tenant_id: str,
        published_only: bool = False,
        limit: int = 100,
    ) -> list[WorkflowBundleRecord]:
        rows = [b for b in self.workflow_bundles if b.tenant_id == tenant_id]
        if published_only:
            published_names = {b.name for b in rows if b.published}
            rows = [b for b in rows if b.name in published_names]
        latest: dict[str, WorkflowBundleRecord] = {}
        for b in rows:
            current = latest.get(b.name)
            if current is None or b.created_at > current.created_at:
                latest[b.name] = b
        ordered = sorted(latest.values(), key=lambda b: b.created_at, reverse=True)
        return ordered[:limit]

    async def list_workflow_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[WorkflowBundleRecord]:
        rows = [b for b in self.workflow_bundles if b.name == name and b.tenant_id == tenant_id]
        return sorted(rows, key=lambda b: b.created_at, reverse=True)[:limit]

    async def list_all_workflow_bundles(
        self, *, limit: int = 100_000
    ) -> list[WorkflowBundleRecord]:
        rows = sorted(self.workflow_bundles, key=lambda b: (b.tenant_id, b.name, b.created_at))
        return rows[:limit]

    async def delete_workflow_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        def _matches(b: WorkflowBundleRecord) -> bool:
            if b.name != name or b.tenant_id != tenant_id:
                return False
            return version is None or b.version == version

        to_delete = [b for b in self.workflow_bundles if _matches(b)]
        self.workflow_bundles = [b for b in self.workflow_bundles if not _matches(b)]
        return len(to_delete)

    async def publish_workflow_version(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str,
    ) -> bool:
        # ADR 037 D1: at most one published version per (tenant, name). Find
        # the row to mutate in place; flip the rest of the name's history off.
        # Pydantic models are immutable by default; rebuild rather than poke
        # the field so the type checker stays happy.
        found = False
        new_rows: list[WorkflowBundleRecord] = []
        for b in self.workflow_bundles:
            if b.tenant_id == tenant_id and b.name == name:
                if b.version == version:
                    found = True
                    new_rows.append(b.model_copy(update={"published": True}))
                else:
                    new_rows.append(b.model_copy(update={"published": False}))
            else:
                new_rows.append(b)
        if not found:
            return False
        self.workflow_bundles = new_rows
        return True

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
    # Dead-letter operations — mirror the SQL backends' semantics.
    # ------------------------------------------------------------------

    async def list_dead_letter_jobs(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        agent: str | None = None,
    ) -> list[JobRecord]:
        rows = [
            j for j in self.jobs if j.tenant_id == tenant_id and j.status == JobStatus.DEAD_LETTER
        ]
        if agent is not None:
            rows = [j for j in rows if j.target == agent]
        return sorted(rows, key=lambda j: j.created_at, reverse=True)[:limit]

    async def requeue_dead_letter_job(self, job_id: str, *, tenant_id: str) -> bool:
        """Reset a DEAD_LETTER job → QUEUED with a fresh budget.

        Status-guarded: only a ``DEAD_LETTER`` row for this tenant is
        touched. Returns ``True`` iff a row was actually requeued.
        Single event loop → atomic by construction.
        """
        for i, j in enumerate(self.jobs):
            if (
                j.job_id == job_id
                and j.tenant_id == tenant_id
                and j.status == JobStatus.DEAD_LETTER
            ):
                self.jobs[i] = j.model_copy(
                    update={
                        "status": JobStatus.QUEUED,
                        "attempt_count": 0,
                        "next_retry_at": None,
                        "claimed_at": None,
                        "completed_at": None,
                        "error": None,
                    }
                )
                return True
        # Missing, cross-tenant, or not DEAD_LETTER — no-op.
        return False

    async def purge_dead_letter_jobs(
        self,
        tenant_id: str,
        *,
        before: datetime | None = None,
    ) -> int:
        """Delete this tenant's DEAD_LETTER rows (optionally older than
        ``before`` by ``completed_at``). Returns the count deleted."""

        def _purgeable(j: JobRecord) -> bool:
            if j.tenant_id != tenant_id or j.status != JobStatus.DEAD_LETTER:
                return False
            if before is not None:
                # Keep rows with no completed_at, or completed at/after the
                # cutoff — matches the SQL backends' strict ``< before`` plus
                # ``completed_at IS NOT NULL`` predicate.
                return j.completed_at is not None and j.completed_at < before
            return True

        to_delete = [j for j in self.jobs if _purgeable(j)]
        if to_delete:
            ids = {j.job_id for j in to_delete}
            self.jobs = [j for j in self.jobs if j.job_id not in ids]
        return len(to_delete)

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
                # COALESCE-preserve project_id (ADR 046 D1): a project-less
                # re-ingest keeps the existing tag; re-ingesting under a
                # project backfills it. Mirrors the SQL backends' ON CONFLICT.
                project_id = (
                    entity.project_id if entity.project_id is not None else existing.project_id
                )
                self.entities[i] = entity.model_copy(
                    update={
                        "entity_id": existing.entity_id,
                        "source_chunk_ids": merged,
                        "project_id": project_id,
                    }
                )
                return
        self.entities.append(entity)

    async def upsert_relation(self, relation: Relation) -> None:
        key = (relation.agent, relation.tenant_id, relation.content_hash)
        for i, existing in enumerate(self.relations):
            if (existing.agent, existing.tenant_id, existing.content_hash) == key:
                merged = sorted(set(existing.source_chunk_ids) | set(relation.source_chunk_ids))
                project_id = (
                    relation.project_id if relation.project_id is not None else existing.project_id
                )
                self.relations[i] = relation.model_copy(
                    update={
                        "relation_id": existing.relation_id,
                        "source_chunk_ids": merged,
                        "project_id": project_id,
                    }
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
        project_id: str | None = None,
    ) -> list[EntityWithScore]:
        from movate.storage._cosine import rank_entities_by_cosine  # noqa: PLC0415

        ents = [
            e
            for e in self.entities
            if e.agent == agent
            and e.tenant_id == tenant_id
            and (project_id is None or e.project_id == project_id)
        ]
        return rank_entities_by_cosine(ents, query_embedding, limit)

    async def expand_neighbors(
        self,
        *,
        agent: str,
        tenant_id: str,
        entity_ids: list[str],
        hops: int = 1,
        limit: int = 50,
        project_id: str | None = None,
    ) -> Subgraph:
        if not entity_ids:
            return Subgraph(entities=[], relations=[])
        rels = [
            r
            for r in self.relations
            if r.agent == agent
            and r.tenant_id == tenant_id
            and (project_id is None or r.project_id == project_id)
        ]
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
            if e.agent == agent
            and e.tenant_id == tenant_id
            and (project_id is None or e.project_id == project_id)
            and e.entity_id in keep_ids
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
        project_id: str | None = None,
    ) -> list[Entity]:
        rows = [
            e
            for e in self.entities
            if e.agent == agent
            and e.tenant_id == tenant_id
            and (project_id is None or e.project_id == project_id)
        ]
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
        project_id: str | None = None,
    ) -> list[Relation]:
        rows = [
            r
            for r in self.relations
            if r.agent == agent
            and r.tenant_id == tenant_id
            and (project_id is None or r.project_id == project_id)
        ]
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

    # ------------------------------------------------------------------
    # Stateful sessions (ADR 045 D10)
    # ------------------------------------------------------------------

    async def save_session(self, session: Session) -> None:
        # Upsert on session_id — matches the Postgres ON CONFLICT /
        # sqlite INSERT OR REPLACE semantics.
        self.sessions = [s for s in self.sessions if s.session_id != session.session_id]
        self.sessions.append(session)

    async def get_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
    ) -> Session | None:
        for s in self.sessions:
            if s.session_id == session_id and s.tenant_id == tenant_id:
                return s
        return None

    async def list_sessions(
        self,
        *,
        tenant_id: str,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[Session]:
        rows = [s for s in self.sessions if s.tenant_id == tenant_id]
        if agent is not None:
            rows = [s for s in rows if s.agent == agent]
        rows = sorted(rows, key=lambda s: s.updated_at, reverse=True)
        return rows[: int(limit)]

    async def append_session_message(self, message: SessionMessage) -> None:
        self.session_messages.append(message)

    async def list_session_messages(
        self,
        session_id: str,
        *,
        tenant_id: str,
        limit: int = 1000,
    ) -> list[SessionMessage]:
        rows = [
            m
            for m in self.session_messages
            if m.session_id == session_id and m.tenant_id == tenant_id
        ]
        rows = sorted(rows, key=lambda m: m.created_at)
        return rows[: int(limit)]

    async def delete_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        self.session_messages = [
            m
            for m in self.session_messages
            if not (m.session_id == session_id and m.tenant_id == tenant_id)
        ]
        before = len(self.sessions)
        self.sessions = [
            s
            for s in self.sessions
            if not (s.session_id == session_id and s.tenant_id == tenant_id)
        ]
        return len(self.sessions) < before

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
    # Observability insights (ADR 047) — append-only.
    # ------------------------------------------------------------------

    async def save_insight(self, insight: ObservabilityInsight) -> None:
        # Append-only: never replace an existing row. Reads dedupe per day.
        self.insights.append(insight)

    async def get_insight(
        self, tenant_id: str, project_id: str, day: date
    ) -> ObservabilityInsight | None:
        candidates = [
            i
            for i in self.insights
            if i.tenant_id == tenant_id and i.project_id == project_id and i.date == day
        ]
        if not candidates:
            return None
        # Latest-per-day: newest created_at wins.
        return max(candidates, key=lambda i: i.created_at)

    async def list_insights(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        since: date | None = None,
        until: date | None = None,
        limit: int = 90,
    ) -> list[ObservabilityInsight]:
        rows = [i for i in self.insights if i.tenant_id == tenant_id]
        if project_id is not None:
            rows = [i for i in rows if i.project_id == project_id]
        if since is not None:
            rows = [i for i in rows if i.date >= since]
        if until is not None:
            rows = [i for i in rows if i.date <= until]
        # Collapse append-only re-runs to the latest row per (project, date).
        latest: dict[tuple[str, date], ObservabilityInsight] = {}
        for i in rows:
            key = (i.project_id, i.date)
            if key not in latest or i.created_at > latest[key].created_at:
                latest[key] = i
        ordered = sorted(latest.values(), key=lambda i: i.date, reverse=True)
        return ordered[:limit]

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

    # ------------------------------------------------------------------
    # Webhook subscriptions (ADR 035 D2 — outbound delivery).
    # ------------------------------------------------------------------

    async def create_webhook(self, sub: WebhookSubscription) -> WebhookSubscription:
        self.webhooks.append(sub)
        return sub

    async def list_webhooks(
        self,
        tenant_id: str,
        *,
        enabled_only: bool = True,
    ) -> list[WebhookSubscription]:
        rows = [w for w in self.webhooks if w.tenant_id == tenant_id]
        if enabled_only:
            rows = [w for w in rows if w.enabled]
        return sorted(rows, key=lambda w: (w.created_at, w.id))

    async def get_webhook(self, tenant_id: str, webhook_id: str) -> WebhookSubscription | None:
        for w in self.webhooks:
            if w.id == webhook_id and w.tenant_id == tenant_id:
                return w
        return None

    async def update_webhook(
        self,
        tenant_id: str,
        webhook_id: str,
        *,
        enabled: bool | None = None,
        failure_count: int | None = None,
    ) -> WebhookSubscription | None:
        existing = await self.get_webhook(tenant_id, webhook_id)
        if existing is None:
            return None
        if enabled is not None:
            existing.enabled = enabled
        if failure_count is not None:
            existing.failure_count = failure_count
        return existing

    async def delete_webhook(self, tenant_id: str, webhook_id: str) -> bool:
        for i, w in enumerate(self.webhooks):
            if w.id == webhook_id and w.tenant_id == tenant_id:
                del self.webhooks[i]
                return True
        return False

    async def record_webhook_attempt(self, attempt: WebhookAttempt) -> None:
        self.webhook_attempts.append(attempt)

    async def list_webhook_attempts(
        self,
        tenant_id: str,
        *,
        webhook_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[WebhookAttempt]:
        rows = [a for a in self.webhook_attempts if a.tenant_id == tenant_id]
        if webhook_id is not None:
            rows = [a for a in rows if a.webhook_id == webhook_id]
        if since is not None:
            rows = [a for a in rows if a.attempted_at >= since]
        rows = sorted(rows, key=lambda a: (a.attempted_at, a.id), reverse=True)
        return rows[:limit]

    async def get_webhook_cursor(self, tenant_id: str, webhook_id: str) -> str | None:
        # Tenant-scoping is enforced one layer up — the worker always
        # reads cursors only after looking up the tenant-scoped webhook
        # row, so a wrong-tenant cursor lookup is impossible by
        # construction. We still verify the webhook exists for that
        # tenant before returning a cursor, mirroring the DB backends.
        webhook = await self.get_webhook(tenant_id, webhook_id)
        if webhook is None:
            return None
        return self.webhook_cursors.get(webhook_id)

    async def set_webhook_cursor(self, tenant_id: str, webhook_id: str, last_event_id: str) -> None:
        # Same tenant-scope discipline as the cursor read.
        webhook = await self.get_webhook(tenant_id, webhook_id)
        if webhook is None:
            return
        self.webhook_cursors[webhook_id] = last_event_id

    # Eval-generation jobs (``mdk eval generate``) — runtime-resident,
    # SSE-driven async jobs. Kept on a simple dict keyed by job_id.
    # ------------------------------------------------------------------

    async def save_eval_generation_job(self, job: Any) -> None:
        # Lazy import keeps the type-hint stub from creating a circular
        # import with movate.core.eval_generator on module load.
        self._eval_generation_jobs[job.job_id] = job

    async def get_eval_generation_job(self, job_id: str, *, tenant_id: str) -> Any | None:
        job = self._eval_generation_jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id:
            return None
        return job

    async def commit_eval_cases(
        self,
        job_id: str,
        *,
        tenant_id: str,
        agents_path: Any,
        case_ids: list[str] | None,
        commit_judge: bool,
    ) -> Any:
        from pathlib import Path  # noqa: PLC0415

        from movate.core.eval_generator import serialize_case_for_dataset  # noqa: PLC0415
        from movate.storage.base import EvalCommitResult  # noqa: PLC0415

        job = await self.get_eval_generation_job(job_id, tenant_id=tenant_id)
        if job is None:
            raise FileNotFoundError(f"eval-generation job {job_id!r} not found")
        if job.result is None:
            raise ValueError(f"job {job_id!r} status={job.status!r} — no result to commit")
        cases = list(job.result.get("cases") or [])
        if case_ids is not None:
            wanted = set(case_ids)
            cases = [c for c in cases if c.get("id") in wanted]

        agent_dir = Path(agents_path) / job.agent_name
        if not agent_dir.is_dir():
            raise FileNotFoundError(f"agent dir not found: {agent_dir}")

        evals_dir = agent_dir / "evals"
        evals_dir.mkdir(parents=True, exist_ok=True)
        dataset = evals_dir / "dataset.jsonl"
        # Append-with-clean-line-boundary: if the existing file doesn't
        # end with \n, prepend one before our first line so two records
        # never run together. Atomic-enough for the local-serve path; the
        # registry write is the durable source for deploys.
        prior = dataset.read_bytes() if dataset.exists() else b""
        if prior and not prior.endswith(b"\n"):
            prior = prior + b"\n"
        with dataset.open("wb") as fh:
            fh.write(prior)
            for case in cases:
                fh.write(serialize_case_for_dataset(case))

        judge_updated = False
        if commit_judge:
            judge_blob = job.result.get("judge_yaml")
            if isinstance(judge_blob, str) and judge_blob.strip():
                (evals_dir / "judge.yaml").write_text(judge_blob, encoding="utf-8")
                judge_updated = True

        return EvalCommitResult(
            agent_name=job.agent_name,
            dataset_path=str(dataset.relative_to(agent_dir.parent)),
            cases_added=len(cases),
            judge_yaml_updated=judge_updated,
        )

    # Diagnoses (ADR 043 D1 — failure-pattern diagnoser)
    # ------------------------------------------------------------------

    async def save_diagnosis(self, record: DiagnosisRecord) -> None:
        # Upsert keyed by diagnosis_id. On overwrite, preserve the
        # insert-time fields the runtime layer never re-sends
        # (``created_at``) so a follow-up "completed" save doesn't
        # accidentally clobber the original timestamp.
        existing = self.diagnoses.get(record.diagnosis_id)
        if existing is not None:
            record = record.model_copy(update={"created_at": existing.created_at})
        self.diagnoses[record.diagnosis_id] = record

    async def get_diagnosis(self, diagnosis_id: str, *, tenant_id: str) -> DiagnosisRecord | None:
        row = self.diagnoses.get(diagnosis_id)
        if row is None:
            return None
        if row.tenant_id != tenant_id:
            # No-leak contract: cross-tenant probe is indistinguishable
            # from a missing record.
            return None
        return row

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
