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
    ConversationThread,
    Entity,
    EntityWithScore,
    EvalRecord,
    FailureRecord,
    FeedbackRecord,
    JobRecord,
    JobStatus,
    KbChunk,
    KbChunkWithScore,
    Relation,
    RunRecord,
    Subgraph,
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
        self.feedback: list[FeedbackRecord] = []
        self.kb_chunks: list[KbChunk] = []
        self.entities: list[Entity] = []
        self.relations: list[Relation] = []
        self.conversation_threads: list[ConversationThread] = []

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
        target: str | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        rows = self.jobs
        if tenant_id:
            rows = [j for j in rows if j.tenant_id == tenant_id]
        if status:
            rows = [j for j in rows if j.status == status]
        if target:
            rows = [j for j in rows if j.target == target]
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
