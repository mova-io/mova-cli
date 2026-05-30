"""PostgreSQL-backed StorageProvider for production deployments.

Same Protocol surface as :class:`SqliteProvider`, same conformance
test suite. Differences are pure mechanics:

* JSON columns use ``JSONB`` (indexed, queryable) instead of ``TEXT``.
* Timestamps use ``TIMESTAMPTZ`` instead of ISO strings.
* Booleans use ``BOOLEAN`` instead of ``INTEGER``.
* The job-claim path uses ``SELECT ... FOR UPDATE SKIP LOCKED`` —
  superior to sqlite's ``BEGIN IMMEDIATE`` because finer-grained row
  locks let multiple workers truly run concurrently.

The pool is configured once with a per-connection ``init`` callback
that registers a ``json.dumps`` / ``json.loads`` codec for ``jsonb``,
so callers pass and receive plain dicts.

Connection string is read from ``MOVATE_DB_URL`` (e.g.
``postgresql://user:pw@host:5432/movate``); see :func:`build_storage`
in :mod:`movate.storage`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from movate.core.dr_backup import ImportResult
from movate.core.events import Event
from movate.core.job_retry import ReclaimResult
from movate.core.models import (
    _DEFAULT_PROJECT_DESCRIPTION,
    _DEFAULT_PROJECT_NAME,
    _TENANT_SYSTEM_PRINCIPAL,
    AgentBundleRecord,
    ApiKeyEnv,
    ApiKeyRecord,
    AuditFinding,
    AuditFindingSeverity,
    AuditRecord,
    BatchRecord,
    BenchModelResult,
    BenchRecord,
    CanaryConfig,
    CatalogEntry,
    CatalogEntryVersion,
    CatalogRatingsSummary,
    CatalogSource,
    ConversationThread,
    DiagnosisRecord,
    DiagnosisStatus,
    Entity,
    EntityWithScore,
    ErrorInfo,
    EvalRecord,
    EvalSchedule,
    FailureRecord,
    FeedbackRecord,
    JobKind,
    JobRecord,
    JobSchedule,
    JobStatus,
    JudgeMethod,
    KbChunk,
    KbChunkWithScore,
    Metrics,
    Project,
    ProjectKbMode,
    ProjectMember,
    ProjectMemberRole,
    Relation,
    RunRecord,
    Session,
    SessionMessage,
    SkillCallRecord,
    Subgraph,
    TenantBudget,
    TenantProviderKey,
    Trigger,
    TurnRecord,
    WorkflowBundleRecord,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.observability.models import ObservabilityInsight
from movate.core.webhooks import WebhookAttempt, WebhookSubscription
from movate.storage._cosine import rank_chunks_by_cosine as _rank_chunks_by_cosine  # noqa: F401

logger = logging.getLogger(__name__)

# Embedding dimension for the KB ``vector(N)`` column (ADR 009 D1). A vector
# column needs a fixed N; one embedding model per deployment is the product
# reality. Override via MOVATE_EMBED_DIM for a non-1536-dim model.
_EMBED_DIM_DEFAULT = 1536


def _embedding_dim() -> int:
    """Configured embedding dimension (``MOVATE_EMBED_DIM``, default 1536).

    Read here independently (storage must not import ``kb``); the documented
    twin for ``kb`` / runtime / doctor callers is ``kb.embed.embedding_dim``.
    Both read the same env var, so they agree.
    """
    import os  # noqa: PLC0415

    raw = os.environ.get("MOVATE_EMBED_DIM", "").strip()
    if not raw:
        return _EMBED_DIM_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        return _EMBED_DIM_DEFAULT
    return n if n > 0 else _EMBED_DIM_DEFAULT


def _vec_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal — ``[0.1,0.2,...]``.

    We bind vectors as text + an explicit ``::vector`` cast rather than a
    client-side codec, so the runtime needs no numpy/pgvector-python at query
    time — only the server-side extension. pgvector's ``vector_in`` tolerates
    the whitespace JSONB casts produce, and its text output is JSON-parseable
    on the way back (see ``_parse_embedding``).
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


@dataclass(frozen=True, slots=True)
class PoolStats:
    """A point-in-time snapshot of the asyncpg pool's connection counts.

    Plain data — storage NEVER imports ``tracing`` (boundary rule 6). The edge
    (``mdk serve`` / ``mdk worker``) reads this and feeds it to the OTel
    observable gauges in :mod:`movate.tracing.metrics` (ADR 034 D3); ``mdk
    doctor`` reads ``max_size`` for the connection-ceiling headroom check (D1).

    Fields mirror the asyncpg ``Pool`` getters:

    * ``size``    — connections currently held (open) by the pool.
    * ``idle``    — of those, how many are checked **in** (available).
    * ``in_use``  — ``size - idle``: connections currently checked **out**.
    * ``waiting`` — callers blocked waiting for a free connection (the pool's
      internal acquire queue). The early-warning saturation signal: a sustained
      non-zero value means the per-pod pool is the bottleneck.
    * ``max_size`` — the configured per-pod ceiling (``create_pool(max_size=)``).
      The denominator for pool-saturation (``in_use / max_size``) and the input
      to the doctor capacity formula ``pods x max_size <= max_connections - headroom``.
    * ``min_size`` — the configured floor (warm connections).
    """

    size: int
    idle: int
    in_use: int
    waiting: int
    max_size: int
    min_size: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    agent            TEXT NOT NULL,
    agent_version   TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    pricing_version  TEXT NOT NULL,
    status           TEXT NOT NULL,
    input            JSONB NOT NULL,
    output           JSONB,
    metrics          JSONB NOT NULL,
    error            JSONB,
    created_at       TIMESTAMPTZ NOT NULL,
    workflow_run_id  TEXT,
    node_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_agent_created
    ON runs(agent, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_workflow_run
    ON runs(workflow_run_id) WHERE workflow_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS failures (
    failure_id   TEXT PRIMARY KEY,
    run_id       TEXT,
    tenant_id    TEXT NOT NULL,
    agent        TEXT NOT NULL,
    failure_type TEXT NOT NULL,
    message      TEXT NOT NULL,
    retryable    BOOLEAN NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evals (
    eval_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent           TEXT NOT NULL,
    agent_version   TEXT NOT NULL,
    dataset_hash    TEXT NOT NULL,
    judge_method    TEXT NOT NULL,
    judge_provider  TEXT,
    runs_per_case   INTEGER NOT NULL,
    gate_mode       TEXT NOT NULL,
    threshold       DOUBLE PRECISION NOT NULL,
    mean_score      DOUBLE PRECISION NOT NULL,
    pass_rate       DOUBLE PRECISION NOT NULL,
    sample_count    INTEGER NOT NULL,
    total_cost_usd  DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evals_agent_created
    ON evals(agent, created_at DESC);

CREATE TABLE IF NOT EXISTS bench (
    bench_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent           TEXT NOT NULL,
    agent_version   TEXT NOT NULL,
    input           JSONB NOT NULL,
    judge_method    TEXT,
    judge_provider  TEXT,
    runs_per_model  INTEGER NOT NULL,
    gate_mode       TEXT NOT NULL,
    models          JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_agent_created
    ON bench(agent, created_at DESC);

-- Claude-orchestrated audit records (read-only audit pipeline). Findings +
-- categories are JSONB. One immutable row per terminal audit, tenant-scoped
-- at the row level. Additive + idempotent — backfill not needed.
CREATE TABLE IF NOT EXISTS audits (
    audit_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    scope_kind      TEXT NOT NULL,
    scope_id        TEXT NOT NULL,
    categories      JSONB NOT NULL,
    severity_floor  TEXT NOT NULL,
    model           TEXT NOT NULL,
    budget_usd      DOUBLE PRECISION NOT NULL,
    findings        JSONB NOT NULL,
    partial         BOOLEAN NOT NULL DEFAULT FALSE,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audits_scope_created
    ON audits(tenant_id, scope_id, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_run_id   TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    workflow          TEXT NOT NULL,
    workflow_version  TEXT NOT NULL,
    status            TEXT NOT NULL,
    initial_state     JSONB NOT NULL,
    final_state       JSONB,
    error_node_id     TEXT,
    error             JSONB,
    created_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_created
    ON workflow_runs(workflow, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    target        TEXT NOT NULL,
    status        TEXT NOT NULL,
    input         JSONB NOT NULL,
    result_run_id TEXT,
    error         JSONB,
    api_key_id    TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    claimed_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    notify_email  TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ
);
-- Upgrade paths for PG instances created on older schemas. PG natively
-- supports ADD COLUMN IF NOT EXISTS so this is idempotent on every init.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS notify_email TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_jobs_queue_head
    ON jobs(tenant_id, created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created
    ON jobs(tenant_id, created_at DESC);
-- Retry-aware claim path: lets `claim_next_job` skip jobs whose
-- next_retry_at hasn't elapsed without a table scan. Common-case
-- (next_retry_at IS NULL, never-failed) bypasses this index via the
-- existing idx_jobs_queue_head.
CREATE INDEX IF NOT EXISTS idx_jobs_retry_at
    ON jobs(tenant_id, next_retry_at)
    WHERE status = 'queued' AND next_retry_at IS NOT NULL;

-- Per-tenant monthly cost ceiling. Absent row = unlimited.
CREATE TABLE IF NOT EXISTS tenant_budgets (
    tenant_id          TEXT PRIMARY KEY,
    monthly_usd_limit  DOUBLE PRECISION,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL
);
-- Cover the per-tenant current-month aggregation. Without this,
-- SUM(metrics->>'cost_usd') WHERE tenant_id=$1 AND created_at>=$2 is
-- a Seq Scan over the full runs table; with it, an index range scan
-- bounded to the month's rows for that tenant.
CREATE INDEX IF NOT EXISTS idx_runs_tenant_created
    ON runs(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    env           TEXT NOT NULL,
    secret_hash   TEXT NOT NULL,
    salt          TEXT NOT NULL,
    label         TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,
    scope         TEXT,
    scopes        JSONB
);
-- Idempotent self-healing migrations for tables that pre-date a
-- column. `CREATE TABLE IF NOT EXISTS` is a no-op when the table is
-- already there, so any column added after the table was first
-- created has to be ALTERed in explicitly. Without this block,
-- upgrading a runtime against a long-lived database surfaces as
-- `UndefinedColumnError` on the first query that names the new
-- column (caught live on dev when an older deployment had created
-- `api_keys` without `expires_at` or `scope`).
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scope TEXT;
-- ADR 013 L2: least-privilege scope SET (supersedes the single `scope`
-- above). JSONB array of scope strings; NULL on a legacy row resolves to
-- the default {read,run,eval} at read time via `effective_scopes` — no
-- destructive backfill.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scopes JSONB;
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_active
    ON api_keys(tenant_id) WHERE revoked_at IS NULL;

-- Operator feedback on runs. Captured by the Chainlit playground
-- (or any client POSTing to /api/v1/runs/{id}/feedback). Mirrored
-- to Langfuse as a score when Langfuse is configured.
CREATE TABLE IF NOT EXISTS run_feedback (
    feedback_id        TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    tenant_id          TEXT NOT NULL,
    agent              TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    score              SMALLINT NOT NULL,
    dimensions         JSONB,
    comment            TEXT,
    langfuse_score_id  TEXT,
    created_at         TIMESTAMPTZ NOT NULL
);
-- Per-run lookup (playground re-opening a rated run).
CREATE INDEX IF NOT EXISTS idx_run_feedback_run_id
    ON run_feedback(run_id);
-- Per-agent + time aggregation (dashboard: "agent X over last 30d").
CREATE INDEX IF NOT EXISTS idx_run_feedback_agent_created
    ON run_feedback(agent, created_at DESC);
-- Per-tenant listing (analytics scoped to a workspace).
CREATE INDEX IF NOT EXISTS idx_run_feedback_tenant_created
    ON run_feedback(tenant_id, created_at DESC);

-- KB chunks for vector retrieval (added 0.8.2.13). The base column is
-- JSONB here; migration ``001_kb_embedding_to_vector`` converts it to
-- pgvector ``vector(N)`` + an HNSW index and search runs as a SQL `<=>`
-- ANN query (ADR 009). The base stays JSONB so this CREATE works without
-- the extension and the conversion lives in exactly one place (the
-- migration), handling fresh and existing DBs uniformly.
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent           TEXT NOT NULL,
    source          TEXT NOT NULL,
    text            TEXT NOT NULL,
    embedding       JSONB NOT NULL,
    embedding_model TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL,
    -- PR-EE: OCR provenance flag. True when the chunk's text was
    -- extracted via Tesseract OCR rather than native text extraction
    -- (pypdf text layer, docx paragraphs, readability HTML strip).
    -- Default false so existing rows (native extraction) are
    -- unaffected on schema upgrade.
    ocr             BOOLEAN NOT NULL DEFAULT false
);
-- Per-agent + per-tenant retrieval scope. Search scans this index
-- range so the Python cosine loop touches only the relevant chunks.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_agent_tenant
    ON kb_chunks(agent, tenant_id);
-- Dedup: re-ingesting the same content for the same agent is a no-op
-- via this unique constraint + ON CONFLICT in the upsert path.
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_chunks_dedup
    ON kb_chunks(agent, tenant_id, content_hash);
-- Per-source listing (mdk kb ls --source <path>).
CREATE INDEX IF NOT EXISTS idx_kb_chunks_source
    ON kb_chunks(agent, tenant_id, source);
-- PR-AA: GIN index on tsvector for native full-text BM25 search.
-- Indexed with the 'english' text search configuration (stemming +
-- stopwords). Queries use plainto_tsquery('english', ...) which
-- converts free text to AND-connected lexemes — same semantics as
-- the Python BM25 scorer in movate.kb.lexical.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_text_gin
    ON kb_chunks USING gin(to_tsvector('english', text));

-- Conversation threads (PR-N) — group runs for multi-turn agents.
-- Runs link via the new ``runs.thread_id`` column (added below as an
-- ADD COLUMN IF NOT EXISTS — idempotent so re-running init() on an
-- upgraded database is safe).
CREATE TABLE IF NOT EXISTS conversation_threads (
    thread_id   TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_tenant_updated
    ON conversation_threads(tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_agent_tenant
    ON conversation_threads(agent, tenant_id);

-- Per-run thread linkage. NULL = standalone (non-threaded) run.
-- Postgres supports ADD COLUMN IF NOT EXISTS so re-running init() on
-- a database that already has the column is a no-op.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS thread_id TEXT;
CREATE INDEX IF NOT EXISTS idx_runs_thread
    ON runs(thread_id, created_at)
    WHERE thread_id IS NOT NULL;

-- ADR 024 D2 — per-step observability retention. JSONB arrays (same strategy
-- as runs.metrics): the executor populates them and they round-trip through
-- save_run / _row_to_run so `mdk explain` reconstructs the turn → skill /
-- retrieval tree OFFLINE (no Langfuse needed). Additive + idempotent
-- (ADD COLUMN IF NOT EXISTS); NULL on pre-migration rows → empty list, so
-- legacy records load fine. `skill_calls` was previously not persisted by the
-- DB providers; retaining it alongside the new `turns` keeps the offline
-- breakdown coherent.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS skill_calls JSONB;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS turns JSONB;

-- PR-Q: jobs carry the thread linkage from queue time so the worker
-- can propagate it onto the spawned run. NULL = standalone.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS thread_id TEXT;

-- GraphRAG: knowledge-graph entities + relations layered over kb_chunks.
-- embedding / source_chunk_ids / metadata are JSONB (same strategy as
-- kb_chunks; pgvector swap stays behind the storage protocol). Dedup via
-- the unique (agent, tenant_id, content_hash) index + ON CONFLICT.
CREATE TABLE IF NOT EXISTS kb_entities (
    entity_id        TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    agent            TEXT NOT NULL,
    name             TEXT NOT NULL,
    type             TEXT NOT NULL,
    description      TEXT,
    embedding        JSONB NOT NULL,
    embedding_model  TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    source_chunk_ids JSONB,
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_entities_agent_tenant
    ON kb_entities(agent, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_entities_dedup
    ON kb_entities(agent, tenant_id, content_hash);

CREATE TABLE IF NOT EXISTS kb_relations (
    relation_id      TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    agent            TEXT NOT NULL,
    src_entity_id    TEXT NOT NULL,
    dst_entity_id    TEXT NOT NULL,
    type             TEXT NOT NULL,
    description      TEXT,
    weight           DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    content_hash     TEXT NOT NULL,
    source_chunk_ids JSONB,
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_relations_dedup
    ON kb_relations(agent, tenant_id, content_hash);
-- Endpoint indexes power the recursive-CTE k-hop traversal join.
CREATE INDEX IF NOT EXISTS idx_kb_relations_src
    ON kb_relations(agent, tenant_id, src_entity_id);
CREATE INDEX IF NOT EXISTS idx_kb_relations_dst
    ON kb_relations(agent, tenant_id, dst_entity_id);

-- ADR 046 D1: project-scope the knowledge graph. Additive nullable column
-- on both graph tables (ADD COLUMN IF NOT EXISTS — idempotent) so the
-- viewer / query API can window a subgraph at the PROJECT grain across an
-- agent's KBs, not only per agent. Pre-migration rows + project-less
-- ingests carry NULL → absent from a project-filtered query, fully visible
-- in the unfiltered per-agent view. Backward-compatible by construction.
-- Partial indexes keep project-filtered scans cheap without bloating the
-- common NULL case.
ALTER TABLE kb_entities ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE kb_relations ADD COLUMN IF NOT EXISTS project_id TEXT;
CREATE INDEX IF NOT EXISTS idx_kb_entities_project
    ON kb_entities(agent, tenant_id, project_id) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kb_relations_project
    ON kb_relations(agent, tenant_id, project_id) WHERE project_id IS NOT NULL;

-- ADR 014 D1: durable agent registry. One immutable row per published
-- (name, version) bundle, tenant-scoped; the ``files`` map is JSONB (same
-- strategy as bench.models / workflow_runs.initial_state). Additive new
-- table (CREATE TABLE IF NOT EXISTS, re-run idempotently on every init) —
-- no ALTER, no backfill. A new publish = a new row; rows are never mutated.
CREATE TABLE IF NOT EXISTS agent_bundles (
    name          TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    version       TEXT NOT NULL,
    created_by    TEXT,
    content_hash  TEXT NOT NULL,
    files         JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, name, version)
);
-- Per-tenant + per-name registry lookup (latest / version history).
CREATE INDEX IF NOT EXISTS idx_agent_bundles_name
    ON agent_bundles(tenant_id, name);
-- Latest-version-per-name + history ordering scan.
CREATE INDEX IF NOT EXISTS idx_agent_bundles_name_created
    ON agent_bundles(tenant_id, name, created_at DESC);

-- ADR 037 D1: durable workflow registry. Workflow analogue of agent_bundles —
-- one immutable (name, version) row per publish, tenant-scoped, ``files`` is
-- JSONB. ``published`` is the operator promote/revert flag (ADR 037 D1) —
-- at most one True per (tenant, name) at any time (enforced application-side
-- by publish_workflow_version, mirroring the agent registry's "no schema
-- constraint, behavior-enforced" pattern). Additive new table (CREATE TABLE
-- IF NOT EXISTS, re-run idempotently on every init) — no ALTER, no backfill.
CREATE TABLE IF NOT EXISTS workflow_bundles (
    name          TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    version       TEXT NOT NULL,
    created_by    TEXT,
    content_hash  TEXT NOT NULL,
    files         JSONB NOT NULL,
    published     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_workflow_bundles_name
    ON workflow_bundles(tenant_id, name);
CREATE INDEX IF NOT EXISTS idx_workflow_bundles_name_created
    ON workflow_bundles(tenant_id, name, created_at DESC);

-- ADR 016 D2: continuous-eval schedules. One row per (tenant, agent) with a
-- cadence; the scheduler tick enqueues EVAL jobs for due rows. Additive new
-- table (CREATE TABLE IF NOT EXISTS, idempotent on every init) — default-off,
-- no ALTER, no backfill.
CREATE TABLE IF NOT EXISTS eval_schedules (
    tenant_id            TEXT NOT NULL,
    agent                TEXT NOT NULL,
    cadence_seconds      INTEGER NOT NULL,
    enabled              BOOLEAN NOT NULL,
    mock                 BOOLEAN NOT NULL,
    runs                 INTEGER NOT NULL,
    gate_mode            TEXT NOT NULL,
    gate                 DOUBLE PRECISION NOT NULL,
    objective            TEXT,
    regression_tolerance DOUBLE PRECISION NOT NULL,
    baseline_id          TEXT,
    notify_email         TEXT,
    created_by           TEXT,
    created_at           TIMESTAMPTZ NOT NULL,
    last_enqueued_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, agent)
);

-- ADR 017 D2: generic agent/workflow cron schedules. One row per
-- (tenant, name) with a cadence + a job payload; the scheduler tick
-- enqueues a JobKind.AGENT/WORKFLOW job for due rows. Additive new table
-- (CREATE TABLE IF NOT EXISTS, idempotent on every init) — default-off,
-- no ALTER, no backfill. ``input`` is JSONB (same codec as jobs.input).
CREATE TABLE IF NOT EXISTS job_schedules (
    tenant_id        TEXT NOT NULL,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL,
    target           TEXT NOT NULL,
    cadence_seconds  INTEGER NOT NULL,
    enabled          BOOLEAN NOT NULL,
    input            JSONB NOT NULL,
    notify_email     TEXT,
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL,
    last_enqueued_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, name)
);

-- ADR 017 D2: inbound event/webhook triggers. One row per (tenant, name)
-- with a public trigger_id (in the webhook URL), a hashed-at-rest per-trigger
-- secret, and default-input merged under the inbound event body. The fire
-- endpoint resolves by trigger_id (no tenant context) and enqueues a
-- JobKind.AGENT/WORKFLOW job. Additive new table (CREATE TABLE IF NOT EXISTS,
-- idempotent) — default-off, no ALTER, no backfill. ``input_defaults`` is
-- JSONB (same codec as jobs.input); trigger_id is uniquely indexed for the
-- fire-path lookup.
CREATE TABLE IF NOT EXISTS triggers (
    tenant_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    trigger_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL,
    secret_hash    TEXT NOT NULL,
    salt           TEXT NOT NULL,
    input_defaults JSONB NOT NULL,
    enabled        BOOLEAN NOT NULL,
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    last_fired_at  TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_triggers_trigger_id ON triggers(trigger_id);

-- item 23: trigger replay / idempotency (ADR 017 D2 follow-up). One row per
-- (trigger_id, delivery_id) recording the job_id the FIRST delivery enqueued,
-- so an at-least-once webhook retry returns the same job instead of double-
-- enqueuing. Additive new table (CREATE TABLE IF NOT EXISTS, idempotent) — a
-- row exists only when a fire request carried an X-Movate-Delivery-Id header,
-- so the no-header path is unchanged. The composite PRIMARY KEY makes
-- record_trigger_delivery's INSERT ... ON CONFLICT DO NOTHING an atomic dedup:
-- a concurrent double-delivery races to one winner.
CREATE TABLE IF NOT EXISTS trigger_deliveries (
    trigger_id  TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trigger_id, delivery_id)
);

-- item 37: submission idempotency. One row per (tenant_id, idempotency_key)
-- recording the job_id the FIRST async submit enqueued, so a client retry
-- (network blip / timeout) returns the same job instead of double-enqueuing.
-- Mirrors trigger_deliveries above but is per-TENANT scoped (the submit path
-- has an AuthContext). Additive new table (CREATE TABLE IF NOT EXISTS,
-- idempotent) — a row exists only when a submit carried an Idempotency-Key
-- header, so the no-header path is unchanged. The composite PRIMARY KEY makes
-- record_run_submission's INSERT ... ON CONFLICT DO NOTHING an atomic dedup:
-- a concurrent retry races to one winner.
CREATE TABLE IF NOT EXISTS run_submissions (
    tenant_id       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key)
);

-- ADR 016 D3: canary / champion-challenger rollout. One row per (tenant,
-- agent): a challenger version + a traffic weight (0 = kill switch), with an
-- optional champion pin + auto-promote eval gate. The run/enqueue path reads
-- this to choose champion vs challenger; NO row → champion-by-latest →
-- byte-for-byte the pre-canary behavior. Additive new table (CREATE TABLE IF
-- NOT EXISTS, idempotent) — default-off, no ALTER, no backfill.
CREATE TABLE IF NOT EXISTS canary_configs (
    tenant_id          TEXT NOT NULL,
    agent              TEXT NOT NULL,
    challenger_version TEXT NOT NULL,
    champion_version   TEXT,
    weight             INTEGER NOT NULL,
    sticky             BOOLEAN NOT NULL,
    enabled            BOOLEAN NOT NULL,
    auto_promote       BOOLEAN NOT NULL,
    eval_gate          DOUBLE PRECISION,
    created_by         TEXT,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, agent)
);

-- ADR 016 D5: opt-in auto-rollback. A scheduled-eval drift regression on the
-- challenger trips the kill switch (weight → 0) when set. Additive +
-- default-off (DEFAULT FALSE): pre-D5 canary rows read back as
-- auto_rollback=False → alert-only, byte-for-byte the pre-D5 behavior. ADD
-- COLUMN IF NOT EXISTS keeps it idempotent on every init.
ALTER TABLE canary_configs ADD COLUMN IF NOT EXISTS auto_rollback BOOLEAN NOT NULL DEFAULT FALSE;

-- ADR 016 D3: carry the canary-chosen agent version to the async worker. The
-- enqueue path stamps the concrete champion/challenger version it picked; the
-- worker resolves THAT version. Nullable — pre-canary rows (and every job
-- with no canary in play) read back as NULL → None → resolve latest,
-- unchanged. ADD COLUMN IF NOT EXISTS keeps it idempotent on every init.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS target_version TEXT;

-- ADR 017 D5 (PR 1): HITL pause checkpoint on workflow_runs. When a workflow
-- pauses at a HUMAN gate the runner stamps these three columns; PR 2's
-- resume-on-signal path reads them to continue. All nullable — existing rows
-- (and every non-paused SUCCESS/ERROR run) read back as NULL → None, so the
-- change is additive + backward compatible. paused_state / human_task are
-- JSONB (same codec as workflow_runs.initial_state). ADD COLUMN IF NOT EXISTS
-- keeps it idempotent on every init.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS paused_node_id TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS paused_state JSONB;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS human_task JSONB;

-- ADR 017 D5 (PR 2): resume-on-signal. The signal endpoint enqueues a
-- JobKind.WORKFLOW continuation job carrying the workflow_run_id to resume
-- from; the worker reads this and calls WorkflowRunner.resume. Nullable —
-- pre-PR-2 rows (and every non-resume job) read back as NULL → None → the
-- worker runs from the entrypoint, unchanged. ADD COLUMN IF NOT EXISTS keeps
-- it idempotent on every init (mirrors the target_version pattern above).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS resume_workflow_run_id TEXT;

-- item 17: batch inference. A batch is parent metadata over N child
-- JobKind.AGENT jobs; the submit endpoint persists one row here and stamps
-- each child job's batch_id (the column added just below). Additive new
-- table (CREATE TABLE IF NOT EXISTS, idempotent) — no row exists unless a
-- batch was submitted, so non-batch behavior is unchanged.
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    total       INTEGER NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_tenant_created
    ON batches(tenant_id, created_at DESC);

-- item 17: link each enqueued dataset row back to its parent batch. Nullable
-- — pre-batch rows (and every non-batch job: single runs, scheduled /
-- triggered / threaded / workflow jobs) read back as NULL → None, byte-for-
-- byte the pre-batch JobRecord. ADD COLUMN IF NOT EXISTS keeps it idempotent
-- (mirrors the target_version pattern above).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS batch_id TEXT;
-- Aggregate the children of one batch (status endpoint). Partial index keeps
-- it tight — most jobs have batch_id NULL.
CREATE INDEX IF NOT EXISTS idx_jobs_batch
    ON jobs(tenant_id, batch_id) WHERE batch_id IS NOT NULL;
-- item 32 (ADR 019): W3C trace-context carrier captured at enqueue so the
-- worker can continue the originating distributed trace (submit → execute as
-- ONE trace). Just a dict[str,str] of traceparent/tracestate — storage never
-- imports OTel. Nullable — pre-R2 rows (and any job enqueued with OTel off)
-- read back as NULL → {} → the worker starts a fresh root span, byte-for-byte
-- the pre-R2 behaviour. ADD COLUMN IF NOT EXISTS keeps it idempotent (mirrors
-- the target_version pattern above).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS trace_context JSONB;

-- item 36 (R4b): cooperative run cancellation. A RUNNING job flagged for
-- cancellation carries cancel_requested = TRUE; the worker honors it at its
-- terminal checkpoint and writes CANCELLED instead of the dispatch outcome (a
-- QUEUED job is flipped straight to CANCELLED by request_job_cancel and never
-- claimed). NOT NULL DEFAULT FALSE so existing rows (and every job that's never
-- cancelled — the overwhelming common case) read back as FALSE and behave
-- byte-for-byte as before. ADD COLUMN IF NOT EXISTS keeps it idempotent on
-- every init (mirrors the attempt_count additive-column pattern above).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;

-- item 24: per-dimension eval means. JSONB ({dim: mean}, same codec as
-- jobs.input) so drift detection can compare per-dimension and catch a
-- single-dimension regression the aggregate mean_score would mask. Nullable
-- — pre-item-24 eval rows read back as NULL → None, and detect_drift then
-- falls back to the aggregate-only path, byte-for-byte the old behaviour. ADD
-- COLUMN IF NOT EXISTS keeps it idempotent on every init (mirrors the
-- target_version pattern above).
ALTER TABLE evals ADD COLUMN IF NOT EXISTS dimension_means JSONB;

-- ADR 018: per-tenant BYOK provider keys. One row per (tenant, provider)
-- holding a Fernet ``ciphertext`` of the tenant's own provider key + a masked
-- ``fingerprint`` for display. The ProviderKeyResolver decrypts ``ciphertext``
-- at run time (tenant-key-first, shared-key fallback). NO row → the run path
-- uses the provider's env-default key → byte-for-byte the pre-BYOK behavior.
-- Additive new table (CREATE TABLE IF NOT EXISTS, idempotent) — default-off,
-- no ALTER, no backfill. The plaintext key is NEVER stored (only its
-- ciphertext + masked tail).
CREATE TABLE IF NOT EXISTS tenant_provider_keys (
    tenant_id   TEXT NOT NULL,
    provider    TEXT NOT NULL,
    ciphertext  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, provider)
);

-- ADR 040 — Projects as a first-class cloud entity. Five additive,
-- idempotent (CREATE TABLE IF NOT EXISTS) tables. No existing column is
-- modified, no ALTER on existing tables; the front end's project UX
-- composes onto this without any back-compat hazard. ``tenant_id NOT NULL``
-- is the invariant on ``projects``; members + junctions derive the tenant
-- via the project's row (joined when needed) so the tenant-isolation story
-- stays single-sourced. Soft delete via ``archived_at`` (D6); a default-
-- per-tenant row is created lazily by ``get_or_create_default_project``
-- (D5) and the unique ``(tenant_id, name)`` index keeps two racing creates
-- collapsing to one.
CREATE TABLE IF NOT EXISTS projects (
    project_id           TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL,
    name                 TEXT NOT NULL,
    description          TEXT,
    owner_principal_id   TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL,
    archived_at          TIMESTAMPTZ,
    UNIQUE (tenant_id, name)
);
-- Listing filter (D6 — hide archived) + tenant scope.
CREATE INDEX IF NOT EXISTS idx_projects_tenant_archived
    ON projects(tenant_id, archived_at);

-- Member CRUD: PK is (project_id, principal_id); the secondary index covers
-- the RBAC "members of project X with role Y" filter the API composes on.
CREATE TABLE IF NOT EXISTS project_members (
    project_id    TEXT NOT NULL,
    principal_id  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('viewer','editor','owner')),
    added_by      TEXT NOT NULL,
    added_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (project_id, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_project_members_project_role
    ON project_members(project_id, role);

-- Agent attachment junction (D2 — M:N). Indices cover both directions:
-- forward ("agents attached to project X") + reverse ("projects this agent
-- belongs to" — D5 implicit-default check).
CREATE TABLE IF NOT EXISTS project_agents (
    project_id  TEXT NOT NULL,
    agent_name  TEXT NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (project_id, agent_name)
);
CREATE INDEX IF NOT EXISTS idx_project_agents_project
    ON project_agents(project_id);
CREATE INDEX IF NOT EXISTS idx_project_agents_agent_name
    ON project_agents(agent_name);

-- Workflow attachment junction (D2 — M:N). Mirror of project_agents.
CREATE TABLE IF NOT EXISTS project_workflows (
    project_id     TEXT NOT NULL,
    workflow_name  TEXT NOT NULL,
    added_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (project_id, workflow_name)
);
CREATE INDEX IF NOT EXISTS idx_project_workflows_project
    ON project_workflows(project_id);
CREATE INDEX IF NOT EXISTS idx_project_workflows_workflow_name
    ON project_workflows(workflow_name);

-- KB attachment junction (D3). Carries the share mode (owned /
-- shared_reference / shared_copy). The "exactly one ``owned`` row per
-- kb_id" invariant is enforced by the API layer on share, not at the
-- storage seam — keeps the table portable.
CREATE TABLE IF NOT EXISTS project_kbs (
    project_id  TEXT NOT NULL,
    kb_id       TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('owned','shared_reference','shared_copy')),
    added_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (project_id, kb_id)
);
CREATE INDEX IF NOT EXISTS idx_project_kbs_project ON project_kbs(project_id);
CREATE INDEX IF NOT EXISTS idx_project_kbs_kb_id ON project_kbs(kb_id);

-- ADR 041: Agent catalog. Three namespaces (movate / private / community)
-- in one schema, distinguished by ``source``. The read API joins
-- ``movate`` + the caller's ``private`` + (future) ``community``.
-- Postgres ignores NULL in unique constraints, so a non-null
-- ``tenant_id_key`` column (set to the sentinel ``__public__`` for public
-- namespaces) preserves PK uniqueness while keeping ``tenant_id`` itself
-- NULL on the wire. The CHECK constraint enforces the namespace ↔
-- tenant_id invariant at the DB layer. Additive new table (CREATE TABLE
-- IF NOT EXISTS, idempotent on every init) — default-off, no ALTER, no
-- backfill.
CREATE TABLE IF NOT EXISTS catalog_entries (
    slug             TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('movate','private','community')),
    tenant_id        TEXT,
    tenant_id_key    TEXT NOT NULL,
    latest_version   TEXT NOT NULL,
    name             TEXT NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL,
    tags             JSONB NOT NULL DEFAULT '[]'::jsonb,
    shape            TEXT,
    recommended_for  TEXT,
    ratings_summary  JSONB NOT NULL DEFAULT '{"count": 0, "avg": 0.0}'::jsonb,
    popularity       INTEGER NOT NULL DEFAULT 0,
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (slug, source, tenant_id_key),
    CHECK (
        (source = 'private' AND tenant_id IS NOT NULL)
        OR (source IN ('movate','community') AND tenant_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_source_tenant
    ON catalog_entries(source, tenant_id);
-- GIN on tags (jsonb) for "?tag=foo" membership filters.
CREATE INDEX IF NOT EXISTS idx_catalog_entries_tags
    ON catalog_entries USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_shape
    ON catalog_entries(shape);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_synced_at
    ON catalog_entries(synced_at DESC);

-- One version per (slug, source, tenant_id_key, version). bundle_tar is
-- the actual entry contents — opaque BYTEA from the catalog service's
-- perspective (ADR 041 D4).
CREATE TABLE IF NOT EXISTS catalog_entry_versions (
    slug          TEXT NOT NULL,
    version       TEXT NOT NULL,
    source        TEXT NOT NULL,
    tenant_id     TEXT,
    tenant_id_key TEXT NOT NULL,
    bundle_tar    BYTEA NOT NULL,
    digest        TEXT NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deprecated_at TIMESTAMPTZ,
    PRIMARY KEY (slug, source, version, tenant_id_key)
);
CREATE INDEX IF NOT EXISTS idx_catalog_entry_versions_slug_source
    ON catalog_entry_versions(slug, source);
CREATE INDEX IF NOT EXISTS idx_catalog_entry_versions_published
    ON catalog_entry_versions(published_at DESC);

-- One rating per tenant per entry. Re-rating overwrites in place.
CREATE TABLE IF NOT EXISTS catalog_entry_ratings (
    slug       TEXT NOT NULL,
    source     TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    rating     SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (slug, source, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_entry_ratings_slug
    ON catalog_entry_ratings(slug);
CREATE INDEX IF NOT EXISTS idx_catalog_entry_ratings_tenant
    ON catalog_entry_ratings(tenant_id);

-- Per-source watermark. One row per source — the last successful sync
-- timestamp. Drives ``?since=`` deltas against catalog.movate.io (ADR 041 D4).
CREATE TABLE IF NOT EXISTS catalog_sync_watermark (
    source           TEXT PRIMARY KEY,
    last_synced_at   TIMESTAMPTZ NOT NULL
);

-- ADR 047: observability insights — the overnight analyst's daily,
-- pre-aggregated telemetry summary per (tenant, project, date). APPEND-ONLY:
-- a re-run for a day INSERTs a new row (keyed by the unique ``id``); reads take
-- the latest row per (tenant, project, date) via ORDER BY created_at DESC. The
-- four JSON columns (anomalies / top_failures / usage_rollup / trends) are
-- JSONB and round-trip through the connection-level json codec (same as
-- runs.metrics). Additive new table (CREATE TABLE IF NOT EXISTS, idempotent) —
-- default-off, no ALTER, no backfill.
CREATE TABLE IF NOT EXISTS observability_insights (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    date             DATE NOT NULL,
    health_score     DOUBLE PRECISION NOT NULL,
    anomalies        JSONB NOT NULL,
    top_failures     JSONB NOT NULL,
    usage_rollup     JSONB NOT NULL,
    trends           JSONB NOT NULL,
    narrative_digest TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observability_insights_tpd
    ON observability_insights(tenant_id, project_id, date);

-- ADR 035 D1: events outbox. Domain events (run.completed,
-- agent.published, eval.failed, drift.detected, canary.promoted/demoted,
-- ...) are recorded at the runtime edges and served by GET
-- /api/v1/events. Additive new table (CREATE TABLE IF NOT EXISTS,
-- idempotent on every init) — no row exists unless an emit happened, so
-- non-event behavior is byte-for-byte unchanged. ``tenant_id NOT NULL``
-- matches the rest of the schema (ADR 013/014). ``data`` is JSONB so
-- future consumers (D2 webhooks, D3 SSE) can filter on payload fields
-- if needed; D1's read API only filters on (tenant_id, kind, subject,
-- created_at).
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);
-- Default list shape: ``WHERE tenant_id=? AND created_at >=? ORDER BY
-- created_at`` (the last-24h default in GET /api/v1/events).
CREATE INDEX IF NOT EXISTS idx_events_tenant_created
    ON events(tenant_id, created_at);
-- Kind-filtered list: ``WHERE tenant_id=? AND kind=? AND created_at>=?``
-- (the typed-subscription shape D2/D3 will lean on).
CREATE INDEX IF NOT EXISTS idx_events_tenant_kind_created
    ON events(tenant_id, kind, created_at);

-- ADR 035 D2 — webhook subscriptions (outbound delivery). One row per
-- configured subscriber. ``secret`` is STORED (not hashed) because the
-- delivery worker must re-sign every outbound POST with the same key
-- the subscriber configured — we never echo it back on the wire after
-- the one-time create response. ``kind_filter`` is a JSONB list (use
-- ``["*"]`` for "all kinds"). Additive — no row exists unless the
-- tenant POSTs ``/api/v1/webhooks``, so pre-D2 behavior is byte-for-
-- byte unchanged.
CREATE TABLE IF NOT EXISTS webhooks (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    kind_filter   JSONB NOT NULL,
    secret        TEXT NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhooks_tenant ON webhooks(tenant_id);

-- ADR 035 D2 — per-attempt delivery log. One row per
-- (webhook_id, event_id, attempt_n) triple. ``response_excerpt`` is
-- truncated server-side (worker bounds it to ~512 chars before
-- insert) so a misbehaving subscriber can't blow up storage with
-- megabytes per attempt. ``status_code`` and ``response_excerpt`` are
-- NULL when the attempt never received an HTTP response (timeout /
-- connection error).
CREATE TABLE IF NOT EXISTS webhook_attempts (
    id                TEXT PRIMARY KEY,
    webhook_id        TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    attempted_at      TIMESTAMPTZ NOT NULL,
    status_code       INTEGER,
    response_excerpt  TEXT,
    error_kind        TEXT NOT NULL,
    attempt_n         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_attempts_tenant_at
    ON webhook_attempts(tenant_id, attempted_at);
CREATE INDEX IF NOT EXISTS idx_webhook_attempts_webhook_at
    ON webhook_attempts(webhook_id, attempted_at);

-- ADR 035 D2 — per-webhook delivery cursor. ``webhook_id`` is PK so
-- the upsert is cheap; per-webhook (rather than global) cursor avoids
-- re-delivering everyone's events when a new subscriber signs up.
-- ``tenant_id`` is denormalized for cheap tenant-bounded reads.
CREATE TABLE IF NOT EXISTS webhook_cursors (
    webhook_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_cursors_tenant ON webhook_cursors(tenant_id);
-- Eval-generation jobs (``mdk eval generate``). Runtime-resident,
-- SSE-driven; persistence is so the status GET + commit endpoint can
-- read back what generation produced. Additive new table (CREATE TABLE
-- IF NOT EXISTS, idempotent) — no row exists unless a caller hit the
-- generate endpoint, so non-generate behavior is byte-for-byte the
-- pre-feature path. ``result`` is a JSONB :class:`GenerationResult`
-- (cases + judge + cost) populated when the pipeline finishes;
-- ``error`` is populated on failure. Both NULL while status='running'.
CREATE TABLE IF NOT EXISTS eval_generation_jobs (
    job_id          TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    status          TEXT NOT NULL,
    description     TEXT NOT NULL,
    count_requested INTEGER NOT NULL,
    categories      JSONB NOT NULL,
    include_judge   BOOLEAN NOT NULL DEFAULT FALSE,
    model           TEXT NOT NULL,
    budget_usd      DOUBLE PRECISION,
    progress        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    result          JSONB,
    error           JSONB,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_eval_gen_jobs_tenant_created
    ON eval_generation_jobs(tenant_id, created_at DESC);
-- ADR 043 D1 — Failure Pattern Diagnoser results. One row per diagnose
-- request; upsert (ON CONFLICT) on diagnosis_id so the runtime's background
-- task can transition a row from ``running`` to ``completed`` without a
-- separate update method. ``request`` / ``result`` / ``error`` are JSONB
-- columns; the typed-fix taxonomy is validated at the wire edge so adding a
-- new fix kind later doesn't require a storage migration. Read-only with
-- respect to agent state — persisting a row never modifies the agent.
CREATE TABLE IF NOT EXISTS diagnoses (
    diagnosis_id  TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    agent         TEXT NOT NULL,
    status        TEXT NOT NULL,
    request       JSONB NOT NULL,
    result        JSONB,
    error         JSONB,
    tokens_used   INTEGER NOT NULL DEFAULT 0,
    cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    model         TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL,
    completed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_diagnoses_agent_created
    ON diagnoses(tenant_id, agent, created_at DESC);

-- ADR 045 D10: stateful sessions — server-side conversation memory.
-- ``sessions`` holds the entity + per-session rollups; ``session_messages``
-- holds the turns (one row per message, ``content`` as JSONB). ``tenant_id``
-- is NOT NULL on both (per-tenant isolation). Additive + idempotent
-- (CREATE ... IF NOT EXISTS) so re-running init() on an upgraded database
-- is a no-op; no backfill needed.
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    agent            TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL,
    turn_count       INTEGER NOT NULL DEFAULT 0,
    total_cost_usd   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    total_tokens_in  INTEGER NOT NULL DEFAULT 0,
    total_tokens_out INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_updated
    ON sessions(tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_tenant
    ON sessions(agent, tenant_id);

CREATE TABLE IF NOT EXISTS session_messages (
    message_id   TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      JSONB NOT NULL,
    run_id       TEXT,
    cost_usd     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_messages_session
    ON session_messages(session_id, tenant_id, created_at);
"""


# ---------------------------------------------------------------------------
# Pool init — register JSONB ↔ dict codec on every connection
# ---------------------------------------------------------------------------


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup: tell asyncpg how to round-trip JSONB.

    Without this, asyncpg returns JSONB columns as ``str`` (they were
    inserted as ``str`` too). Registering the codec lets handlers pass
    and receive plain Python dicts — same UX as the sqlite path
    (which wraps json.dumps/loads inline).
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


# ---------------------------------------------------------------------------
# Migrations (ADR 009 D5) — ordered, idempotent, transactional
# ---------------------------------------------------------------------------


async def _migrate_kb_embedding_to_vector(conn: asyncpg.Connection) -> None:
    """Convert ``kb_chunks.embedding`` from JSONB to pgvector ``vector(N)``.

    Handles fresh DBs (the base schema created a JSONB column, immediately
    converted here) and existing DBs (data is cast in place) uniformly.

    Safety (ADR 009 D4): the cast ``jsonb -> text -> vector`` is done on a new
    column; every existing row's dimension is validated against the configured
    N *before* the destructive drop/rename, and the migration aborts (rolling
    back the whole transaction) if any row mismatches. ``vector`` stores
    float4, so this is effectively lossless for embeddings.
    """
    dim = _embedding_dim()

    # Already a vector column? (idempotent guard beyond schema_migrations).
    udt = await conn.fetchval(
        "SELECT udt_name FROM information_schema.columns "
        "WHERE table_name = 'kb_chunks' AND column_name = 'embedding'"
    )
    if udt == "vector":
        return

    # Validate dimensions of existing rows BEFORE any destructive change.
    bad = await conn.fetch(
        "SELECT chunk_id, jsonb_array_length(embedding) AS dim "
        "FROM kb_chunks WHERE jsonb_array_length(embedding) <> $1 LIMIT 5",
        dim,
    )
    if bad:
        sample = ", ".join(f"{r['chunk_id']}(dim={r['dim']})" for r in bad)
        raise RuntimeError(
            f"cannot migrate kb_chunks.embedding to vector({dim}): some rows "
            f"have a different dimension (e.g. {sample}). Re-ingest with a "
            "consistent embedding model, or set MOVATE_EMBED_DIM to match."
        )

    await conn.execute(f"ALTER TABLE kb_chunks ADD COLUMN embedding_vec vector({dim})")
    await conn.execute("UPDATE kb_chunks SET embedding_vec = embedding::text::vector")
    await conn.execute("ALTER TABLE kb_chunks DROP COLUMN embedding")
    await conn.execute("ALTER TABLE kb_chunks RENAME COLUMN embedding_vec TO embedding")
    await conn.execute("ALTER TABLE kb_chunks ALTER COLUMN embedding SET NOT NULL")
    # ANN index for cosine distance (`<=>`). HNSW: good recall/latency, no
    # pre-populated table needed to build well (ADR 009 D3).
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw "
        "ON kb_chunks USING hnsw (embedding vector_cosine_ops)"
    )


# Ordered list of (version, coroutine). Append new migrations; never edit or
# reorder shipped ones.
_MIGRATIONS: list[tuple[str, Any]] = [
    ("001_kb_embedding_to_vector", _migrate_kb_embedding_to_vector),
]

# Session-level advisory-lock key serializing ``_run_migrations`` across pods
# (item 39). On a fresh/scaled deploy every api/worker pod runs migrations on
# startup; without a lock they race the ``schema_migrations`` tracking and the
# (not-fully-idempotent) DDL. A fixed signed-64-bit constant — it MUST be
# identical across all pods, since ``pg_advisory_lock`` only blocks callers
# contending for the *same* key. Memorable hardcoded value; documented here so
# it's never reused for another lock. (Postgres advisory keys are bigint, so
# this stays inside the int64 range.)
_MIGRATION_LOCK_KEY = 0x4D44_4B5F_4D49_4752  # "MDK_MIGR" packed as hex bytes


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class PostgresProvider:
    """Implements :class:`StorageProvider` against PostgreSQL via asyncpg.

    Connections come from a pool. Single-statement methods acquire a
    connection per call; the claim path takes a transaction so the
    SELECT-then-UPDATE pair is atomic across concurrent workers.
    """

    name = "postgres"

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        # ACA wires the DSN with an empty password slot
        # (``postgresql://user:@host/db``) and surfaces the actual
        # password as a separate ``PGPASSWORD`` env var — see the
        # comment block in
        # ``infra/azure/modules/containerapp-api.bicep`` next to
        # MOVATE_DB_URL. asyncpg's documented "fall back to
        # PGPASSWORD" only fires when the DSN's password component
        # is MISSING (``user@host``); when it's present-but-empty
        # (``user:@host``) asyncpg authenticates with the empty
        # string and the server rejects it as
        # ``InvalidPasswordError``. We sidestep this by passing
        # PGPASSWORD as an explicit kwarg — asyncpg uses the kwarg
        # in preference to the DSN's password component, so the
        # bicep keeps working with no infra changes.
        import os  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "min_size": self._min_size,
            "max_size": self._max_size,
            "init": _init_connection,
        }
        env_password = os.environ.get("PGPASSWORD")
        if env_password:
            kwargs["password"] = env_password
        self._pool = await asyncpg.create_pool(self._dsn, **kwargs)
        async with self._pool.acquire() as conn:
            await self._ensure_pgvector(conn)
            await conn.execute(_SCHEMA)
            await self._run_migrations(conn)

    @staticmethod
    async def _ensure_pgvector(conn: asyncpg.Connection) -> None:
        """Create the pgvector extension. **Required** — KB embeddings are
        stored in a ``vector(N)`` column (ADR 009).

        On Azure Postgres Flexible Server the extension must be allow-listed
        via the ``azure.extensions`` server parameter
        (``infra/azure/modules/postgres.bicep``) before ``CREATE EXTENSION``
        will succeed. We fail loudly rather than silently fall back to a
        non-vector column.
        """
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        except asyncpg.PostgresError as exc:
            raise RuntimeError(
                "pgvector extension is required for KB storage but could not be "
                f"created: {exc}. On Azure Postgres add 'VECTOR' to the "
                "azure.extensions server parameter "
                "(infra/azure/modules/postgres.bicep) and redeploy."
            ) from exc

    async def _run_migrations(self, conn: asyncpg.Connection) -> None:
        """Apply ordered, idempotent schema migrations (ADR 009 D5).

        The base ``_SCHEMA`` is all ``CREATE ... IF NOT EXISTS`` and cannot
        express a column-type change, so structural upgrades live here. Each
        migration runs at most once, tracked in ``schema_migrations``, inside
        its own transaction (so a failure rolls back cleanly and leaves the
        version unrecorded for a safe retry).

        Concurrent-startup safety (item 39): on a fresh/scaled deploy multiple
        api/worker pods call this at once, racing the ``schema_migrations``
        tracking and the not-fully-idempotent DDL. We serialize with a
        *session-level* ``pg_advisory_lock`` on ``conn`` so exactly one pod
        runs migrations at a time; the rest block at acquisition, then proceed
        and find every migration already applied (idempotent no-ops). We use
        the blocking lock (not ``try_advisory_lock``) deliberately — a late pod
        must wait for migrations to finish, never skip them and serve against a
        half-migrated schema. The lock is session-scoped, so if a pod dies
        mid-migration its connection drops and Postgres auto-releases the lock;
        a stuck migration can't wedge the lock forever. (SQLite's single-writer
        model already serializes this, so only Postgres needs the bracket.)
        """
        # Acquire on the SAME connection (session-scoped); release in finally so
        # an erroring migration can't leak the lock.
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  version TEXT PRIMARY KEY,"
                "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
            applied = {
                r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")
            }
            for version, migrate in _MIGRATIONS:
                if version in applied:
                    continue
                async with conn.transaction():
                    await migrate(conn)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)

    async def ping(self) -> None:
        """``SELECT 1`` against the pool — picks up DB-down /
        pool-exhausted / network-blip conditions for the ``/ready``
        endpoint. Acquires from the pool so we exercise the same
        path real queries take."""
        await self._db.execute("SELECT 1")

    @property
    def _db(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresProvider.init() not called")
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def pool_stats(self) -> PoolStats | None:
        """Snapshot the live asyncpg pool's connection counts (ADR 034 D3).

        Synchronous + non-blocking: reads the pool's in-memory counters, never
        touches the DB, so it's safe to call from an OTel observable-gauge
        callback on the collection thread. Returns ``None`` before ``init()``
        (no pool yet) so the edge's gauge simply records nothing that cycle.

        ``size`` / ``idle`` / ``max_size`` / ``min_size`` come from asyncpg's
        public ``Pool`` getters (``get_size`` etc., 0.25.0+). ``in_use`` is
        derived (``size - idle``). ``waiting`` reads the acquire queue's blocked
        getters — asyncpg has no public getter for it, so it's read defensively
        from the LifoQueue's ``_getters`` deque and degrades to ``0`` if a future
        asyncpg release changes that internal (the gauge stays useful; only the
        early-warning waiters line goes quiet). Never raises.
        """
        pool = self._pool
        if pool is None:
            return None
        try:
            size = pool.get_size()
            idle = pool.get_idle_size()
            max_size = pool.get_max_size()
            min_size = pool.get_min_size()
        except Exception:  # pragma: no cover - defensive; getters are simple
            return None
        # ``waiting`` = callers parked on the pool's acquire queue. asyncpg uses
        # an asyncio.LifoQueue whose blocked getters live in ``_getters``; guard
        # the whole access so an asyncpg internals change can't break the gauge.
        waiting = 0
        try:
            queue = pool._queue
            getters = getattr(queue, "_getters", None)
            if getters is not None:
                waiting = len(getters)
        except Exception:  # pragma: no cover - defensive
            waiting = 0
        return PoolStats(
            size=size,
            idle=idle,
            in_use=max(size - idle, 0),
            waiting=waiting,
            max_size=max_size,
            min_size=min_size,
        )

    # ------------------------------------------------------------------
    # Diagnoses (ADR 043 D1 — Failure Pattern Diagnoser results)
    # ------------------------------------------------------------------

    async def save_diagnosis(self, record: DiagnosisRecord) -> None:
        # Upsert keyed by diagnosis_id. ON CONFLICT preserves the
        # original created_at — the runtime's background task uses this
        # to transition the row from ``running`` to ``completed`` (or
        # ``error``) without re-stamping the insert timestamp.
        await self._db.execute(
            """
            INSERT INTO diagnoses (
                diagnosis_id, tenant_id, agent, status, request, result,
                error, tokens_used, cost_usd, model, created_at, completed_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
            )
            ON CONFLICT (diagnosis_id) DO UPDATE SET
                status       = EXCLUDED.status,
                request      = EXCLUDED.request,
                result       = EXCLUDED.result,
                error        = EXCLUDED.error,
                tokens_used  = EXCLUDED.tokens_used,
                cost_usd     = EXCLUDED.cost_usd,
                model        = EXCLUDED.model,
                completed_at = EXCLUDED.completed_at
            """,
            record.diagnosis_id,
            record.tenant_id,
            record.agent,
            record.status.value,
            record.request,
            record.result,
            record.error.model_dump() if record.error else None,
            record.tokens_used,
            record.cost_usd,
            record.model,
            record.created_at,
            record.completed_at,
        )

    async def get_diagnosis(self, diagnosis_id: str, *, tenant_id: str) -> DiagnosisRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM diagnoses WHERE diagnosis_id = $1 AND tenant_id = $2",
            diagnosis_id,
            tenant_id,
        )
        return _row_to_diagnosis(row) if row else None

    # ------------------------------------------------------------------
    # DR backup/restore (item 26) — delegate to the backend-agnostic
    # orchestration in movate.core.dr_backup (reads/writes only through this
    # Protocol's methods, so the snapshot round-trips across all backends).
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
    # Events outbox (ADR 035 D1 — durable lifecycle events). ``data``
    # round-trips as JSONB via the per-connection codec registered in
    # ``_init_connection`` — handler passes/receives plain dicts.
    # Tenant-scoped on every read.
    # ------------------------------------------------------------------

    async def record_event(self, event: Event) -> None:
        await self._db.execute(
            "INSERT INTO events (id, tenant_id, kind, subject, data, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            event.id,
            event.tenant_id,
            event.kind,
            event.subject,
            event.data,
            event.created_at,
        )

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
        clauses: list[str] = ["tenant_id = $1"]
        params: list[Any] = [tenant_id]
        if since is not None:
            params.append(since)
            clauses.append(f"created_at >= ${len(params)}")
        if until is not None:
            params.append(until)
            clauses.append(f"created_at < ${len(params)}")
        if kind is not None:
            params.append(kind)
            clauses.append(f"kind = ${len(params)}")
        if subject is not None:
            params.append(subject)
            clauses.append(f"subject = ${len(params)}")
        # Cursor pagination — resolve the cursor row's (created_at, id)
        # so the comparison is stable when multiple events share a
        # timestamp (id as tie-breaker). Tenant-scoped cursor lookup so
        # an after_id from another tenant silently falls back to "from
        # the beginning" (no existence leak).
        if after_id is not None:
            cursor_at = await self._db.fetchval(
                "SELECT created_at FROM events WHERE id = $1 AND tenant_id = $2",
                after_id,
                tenant_id,
            )
            if cursor_at is not None:
                params.append(cursor_at)
                ts_idx = len(params)
                params.append(after_id)
                id_idx = len(params)
                clauses.append(f"(created_at, id) > (${ts_idx}, ${id_idx})")
        params.append(limit)
        limit_idx = len(params)
        sql = (
            "SELECT id, tenant_id, kind, subject, data, created_at "
            "FROM events WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at ASC, id ASC LIMIT ${limit_idx}"
        )
        rows = await self._db.fetch(sql, *params)
        return [
            Event(
                id=r["id"],
                tenant_id=r["tenant_id"],
                kind=r["kind"],
                subject=r["subject"],
                data=r["data"] or {},
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def save_run(self, run: RunRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO runs (
                run_id, job_id, tenant_id, agent, agent_version, prompt_hash,
                provider, provider_version, pricing_version, status,
                input, output, metrics, error, created_at,
                workflow_run_id, node_id, thread_id, skill_calls, turns
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19, $20
            )
            """,
            run.run_id,
            run.job_id,
            run.tenant_id,
            run.agent,
            run.agent_version,
            run.prompt_hash,
            run.provider,
            run.provider_version,
            run.pricing_version,
            run.status.value,
            run.input,
            run.output,
            run.metrics.model_dump(),
            run.error.model_dump() if run.error else None,
            run.created_at,
            run.workflow_run_id,
            run.node_id,
            run.thread_id,
            # ADR 024 D2 — per-step retention (JSONB codec wraps json.dumps).
            [c.model_dump() for c in run.skill_calls],
            [t.model_dump() for t in run.turns],
        )

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        # tenant_id in WHERE is the SQL-layer enforcement; a caller can't
        # read another tenant's run even by guessing the run_id.
        row = await self._db.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1 AND tenant_id = $2",
            run_id,
            tenant_id,
        )
        return _row_to_run(row) if row else None

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if workflow_run_id is not None:
            params.append(workflow_run_id)
            clauses.append(f"workflow_run_id = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------

    async def save_failure(self, f: FailureRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO failures (
                failure_id, run_id, tenant_id, agent, failure_type,
                message, retryable, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            f.failure_id,
            f.run_id,
            f.tenant_id,
            f.agent,
            f.failure_type,
            f.message,
            f.retryable,
            f.created_at,
        )

    # ------------------------------------------------------------------
    # Evals
    # ------------------------------------------------------------------

    async def save_eval(self, e: EvalRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO evals (
                eval_id, tenant_id, agent, agent_version, dataset_hash,
                judge_method, judge_provider, runs_per_case, gate_mode,
                threshold, mean_score, pass_rate, sample_count,
                total_cost_usd, created_at, dimension_means
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16
            )
            """,
            e.eval_id,
            e.tenant_id,
            e.agent,
            e.agent_version,
            e.dataset_hash,
            e.judge_method.value,
            e.judge_provider,
            e.runs_per_case,
            e.gate_mode,
            e.threshold,
            e.mean_score,
            e.pass_rate,
            e.sample_count,
            e.total_cost_usd,
            e.created_at,
            # item 24: JSONB column; the connection codec json.dumps a dict.
            # None for legacy / exact-match records → SQL NULL → reads back None.
            e.dimension_means,
        )

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM evals WHERE eval_id = $1 AND tenant_id = $2",
            eval_id,
            tenant_id,
        )
        return _row_to_eval(row) if row else None

    # ------------------------------------------------------------------
    # Observability insights (ADR 047) — append-only.
    # ------------------------------------------------------------------

    async def save_insight(self, insight: ObservabilityInsight) -> None:
        # INSERT-only (never UPDATE / ON CONFLICT): a re-run for the same day
        # appends a new row keyed by the unique ``id``; reads take the latest.
        # The four JSON columns serialize via the connection-level json codec.
        await self._db.execute(
            """
            INSERT INTO observability_insights (
                id, tenant_id, project_id, date, health_score,
                anomalies, top_failures, usage_rollup, trends,
                narrative_digest, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            insight.id,
            insight.tenant_id,
            insight.project_id,
            insight.date,
            insight.health_score,
            insight.anomalies,
            insight.top_failures,
            insight.usage_rollup,
            insight.trends,
            insight.narrative_digest,
            insight.created_at,
        )

    async def get_insight(
        self, tenant_id: str, project_id: str, day: date
    ) -> ObservabilityInsight | None:
        # Latest-per-day: ORDER BY created_at DESC LIMIT 1 collapses append-only
        # re-runs to the newest row. tenant_id in WHERE is the no-leak gate.
        row = await self._db.fetchrow(
            """
            SELECT * FROM observability_insights
            WHERE tenant_id = $1 AND project_id = $2 AND date = $3
            ORDER BY created_at DESC LIMIT 1
            """,
            tenant_id,
            project_id,
            day,
        )
        return _row_to_insight(row) if row else None

    async def list_insights(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        since: date | None = None,
        until: date | None = None,
        limit: int = 90,
    ) -> list[ObservabilityInsight]:
        # DISTINCT ON (project_id, date) collapses append-only re-runs to the
        # latest row per day server-side (Postgres-specific), then we re-sort
        # the deduped set newest-day-first and cap to ``limit``.
        clauses = ["tenant_id = $1"]
        params: list[Any] = [tenant_id]
        if project_id is not None:
            params.append(project_id)
            clauses.append(f"project_id = ${len(params)}")
        if since is not None:
            params.append(since)
            clauses.append(f"date >= ${len(params)}")
        if until is not None:
            params.append(until)
            clauses.append(f"date <= ${len(params)}")
        params.append(limit)
        where = " AND ".join(clauses)
        sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (project_id, date) *
                FROM observability_insights
                WHERE {where}
                ORDER BY project_id, date, created_at DESC
            ) latest
            ORDER BY date DESC
            LIMIT ${len(params)}
        """
        rows = await self._db.fetch(sql, *params)
        return [_row_to_insight(r) for r in rows]

    async def list_evals(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM evals {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_eval(r) for r in rows]

    # ------------------------------------------------------------------
    # Bench (BACKLOG #64)
    # ------------------------------------------------------------------

    async def save_bench(self, b: BenchRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO bench (
                bench_id, tenant_id, agent, agent_version, input,
                judge_method, judge_provider, runs_per_model, gate_mode,
                models, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
            )
            """,
            b.bench_id,
            b.tenant_id,
            b.agent,
            b.agent_version,
            b.input,
            b.judge_method.value if b.judge_method else None,
            b.judge_provider,
            b.runs_per_model,
            b.gate_mode,
            [m.model_dump() for m in b.models],
            b.created_at,
        )

    async def get_bench(self, bench_id: str, *, tenant_id: str) -> BenchRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM bench WHERE bench_id = $1 AND tenant_id = $2",
            bench_id,
            tenant_id,
        )
        return _row_to_bench(row) if row else None

    async def list_bench(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[BenchRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM bench {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_bench(r) for r in rows]

    # ------------------------------------------------------------------
    # Audit records (Claude-orchestrated read-only audit pipeline). See
    # base.py for the read-only contract — this provider's writes are
    # gated to ``save_audit`` only.
    # ------------------------------------------------------------------

    async def save_audit(self, a: AuditRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO audits (
                audit_id, tenant_id, scope_kind, scope_id, categories,
                severity_floor, model, budget_usd, findings, partial,
                tokens_used, cost_usd, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
            )
            """,
            a.audit_id,
            a.tenant_id,
            a.scope_kind,
            a.scope_id,
            a.categories,
            a.severity_floor.value,
            a.model,
            a.budget_usd,
            [f.model_dump() for f in a.findings],
            a.partial,
            a.tokens_used,
            a.cost_usd,
            a.created_at,
        )

    async def get_audit(self, audit_id: str, *, tenant_id: str) -> AuditRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM audits WHERE audit_id = $1 AND tenant_id = $2",
            audit_id,
            tenant_id,
        )
        return _row_to_audit(row) if row else None

    async def list_audits(
        self,
        *,
        tenant_id: str | None = None,
        scope_id: str | None = None,
        limit: int = 20,
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if scope_id is not None:
            params.append(scope_id)
            clauses.append(f"scope_id = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM audits {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_audit(r) for r in rows]

    # ------------------------------------------------------------------
    # Eval schedules (ADR 016 D2)
    # ------------------------------------------------------------------

    async def save_eval_schedule(self, schedule: EvalSchedule) -> None:
        await self._db.execute(
            """
            INSERT INTO eval_schedules (
                tenant_id, agent, cadence_seconds, enabled, mock, runs,
                gate_mode, gate, objective, regression_tolerance, baseline_id,
                notify_email, created_by, created_at, last_enqueued_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
            )
            ON CONFLICT (tenant_id, agent) DO UPDATE SET
                cadence_seconds = EXCLUDED.cadence_seconds,
                enabled = EXCLUDED.enabled,
                mock = EXCLUDED.mock,
                runs = EXCLUDED.runs,
                gate_mode = EXCLUDED.gate_mode,
                gate = EXCLUDED.gate,
                objective = EXCLUDED.objective,
                regression_tolerance = EXCLUDED.regression_tolerance,
                baseline_id = EXCLUDED.baseline_id,
                notify_email = EXCLUDED.notify_email,
                created_by = EXCLUDED.created_by,
                created_at = EXCLUDED.created_at,
                last_enqueued_at = EXCLUDED.last_enqueued_at
            """,
            schedule.tenant_id,
            schedule.agent,
            schedule.cadence_seconds,
            schedule.enabled,
            schedule.mock,
            schedule.runs,
            schedule.gate_mode,
            schedule.gate,
            schedule.objective,
            schedule.regression_tolerance,
            schedule.baseline_id,
            schedule.notify_email,
            schedule.created_by,
            schedule.created_at,
            schedule.last_enqueued_at,
        )

    async def get_eval_schedule(self, agent: str, *, tenant_id: str) -> EvalSchedule | None:
        row = await self._db.fetchrow(
            "SELECT * FROM eval_schedules WHERE agent = $1 AND tenant_id = $2",
            agent,
            tenant_id,
        )
        return _row_to_eval_schedule(row) if row else None

    async def list_eval_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[EvalSchedule]:
        params: list[Any] = []
        where = ""
        if tenant_id is not None:
            params.append(tenant_id)
            where = f"WHERE tenant_id = ${len(params)}"
        params.append(limit)
        sql = f"SELECT * FROM eval_schedules {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_eval_schedule(r) for r in rows]

    async def delete_eval_schedule(self, agent: str, *, tenant_id: str) -> bool:
        status: str = await self._db.execute(
            "DELETE FROM eval_schedules WHERE agent = $1 AND tenant_id = $2",
            agent,
            tenant_id,
        )
        # asyncpg returns a status string like "DELETE 1" / "DELETE 0".
        return status.startswith("DELETE ") and not status.endswith(" 0")

    async def touch_eval_schedule(
        self,
        agent: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        await self._db.execute(
            "UPDATE eval_schedules SET last_enqueued_at = $1 WHERE agent = $2 AND tenant_id = $3",
            last_enqueued_at,
            agent,
            tenant_id,
        )

    # ------------------------------------------------------------------
    # Job schedules (ADR 017 D2)
    # ------------------------------------------------------------------

    async def save_job_schedule(self, schedule: JobSchedule) -> None:
        await self._db.execute(
            """
            INSERT INTO job_schedules (
                tenant_id, name, kind, target, cadence_seconds, enabled,
                input, notify_email, created_by, created_at, last_enqueued_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
            )
            ON CONFLICT (tenant_id, name) DO UPDATE SET
                kind = EXCLUDED.kind,
                target = EXCLUDED.target,
                cadence_seconds = EXCLUDED.cadence_seconds,
                enabled = EXCLUDED.enabled,
                input = EXCLUDED.input,
                notify_email = EXCLUDED.notify_email,
                created_by = EXCLUDED.created_by,
                created_at = EXCLUDED.created_at,
                last_enqueued_at = EXCLUDED.last_enqueued_at
            """,
            schedule.tenant_id,
            schedule.name,
            schedule.kind.value,
            schedule.target,
            schedule.cadence_seconds,
            schedule.enabled,
            schedule.input,
            schedule.notify_email,
            schedule.created_by,
            schedule.created_at,
            schedule.last_enqueued_at,
        )

    async def get_job_schedule(self, name: str, *, tenant_id: str) -> JobSchedule | None:
        row = await self._db.fetchrow(
            "SELECT * FROM job_schedules WHERE name = $1 AND tenant_id = $2",
            name,
            tenant_id,
        )
        return _row_to_job_schedule(row) if row else None

    async def list_job_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[JobSchedule]:
        params: list[Any] = []
        where = ""
        if tenant_id is not None:
            params.append(tenant_id)
            where = f"WHERE tenant_id = ${len(params)}"
        params.append(limit)
        sql = f"SELECT * FROM job_schedules {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_job_schedule(r) for r in rows]

    async def delete_job_schedule(self, name: str, *, tenant_id: str) -> bool:
        status: str = await self._db.execute(
            "DELETE FROM job_schedules WHERE name = $1 AND tenant_id = $2",
            name,
            tenant_id,
        )
        # asyncpg returns a status string like "DELETE 1" / "DELETE 0".
        return status.startswith("DELETE ") and not status.endswith(" 0")

    async def touch_job_schedule(
        self,
        name: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        await self._db.execute(
            "UPDATE job_schedules SET last_enqueued_at = $1 WHERE name = $2 AND tenant_id = $3",
            last_enqueued_at,
            name,
            tenant_id,
        )

    # ------------------------------------------------------------------
    # Triggers (ADR 017 D2 — inbound event/webhook → enqueue a job)
    # ------------------------------------------------------------------

    async def save_trigger(self, trigger: Trigger) -> None:
        await self._db.execute(
            """
            INSERT INTO triggers (
                tenant_id, name, trigger_id, kind, target, secret_hash, salt,
                input_defaults, enabled, created_by, created_at, last_fired_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
            )
            ON CONFLICT (tenant_id, name) DO UPDATE SET
                trigger_id = EXCLUDED.trigger_id,
                kind = EXCLUDED.kind,
                target = EXCLUDED.target,
                secret_hash = EXCLUDED.secret_hash,
                salt = EXCLUDED.salt,
                input_defaults = EXCLUDED.input_defaults,
                enabled = EXCLUDED.enabled,
                created_by = EXCLUDED.created_by,
                created_at = EXCLUDED.created_at,
                last_fired_at = EXCLUDED.last_fired_at
            """,
            trigger.tenant_id,
            trigger.name,
            trigger.trigger_id,
            trigger.kind.value,
            trigger.target,
            trigger.secret_hash,
            trigger.salt,
            trigger.input_defaults,
            trigger.enabled,
            trigger.created_by,
            trigger.created_at,
            trigger.last_fired_at,
        )

    async def get_trigger(self, name: str, *, tenant_id: str) -> Trigger | None:
        row = await self._db.fetchrow(
            "SELECT * FROM triggers WHERE name = $1 AND tenant_id = $2",
            name,
            tenant_id,
        )
        return _row_to_trigger(row) if row else None

    async def get_trigger_by_id(self, trigger_id: str) -> Trigger | None:
        row = await self._db.fetchrow(
            "SELECT * FROM triggers WHERE trigger_id = $1",
            trigger_id,
        )
        return _row_to_trigger(row) if row else None

    async def list_triggers(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[Trigger]:
        params: list[Any] = []
        where = ""
        if tenant_id is not None:
            params.append(tenant_id)
            where = f"WHERE tenant_id = ${len(params)}"
        params.append(limit)
        sql = f"SELECT * FROM triggers {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_trigger(r) for r in rows]

    async def delete_trigger(self, name: str, *, tenant_id: str) -> bool:
        status: str = await self._db.execute(
            "DELETE FROM triggers WHERE name = $1 AND tenant_id = $2",
            name,
            tenant_id,
        )
        # asyncpg returns a status string like "DELETE 1" / "DELETE 0".
        return status.startswith("DELETE ") and not status.endswith(" 0")

    async def touch_trigger(self, trigger_id: str, *, last_fired_at: datetime) -> None:
        await self._db.execute(
            "UPDATE triggers SET last_fired_at = $1 WHERE trigger_id = $2",
            last_fired_at,
            trigger_id,
        )

    async def get_trigger_delivery(self, trigger_id: str, delivery_id: str) -> str | None:
        row = await self._db.fetchrow(
            "SELECT job_id FROM trigger_deliveries WHERE trigger_id = $1 AND delivery_id = $2",
            trigger_id,
            delivery_id,
        )
        return row["job_id"] if row else None

    async def record_trigger_delivery(self, trigger_id: str, delivery_id: str, job_id: str) -> bool:
        # INSERT ... ON CONFLICT DO NOTHING on the (trigger_id, delivery_id)
        # PRIMARY KEY: the row is written only if absent, so a concurrent
        # double-delivery races atomically to one winner. asyncpg returns the
        # command tag "INSERT 0 1" on a fresh insert, "INSERT 0 0" when the
        # conflict suppressed it.
        status: str = await self._db.execute(
            "INSERT INTO trigger_deliveries (trigger_id, delivery_id, job_id, created_at) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (trigger_id, delivery_id) DO NOTHING",
            trigger_id,
            delivery_id,
            job_id,
            datetime.now(UTC),
        )
        return status.endswith(" 1")

    async def get_run_submission(self, tenant_id: str, idempotency_key: str) -> str | None:
        row = await self._db.fetchrow(
            "SELECT job_id FROM run_submissions WHERE tenant_id = $1 AND idempotency_key = $2",
            tenant_id,
            idempotency_key,
        )
        return row["job_id"] if row else None

    async def record_run_submission(
        self, tenant_id: str, idempotency_key: str, job_id: str
    ) -> bool:
        # INSERT ... ON CONFLICT DO NOTHING on the (tenant_id, idempotency_key)
        # PRIMARY KEY: the row is written only if absent, so a concurrent retry
        # races atomically to one winner. asyncpg returns the command tag
        # "INSERT 0 1" on a fresh insert, "INSERT 0 0" when the conflict
        # suppressed it.
        status: str = await self._db.execute(
            "INSERT INTO run_submissions (tenant_id, idempotency_key, job_id, created_at) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING",
            tenant_id,
            idempotency_key,
            job_id,
            datetime.now(UTC),
        )
        return status.endswith(" 1")

    # ------------------------------------------------------------------
    # Tenant provider keys (ADR 018 — per-tenant BYOK provider credentials)
    # ------------------------------------------------------------------

    async def save_tenant_provider_key(self, key: TenantProviderKey) -> None:
        await self._db.execute(
            """
            INSERT INTO tenant_provider_keys (
                tenant_id, provider, ciphertext, fingerprint,
                created_by, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (tenant_id, provider) DO UPDATE SET
                ciphertext = EXCLUDED.ciphertext,
                fingerprint = EXCLUDED.fingerprint,
                created_by = EXCLUDED.created_by,
                updated_at = EXCLUDED.updated_at
            """,
            key.tenant_id,
            key.provider,
            key.ciphertext,
            key.fingerprint,
            key.created_by,
            key.created_at,
            key.updated_at,
        )

    async def get_tenant_provider_key(
        self, provider: str, *, tenant_id: str
    ) -> TenantProviderKey | None:
        row = await self._db.fetchrow(
            "SELECT * FROM tenant_provider_keys WHERE provider = $1 AND tenant_id = $2",
            provider,
            tenant_id,
        )
        return _row_to_tenant_provider_key(row) if row else None

    async def list_tenant_provider_keys(self, *, tenant_id: str) -> list[TenantProviderKey]:
        rows = await self._db.fetch(
            "SELECT * FROM tenant_provider_keys WHERE tenant_id = $1 ORDER BY provider ASC",
            tenant_id,
        )
        return [_row_to_tenant_provider_key(r) for r in rows]

    async def list_all_tenant_provider_keys(
        self, *, limit: int = 100_000
    ) -> list[TenantProviderKey]:
        # item 26 (DR export) — fleet-wide, operator-only. Stable order.
        rows = await self._db.fetch(
            "SELECT * FROM tenant_provider_keys ORDER BY tenant_id, provider LIMIT $1",
            limit,
        )
        return [_row_to_tenant_provider_key(r) for r in rows]

    async def delete_tenant_provider_key(self, provider: str, *, tenant_id: str) -> bool:
        status: str = await self._db.execute(
            "DELETE FROM tenant_provider_keys WHERE provider = $1 AND tenant_id = $2",
            provider,
            tenant_id,
        )
        return status.startswith("DELETE ") and not status.endswith(" 0")

    # ------------------------------------------------------------------
    # Canary configs (ADR 016 D3 — champion/challenger rollout)
    # ------------------------------------------------------------------

    async def save_canary_config(self, config: CanaryConfig) -> None:
        await self._db.execute(
            """
            INSERT INTO canary_configs (
                tenant_id, agent, challenger_version, champion_version, weight,
                sticky, enabled, auto_promote, eval_gate, created_by,
                created_at, updated_at, auto_rollback
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
            )
            ON CONFLICT (tenant_id, agent) DO UPDATE SET
                challenger_version = EXCLUDED.challenger_version,
                champion_version = EXCLUDED.champion_version,
                weight = EXCLUDED.weight,
                sticky = EXCLUDED.sticky,
                enabled = EXCLUDED.enabled,
                auto_promote = EXCLUDED.auto_promote,
                eval_gate = EXCLUDED.eval_gate,
                created_by = EXCLUDED.created_by,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                auto_rollback = EXCLUDED.auto_rollback
            """,
            config.tenant_id,
            config.agent,
            config.challenger_version,
            config.champion_version,
            config.weight,
            config.sticky,
            config.enabled,
            config.auto_promote,
            config.eval_gate,
            config.created_by,
            config.created_at,
            config.updated_at,
            config.auto_rollback,
        )

    async def get_canary_config(self, agent: str, *, tenant_id: str) -> CanaryConfig | None:
        row = await self._db.fetchrow(
            "SELECT * FROM canary_configs WHERE agent = $1 AND tenant_id = $2",
            agent,
            tenant_id,
        )
        return _row_to_canary_config(row) if row else None

    async def list_canary_configs(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[CanaryConfig]:
        params: list[Any] = []
        where = ""
        if tenant_id is not None:
            params.append(tenant_id)
            where = f"WHERE tenant_id = ${len(params)}"
        params.append(limit)
        sql = f"SELECT * FROM canary_configs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_canary_config(r) for r in rows]

    async def delete_canary_config(self, agent: str, *, tenant_id: str) -> bool:
        status: str = await self._db.execute(
            "DELETE FROM canary_configs WHERE agent = $1 AND tenant_id = $2",
            agent,
            tenant_id,
        )
        # asyncpg returns a status string like "DELETE 1" / "DELETE 0".
        return status.startswith("DELETE ") and not status.endswith(" 0")

    # ------------------------------------------------------------------
    # Agent registry (ADR 014 D1)
    # ------------------------------------------------------------------

    async def save_agent_bundle(self, bundle: AgentBundleRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO agent_bundles (
                name, tenant_id, version, created_by, content_hash,
                files, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7
            )
            """,
            bundle.name,
            bundle.tenant_id,
            bundle.version,
            bundle.created_by,
            bundle.content_hash,
            bundle.files,
            bundle.created_at,
        )

    async def get_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> AgentBundleRecord | None:
        if version is not None:
            row = await self._db.fetchrow(
                "SELECT * FROM agent_bundles WHERE name = $1 AND tenant_id = $2 AND version = $3",
                name,
                tenant_id,
                version,
            )
        else:
            # version=None → latest by created_at.
            row = await self._db.fetchrow(
                "SELECT * FROM agent_bundles WHERE name = $1 AND tenant_id = $2 "
                "ORDER BY created_at DESC LIMIT 1",
                name,
                tenant_id,
            )
        return _row_to_agent_bundle(row) if row else None

    async def list_agents(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[AgentBundleRecord]:
        # Latest version per name, newest-first: DISTINCT ON the name picks
        # each name's most-recent row, then the outer sort orders names by
        # that row's created_at DESC.
        rows = await self._db.fetch(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (name) * FROM agent_bundles
                WHERE tenant_id = $1
                ORDER BY name, created_at DESC
            ) latest
            ORDER BY created_at DESC
            LIMIT $2
            """,
            tenant_id,
            limit,
        )
        return [_row_to_agent_bundle(r) for r in rows]

    async def list_agent_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[AgentBundleRecord]:
        rows = await self._db.fetch(
            "SELECT * FROM agent_bundles WHERE name = $1 AND tenant_id = $2 "
            "ORDER BY created_at DESC LIMIT $3",
            name,
            tenant_id,
            limit,
        )
        return [_row_to_agent_bundle(r) for r in rows]

    async def list_all_agent_bundles(self, *, limit: int = 100_000) -> list[AgentBundleRecord]:
        # item 26 (DR export) — every version, every tenant. Stable order.
        rows = await self._db.fetch(
            "SELECT * FROM agent_bundles ORDER BY tenant_id, name, created_at LIMIT $1",
            limit,
        )
        return [_row_to_agent_bundle(r) for r in rows]

    async def delete_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        if version is not None:
            status = await self._db.execute(
                "DELETE FROM agent_bundles WHERE name = $1 AND tenant_id = $2 AND version = $3",
                name,
                tenant_id,
                version,
            )
        else:
            status = await self._db.execute(
                "DELETE FROM agent_bundles WHERE name = $1 AND tenant_id = $2",
                name,
                tenant_id,
            )
        # asyncpg returns a command tag like "DELETE <n>".
        return int(status.split()[-1])

    # ------------------------------------------------------------------
    # Projects (ADR 040)
    # ------------------------------------------------------------------

    async def create_project(self, project: Project) -> Project:
        try:
            await self._db.execute(
                """
                INSERT INTO projects (
                    project_id, tenant_id, name, description,
                    owner_principal_id, created_at, updated_at, archived_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                project.project_id,
                project.tenant_id,
                project.name,
                project.description,
                project.owner_principal_id,
                project.created_at,
                project.updated_at,
                project.archived_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(
                f"project ({project.tenant_id!r}, {project.name!r}) already exists"
            ) from exc
        return project

    async def get_project(self, tenant_id: str, project_id: str) -> Project | None:
        row = await self._db.fetchrow(
            "SELECT * FROM projects WHERE project_id = $1 AND tenant_id = $2",
            project_id,
            tenant_id,
        )
        return _row_to_project(row) if row else None

    async def get_project_by_name(self, tenant_id: str, name: str) -> Project | None:
        row = await self._db.fetchrow(
            "SELECT * FROM projects WHERE tenant_id = $1 AND name = $2",
            tenant_id,
            name,
        )
        return _row_to_project(row) if row else None

    async def list_projects(
        self,
        tenant_id: str,
        *,
        include_archived: bool = False,
        limit: int = 100,
        after_id: str | None = None,
    ) -> list[Project]:
        clauses = ["tenant_id = $1"]
        params: list[object] = [tenant_id]
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if after_id is not None:
            cursor_row = await self._db.fetchrow(
                "SELECT created_at, project_id FROM projects WHERE project_id = $1",
                after_id,
            )
            if cursor_row is not None:
                params.append(cursor_row["created_at"])
                params.append(cursor_row["project_id"])
                clauses.append(f"(created_at, project_id) < (${len(params) - 1}, ${len(params)})")
        params.append(limit)
        sql = (
            "SELECT * FROM projects WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at DESC, project_id DESC LIMIT ${len(params)}"
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_project(r) for r in rows]

    async def update_project(
        self,
        tenant_id: str,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Project | None:
        if name is None and description is None:
            return await self.get_project(tenant_id, project_id)
        sets = ["updated_at = $1"]
        params: list[object] = [datetime.now(UTC)]
        if name is not None:
            params.append(name)
            sets.append(f"name = ${len(params)}")
        if description is not None:
            params.append(description)
            sets.append(f"description = ${len(params)}")
        params.append(project_id)
        params.append(tenant_id)
        sql = (
            f"UPDATE projects SET {', '.join(sets)} "
            f"WHERE project_id = ${len(params) - 1} AND tenant_id = ${len(params)}"
        )
        try:
            status = await self._db.execute(sql, *params)
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(
                f"project name {name!r} already exists in tenant {tenant_id!r}"
            ) from exc
        # asyncpg returns "UPDATE <n>" — int 0 means no row matched.
        if int(status.split()[-1]) == 0:
            return None
        return await self.get_project(tenant_id, project_id)

    async def archive_project(self, tenant_id: str, project_id: str) -> bool:
        existing = await self.get_project(tenant_id, project_id)
        if existing is None:
            return False
        if existing.name == _DEFAULT_PROJECT_NAME:
            raise ValueError(f"default project for tenant {tenant_id!r} cannot be archived")
        if existing.archived_at is not None:
            return False
        now = datetime.now(UTC)
        status = await self._db.execute(
            "UPDATE projects SET archived_at = $1, updated_at = $1 "
            "WHERE project_id = $2 AND tenant_id = $3 AND archived_at IS NULL",
            now,
            project_id,
            tenant_id,
        )
        return int(status.split()[-1]) > 0

    async def add_project_member(
        self,
        project_id: str,
        principal_id: str,
        role: ProjectMemberRole,
        added_by: str,
    ) -> None:
        try:
            await self._db.execute(
                "INSERT INTO project_members "
                "(project_id, principal_id, role, added_by, added_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                project_id,
                principal_id,
                role.value,
                added_by,
                datetime.now(UTC),
            )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(f"member {principal_id!r} already on project {project_id!r}") from exc

    async def list_project_members(self, project_id: str) -> list[ProjectMember]:
        rows = await self._db.fetch(
            "SELECT * FROM project_members WHERE project_id = $1 "
            "ORDER BY added_at ASC, principal_id ASC",
            project_id,
        )
        return [_row_to_project_member(r) for r in rows]

    async def update_project_member(
        self,
        project_id: str,
        principal_id: str,
        *,
        role: ProjectMemberRole,
    ) -> ProjectMember | None:
        status = await self._db.execute(
            "UPDATE project_members SET role = $1 WHERE project_id = $2 AND principal_id = $3",
            role.value,
            project_id,
            principal_id,
        )
        if int(status.split()[-1]) == 0:
            return None
        return await self.get_project_member(project_id, principal_id)

    async def remove_project_member(self, project_id: str, principal_id: str) -> bool:
        status = await self._db.execute(
            "DELETE FROM project_members WHERE project_id = $1 AND principal_id = $2",
            project_id,
            principal_id,
        )
        return int(status.split()[-1]) > 0

    async def get_project_member(
        self,
        project_id: str,
        principal_id: str,
    ) -> ProjectMember | None:
        row = await self._db.fetchrow(
            "SELECT * FROM project_members WHERE project_id = $1 AND principal_id = $2",
            project_id,
            principal_id,
        )
        return _row_to_project_member(row) if row else None

    async def attach_agent_to_project(self, project_id: str, agent_name: str) -> None:
        await self._db.execute(
            "INSERT INTO project_agents (project_id, agent_name, added_at) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            project_id,
            agent_name,
            datetime.now(UTC),
        )

    async def detach_agent_from_project(self, project_id: str, agent_name: str) -> bool:
        status = await self._db.execute(
            "DELETE FROM project_agents WHERE project_id = $1 AND agent_name = $2",
            project_id,
            agent_name,
        )
        return int(status.split()[-1]) > 0

    async def list_project_agents(self, project_id: str) -> list[str]:
        rows = await self._db.fetch(
            "SELECT agent_name FROM project_agents WHERE project_id = $1 "
            "ORDER BY added_at ASC, agent_name ASC",
            project_id,
        )
        return [r["agent_name"] for r in rows]

    async def list_projects_for_agent(self, tenant_id: str, agent_name: str) -> list[str]:
        rows = await self._db.fetch(
            "SELECT pa.project_id FROM project_agents pa "
            "JOIN projects p ON p.project_id = pa.project_id "
            "WHERE p.tenant_id = $1 AND pa.agent_name = $2 "
            "ORDER BY pa.added_at ASC, pa.project_id ASC",
            tenant_id,
            agent_name,
        )
        if rows:
            return [r["project_id"] for r in rows]
        default_project = await self.get_or_create_default_project(tenant_id)
        return [default_project.project_id]

    async def attach_workflow_to_project(self, project_id: str, workflow_name: str) -> None:
        await self._db.execute(
            "INSERT INTO project_workflows (project_id, workflow_name, added_at) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            project_id,
            workflow_name,
            datetime.now(UTC),
        )

    async def detach_workflow_from_project(self, project_id: str, workflow_name: str) -> bool:
        status = await self._db.execute(
            "DELETE FROM project_workflows WHERE project_id = $1 AND workflow_name = $2",
            project_id,
            workflow_name,
        )
        return int(status.split()[-1]) > 0

    async def list_project_workflows(self, project_id: str) -> list[str]:
        rows = await self._db.fetch(
            "SELECT workflow_name FROM project_workflows WHERE project_id = $1 "
            "ORDER BY added_at ASC, workflow_name ASC",
            project_id,
        )
        return [r["workflow_name"] for r in rows]

    async def list_projects_for_workflow(self, tenant_id: str, workflow_name: str) -> list[str]:
        rows = await self._db.fetch(
            "SELECT pw.project_id FROM project_workflows pw "
            "JOIN projects p ON p.project_id = pw.project_id "
            "WHERE p.tenant_id = $1 AND pw.workflow_name = $2 "
            "ORDER BY pw.added_at ASC, pw.project_id ASC",
            tenant_id,
            workflow_name,
        )
        if rows:
            return [r["project_id"] for r in rows]
        default_project = await self.get_or_create_default_project(tenant_id)
        return [default_project.project_id]

    async def attach_kb_to_project(
        self,
        project_id: str,
        kb_id: str,
        mode: ProjectKbMode,
    ) -> None:
        await self._db.execute(
            "INSERT INTO project_kbs (project_id, kb_id, mode, added_at) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT(project_id, kb_id) DO UPDATE SET mode = EXCLUDED.mode",
            project_id,
            kb_id,
            mode.value,
            datetime.now(UTC),
        )

    async def detach_kb_from_project(self, project_id: str, kb_id: str) -> bool:
        status = await self._db.execute(
            "DELETE FROM project_kbs WHERE project_id = $1 AND kb_id = $2",
            project_id,
            kb_id,
        )
        return int(status.split()[-1]) > 0

    async def list_project_kbs(self, project_id: str) -> list[tuple[str, ProjectKbMode]]:
        rows = await self._db.fetch(
            "SELECT kb_id, mode FROM project_kbs WHERE project_id = $1 "
            "ORDER BY added_at ASC, kb_id ASC",
            project_id,
        )
        return [(r["kb_id"], ProjectKbMode(r["mode"])) for r in rows]

    async def list_projects_for_kb(self, tenant_id: str, kb_id: str) -> list[str]:
        rows = await self._db.fetch(
            "SELECT pk.project_id FROM project_kbs pk "
            "JOIN projects p ON p.project_id = pk.project_id "
            "WHERE p.tenant_id = $1 AND pk.kb_id = $2 "
            "ORDER BY pk.added_at ASC, pk.project_id ASC",
            tenant_id,
            kb_id,
        )
        return [r["project_id"] for r in rows]

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

    # ------------------------------------------------------------------
    # Agent catalog (ADR 041)
    # ------------------------------------------------------------------

    async def upsert_catalog_entry(self, entry: CatalogEntry) -> None:
        _enforce_catalog_namespace(entry.source, entry.tenant_id)
        tkey = _tenant_key(entry.tenant_id)
        await self._db.execute(
            """
            INSERT INTO catalog_entries (
                slug, source, tenant_id, tenant_id_key, latest_version,
                name, title, description, tags, shape, recommended_for,
                ratings_summary, popularity, synced_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (slug, source, tenant_id_key) DO UPDATE SET
                latest_version  = EXCLUDED.latest_version,
                name            = EXCLUDED.name,
                title           = EXCLUDED.title,
                description     = EXCLUDED.description,
                tags            = EXCLUDED.tags,
                shape           = EXCLUDED.shape,
                recommended_for = EXCLUDED.recommended_for,
                ratings_summary = EXCLUDED.ratings_summary,
                popularity     = EXCLUDED.popularity,
                synced_at       = EXCLUDED.synced_at
            """,
            entry.slug,
            entry.source.value,
            entry.tenant_id,
            tkey,
            entry.latest_version,
            entry.name,
            entry.title,
            entry.description,
            list(entry.tags),
            entry.shape,
            entry.recommended_for,
            entry.ratings_summary.model_dump(),
            entry.popularity,
            entry.synced_at,
        )

    async def get_catalog_entry(
        self,
        slug: str,
        *,
        source: CatalogSource,
        tenant_id: str | None = None,
    ) -> CatalogEntry | None:
        if source is CatalogSource.PRIVATE and tenant_id is None:
            return None
        tkey = _tenant_key(tenant_id if source is CatalogSource.PRIVATE else None)
        row = await self._db.fetchrow(
            "SELECT * FROM catalog_entries WHERE slug = $1 AND source = $2 AND tenant_id_key = $3",
            slug,
            source.value,
            tkey,
        )
        return _row_to_catalog_entry(row) if row else None

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
        clauses: list[str] = [
            "(source = 'movate' OR (source = 'private' AND tenant_id = $1) OR source = 'community')"
        ]
        params: list[Any] = [tenant_id]

        def _next() -> str:
            return f"${len(params) + 1}"

        if source_filter is not None:
            clauses.append(f"source = {_next()}")
            params.append(source_filter.value)
        if shape_filter is not None:
            clauses.append(f"shape = {_next()}")
            params.append(shape_filter)
        if tag_filter is not None:
            clauses.append(f"tags @> {_next()}::jsonb")
            params.append(json.dumps([tag_filter]))
        if q:
            needle = f"%{q.lower()}%"
            n = _next()
            clauses.append(
                f"(LOWER(name) LIKE {n} OR LOWER(title) LIKE {n} OR LOWER(description) LIKE {n})"
            )
            params.append(needle)
        if after_slug is not None:
            clauses.append(f"slug > {_next()}")
            params.append(after_slug)

        sql = (
            "SELECT * FROM catalog_entries WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY slug ASC LIMIT {_next()}"
        )
        params.append(int(limit))
        rows = await self._db.fetch(sql, *params)
        return [_row_to_catalog_entry(r) for r in rows]

    async def get_catalog_entry_versions(
        self,
        slug: str,
        *,
        source: CatalogSource,
        tenant_id: str | None = None,
    ) -> list[CatalogEntryVersion]:
        if source is CatalogSource.PRIVATE and tenant_id is None:
            return []
        tkey = _tenant_key(tenant_id if source is CatalogSource.PRIVATE else None)
        rows = await self._db.fetch(
            "SELECT * FROM catalog_entry_versions "
            "WHERE slug = $1 AND source = $2 AND tenant_id_key = $3 "
            "ORDER BY published_at DESC",
            slug,
            source.value,
            tkey,
        )
        return [_row_to_catalog_entry_version(r) for r in rows]

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
        tkey = _tenant_key(tenant_id if source is CatalogSource.PRIVATE else None)
        row = await self._db.fetchrow(
            "SELECT * FROM catalog_entry_versions "
            "WHERE slug = $1 AND source = $2 AND version = $3 AND tenant_id_key = $4",
            slug,
            source.value,
            version,
            tkey,
        )
        return _row_to_catalog_entry_version(row) if row else None

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
        _enforce_catalog_namespace(source, tenant_id)
        tkey = _tenant_key(tenant_id)
        now = datetime.now(UTC)
        row = await self._db.fetchrow(
            """
            INSERT INTO catalog_entry_versions (
                slug, version, source, tenant_id, tenant_id_key,
                bundle_tar, digest, published_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (slug, source, version, tenant_id_key) DO UPDATE SET
                bundle_tar = EXCLUDED.bundle_tar,
                digest     = EXCLUDED.digest
            RETURNING *
            """,
            slug,
            version,
            source.value,
            tenant_id,
            tkey,
            bundle_tar,
            digest,
            now,
        )
        assert row is not None
        return _row_to_catalog_entry_version(row)

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
        async with self._db.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO catalog_entry_ratings (
                    slug, source, tenant_id, rating, comment, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (slug, source, tenant_id) DO UPDATE SET
                    rating     = EXCLUDED.rating,
                    comment    = EXCLUDED.comment,
                    created_at = EXCLUDED.created_at
                """,
                slug,
                source.value,
                tenant_id,
                int(rating),
                comment,
                datetime.now(UTC),
            )
            agg = await conn.fetchrow(
                "SELECT COUNT(*) AS c, AVG(rating)::float AS a FROM catalog_entry_ratings "
                "WHERE slug = $1 AND source = $2",
                slug,
                source.value,
            )
            count = int(agg["c"] or 0)
            avg = float(agg["a"] or 0.0)
            summary = CatalogRatingsSummary(count=count, avg=avg)
            await conn.execute(
                "UPDATE catalog_entries SET ratings_summary = $1 WHERE slug = $2 AND source = $3",
                summary.model_dump(),
                slug,
                source.value,
            )
        return summary

    async def get_catalog_sync_watermark(self, source: CatalogSource) -> datetime | None:
        row = await self._db.fetchrow(
            "SELECT last_synced_at FROM catalog_sync_watermark WHERE source = $1",
            source.value,
        )
        if row is None:
            return None
        ts: datetime = row["last_synced_at"]
        return ts

    async def set_catalog_sync_watermark(self, source: CatalogSource, ts: datetime) -> None:
        await self._db.execute(
            """
            INSERT INTO catalog_sync_watermark (source, last_synced_at)
            VALUES ($1, $2)
            ON CONFLICT (source) DO UPDATE SET last_synced_at = EXCLUDED.last_synced_at
            """,
            source.value,
            ts,
        )

    # ------------------------------------------------------------------
    # Durable workflow registry (ADR 037 D1)
    # ------------------------------------------------------------------

    async def save_workflow_bundle(self, bundle: WorkflowBundleRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO workflow_bundles (
                name, tenant_id, version, created_by, content_hash,
                files, published, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8
            )
            """,
            bundle.name,
            bundle.tenant_id,
            bundle.version,
            bundle.created_by,
            bundle.content_hash,
            bundle.files,
            bundle.published,
            bundle.created_at,
        )

    async def get_workflow_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> WorkflowBundleRecord | None:
        if version is not None:
            row = await self._db.fetchrow(
                "SELECT * FROM workflow_bundles "
                "WHERE name = $1 AND tenant_id = $2 AND version = $3",
                name,
                tenant_id,
                version,
            )
        else:
            row = await self._db.fetchrow(
                "SELECT * FROM workflow_bundles WHERE name = $1 AND tenant_id = $2 "
                "ORDER BY created_at DESC LIMIT 1",
                name,
                tenant_id,
            )
        return _row_to_workflow_bundle(row) if row else None

    async def list_workflows(
        self,
        *,
        tenant_id: str,
        published_only: bool = False,
        limit: int = 100,
    ) -> list[WorkflowBundleRecord]:
        # Latest version per name, newest-first: DISTINCT ON the name picks
        # each name's most-recent row, then the outer sort orders by that
        # row's created_at DESC. Mirrors list_agents in postgres.py.
        if published_only:
            rows = await self._db.fetch(
                """
                SELECT * FROM (
                    SELECT DISTINCT ON (name) * FROM workflow_bundles
                    WHERE tenant_id = $1
                      AND name IN (
                          SELECT DISTINCT name FROM workflow_bundles
                          WHERE tenant_id = $1 AND published = TRUE
                      )
                    ORDER BY name, created_at DESC
                ) latest
                ORDER BY created_at DESC
                LIMIT $2
                """,
                tenant_id,
                limit,
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT * FROM (
                    SELECT DISTINCT ON (name) * FROM workflow_bundles
                    WHERE tenant_id = $1
                    ORDER BY name, created_at DESC
                ) latest
                ORDER BY created_at DESC
                LIMIT $2
                """,
                tenant_id,
                limit,
            )
        return [_row_to_workflow_bundle(r) for r in rows]

    async def list_workflow_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[WorkflowBundleRecord]:
        rows = await self._db.fetch(
            "SELECT * FROM workflow_bundles WHERE name = $1 AND tenant_id = $2 "
            "ORDER BY created_at DESC LIMIT $3",
            name,
            tenant_id,
            limit,
        )
        return [_row_to_workflow_bundle(r) for r in rows]

    async def list_all_workflow_bundles(
        self, *, limit: int = 100_000
    ) -> list[WorkflowBundleRecord]:
        rows = await self._db.fetch(
            "SELECT * FROM workflow_bundles ORDER BY tenant_id, name, created_at LIMIT $1",
            limit,
        )
        return [_row_to_workflow_bundle(r) for r in rows]

    async def delete_workflow_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        if version is not None:
            status = await self._db.execute(
                "DELETE FROM workflow_bundles WHERE name = $1 AND tenant_id = $2 AND version = $3",
                name,
                tenant_id,
                version,
            )
        else:
            status = await self._db.execute(
                "DELETE FROM workflow_bundles WHERE name = $1 AND tenant_id = $2",
                name,
                tenant_id,
            )
        return int(status.split()[-1])

    async def publish_workflow_version(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str,
    ) -> bool:
        # ADR 037 D1: at most one published row per (tenant, name). Use a
        # transaction so the promote + clear-others is atomic — a concurrent
        # publish can't leave two rows flagged. The exists-probe distinguishes
        # 404 (no such version) from a successful idempotent re-promote.
        async with self._db.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT 1 FROM workflow_bundles "
                "WHERE tenant_id = $1 AND name = $2 AND version = $3",
                tenant_id,
                name,
                version,
            )
            if row is None:
                return False
            await conn.execute(
                "UPDATE workflow_bundles SET published = TRUE "
                "WHERE tenant_id = $1 AND name = $2 AND version = $3",
                tenant_id,
                name,
                version,
            )
            await conn.execute(
                "UPDATE workflow_bundles SET published = FALSE "
                "WHERE tenant_id = $1 AND name = $2 AND version <> $3",
                tenant_id,
                name,
                version,
            )
            return True

    # ------------------------------------------------------------------
    # Workflow runs
    # ------------------------------------------------------------------

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None:
        # Upsert on the workflow_run_id PRIMARY KEY: a resume (ADR 017 D5,
        # PR 2) re-saves the SAME workflow_run_id when a paused run continues
        # to completion (or re-pauses), and the signal endpoint persists the
        # merged checkpoint back under the same id. ON CONFLICT DO UPDATE
        # makes save idempotent on the id — latest write wins. A first-time
        # save (no existing row) takes the plain INSERT path unchanged.
        await self._db.execute(
            """
            INSERT INTO workflow_runs (
                workflow_run_id, tenant_id, workflow, workflow_version,
                status, initial_state, final_state, error_node_id, error,
                created_at, paused_node_id, paused_state, human_task
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (workflow_run_id) DO UPDATE SET
                tenant_id        = EXCLUDED.tenant_id,
                workflow         = EXCLUDED.workflow,
                workflow_version = EXCLUDED.workflow_version,
                status           = EXCLUDED.status,
                initial_state    = EXCLUDED.initial_state,
                final_state      = EXCLUDED.final_state,
                error_node_id    = EXCLUDED.error_node_id,
                error            = EXCLUDED.error,
                created_at       = EXCLUDED.created_at,
                paused_node_id   = EXCLUDED.paused_node_id,
                paused_state     = EXCLUDED.paused_state,
                human_task       = EXCLUDED.human_task
            """,
            w.workflow_run_id,
            w.tenant_id,
            w.workflow,
            w.workflow_version,
            w.status.value,
            w.initial_state,
            w.final_state,
            w.error_node_id,
            w.error.model_dump() if w.error else None,
            w.created_at,
            w.paused_node_id,
            w.paused_state,
            w.human_task,
        )

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM workflow_runs WHERE workflow_run_id = $1 AND tenant_id = $2",
            workflow_run_id,
            tenant_id,
        )
        return _row_to_workflow_run(row) if row else None

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if workflow is not None:
            params.append(workflow)
            clauses.append(f"workflow = ${len(params)}")
        if status is not None:
            params.append(status.value)
            clauses.append(f"status = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM workflow_runs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_workflow_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Batches (item 17 — batch inference)
    # ------------------------------------------------------------------

    async def save_batch(self, batch: BatchRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO batches (
                batch_id, tenant_id, agent, total, created_by, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            batch.batch_id,
            batch.tenant_id,
            batch.agent,
            batch.total,
            batch.created_by,
            batch.created_at,
        )

    async def get_batch(self, batch_id: str, *, tenant_id: str) -> BatchRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM batches WHERE batch_id = $1 AND tenant_id = $2",
            batch_id,
            tenant_id,
        )
        return _row_to_batch(row) if row else None

    async def list_batches(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 20,
    ) -> list[BatchRecord]:
        params: list[Any] = []
        where = ""
        if tenant_id is not None:
            params.append(tenant_id)
            where = f"WHERE tenant_id = ${len(params)}"
        params.append(limit)
        sql = f"SELECT * FROM batches {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_batch(r) for r in rows]

    async def save_job(self, job: JobRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO jobs (
                job_id, tenant_id, kind, target, status, input,
                result_run_id, error, api_key_id,
                created_at, claimed_at, completed_at,
                notify_email, attempt_count, next_retry_at, thread_id,
                target_version, resume_workflow_run_id, batch_id,
                trace_context, cancel_requested
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
            )
            """,
            job.job_id,
            job.tenant_id,
            job.kind.value,
            job.target,
            job.status.value,
            job.input,
            job.result_run_id,
            job.error.model_dump() if job.error else None,
            job.api_key_id,
            job.created_at,
            job.claimed_at,
            job.completed_at,
            job.notify_email,
            job.attempt_count,
            job.next_retry_at,
            job.thread_id,
            job.target_version,
            job.resume_workflow_run_id,
            job.batch_id,
            # item 32 (ADR 019): W3C trace-context carrier. JSONB via the
            # pool's json codec (encoder=json.dumps). Empty dict {} when OTel
            # was off / no active span at enqueue.
            job.trace_context,
            # item 36 (R4b): cooperative-cancel flag. Always FALSE at insert
            # (a fresh job is never pre-cancelled); set later by
            # request_job_cancel for a RUNNING job.
            job.cancel_requested,
        )

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM jobs WHERE job_id = $1 AND tenant_id = $2",
            job_id,
            tenant_id,
        )
        return _row_to_job(row) if row else None

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        target: str | None = None,
        batch_id: str | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if status is not None:
            params.append(status.value)
            clauses.append(f"status = ${len(params)}")
        if target is not None:
            params.append(target)
            clauses.append(f"target = ${len(params)}")
        if batch_id is not None:
            params.append(batch_id)
            clauses.append(f"batch_id = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_job(r) for r in rows]

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """Postgres claim path: ``SELECT ... FOR UPDATE SKIP LOCKED``.

        Two workers hitting this concurrently take row-level locks on
        DIFFERENT rows; whichever sees the oldest queued first wins
        that row, the second sees no available rows (or moves on to
        the next queued one). No global serialization, no retries.
        """
        async with self._db.acquire() as conn, conn.transaction():
            now = datetime.now(UTC)
            # Retry-aware claim: skip rows whose next_retry_at is in
            # the future. The `next_retry_at IS NULL` branch is the
            # common case (fresh jobs); `<= now` covers re-queued jobs
            # whose backoff has elapsed.
            if tenant_id:
                tenant_clause = "AND tenant_id = $2"
                params: tuple[Any, ...] = (now, tenant_id)
            else:
                tenant_clause = ""
                params = (now,)
            row = await conn.fetchrow(
                f"""
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= $1)
                  {tenant_clause}
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                *params,
            )
            if row is None:
                return None

            # Reuse the `now` we computed for the claim-window filter.
            await conn.execute(
                """
                UPDATE jobs SET status = 'running', claimed_at = $1
                WHERE job_id = $2
                """,
                now,
                row["job_id"],
            )

            # Re-fetch to return the post-update row. (asyncpg's
            # transaction context closes on exit; we do this inside
            # so the caller sees a consistent snapshot.)
            updated = await conn.fetchrow("SELECT * FROM jobs WHERE job_id = $1", row["job_id"])
            return _row_to_job(updated) if updated else None

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
            raise ValueError(
                f"update_job only accepts terminal statuses; got {status!r}. "
                f"Use save_job/claim_next_job/requeue_job for non-terminal transitions."
            )
        # tenant_id in WHERE: even a misconfigured worker can't mutate
        # another tenant's job. Silently no-ops on tenant mismatch.
        await self._db.execute(
            """
            UPDATE jobs
            SET status = $1, result_run_id = $2, error = $3, completed_at = $4
            WHERE job_id = $5 AND tenant_id = $6
            """,
            status.value,
            result_run_id,
            error,
            datetime.now(UTC),
            job_id,
            tenant_id,
        )

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Re-queue a ``RUNNING`` job for a retry after a transient failure.

        Sets status back to ``QUEUED``, clears ``claimed_at`` (so the
        next claim records a fresh attempt), bumps ``attempt_count``,
        and sets ``next_retry_at`` so the claim path skips this row
        until backoff elapses.
        """
        await self._db.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                claimed_at = NULL,
                attempt_count = $1,
                next_retry_at = $2
            WHERE job_id = $3 AND tenant_id = $4
            """,
            attempt_count,
            next_retry_at,
            job_id,
            tenant_id,
        )

    async def reclaim_stale_jobs(
        self,
        *,
        older_than: datetime,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> ReclaimResult:
        """Reclaim orphaned ``RUNNING`` jobs — cross-tenant, atomic.

        Two ``UPDATE ... RETURNING`` statements inside ONE transaction.
        The dead-letter UPDATE runs FIRST (stricter
        ``attempt_count + 1 >= max_attempts`` predicate); the requeue
        UPDATE then sweeps the remaining stale ``running`` rows. Counts
        come back via ``len(RETURNING)``.
        """
        effective_now = now if now is not None else datetime.now(UTC)
        dead_letter_error: dict[str, object] = {
            "type": "reaper_dead_letter",
            "message": "orphaned in running past visibility timeout; retry budget exhausted",
        }
        async with self._db.acquire() as conn, conn.transaction():
            dead_rows = await conn.fetch(
                """
                UPDATE jobs
                SET status = 'dead_letter',
                    completed_at = $1,
                    error = $2
                WHERE status = 'running'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < $3
                  AND attempt_count + 1 >= $4
                RETURNING job_id
                """,
                effective_now,
                dead_letter_error,
                older_than,
                max_attempts,
            )
            requeued_rows = await conn.fetch(
                """
                UPDATE jobs
                SET status = 'queued',
                    claimed_at = NULL,
                    attempt_count = attempt_count + 1,
                    next_retry_at = $1
                WHERE status = 'running'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < $2
                RETURNING job_id
                """,
                effective_now,
                older_than,
            )
        return ReclaimResult(
            requeued=len(requeued_rows),
            dead_lettered=len(dead_rows),
        )

    async def request_job_cancel(self, job_id: str, *, tenant_id: str) -> JobStatus | None:
        """Cooperatively cancel a job — single atomic UPDATE … RETURNING.

        One statement covers all three live transitions via a ``CASE``:

        * ``queued`` → ``cancelled`` (+ ``completed_at = now``); never
          claimed, so cancellation is immediate.
        * ``running`` → keep ``running`` but set ``cancel_requested =
          TRUE``; the worker writes ``CANCELLED`` at its checkpoint.
        * any terminal status → the ``CASE`` leaves status untouched and
          the row is still RETURNED, so we report the unchanged status.

        The UPDATE matches every status (no status filter in WHERE) so a
        terminal row is returned as a no-op rather than appearing
        ``None`` (missing). ``tenant_id`` IS in WHERE — a cross-tenant id
        returns ``None`` (→ 404), never mutating another tenant's job.
        """
        row = await self._db.fetchrow(
            """
            UPDATE jobs
            SET status = CASE
                    WHEN status = 'queued' THEN 'cancelled'
                    ELSE status
                END,
                completed_at = CASE
                    WHEN status = 'queued' THEN $1
                    ELSE completed_at
                END,
                cancel_requested = CASE
                    WHEN status = 'running' THEN TRUE
                    ELSE cancel_requested
                END
            WHERE job_id = $2 AND tenant_id = $3
            RETURNING status
            """,
            datetime.now(UTC),
            job_id,
            tenant_id,
        )
        if row is None:
            # No row for this (job_id, tenant_id) — missing or cross-tenant.
            return None
        return JobStatus(row["status"])

    # ------------------------------------------------------------------
    # Dead-letter operations (operate retry-exhausted jobs)
    # ------------------------------------------------------------------

    async def list_dead_letter_jobs(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        agent: str | None = None,
    ) -> list[JobRecord]:
        """Newest-first DEAD_LETTER rows for ``tenant_id`` (optional ``agent``
        = ``target`` filter). Tenant-scoped in WHERE."""
        params: list[Any] = [tenant_id]
        clauses = ["tenant_id = $1", "status = 'dead_letter'"]
        if agent is not None:
            params.append(agent)
            clauses.append(f"target = ${len(params)}")
        params.append(limit)
        sql = (
            f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT ${len(params)}"
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_job(r) for r in rows]

    async def requeue_dead_letter_job(self, job_id: str, *, tenant_id: str) -> bool:
        """Reset a DEAD_LETTER job → QUEUED with a fresh attempt budget.

        Single ``UPDATE ... RETURNING`` guarded on ``status =
        'dead_letter'`` (and tenant). The RETURNING row is present iff a
        row actually flipped, so we report ``True``/``False`` — a
        non-dead-letter / cross-tenant id returns ``False`` and the
        API/CLI maps that to a clean error."""
        row = await self._db.fetchrow(
            """
            UPDATE jobs
            SET status = 'queued',
                attempt_count = 0,
                next_retry_at = NULL,
                claimed_at = NULL,
                completed_at = NULL,
                error = NULL
            WHERE job_id = $1 AND tenant_id = $2 AND status = 'dead_letter'
            RETURNING job_id
            """,
            job_id,
            tenant_id,
        )
        return row is not None

    async def purge_dead_letter_jobs(
        self,
        tenant_id: str,
        *,
        before: datetime | None = None,
    ) -> int:
        """Delete this tenant's DEAD_LETTER rows; returns the count deleted.

        Tenant + status scoped in WHERE. ``before`` narrows to rows whose
        ``completed_at`` is strictly older than the cutoff (and non-NULL);
        ``None`` purges all dead-letter rows for the tenant."""
        params: list[Any] = [tenant_id]
        clauses = ["tenant_id = $1", "status = 'dead_letter'"]
        if before is not None:
            params.append(before)
            clauses.append("completed_at IS NOT NULL")
            clauses.append(f"completed_at < ${len(params)}")
        rows = await self._db.fetch(
            f"DELETE FROM jobs WHERE {' AND '.join(clauses)} RETURNING job_id",
            *params,
        )
        return len(rows)

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO api_keys (
                key_id, tenant_id, env, secret_hash, salt, label,
                created_at, last_used_at, revoked_at, expires_at, scope, scopes
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            key.key_id,
            key.tenant_id,
            key.env.value,
            key.secret_hash,
            key.salt,
            key.label,
            key.created_at,
            key.last_used_at,
            key.revoked_at,
            key.expires_at,
            key.scope,
            # JSONB via the pool's json codec (encoder=json.dumps). Empty
            # list → NULL so a round-trip matches a never-scoped legacy row
            # → resolves to the default at check time via effective_scopes.
            list(key.scopes) if key.scopes else None,
        )

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        row = await self._db.fetchrow("SELECT * FROM api_keys WHERE key_id = $1", key_id)
        return _row_to_api_key(row) if row else None

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM api_keys {where} ORDER BY created_at DESC"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_api_key(r) for r in rows]

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        # tenant_id in WHERE: a tenant can only revoke its own keys
        # even if it discovers another tenant's key_id.
        await self._db.execute(
            """
            UPDATE api_keys SET revoked_at = $1
            WHERE key_id = $2 AND tenant_id = $3 AND revoked_at IS NULL
            """,
            datetime.now(UTC),
            key_id,
            tenant_id,
        )

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        # tenant_id is defense in depth — the auth path already
        # cross-checks the presented key's tenant prefix against the
        # looked-up record. The storage layer enforces independently.
        await self._db.execute(
            "UPDATE api_keys SET last_used_at = $1 WHERE key_id = $2 AND tenant_id = $3",
            datetime.now(UTC),
            key_id,
            tenant_id,
        )

    async def set_api_key_expiry(
        self, key_id: str, *, tenant_id: str, expires_at: datetime
    ) -> None:
        # Grace window on the OLD key during rotation (ADR 013 D5).
        # tenant_id + revoked_at IS NULL: tenant-scoped, never re-arm a
        # dead key. No-op on missing / cross-tenant / already-revoked.
        await self._db.execute(
            """
            UPDATE api_keys SET expires_at = $1
            WHERE key_id = $2 AND tenant_id = $3 AND revoked_at IS NULL
            """,
            expires_at,
            key_id,
            tenant_id,
        )

    async def update_api_key_scopes(self, key_id: str, *, scopes: list[str]) -> None:
        # Bootstrap-key self-heal: rewrite ONLY the scopes column by key_id.
        # Not tenant-scoped — the sole caller resolves the row by the parsed
        # key_id from MOVATE_SEED_API_KEY at startup. Mirrors save_api_key's
        # JSONB encoding (empty list → NULL). No-op on missing; leaves every
        # other column (secret_hash/salt/tenant_id/env/created_at) untouched.
        await self._db.execute(
            "UPDATE api_keys SET scopes = $1 WHERE key_id = $2",
            list(scopes) if scopes else None,
            key_id,
        )

    async def revoke_all_api_keys(self, *, tenant_id: str, except_key_id: str | None = None) -> int:
        # Compromise-response bulk revoke (ADR 013 D5). Returns the count
        # revoked. asyncpg's execute() returns a status string like
        # "UPDATE 3" — parse the trailing integer. Each UPDATE touches
        # only the active subset, so a re-run returns 0.
        if except_key_id is not None:
            status = await self._db.execute(
                """
                UPDATE api_keys SET revoked_at = $1
                WHERE tenant_id = $2 AND revoked_at IS NULL AND key_id != $3
                """,
                datetime.now(UTC),
                tenant_id,
                except_key_id,
            )
        else:
            status = await self._db.execute(
                """
                UPDATE api_keys SET revoked_at = $1
                WHERE tenant_id = $2 AND revoked_at IS NULL
                """,
                datetime.now(UTC),
                tenant_id,
            )
        # status: "UPDATE <n>" — the affected row count is the last token.
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):  # pragma: no cover — defensive
            return 0

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        row = await self._db.fetchrow(
            "SELECT * FROM tenant_budgets WHERE tenant_id = $1", tenant_id
        )
        if row is None:
            return None
        return TenantBudget(
            tenant_id=row["tenant_id"],
            monthly_usd_limit=row["monthly_usd_limit"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        # ``ON CONFLICT ... DO UPDATE`` preserves created_at on
        # updates — operators see "first set" vs "last touched"
        # separately.
        now = datetime.now(UTC)
        await self._db.execute(
            """
            INSERT INTO tenant_budgets (
                tenant_id, monthly_usd_limit, created_at, updated_at
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id) DO UPDATE SET
                monthly_usd_limit = EXCLUDED.monthly_usd_limit,
                updated_at = EXCLUDED.updated_at
            """,
            budget.tenant_id,
            budget.monthly_usd_limit,
            budget.created_at,
            now,
        )

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        rows = await self._db.fetch("SELECT * FROM tenant_budgets ORDER BY created_at")
        return [
            TenantBudget(
                tenant_id=r["tenant_id"],
                monthly_usd_limit=r["monthly_usd_limit"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Run feedback (Chainlit playground writes here)
    # ------------------------------------------------------------------

    async def save_feedback(self, feedback: FeedbackRecord) -> None:
        # ON CONFLICT: operators can edit their feedback (replace
        # score / comment / dimensions). The primary key is the
        # feedback_id so re-saving with the same id updates in place.
        # ``run_id`` + ``tenant_id`` + ``agent`` + ``user_id`` +
        # ``created_at`` are intentionally NOT updated on conflict —
        # those identify the feedback row's provenance and shouldn't
        # mutate post-create.
        await self._db.execute(
            """
            INSERT INTO run_feedback (
                feedback_id, run_id, tenant_id, agent, user_id,
                score, dimensions, comment, langfuse_score_id, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (feedback_id) DO UPDATE
                SET score = EXCLUDED.score,
                    dimensions = EXCLUDED.dimensions,
                    comment = EXCLUDED.comment,
                    langfuse_score_id = EXCLUDED.langfuse_score_id
            """,
            feedback.feedback_id,
            feedback.run_id,
            feedback.tenant_id,
            feedback.agent,
            feedback.user_id,
            feedback.score,
            feedback.dimensions,
            feedback.comment,
            feedback.langfuse_score_id,
            feedback.created_at,
        )

    async def list_feedback(
        self,
        *,
        run_id: str | None = None,
        agent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        # Build WHERE clauses dynamically — keeps the indexed paths
        # (run_id, agent+created_at, tenant_id+created_at) usable
        # depending on which filters are set.
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            params.append(run_id)
            clauses.append(f"run_id = ${len(params)}")
        if agent is not None:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if user_id is not None:
            params.append(user_id)
            clauses.append(f"user_id = ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        sql = (
            "SELECT feedback_id, run_id, tenant_id, agent, user_id, score, "
            "dimensions, comment, langfuse_score_id, created_at "
            "FROM run_feedback" + where + " ORDER BY created_at DESC LIMIT $" + str(len(params))
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_feedback(r) for r in rows]

    # ------------------------------------------------------------------
    # KB chunks — vector retrieval. Cosine in Python (no pgvector yet).
    # ------------------------------------------------------------------

    async def save_kb_chunk(self, chunk: KbChunk) -> None:
        await self._db.execute(
            """
            INSERT INTO kb_chunks (
                chunk_id, tenant_id, agent, source, text, embedding,
                embedding_model, content_hash, metadata, created_at, ocr
            ) VALUES ($1,$2,$3,$4,$5,$6::vector,$7,$8,$9,$10,$11)
            ON CONFLICT (agent, tenant_id, content_hash) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    embedding_model = EXCLUDED.embedding_model,
                    metadata = EXCLUDED.metadata,
                    source = EXCLUDED.source,
                    ocr = EXCLUDED.ocr
            """,
            chunk.chunk_id,
            chunk.tenant_id,
            chunk.agent,
            chunk.source,
            chunk.text,
            _vec_literal(chunk.embedding),
            chunk.embedding_model,
            chunk.content_hash,
            chunk.metadata,
            chunk.created_at,
            chunk.ocr,
        )

    async def search_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        # pgvector ANN search: cosine distance `<=>` against the HNSW index,
        # top-K computed in SQL (ADR 009 D2/D3). `<=>` is cosine *distance*;
        # we return `1 - distance` to keep the existing 0-1 similarity score
        # contract (RRF fusion + CLI thresholds depend on it).
        dim = _embedding_dim()
        if len(query_embedding) != dim:
            raise ValueError(
                f"query embedding has dimension {len(query_embedding)}, but the "
                f"store is configured for vector({dim}) (MOVATE_EMBED_DIM)"
            )
        rows = await self._db.fetch(
            """
            SELECT chunk_id, tenant_id, agent, source, text, embedding,
                   embedding_model, content_hash, metadata, created_at, ocr,
                   1 - (embedding <=> $3::vector) AS score
            FROM kb_chunks
            WHERE agent = $1 AND tenant_id = $2
            ORDER BY embedding <=> $3::vector
            LIMIT $4
            """,
            agent,
            tenant_id,
            _vec_literal(query_embedding),
            int(limit),
        )
        return [KbChunkWithScore(chunk=_row_to_kb_chunk(r), score=float(r["score"])) for r in rows]

    async def list_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
        limit: int = 1000,
    ) -> list[KbChunk]:
        clauses = ["agent = $1", "tenant_id = $2"]
        params: list[Any] = [agent, tenant_id]
        if source is not None:
            params.append(source)
            clauses.append(f"source = ${len(params)}")
        params.append(int(limit))
        sql = (
            "SELECT chunk_id, tenant_id, agent, source, text, embedding, "
            "embedding_model, content_hash, metadata, created_at, ocr "
            "FROM kb_chunks WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT $"
            + str(len(params))
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_kb_chunk(r) for r in rows]

    async def delete_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        if source is not None:
            result = await self._db.execute(
                "DELETE FROM kb_chunks WHERE agent = $1 AND tenant_id = $2 AND source = $3",
                agent,
                tenant_id,
                source,
            )
        else:
            result = await self._db.execute(
                "DELETE FROM kb_chunks WHERE agent = $1 AND tenant_id = $2",
                agent,
                tenant_id,
            )
        # asyncpg's execute returns "DELETE <count>" — parse the count.
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def reindex_kb(self, *, agent: str, tenant_id: str) -> int:
        # The HNSW index is GLOBAL to the kb_chunks table, not per-agent
        # (it indexes the embedding column for the whole table), so a
        # rebuild serves every agent at once — the (agent, tenant_id)
        # scope only narrows the returned count, not what gets rebuilt.
        # Drop + re-create with the exact DDL the migration uses (ADR
        # 009 D3) so a degraded index or a changed index parameter is
        # picked up. No embedding happens here — re-embedding is the
        # caller's job (kb/cli), preserving the storage-layer boundary
        # that storage never imports the embedder.
        async with self._db.acquire() as conn:
            await conn.execute("DROP INDEX IF EXISTS idx_kb_chunks_embedding_hnsw")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw "
                "ON kb_chunks USING hnsw (embedding vector_cosine_ops)"
            )
            count = await conn.fetchval(
                "SELECT count(*) FROM kb_chunks WHERE agent = $1 AND tenant_id = $2",
                agent,
                tenant_id,
            )
        return int(count or 0)

    # ------------------------------------------------------------------
    # Knowledge graph (GraphRAG)
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> None:
        # Merge provenance with any existing row (UNION source_chunk_ids).
        existing = await self._db.fetchval(
            "SELECT source_chunk_ids FROM kb_entities "
            "WHERE agent = $1 AND tenant_id = $2 AND content_hash = $3",
            entity.agent,
            entity.tenant_id,
            entity.content_hash,
        )
        merged = sorted(set(existing or []) | set(entity.source_chunk_ids))
        await self._db.execute(
            """
            INSERT INTO kb_entities (
                entity_id, tenant_id, agent, project_id, name, type, description,
                embedding, embedding_model, content_hash, source_chunk_ids,
                metadata, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (agent, tenant_id, content_hash) DO UPDATE
                SET project_id = COALESCE(EXCLUDED.project_id, kb_entities.project_id),
                    name = EXCLUDED.name,
                    type = EXCLUDED.type,
                    description = EXCLUDED.description,
                    embedding = EXCLUDED.embedding,
                    embedding_model = EXCLUDED.embedding_model,
                    source_chunk_ids = EXCLUDED.source_chunk_ids,
                    metadata = EXCLUDED.metadata
            """,
            entity.entity_id,
            entity.tenant_id,
            entity.agent,
            entity.project_id,
            entity.name,
            entity.type,
            entity.description,
            entity.embedding,
            entity.embedding_model,
            entity.content_hash,
            merged,
            entity.metadata,
            entity.created_at,
        )

    async def upsert_relation(self, relation: Relation) -> None:
        existing = await self._db.fetchval(
            "SELECT source_chunk_ids FROM kb_relations "
            "WHERE agent = $1 AND tenant_id = $2 AND content_hash = $3",
            relation.agent,
            relation.tenant_id,
            relation.content_hash,
        )
        merged = sorted(set(existing or []) | set(relation.source_chunk_ids))
        await self._db.execute(
            """
            INSERT INTO kb_relations (
                relation_id, tenant_id, agent, project_id, src_entity_id,
                dst_entity_id, type, description, weight, content_hash,
                source_chunk_ids, metadata, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (agent, tenant_id, content_hash) DO UPDATE
                SET project_id = COALESCE(EXCLUDED.project_id, kb_relations.project_id),
                    src_entity_id = EXCLUDED.src_entity_id,
                    dst_entity_id = EXCLUDED.dst_entity_id,
                    type = EXCLUDED.type,
                    description = EXCLUDED.description,
                    weight = EXCLUDED.weight,
                    source_chunk_ids = EXCLUDED.source_chunk_ids,
                    metadata = EXCLUDED.metadata
            """,
            relation.relation_id,
            relation.tenant_id,
            relation.agent,
            relation.project_id,
            relation.src_entity_id,
            relation.dst_entity_id,
            relation.type,
            relation.description,
            relation.weight,
            relation.content_hash,
            merged,
            relation.metadata,
            relation.created_at,
        )

    async def search_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 10,
        project_id: str | None = None,
    ) -> list[EntityWithScore]:
        # No pgvector yet — load matching entities and rank in Python,
        # same primitive (and future swap point) as search_kb_chunks.
        # ``project_id`` (None = no filter) appended as $3 only when set.
        proj_clause = "" if project_id is None else " AND project_id = $3"
        proj_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        rows = await self._db.fetch(
            f"""
            SELECT entity_id, tenant_id, agent, project_id, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities WHERE agent = $1 AND tenant_id = $2{proj_clause}
            """,
            agent,
            tenant_id,
            *proj_args,
        )
        from movate.storage._cosine import rank_entities_by_cosine  # noqa: PLC0415

        return rank_entities_by_cosine([_row_to_entity(r) for r in rows], query_embedding, limit)

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
        # Optional project filter — None means "no filter" (per-agent view,
        # the historical behavior). When set, only project-tagged rows
        # participate, keeping the traversal inside the project subgraph.
        # Recursive CTE: bounded k-hop reachability from the seeds.
        # Undirected for reachability; UNION dedups so cycles terminate,
        # depth < hops bounds the walk.
        cte_proj = "" if project_id is None else " AND r.project_id = $5"
        cte_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        reach_rows = await self._db.fetch(
            f"""
            WITH RECURSIVE reachable(eid, depth) AS (
                SELECT e, 0 FROM unnest($1::text[]) AS e
              UNION
                SELECT CASE WHEN r.src_entity_id = reachable.eid
                            THEN r.dst_entity_id ELSE r.src_entity_id END,
                       reachable.depth + 1
                FROM kb_relations r
                JOIN reachable
                  ON (r.src_entity_id = reachable.eid OR r.dst_entity_id = reachable.eid)
                WHERE reachable.depth < $2 AND r.agent = $3 AND r.tenant_id = $4{cte_proj}
            )
            SELECT DISTINCT eid FROM reachable
            """,
            entity_ids,
            int(hops),
            agent,
            tenant_id,
            *cte_args,
        )
        reachable = [r["eid"] for r in reach_rows]
        if not reachable:
            return Subgraph(entities=[], relations=[])
        rel_proj = "" if project_id is None else " AND project_id = $5"
        rel_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        rel_rows = await self._db.fetch(
            f"""
            SELECT relation_id, tenant_id, agent, project_id, src_entity_id,
                   dst_entity_id, type, description, weight, content_hash,
                   source_chunk_ids, metadata, created_at
            FROM kb_relations
            WHERE agent = $1 AND tenant_id = $2
              AND src_entity_id = ANY($3::text[]) AND dst_entity_id = ANY($3::text[]){rel_proj}
            ORDER BY weight DESC, relation_id LIMIT $4
            """,
            agent,
            tenant_id,
            reachable,
            int(limit),
            *rel_args,
        )
        relations = [_row_to_relation(r) for r in rel_rows]
        keep = set(entity_ids)
        for rel in relations:
            keep.add(rel.src_entity_id)
            keep.add(rel.dst_entity_id)
        ent_proj = "" if project_id is None else " AND project_id = $4"
        ent_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        ent_rows = await self._db.fetch(
            f"""
            SELECT entity_id, tenant_id, agent, project_id, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities
            WHERE agent = $1 AND tenant_id = $2 AND entity_id = ANY($3::text[]){ent_proj}
            """,
            agent,
            tenant_id,
            list(keep),
            *ent_args,
        )
        return Subgraph(
            entities=[_row_to_entity(r) for r in ent_rows],
            relations=relations,
        )

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        row = await self._db.fetchrow(
            """
            SELECT entity_id, tenant_id, agent, project_id, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities WHERE entity_id = $1 AND tenant_id = $2
            """,
            entity_id,
            tenant_id,
        )
        return _row_to_entity(row) if row is not None else None

    async def list_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        source_chunk_id: str | None = None,
        limit: int = 1000,
        project_id: str | None = None,
    ) -> list[Entity]:
        proj_clause = "" if project_id is None else " AND project_id = $4"
        proj_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        rows = await self._db.fetch(
            f"""
            SELECT entity_id, tenant_id, agent, project_id, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities WHERE agent = $1 AND tenant_id = $2{proj_clause}
            ORDER BY created_at DESC LIMIT $3
            """,
            agent,
            tenant_id,
            int(limit),
            *proj_args,
        )
        entities = [_row_to_entity(r) for r in rows]
        if source_chunk_id is not None:
            entities = [e for e in entities if source_chunk_id in e.source_chunk_ids]
        return entities

    async def list_relations(
        self,
        *,
        agent: str,
        tenant_id: str,
        limit: int = 1000,
        project_id: str | None = None,
    ) -> list[Relation]:
        proj_clause = "" if project_id is None else " AND project_id = $4"
        proj_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        rows = await self._db.fetch(
            f"""
            SELECT relation_id, tenant_id, agent, project_id, src_entity_id,
                   dst_entity_id, type, description, weight, content_hash,
                   source_chunk_ids, metadata, created_at
            FROM kb_relations WHERE agent = $1 AND tenant_id = $2{proj_clause}
            ORDER BY created_at DESC LIMIT $3
            """,
            agent,
            tenant_id,
            int(limit),
            *proj_args,
        )
        return [_row_to_relation(r) for r in rows]

    async def delete_graph(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        def _count(result: object) -> int:
            try:
                return int(str(result).split()[-1])
            except (ValueError, IndexError):
                return 0

        if source is None:
            e_res = await self._db.execute(
                "DELETE FROM kb_entities WHERE agent = $1 AND tenant_id = $2",
                agent,
                tenant_id,
            )
            r_res = await self._db.execute(
                "DELETE FROM kb_relations WHERE agent = $1 AND tenant_id = $2",
                agent,
                tenant_id,
            )
            return _count(e_res) + _count(r_res)
        # Per-source delete: rows whose provenance is SOLELY this source
        # (source_chunk_ids ⊆ that source's chunks). Multi-source rows
        # survive. Resolve + filter in Python (bounded scale).
        chunk_rows = await self._db.fetch(
            "SELECT chunk_id FROM kb_chunks WHERE agent = $1 AND tenant_id = $2 AND source = $3",
            agent,
            tenant_id,
            source,
        )
        chunk_ids = {r["chunk_id"] for r in chunk_rows}
        entities = await self.list_entities(agent=agent, tenant_id=tenant_id, limit=10**9)
        relations = await self.list_relations(agent=agent, tenant_id=tenant_id, limit=10**9)
        doomed_entities = [
            e.entity_id
            for e in entities
            if e.source_chunk_ids and set(e.source_chunk_ids) <= chunk_ids
        ]
        doomed_relations = [
            r.relation_id
            for r in relations
            if r.source_chunk_ids and set(r.source_chunk_ids) <= chunk_ids
        ]
        deleted = 0
        if doomed_entities:
            res = await self._db.execute(
                "DELETE FROM kb_entities WHERE entity_id = ANY($1::text[])",
                doomed_entities,
            )
            deleted += _count(res)
        if doomed_relations:
            res = await self._db.execute(
                "DELETE FROM kb_relations WHERE relation_id = ANY($1::text[])",
                doomed_relations,
            )
            deleted += _count(res)
        return deleted

    async def search_kb_chunks_lexical(
        self,
        *,
        agent: str,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """Postgres tsvector + GIN index BM25-style lexical search.

        Uses ``plainto_tsquery`` (converts plain text to AND-connected
        lexemes) + ``ts_rank`` for scoring. Returns up to ``limit``
        chunks ranked most-relevant first. Empty query or no matches
        → empty list. NEVER raises.
        """
        if not query.strip():
            return []
        try:
            rows = await self._db.fetch(
                """
                SELECT chunk_id, tenant_id, agent, source, text, embedding,
                       embedding_model, content_hash, metadata, created_at, ocr,
                       ts_rank(
                           to_tsvector('english', text),
                           plainto_tsquery('english', $3)
                       ) AS rank
                FROM kb_chunks
                WHERE agent = $1
                  AND tenant_id = $2
                  AND to_tsvector('english', text) @@ plainto_tsquery('english', $3)
                ORDER BY rank DESC
                LIMIT $4
                """,
                agent,
                tenant_id,
                query,
                int(limit),
            )
        except Exception:
            return []
        results = []
        for r in rows:
            rank = float(r["rank"] or 0.0)
            # ts_rank returns [0, 1] normally. Clamp defensively.
            score = min(max(rank, 0.0), 1.0)
            results.append(KbChunkWithScore(chunk=_row_to_kb_chunk(r), score=score))
        return results

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        # ``date_trunc('month', now() at time zone 'utc')`` returns the
        # 1st-of-month UTC. The ``metrics->>'cost_usd'`` extraction is
        # JSONB-aware — postgres treats this as a typed real cast.
        # COALESCE keeps the no-runs-yet case as 0.0.
        result = await self._db.fetchval(
            """
            SELECT COALESCE(SUM(
                (metrics->>'cost_usd')::DOUBLE PRECISION
            ), 0.0)
            FROM runs
            WHERE tenant_id = $1
              AND created_at >= date_trunc('month', NOW() AT TIME ZONE 'utc')
            """,
            tenant_id,
        )
        return float(result or 0.0)

    # ------------------------------------------------------------------
    # Conversation threads (PR-N) — multi-turn agent foundation.
    # ------------------------------------------------------------------

    async def save_conversation_thread(self, thread: ConversationThread) -> None:
        # ON CONFLICT DO UPDATE so clients can call this on every
        # appended message to refresh ``updated_at`` (and optionally
        # ``title``) without re-inserting.
        await self._db.execute(
            """
            INSERT INTO conversation_threads
            (thread_id, tenant_id, agent, title, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (thread_id) DO UPDATE SET
                title = EXCLUDED.title,
                updated_at = EXCLUDED.updated_at
            """,
            thread.thread_id,
            thread.tenant_id,
            thread.agent,
            thread.title,
            thread.created_at,
            thread.updated_at,
        )

    async def get_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> ConversationThread | None:
        row = await self._db.fetchrow(
            "SELECT * FROM conversation_threads WHERE thread_id = $1 AND tenant_id = $2",
            thread_id,
            tenant_id,
        )
        if row is None:
            return None
        return _row_to_thread(row)

    async def list_conversation_threads(
        self,
        *,
        tenant_id: str,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[ConversationThread]:
        if agent is not None:
            rows = await self._db.fetch(
                """
                SELECT * FROM conversation_threads
                WHERE tenant_id = $1 AND agent = $2
                ORDER BY updated_at DESC
                LIMIT $3
                """,
                tenant_id,
                agent,
                int(limit),
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT * FROM conversation_threads
                WHERE tenant_id = $1
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                tenant_id,
                int(limit),
            )
        return [_row_to_thread(r) for r in rows]

    async def list_runs_for_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[RunRecord]:
        # Tenant-scoped via the WHERE clause — cross-tenant returns [].
        # ASC by created_at for chronological conversation history.
        rows = await self._db.fetch(
            """
            SELECT * FROM runs
            WHERE thread_id = $1 AND tenant_id = $2
            ORDER BY created_at ASC
            LIMIT $3
            """,
            thread_id,
            tenant_id,
            int(limit),
        )
        return [_row_to_run(r) for r in rows]

    async def delete_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        # asyncpg returns a status string like ``DELETE 1`` /
        # ``DELETE 0``; parse the trailing count for the bool.
        # Tenant-scoped via the WHERE clause — cross-tenant deletes
        # don't touch the row + return False.
        status = await self._db.execute(
            "DELETE FROM conversation_threads WHERE thread_id = $1 AND tenant_id = $2",
            thread_id,
            tenant_id,
        )
        return status.endswith(" 1") if isinstance(status, str) else False

    # ------------------------------------------------------------------
    # Stateful sessions (ADR 045 D10)
    # ------------------------------------------------------------------

    async def save_session(self, session: Session) -> None:
        # ON CONFLICT DO UPDATE so callers can refresh title / updated_at
        # and the rollups on each appended turn without re-inserting.
        # created_at is intentionally NOT in the SET list so it survives
        # updates.
        await self._db.execute(
            """
            INSERT INTO sessions
            (session_id, tenant_id, agent, title, created_at, updated_at,
             turn_count, total_cost_usd, total_tokens_in, total_tokens_out)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (session_id) DO UPDATE SET
                title = EXCLUDED.title,
                updated_at = EXCLUDED.updated_at,
                turn_count = EXCLUDED.turn_count,
                total_cost_usd = EXCLUDED.total_cost_usd,
                total_tokens_in = EXCLUDED.total_tokens_in,
                total_tokens_out = EXCLUDED.total_tokens_out
            """,
            session.session_id,
            session.tenant_id,
            session.agent,
            session.title,
            session.created_at,
            session.updated_at,
            session.turn_count,
            session.total_cost_usd,
            session.total_tokens_in,
            session.total_tokens_out,
        )

    async def get_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
    ) -> Session | None:
        row = await self._db.fetchrow(
            "SELECT * FROM sessions WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        )
        if row is None:
            return None
        return _row_to_session(row)

    async def list_sessions(
        self,
        *,
        tenant_id: str,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[Session]:
        if agent is not None:
            rows = await self._db.fetch(
                """
                SELECT * FROM sessions
                WHERE tenant_id = $1 AND agent = $2
                ORDER BY updated_at DESC
                LIMIT $3
                """,
                tenant_id,
                agent,
                int(limit),
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT * FROM sessions
                WHERE tenant_id = $1
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                tenant_id,
                int(limit),
            )
        return [_row_to_session(r) for r in rows]

    async def append_session_message(self, message: SessionMessage) -> None:
        # ``content`` rides the jsonb codec registered in
        # _init_connection — pass the dict directly.
        await self._db.execute(
            """
            INSERT INTO session_messages
            (message_id, session_id, tenant_id, role, content, run_id,
             cost_usd, tokens_in, tokens_out, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            message.message_id,
            message.session_id,
            message.tenant_id,
            message.role,
            message.content,
            message.run_id,
            message.cost_usd,
            message.tokens_in,
            message.tokens_out,
            message.created_at,
        )

    async def list_session_messages(
        self,
        session_id: str,
        *,
        tenant_id: str,
        limit: int = 1000,
    ) -> list[SessionMessage]:
        # Chronological — earliest first. Tenant-scoped via WHERE.
        rows = await self._db.fetch(
            """
            SELECT * FROM session_messages
            WHERE session_id = $1 AND tenant_id = $2
            ORDER BY created_at ASC
            LIMIT $3
            """,
            session_id,
            tenant_id,
            int(limit),
        )
        return [_row_to_session_message(r) for r in rows]

    async def delete_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        # Delete messages first, then the session. Both tenant-scoped.
        # The bool reflects whether the SESSION row existed.
        await self._db.execute(
            "DELETE FROM session_messages WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        )
        status = await self._db.execute(
            "DELETE FROM sessions WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        )
        return status.endswith(" 1") if isinstance(status, str) else False

    # ------------------------------------------------------------------
    # Webhook subscriptions (ADR 035 D2 — outbound delivery). ``secret``
    # is stored verbatim so the worker can re-sign every outbound POST
    # (echoed on the wire only on the create response). ``kind_filter``
    # round-trips through the JSONB codec registered in
    # ``_init_connection`` — handler passes/receives plain Python lists.
    # ------------------------------------------------------------------

    async def create_webhook(self, sub: WebhookSubscription) -> WebhookSubscription:
        await self._db.execute(
            "INSERT INTO webhooks "
            "(id, tenant_id, url, kind_filter, secret, enabled, failure_count, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            sub.id,
            sub.tenant_id,
            sub.url,
            sub.kind_filter,
            sub.secret,
            sub.enabled,
            sub.failure_count,
            sub.created_at,
        )
        return sub

    async def list_webhooks(
        self,
        tenant_id: str,
        *,
        enabled_only: bool = True,
    ) -> list[WebhookSubscription]:
        clauses: list[str] = ["tenant_id = $1"]
        params: list[Any] = [tenant_id]
        if enabled_only:
            clauses.append("enabled = TRUE")
        sql = (
            "SELECT id, tenant_id, url, kind_filter, secret, enabled, failure_count, created_at "
            "FROM webhooks WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC, id ASC"
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_webhook(r) for r in rows]

    async def get_webhook(self, tenant_id: str, webhook_id: str) -> WebhookSubscription | None:
        row = await self._db.fetchrow(
            "SELECT id, tenant_id, url, kind_filter, secret, enabled, failure_count, created_at "
            "FROM webhooks WHERE id = $1 AND tenant_id = $2",
            webhook_id,
            tenant_id,
        )
        return _row_to_webhook(row) if row is not None else None

    async def update_webhook(
        self,
        tenant_id: str,
        webhook_id: str,
        *,
        enabled: bool | None = None,
        failure_count: int | None = None,
    ) -> WebhookSubscription | None:
        if enabled is None and failure_count is None:
            return await self.get_webhook(tenant_id, webhook_id)
        sets: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            params.append(enabled)
            sets.append(f"enabled = ${len(params)}")
        if failure_count is not None:
            params.append(failure_count)
            sets.append(f"failure_count = ${len(params)}")
        params.append(webhook_id)
        id_idx = len(params)
        params.append(tenant_id)
        tenant_idx = len(params)
        await self._db.execute(
            f"UPDATE webhooks SET {', '.join(sets)} "
            f"WHERE id = ${id_idx} AND tenant_id = ${tenant_idx}",
            *params,
        )
        return await self.get_webhook(tenant_id, webhook_id)

    async def delete_webhook(self, tenant_id: str, webhook_id: str) -> bool:
        status = await self._db.execute(
            "DELETE FROM webhooks WHERE id = $1 AND tenant_id = $2",
            webhook_id,
            tenant_id,
        )
        return status.endswith(" 1") if isinstance(status, str) else False

    async def record_webhook_attempt(self, attempt: WebhookAttempt) -> None:
        await self._db.execute(
            "INSERT INTO webhook_attempts "
            "(id, webhook_id, event_id, tenant_id, attempted_at, status_code, "
            " response_excerpt, error_kind, attempt_n) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            attempt.id,
            attempt.webhook_id,
            attempt.event_id,
            attempt.tenant_id,
            attempt.attempted_at,
            attempt.status_code,
            attempt.response_excerpt,
            attempt.error_kind,
            attempt.attempt_n,
        )

    async def list_webhook_attempts(
        self,
        tenant_id: str,
        *,
        webhook_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[WebhookAttempt]:
        clauses: list[str] = ["tenant_id = $1"]
        params: list[Any] = [tenant_id]
        if webhook_id is not None:
            params.append(webhook_id)
            clauses.append(f"webhook_id = ${len(params)}")
        if since is not None:
            params.append(since)
            clauses.append(f"attempted_at >= ${len(params)}")
        params.append(limit)
        sql = (
            "SELECT id, webhook_id, event_id, tenant_id, attempted_at, status_code, "
            "       response_excerpt, error_kind, attempt_n "
            "FROM webhook_attempts WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY attempted_at DESC, id DESC LIMIT ${len(params)}"
        )
        rows = await self._db.fetch(sql, *params)
        return [_row_to_webhook_attempt(r) for r in rows]

    async def get_webhook_cursor(self, tenant_id: str, webhook_id: str) -> str | None:
        # asyncpg.fetchval returns ``Any``; explicit cast keeps mypy happy
        # without disabling no-any-return wholesale on this file.
        value = await self._db.fetchval(
            "SELECT last_event_id FROM webhook_cursors WHERE webhook_id = $1 AND tenant_id = $2",
            webhook_id,
            tenant_id,
        )
        return value if value is None else str(value)

    async def set_webhook_cursor(self, tenant_id: str, webhook_id: str, last_event_id: str) -> None:
        # Postgres ``INSERT ... ON CONFLICT ... DO UPDATE`` upsert. The
        # cursor is keyed on ``webhook_id``; ``tenant_id`` is
        # denormalized on the row so a tenant-scoped read stays cheap.
        await self._db.execute(
            "INSERT INTO webhook_cursors (webhook_id, tenant_id, last_event_id, updated_at) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (webhook_id) DO UPDATE SET "
            "  last_event_id = EXCLUDED.last_event_id, "
            "  updated_at = EXCLUDED.updated_at",
            webhook_id,
            tenant_id,
            last_event_id,
            datetime.now(UTC),
        )

    # Eval-generation jobs (``mdk eval generate``)
    # ------------------------------------------------------------------

    async def save_eval_generation_job(self, job: Any) -> None:
        from movate.core.eval_generator import EvalGenerationJob  # noqa: PLC0415

        assert isinstance(job, EvalGenerationJob)
        # The JSONB codec on the pool round-trips dict ↔ JSONB, but
        # `categories` is a list — asyncpg's JSONB encoder handles lists
        # too via json.dumps. ``created_at`` / ``completed_at`` arrive
        # as ISO strings from the EvalGenerationJob dataclass (it's
        # frozen + json-safe); we cast to datetime so TIMESTAMPTZ accepts
        # them. NULL preserved when absent.
        created_at = _parse_iso(job.created_at) if job.created_at else datetime.now(UTC)
        completed_at = _parse_iso(job.completed_at) if job.completed_at else None
        await self._db.execute(
            """
            INSERT INTO eval_generation_jobs (
                job_id, tenant_id, agent_name, status, description,
                count_requested, categories, include_judge, model,
                budget_usd, progress, result, error, tokens_used,
                cost_usd, created_at, completed_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17
            )
            ON CONFLICT (job_id) DO UPDATE SET
                status = EXCLUDED.status,
                progress = EXCLUDED.progress,
                result = EXCLUDED.result,
                error = EXCLUDED.error,
                tokens_used = EXCLUDED.tokens_used,
                cost_usd = EXCLUDED.cost_usd,
                completed_at = EXCLUDED.completed_at
            """,
            job.job_id,
            job.tenant_id,
            job.agent_name,
            job.status,
            job.description,
            job.count,
            list(job.categories),
            job.include_judge,
            job.model,
            job.budget_usd,
            job.progress,
            job.result,
            job.error,
            job.tokens_used,
            job.cost_usd,
            created_at,
            completed_at,
        )

    async def get_eval_generation_job(self, job_id: str, *, tenant_id: str) -> Any | None:
        from movate.core.eval_generator import EvalGenerationJob  # noqa: PLC0415

        row = await self._db.fetchrow(
            "SELECT * FROM eval_generation_jobs WHERE job_id = $1 AND tenant_id = $2",
            job_id,
            tenant_id,
        )
        if row is None:
            return None
        d = dict(row)
        return EvalGenerationJob(
            job_id=d["job_id"],
            tenant_id=d["tenant_id"],
            agent_name=d["agent_name"],
            status=d["status"],
            description=d["description"],
            count=d["count_requested"],
            categories=list(d["categories"] or []),
            include_judge=d["include_judge"],
            model=d["model"],
            budget_usd=d["budget_usd"],
            progress=d["progress"],
            result=d.get("result"),
            error=d.get("error"),
            tokens_used=d["tokens_used"],
            cost_usd=d["cost_usd"],
            created_at=d["created_at"].isoformat() if d.get("created_at") else "",
            completed_at=d["completed_at"].isoformat() if d.get("completed_at") else None,
        )

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


def _parse_iso(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp back into a tz-aware ``datetime``.

    Used by the eval-generation save path: the
    :class:`EvalGenerationJob` dataclass carries created_at / completed_at
    as ISO strings (it's serializable through the JSONB result blob), but
    the postgres column is ``TIMESTAMPTZ`` which asyncpg wants as a
    ``datetime``. Accepts both ``...Z`` and ``+00:00`` suffixes.
    """
    s = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Row → model converters
# ---------------------------------------------------------------------------


def _row_to_webhook(row: asyncpg.Record) -> WebhookSubscription:
    return WebhookSubscription(
        id=row["id"],
        tenant_id=row["tenant_id"],
        url=row["url"],
        # JSONB codec decoded this to a Python list already.
        kind_filter=list(row["kind_filter"] or []),
        secret=row["secret"],
        enabled=bool(row["enabled"]),
        failure_count=row["failure_count"] or 0,
        created_at=row["created_at"],
    )


def _row_to_webhook_attempt(row: asyncpg.Record) -> WebhookAttempt:
    return WebhookAttempt(
        id=row["id"],
        webhook_id=row["webhook_id"],
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        attempted_at=row["attempted_at"],
        status_code=row["status_code"],
        response_excerpt=row["response_excerpt"],
        error_kind=row["error_kind"],
        attempt_n=row["attempt_n"],
    )


def _row_to_run(row: asyncpg.Record) -> RunRecord:
    metrics_data = dict(row["metrics"])
    # ADR 024 D2 — per-step JSONB arrays. The jsonb codec already decoded them
    # to Python lists; NULL on pre-migration rows → empty list, so legacy
    # records load + render as a single node (no crash). ``row.get`` guards
    # against a pre-migration column being absent entirely.
    raw_skill_calls = row.get("skill_calls")
    raw_turns = row.get("turns")
    skill_calls = [SkillCallRecord.model_validate(c) for c in (raw_skill_calls or [])]
    turns = [TurnRecord.model_validate(t) for t in (raw_turns or [])]
    return RunRecord(
        run_id=row["run_id"],
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        prompt_hash=row["prompt_hash"],
        provider=row["provider"],
        provider_version=row["provider_version"],
        pricing_version=row["pricing_version"],
        status=JobStatus(row["status"]),
        input=dict(row["input"]),
        output=dict(row["output"]) if row["output"] else None,
        metrics=Metrics.model_validate(metrics_data),
        error=ErrorInfo.model_validate(dict(row["error"])) if row["error"] else None,
        created_at=row["created_at"],
        workflow_run_id=row["workflow_run_id"],
        node_id=row["node_id"],
        thread_id=row["thread_id"],
        skill_calls=skill_calls,
        turns=turns,
    )


def _row_to_thread(row: asyncpg.Record) -> ConversationThread:
    return ConversationThread(
        thread_id=row["thread_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        title=row["title"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_session(row: asyncpg.Record) -> Session:
    return Session(
        session_id=row["session_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        title=row["title"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        turn_count=row["turn_count"],
        total_cost_usd=row["total_cost_usd"],
        total_tokens_in=row["total_tokens_in"],
        total_tokens_out=row["total_tokens_out"],
    )


def _row_to_session_message(row: asyncpg.Record) -> SessionMessage:
    # ``content`` round-trips through the jsonb codec → a Python dict.
    content = row["content"]
    if isinstance(content, str):
        content = json.loads(content)
    return SessionMessage(
        message_id=row["message_id"],
        session_id=row["session_id"],
        tenant_id=row["tenant_id"],
        role=row["role"],
        content=content,
        run_id=row["run_id"],
        cost_usd=row["cost_usd"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        created_at=row["created_at"],
    )


def _row_to_diagnosis(row: asyncpg.Record) -> DiagnosisRecord:
    request_blob = row["request"]
    result_blob = row["result"]
    error_blob = row["error"]
    error_obj: ErrorInfo | None = None
    if error_blob is not None:
        error_obj = ErrorInfo.model_validate(dict(error_blob))
    return DiagnosisRecord(
        diagnosis_id=row["diagnosis_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        status=DiagnosisStatus(row["status"]),
        request=dict(request_blob) if request_blob else {},
        result=dict(result_blob) if result_blob is not None else None,
        error=error_obj,
        tokens_used=row["tokens_used"] or 0,
        cost_usd=float(row["cost_usd"] or 0.0),
        model=row["model"] or "",
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _row_to_bench(row: asyncpg.Record) -> BenchRecord:
    return BenchRecord(
        bench_id=row["bench_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        input=dict(row["input"]),
        judge_method=JudgeMethod(row["judge_method"]) if row["judge_method"] else None,
        judge_provider=row["judge_provider"],
        runs_per_model=row["runs_per_model"],
        gate_mode=row["gate_mode"],
        models=[BenchModelResult.model_validate(m) for m in row["models"]],
        created_at=row["created_at"],
    )


def _row_to_audit(row: asyncpg.Record) -> AuditRecord:
    return AuditRecord(
        audit_id=row["audit_id"],
        tenant_id=row["tenant_id"],
        scope_kind=row["scope_kind"],
        scope_id=row["scope_id"],
        categories=list(row["categories"]),
        severity_floor=AuditFindingSeverity(row["severity_floor"]),
        model=row["model"],
        budget_usd=float(row["budget_usd"]),
        findings=[AuditFinding.model_validate(f) for f in row["findings"]],
        partial=bool(row["partial"]),
        tokens_used=int(row["tokens_used"]),
        cost_usd=float(row["cost_usd"]),
        created_at=row["created_at"],
    )


def _row_to_eval_schedule(row: asyncpg.Record) -> EvalSchedule:
    return EvalSchedule(
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        cadence_seconds=row["cadence_seconds"],
        enabled=row["enabled"],
        mock=row["mock"],
        runs=row["runs"],
        gate_mode=row["gate_mode"],
        gate=row["gate"],
        objective=row["objective"],
        regression_tolerance=row["regression_tolerance"],
        baseline_id=row["baseline_id"],
        notify_email=row["notify_email"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        last_enqueued_at=row["last_enqueued_at"],
    )


def _row_to_job_schedule(row: asyncpg.Record) -> JobSchedule:
    return JobSchedule(
        tenant_id=row["tenant_id"],
        name=row["name"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        cadence_seconds=row["cadence_seconds"],
        enabled=row["enabled"],
        input=dict(row["input"]),
        notify_email=row["notify_email"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        last_enqueued_at=row["last_enqueued_at"],
    )


def _row_to_canary_config(row: asyncpg.Record) -> CanaryConfig:
    return CanaryConfig(
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        challenger_version=row["challenger_version"],
        champion_version=row["champion_version"],
        weight=row["weight"],
        sticky=row["sticky"],
        enabled=row["enabled"],
        auto_promote=row["auto_promote"],
        eval_gate=row["eval_gate"],
        auto_rollback=row["auto_rollback"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_trigger(row: asyncpg.Record) -> Trigger:
    return Trigger(
        tenant_id=row["tenant_id"],
        name=row["name"],
        trigger_id=row["trigger_id"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        secret_hash=row["secret_hash"],
        salt=row["salt"],
        input_defaults=dict(row["input_defaults"]),
        enabled=row["enabled"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        last_fired_at=row["last_fired_at"],
    )


def _row_to_tenant_provider_key(row: asyncpg.Record) -> TenantProviderKey:
    return TenantProviderKey(
        tenant_id=row["tenant_id"],
        provider=row["provider"],
        ciphertext=row["ciphertext"],
        fingerprint=row["fingerprint"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_agent_bundle(row: asyncpg.Record) -> AgentBundleRecord:
    return AgentBundleRecord(
        name=row["name"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        created_by=row["created_by"],
        content_hash=row["content_hash"],
        files=dict(row["files"]),
        created_at=row["created_at"],
    )


def _row_to_project(row: asyncpg.Record) -> Project:
    return Project(
        project_id=row["project_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        description=row["description"],
        owner_principal_id=row["owner_principal_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _row_to_project_member(row: asyncpg.Record) -> ProjectMember:
    return ProjectMember(
        project_id=row["project_id"],
        principal_id=row["principal_id"],
        role=ProjectMemberRole(row["role"]),
        added_by=row["added_by"],
        added_at=row["added_at"],
    )


# ---------------------------------------------------------------------------
# Agent catalog (ADR 041) — row converters + namespace invariant
# ---------------------------------------------------------------------------


_RATING_MIN = 1
_RATING_MAX = 5


def _tenant_key(tenant_id: str | None) -> str:
    """Public-namespace sentinel so the composite PK uniqueness is enforced
    even when ``tenant_id`` itself is NULL (Postgres ignores NULL in unique
    constraints by default)."""

    return tenant_id if tenant_id is not None else "__public__"


def _enforce_catalog_namespace(source: CatalogSource, tenant_id: str | None) -> None:
    """Same invariant the DB CHECK enforces, raised early for a clear error."""

    if source is CatalogSource.PRIVATE:
        if not tenant_id:
            raise ValueError("catalog 'private' entries require tenant_id")
    elif tenant_id is not None:
        raise ValueError(f"catalog '{source.value}' entries must have tenant_id=None")


def _row_to_catalog_entry(row: asyncpg.Record) -> CatalogEntry:
    raw_summary = row["ratings_summary"]
    if isinstance(raw_summary, str):
        raw_summary = json.loads(raw_summary)
    summary = CatalogRatingsSummary.model_validate(raw_summary or {})
    raw_tags = row["tags"]
    if isinstance(raw_tags, str):
        raw_tags = json.loads(raw_tags)
    return CatalogEntry(
        slug=row["slug"],
        source=CatalogSource(row["source"]),
        tenant_id=row["tenant_id"],
        latest_version=row["latest_version"],
        name=row["name"],
        title=row["title"],
        description=row["description"],
        tags=list(raw_tags or []),
        shape=row["shape"],
        recommended_for=row["recommended_for"],
        ratings_summary=summary,
        popularity=int(row["popularity"]),
        synced_at=row["synced_at"],
    )


def _row_to_catalog_entry_version(row: asyncpg.Record) -> CatalogEntryVersion:
    return CatalogEntryVersion(
        slug=row["slug"],
        version=row["version"],
        source=CatalogSource(row["source"]),
        tenant_id=row["tenant_id"],
        bundle_tar=bytes(row["bundle_tar"]),
        digest=row["digest"],
        published_at=row["published_at"],
        deprecated_at=row["deprecated_at"],
    )


def _row_to_insight(row: asyncpg.Record) -> ObservabilityInsight:
    # The JSONB ↔ dict/list codec (registered in _init_connection) decodes the
    # four JSON columns; ``date`` / ``created_at`` come back as date/datetime.
    return ObservabilityInsight(
        id=row["id"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        date=row["date"],
        health_score=row["health_score"],
        anomalies=list(row["anomalies"] or []),
        top_failures=list(row["top_failures"] or []),
        usage_rollup=dict(row["usage_rollup"] or {}),
        trends=dict(row["trends"] or {}),
        narrative_digest=row["narrative_digest"] or "",
        created_at=row["created_at"],
    )


def _row_to_workflow_bundle(row: asyncpg.Record) -> WorkflowBundleRecord:
    # ADR 037 D1 — JSONB ``files`` decoded by the per-connection codec, same
    # as agent_bundles. ``published`` is a real BOOLEAN column in postgres.
    return WorkflowBundleRecord(
        name=row["name"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        created_by=row["created_by"],
        content_hash=row["content_hash"],
        files=dict(row["files"]),
        published=bool(row["published"]),
        created_at=row["created_at"],
    )


def _row_to_eval(row: asyncpg.Record) -> EvalRecord:
    # item 24 per-dimension means. The JSONB ↔ dict codec (registered in
    # _init_connection) decodes the column to a dict; NULL on pre-item-24
    # rows → None, so old records load unchanged and drift falls back to the
    # aggregate-only path.
    dim_means = row["dimension_means"]
    return EvalRecord(
        eval_id=row["eval_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        dataset_hash=row["dataset_hash"],
        judge_method=JudgeMethod(row["judge_method"]),
        judge_provider=row["judge_provider"],
        runs_per_case=row["runs_per_case"],
        gate_mode=row["gate_mode"],
        threshold=row["threshold"],
        mean_score=row["mean_score"],
        pass_rate=row["pass_rate"],
        sample_count=row["sample_count"],
        total_cost_usd=row["total_cost_usd"],
        created_at=row["created_at"],
        dimension_means=dict(dim_means) if dim_means is not None else None,
    )


def _row_to_workflow_run(row: asyncpg.Record) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=row["workflow_run_id"],
        tenant_id=row["tenant_id"],
        workflow=row["workflow"],
        workflow_version=row["workflow_version"],
        status=WorkflowStatus(row["status"]),
        initial_state=dict(row["initial_state"]),
        final_state=dict(row["final_state"]) if row["final_state"] else None,
        error_node_id=row["error_node_id"],
        error=ErrorInfo.model_validate(dict(row["error"])) if row["error"] else None,
        created_at=row["created_at"],
        # ADR 017 D5 (PR 1): HITL checkpoint. NULL on pre-migration / non-paused
        # rows → None, so old records load unchanged.
        paused_node_id=row["paused_node_id"],
        paused_state=dict(row["paused_state"]) if row["paused_state"] else None,
        human_task=dict(row["human_task"]) if row["human_task"] else None,
    )


def _row_to_job(row: asyncpg.Record) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        status=JobStatus(row["status"]),
        input=dict(row["input"]),
        result_run_id=row["result_run_id"],
        error=ErrorInfo.model_validate(dict(row["error"])) if row["error"] else None,
        api_key_id=row["api_key_id"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        notify_email=row["notify_email"],
        attempt_count=row["attempt_count"],
        next_retry_at=row["next_retry_at"],
        thread_id=row["thread_id"],
        target_version=row["target_version"],
        resume_workflow_run_id=row["resume_workflow_run_id"],
        batch_id=row["batch_id"],
        # item 32 (ADR 019): W3C trace-context carrier. NULL (pre-R2 row, or a
        # job enqueued with OTel off) → {} so the worker starts a fresh root
        # span — byte-for-byte the pre-R2 behaviour.
        trace_context=dict(row["trace_context"]) if row["trace_context"] else {},
        # item 36 (R4b): cooperative-cancel flag. NOT NULL DEFAULT FALSE in the
        # schema, so a pre-cancel / never-cancelled row reads back as False.
        cancel_requested=row["cancel_requested"],
    )


def _row_to_batch(row: asyncpg.Record) -> BatchRecord:
    return BatchRecord(
        batch_id=row["batch_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        total=row["total"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _parse_embedding(value: Any) -> list[float]:
    """Coerce a stored embedding to ``list[float]``.

    The ``vector`` column (no client-side codec) comes back as the text
    ``[1,2,3]`` — JSON-parseable. Legacy/JSONB rows (pre-migration, or other
    paths) arrive as a Python list. Handle both.
    """
    if isinstance(value, str):
        value = json.loads(value)
    return [float(x) for x in value]


def _row_to_kb_chunk(row: asyncpg.Record) -> KbChunk:
    row_dict = dict(row)
    return KbChunk(
        chunk_id=row_dict["chunk_id"],
        tenant_id=row_dict["tenant_id"],
        agent=row_dict["agent"],
        source=row_dict["source"],
        text=row_dict["text"],
        embedding=_parse_embedding(row_dict["embedding"]),
        embedding_model=row_dict["embedding_model"],
        content_hash=row_dict["content_hash"],
        metadata=row_dict.get("metadata"),
        ocr=bool(row_dict.get("ocr", False)),
        created_at=row_dict["created_at"],
    )


def _row_to_entity(row: asyncpg.Record) -> Entity:
    row_dict = dict(row)
    return Entity(
        entity_id=row_dict["entity_id"],
        tenant_id=row_dict["tenant_id"],
        agent=row_dict["agent"],
        project_id=row_dict.get("project_id"),
        name=row_dict["name"],
        type=row_dict["type"],
        description=row_dict.get("description"),
        embedding=list(row_dict["embedding"]),
        embedding_model=row_dict["embedding_model"],
        content_hash=row_dict["content_hash"],
        source_chunk_ids=list(row_dict.get("source_chunk_ids") or []),
        metadata=row_dict.get("metadata"),
        created_at=row_dict["created_at"],
    )


def _row_to_relation(row: asyncpg.Record) -> Relation:
    row_dict = dict(row)
    return Relation(
        relation_id=row_dict["relation_id"],
        tenant_id=row_dict["tenant_id"],
        agent=row_dict["agent"],
        project_id=row_dict.get("project_id"),
        src_entity_id=row_dict["src_entity_id"],
        dst_entity_id=row_dict["dst_entity_id"],
        type=row_dict["type"],
        description=row_dict.get("description"),
        weight=row_dict["weight"],
        content_hash=row_dict["content_hash"],
        source_chunk_ids=list(row_dict.get("source_chunk_ids") or []),
        metadata=row_dict.get("metadata"),
        created_at=row_dict["created_at"],
    )


def _row_to_feedback(row: asyncpg.Record) -> FeedbackRecord:
    row_dict = dict(row)
    return FeedbackRecord(
        feedback_id=row_dict["feedback_id"],
        run_id=row_dict["run_id"],
        tenant_id=row_dict["tenant_id"],
        agent=row_dict["agent"],
        user_id=row_dict["user_id"],
        score=row_dict["score"],
        dimensions=row_dict.get("dimensions"),
        comment=row_dict.get("comment"),
        langfuse_score_id=row_dict.get("langfuse_score_id"),
        created_at=row_dict["created_at"],
    )


def _row_to_api_key(row: asyncpg.Record) -> ApiKeyRecord:
    row_dict = dict(row)
    # JSONB ``scopes`` round-trips through the pool's json codec as a list
    # (or None on a legacy row). NULL → [] → legacy default at check time.
    raw_scopes = row_dict.get("scopes")
    scopes = [str(s) for s in raw_scopes] if isinstance(raw_scopes, list) else []
    return ApiKeyRecord(
        key_id=row_dict["key_id"],
        tenant_id=row_dict["tenant_id"],
        env=ApiKeyEnv(row_dict["env"]),
        secret_hash=row_dict["secret_hash"],
        salt=row_dict["salt"],
        label=row_dict["label"],
        created_at=row_dict["created_at"],
        last_used_at=row_dict["last_used_at"],
        revoked_at=row_dict["revoked_at"],
        expires_at=row_dict.get("expires_at"),
        scope=row_dict.get("scope"),
        scopes=scopes,
    )


__all__ = ["PostgresProvider"]
