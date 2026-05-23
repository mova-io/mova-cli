"""StorageProvider Protocol — every implementation passes the same conformance suite.

v0.1 surface is intentionally narrow: runs + failures, plus list_runs for
``movate logs``. Jobs / API keys / evals join in v0.2 and v0.5 as their
phases ship.

**Tenant isolation (v1.0 stage 4).** Every read of a single record by id
takes a mandatory ``tenant_id`` kwarg and filters by it server-side, so a
caller authenticated as tenant A can never read tenant B's data even by
guessing ids. List methods that omit ``tenant_id`` reserve cross-tenant
reads for operator tooling (``movate worker --tenant-id=None`` drain
mode) — never exposed on the HTTP API. Mutating methods on per-tenant
rows (``update_job``, ``revoke_api_key``, ``touch_api_key``) likewise
require ``tenant_id`` so the WHERE clause stops cross-tenant writes at
the SQL layer, not just at the HTTP layer.

The one exception is ``get_api_key(key_id)`` — the auth middleware
parses the full ``mvt_<env>_<tenant>_<keyid>_<secret>`` key before
lookup and cross-checks the record's ``tenant_id`` against the
presented tenant prefix in ``check_record``. Tenant isolation on this
path is enforced by ``check_record``, not the storage method.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

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


class StorageProvider(Protocol):
    async def init(self) -> None:
        """Idempotent setup (schema migration, etc.)."""

    async def ping(self) -> None:
        """Cheap liveness check: validate the backend connection is alive.

        Used by ``GET /ready`` to gate ACA traffic — if this raises,
        the pod is reporting "not ready" and ACA stops routing to it
        without restarting it (the liveness probe on ``/healthz``
        stays green so the pod isn't killed for a transient blip).

        Implementations should make this as cheap as possible:
        sqlite does a ``SELECT 1``; postgres does a pool-acquire +
        ``SELECT 1``. Raises any backend error on failure (the
        caller catches and converts to a 503).
        """

    async def save_run(self, run: RunRecord) -> None: ...

    async def save_failure(self, f: FailureRecord) -> None: ...

    async def save_eval(self, e: EvalRecord) -> None: ...

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None: ...

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        """Exact lookup by run_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the run exists but belongs to
        a different tenant — same return shape either way so a caller
        can't probe for the existence of other tenants' runs.
        """

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        """Exact lookup by workflow_run_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the workflow run belongs to
        a different tenant.
        """

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        """Exact lookup by eval_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the eval belongs to a
        different tenant.
        """

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]: ...

    async def list_evals(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        """List evals newest-first, optionally filtered.

        ``tenant_id=None`` returns evals across all tenants — operator
        tooling only; never exposed on the HTTP API.
        """

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        """List workflow runs newest-first, optionally filtered.

        ``tenant_id=None`` returns runs across all tenants — operator
        tooling only; never exposed on the HTTP API.
        """

    # ------------------------------------------------------------------
    # Job queue (v0.5)
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        """Insert a brand-new ``QUEUED`` job. Errors on duplicate ``job_id``."""

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        """Exact lookup by job_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the job belongs to a
        different tenant — same return shape either way so a caller
        can't probe for the existence of other tenants' jobs.
        """

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        target: str | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        """List jobs newest-first, optionally filtered.

        Tenants must filter by ``tenant_id`` for the multi-tenant audit
        path. Listing across tenants (``tenant_id=None``) is reserved for
        operator tooling (``movate worker --all-tenants``) — never exposed
        on the HTTP API.

        ``target`` filters to one agent (or workflow) name — drives the
        Angular agent-profile page's "recent runs" tab via
        ``GET /api/v1/jobs?agent=<name>`` (item 74).
        """

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """Atomically claim the oldest ``QUEUED`` job and flip it to ``RUNNING``.

        Returns the now-claimed :class:`JobRecord` (with ``claimed_at`` set)
        or ``None`` if the queue is empty for this tenant.

        Implementations must guarantee no two callers ever return the same
        job — Postgres uses ``SELECT ... FOR UPDATE SKIP LOCKED``;
        sqlite uses ``BEGIN IMMEDIATE`` + atomic UPDATE.
        """

    async def update_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        status: JobStatus,
        result_run_id: str | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        """Transition a claimed job to a terminal status, scoped to ``tenant_id``.

        ``status`` must be one of ``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED``
        / ``DEAD_LETTER``; ``QUEUED`` and ``RUNNING`` are reserved for the
        lifecycle helpers (``save_job``, ``claim_next_job``, ``requeue_job``).
        Sets ``completed_at = now()`` as a side effect.

        The ``tenant_id`` filter on WHERE is the SQL-layer enforcement
        that prevents a misconfigured worker (or a direct storage call
        from a buggy path) from mutating another tenant's job. Silently
        no-op if no row matches both id + tenant.
        """

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Re-queue a ``RUNNING`` job after a transient failure.

        Sets status back to ``QUEUED``, clears ``claimed_at``, bumps
        ``attempt_count``, and stamps ``next_retry_at`` so the claim
        path skips this row until backoff elapses.

        The worker calls this instead of ``update_job`` when the
        dispatch outcome reports a retryable error AND the retry
        budget isn't exhausted (see :mod:`movate.core.job_retry`).
        Tenant-scoped in WHERE; silently no-ops on mismatch.
        """

    # ------------------------------------------------------------------
    # API keys (v0.5 stage 2)
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        """Persist a freshly-minted ApiKeyRecord (no plaintext secret)."""

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        """Exact lookup by key_id. Returns ``None`` if no match.

        The HTTP middleware uses this to resolve the presented key into
        a record for verification.
        """

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        """List keys for the management UI. Defaults to active keys only.

        ``tenant_id=None`` returns keys across all tenants — operator-only,
        never exposed on the HTTP API.
        """

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Set ``revoked_at`` to now, scoped to ``tenant_id``.

        Idempotent — re-revoking is a no-op. The ``tenant_id`` filter
        on WHERE prevents a tenant from revoking another tenant's keys
        by guessing key ids.
        """

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Bump ``last_used_at``, scoped to ``tenant_id``.

        Called fire-and-forget after a successful verify; failure to
        touch must not fail the request. The tenant filter is defense
        in depth — the auth middleware has already cross-checked the
        record's tenant matches the presented key, but the storage
        layer enforces it independently.
        """

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        """Return the budget row for ``tenant_id``, or ``None`` if no
        budget is set (= unlimited).

        Read on every ``Executor.execute`` entry, so implementations
        should be cheap (PK lookup; sub-millisecond).
        """

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        """Insert-or-update the row for ``budget.tenant_id``.

        Sets ``updated_at = now()`` server-side so the operator can see
        when a limit was last touched. ``created_at`` is preserved on
        update — only changes on the first insert for a tenant.
        """

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        """List all configured tenant budgets, oldest-first. Operator
        tooling only — never exposed on the HTTP API."""

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        """Sum ``runs.metrics.cost_usd`` for ``tenant_id`` for the
        current calendar month (UTC). 0.0 if no runs.

        ``Executor`` calls this at the top of every run to check
        against the budget; the cost-drift + per-run budget checks
        later in execute() are independent of this. Index on
        ``(tenant_id, created_at)`` is the perf path.
        """

    # ------------------------------------------------------------------
    # Run feedback (added 2026-05-19) — Chainlit playground writes here.
    # ------------------------------------------------------------------

    async def save_feedback(self, feedback: FeedbackRecord) -> None:
        """Persist a :class:`FeedbackRecord`. Idempotent on
        ``feedback_id``: re-saving the same id updates score / comment
        / dimensions in place (operators can edit their feedback).
        """

    async def list_feedback(
        self,
        *,
        run_id: str | None = None,
        agent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        """List feedback rows ordered created_at DESC. Filters AND
        together. Used by the analytics dashboard + by the playground
        when the operator re-opens a run they previously rated.
        """

    # ------------------------------------------------------------------
    # KB chunks (added 0.8.2.13) — vector retrieval MVP. The retrieval
    # primitive is cosine similarity computed in Python over JSONB-
    # stored float arrays; pgvector will swap in later behind the same
    # protocol surface.
    # ------------------------------------------------------------------

    async def save_kb_chunk(self, chunk: KbChunk) -> None:
        """Persist a :class:`KbChunk`. Upsert on ``(agent, tenant_id,
        content_hash)``: re-ingesting an unchanged document is idempotent
        (existing chunks updated in place, not duplicated). Chunks whose
        ``content_hash`` already exists for the agent get their
        ``embedding`` + ``embedding_model`` + ``metadata`` refreshed.
        """

    async def search_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """Top-K most-similar chunks for the agent's KB.

        Implementation: load all chunks matching ``(agent, tenant_id)``
        from storage, compute cosine similarity against
        ``query_embedding`` in Python, sort descending, return the top
        ``limit``. Acceptable for KBs up to ~10k chunks; beyond that
        the linear scan becomes a bottleneck and you'd want a real
        vector index (pgvector / sqlite-vss).

        Empty KB returns ``[]`` cleanly — no special-case needed.
        """

    async def search_kb_chunks_lexical(
        self,
        *,
        agent: str,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """Full-text BM25 lexical search over ``text`` column.

        SQLite uses FTS5 + native ``bm25()`` ranking.
        Postgres uses ``to_tsvector`` + GIN index + ``ts_rank``.
        InMemory falls back to the Python BM25 scorer in
        :func:`movate.kb.lexical.bm25_search`.

        Returns up to ``limit`` chunks ranked by relevance.
        Empty query or no matching terms → empty list.
        NEVER raises — same graceful contract as the other
        retrieval helpers.
        """

    async def list_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
        limit: int = 1000,
    ) -> list[KbChunk]:
        """List chunks for inspection / debugging. Filters AND
        together. Returns embeddings + text + metadata; callers that
        only need text should slice their fields after this returns
        rather than this method maintaining a thin variant."""

    async def delete_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        """Delete chunks scoped to an agent. When ``source`` is set,
        only chunks from that source URI are removed (re-ingest with
        --replace workflow). Returns the count deleted."""

    async def reindex_kb(self, *, agent: str, tenant_id: str) -> int:
        """Rebuild the backend's vector index from the stored chunk
        vectors and return the number of chunks indexed for ``(agent,
        tenant_id)``.

        Used by ``mdk kb reindex`` (and the runtime's ``POST
        .../kb/reindex``) to recover from a degraded index or to pick up
        new index parameters WITHOUT re-embedding — the stored vectors
        are reused as-is. Re-embedding (when the model/dim changes) is
        orchestrated one layer up in ``kb``/cli, which re-embeds each
        chunk's text and persists via :meth:`save_kb_chunk` before
        calling this; the storage layer never imports the embedder.

        Backends with a real vector index (Postgres / pgvector) drop and
        re-create it here. Backends that brute-force search (sqlite,
        in-memory) have no index to rebuild and return the chunk count as
        a graceful no-op — NEVER raise. The HNSW index on Postgres is
        global to the ``kb_chunks`` table, not per-agent, so rebuilding
        it serves every agent; the returned count is still scoped to
        ``(agent, tenant_id)`` so callers can report what they touched."""

    # ------------------------------------------------------------------
    # Conversation threads (Tier 10.5, added 0.8.2.27 / PR-N) — group
    # runs together so multi-turn agents can fetch prior context when
    # rendering the next message's prompt. Runtime endpoint + Chainlit
    # thread-aware mode land in follow-up PRs.
    # ------------------------------------------------------------------

    async def save_conversation_thread(self, thread: ConversationThread) -> None:
        """Persist a :class:`ConversationThread`. Idempotent on
        ``thread_id``: re-saving the same id refreshes ``title`` /
        ``updated_at`` (clients call this each time they append a
        message so the thread sorts most-recently-active first)."""

    async def get_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> ConversationThread | None:
        """Fetch a thread by id, scoped to ``tenant_id``. Returns
        ``None`` if the thread doesn't exist OR belongs to a different
        tenant — never leaks existence across tenants (mirrors the
        single-record-by-id contract on every storage method)."""

    async def list_conversation_threads(
        self,
        *,
        tenant_id: str,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[ConversationThread]:
        """List threads for a tenant, ordered ``updated_at DESC`` so
        the active conversations float to the top. Optional ``agent``
        filter when the client wants threads for one specific agent
        (Chainlit's typical case — one tab per agent picker)."""

    async def list_runs_for_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[RunRecord]:
        """Fetch runs that belong to ``thread_id``, ordered
        ``created_at ASC`` (chronological — earliest turn first) so the
        runtime can render the conversation history straight from the
        list without an extra reverse. Tenant-scoped: a cross-tenant
        thread id returns ``[]`` rather than raising or leaking."""

    async def delete_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        """Hard-delete a thread row scoped to ``tenant_id``.

        Returns True when a row was deleted, False when no matching
        thread existed (or it belonged to a different tenant — same
        404-not-403 semantics as ``get_conversation_thread``).

        Runs that referenced the thread_id stay in storage; their
        ``thread_id`` column becomes a dangling reference (the runs
        still exist but ``list_runs_for_thread`` returns them only
        when the operator queries by the now-deleted thread id, which
        is fine — operators delete a thread when they don't want to
        see it anymore, not when they want to nuke the historical
        runs themselves)."""

    # ------------------------------------------------------------------
    # Knowledge graph (GraphRAG) — entities + relations layered over the
    # KB chunks. Storage mirrors kb_chunks: embeddings as JSONB/TEXT float
    # arrays, cosine in Python (pgvector swap stays behind this surface).
    # The ONLY traversal primitive exposed is ``expand_neighbors`` (bounded
    # k-hop) — no raw query language crosses the Protocol boundary, so a
    # future Neo4jProvider implements the same contract without leaking
    # Cypher to callers.
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> None:
        """Persist an :class:`Entity`. Upsert on ``(agent, tenant_id,
        content_hash)``: re-ingesting the same corpus refreshes
        ``description`` / ``embedding`` / ``embedding_model`` / ``metadata``
        and UNIONs ``source_chunk_ids`` in place rather than duplicating
        the node. The dedup key is ``content_hash`` (SHA-256 of normalized
        name+type), so two extractions of the same real-world entity
        collapse to one row."""

    async def upsert_relation(self, relation: Relation) -> None:
        """Persist a :class:`Relation`. Upsert on ``(agent, tenant_id,
        content_hash)``; UNIONs ``source_chunk_ids`` on conflict.

        The caller MUST upsert both endpoint entities before the relation —
        the storage layer does not auto-create dangling endpoints. It does
        not enforce referential integrity either (no FK): an edge whose
        endpoint was deleted simply never appears in an expansion because
        the join drops it. Keeps the write path cheap and backend-portable."""

    async def search_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[EntityWithScore]:
        """Top-K most-similar entities for the agent's graph — the vector
        SEED step of GraphRAG retrieval.

        Same primitive as :meth:`search_kb_chunks`: load entities matching
        ``(agent, tenant_id)``, compute cosine against ``query_embedding``
        in Python, return the top ``limit``. Empty graph returns ``[]``.
        Callers feed the resulting ``entity_id``s into
        :meth:`expand_neighbors`."""

    async def expand_neighbors(
        self,
        *,
        agent: str,
        tenant_id: str,
        entity_ids: list[str],
        hops: int = 1,
        limit: int = 50,
    ) -> Subgraph:
        """Bounded k-hop expansion from ``entity_ids`` — the ONLY traversal
        primitive. Returns the reached entities (including the seeds) plus
        every relation traversed, as a flat :class:`Subgraph`.

        ``hops`` caps traversal depth; ``limit`` caps the total number of
        relations followed (the budget guard against a hub node exploding
        the result). Edges are followed in descending ``weight`` order so a
        truncated expansion keeps the strongest relationships. Traversal is
        undirected for reachability (an edge connects its endpoints both
        ways) — direction is preserved in the returned ``Relation`` rows for
        the caller to interpret.

        Implementations: recursive CTE over ``kb_relations`` on sqlite /
        postgres; breadth-first walk in :class:`InMemoryStorage`. Unknown or
        cross-tenant ``entity_ids`` contribute nothing rather than raising —
        same no-leak contract as the single-record getters. Empty
        ``entity_ids`` → empty :class:`Subgraph`."""

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        """Exact lookup by entity_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the entity belongs to a different
        tenant — same 404-not-403 shape as every other single-record
        getter, so a caller can't probe for other tenants' nodes."""

    async def list_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        source_chunk_id: str | None = None,
        limit: int = 1000,
    ) -> list[Entity]:
        """List entities for inspection / debugging. When ``source_chunk_id``
        is set, returns only entities extracted from that chunk (drives
        provenance views — "what did this passage contribute to the
        graph?"). Filters AND together. Empty graph → ``[]``."""

    async def list_relations(
        self,
        *,
        agent: str,
        tenant_id: str,
        limit: int = 1000,
    ) -> list[Relation]:
        """List relations for inspection / debugging, scoped to
        ``(agent, tenant_id)``. Empty graph → ``[]``."""

    async def delete_graph(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        """Delete an agent's graph, scoped to ``tenant_id``. When ``source``
        is set, removes only entities/relations whose ``source_chunk_ids``
        trace to chunks from that source URI (the per-source re-ingest
        workflow, mirroring :meth:`delete_kb_chunks`). Returns the total
        rows deleted (entities + relations)."""

    async def close(self) -> None: ...
