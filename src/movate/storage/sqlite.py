"""SQLite-backed StorageProvider for local dev and CI.

Uses ``aiosqlite`` for async access. Schema is created on ``init()`` —
idempotent. v0.1 implements runs + failures; jobs/api_keys/evals tables
will be added in their respective phases.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from movate.core.dr_backup import ImportResult
from movate.core.events import Event
from movate.core.job_retry import ReclaimResult
from movate.core.models import (
    _DEFAULT_PROJECT_DESCRIPTION,
    _DEFAULT_PROJECT_NAME,
    _TENANT_SYSTEM_PRINCIPAL,
    AgentBundleRecord,
    AgentRuntimeState,
    AgentStatus,
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
    ContextRecord,
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
    SkillRecord,
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

if TYPE_CHECKING:
    from movate.storage.base import RunSubmissionRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    agent            TEXT NOT NULL,
    agent_version    TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    pricing_version  TEXT NOT NULL,
    status           TEXT NOT NULL,
    input            TEXT NOT NULL,
    output           TEXT,
    metrics          TEXT NOT NULL,
    error            TEXT,
    created_at       TEXT NOT NULL,
    workflow_run_id  TEXT,
    node_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_agent_created
    ON runs(agent, created_at DESC);
-- Note: idx_runs_workflow_run is created in _MIGRATIONS, not here, because
-- upgraders from a pre-v0.3 schema lack the workflow_run_id column when
-- this script runs. Sqlite errors on CREATE INDEX referencing a missing
-- column even with IF NOT EXISTS. Putting the index after the ALTER TABLE
-- migrations sidesteps the ordering trap.

CREATE TABLE IF NOT EXISTS failures (
    failure_id   TEXT PRIMARY KEY,
    run_id       TEXT,
    tenant_id    TEXT NOT NULL,
    agent        TEXT NOT NULL,
    failure_type TEXT NOT NULL,
    message      TEXT NOT NULL,
    retryable    INTEGER NOT NULL,
    created_at   TEXT NOT NULL
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
    threshold       REAL NOT NULL,
    mean_score      REAL NOT NULL,
    pass_rate       REAL NOT NULL,
    sample_count    INTEGER NOT NULL,
    total_cost_usd  REAL NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evals_agent_created
    ON evals(agent, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_run_id   TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    workflow          TEXT NOT NULL,
    workflow_version  TEXT NOT NULL,
    status            TEXT NOT NULL,
    initial_state     TEXT NOT NULL,
    final_state       TEXT,
    error_node_id     TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_created
    ON workflow_runs(workflow, created_at DESC);
"""

# Lightweight migrations applied after `executescript(_SCHEMA)`. ALTERs are
# wrapped in a try/except for the "duplicate column" error so reruns are
# safe. Indexes use IF NOT EXISTS so reruns are safe too — but they MUST
# come after their corresponding ALTER TABLE so upgraders from a pre-v0.3
# schema (no workflow_run_id column yet) don't fail on the column reference.
#
# The jobs table (v0.5) lives here too rather than in _SCHEMA — keeps all
# additive schema changes in one ordered list so the upgrade path is
# obvious. Same idempotency story (CREATE TABLE IF NOT EXISTS).
_MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN workflow_run_id TEXT",
    "ALTER TABLE runs ADD COLUMN node_id TEXT",
    (
        "CREATE INDEX IF NOT EXISTS idx_runs_workflow_run "
        "ON runs(workflow_run_id) WHERE workflow_run_id IS NOT NULL"
    ),
    # v0.5: jobs queue.
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id        TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        kind          TEXT NOT NULL,
        target        TEXT NOT NULL,
        status        TEXT NOT NULL,
        input         TEXT NOT NULL,
        result_run_id TEXT,
        error         TEXT,
        api_key_id    TEXT,
        created_at    TEXT NOT NULL,
        claimed_at    TEXT,
        completed_at  TEXT
    )
    """,
    # Partial index over the queue head — `claim_next_job` reads the
    # oldest queued row, so this keeps that O(queued) regardless of
    # historical queue depth.
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_queue_head "
        "ON jobs(tenant_id, created_at) WHERE status = 'queued'"
    ),
    # Tenant-scoped listing for the `/jobs` endpoint and `movate worker`
    # filtering.
    ("CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created ON jobs(tenant_id, created_at DESC)"),
    # v0.5 stage 2: API keys.
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        key_id        TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        env           TEXT NOT NULL,
        secret_hash   TEXT NOT NULL,
        salt          TEXT NOT NULL,
        label         TEXT,
        created_at    TEXT NOT NULL,
        last_used_at  TEXT,
        revoked_at    TEXT
    )
    """,
    # Active-keys-by-tenant lookup for `movate auth list` and audit reports.
    # Partial index over WHERE revoked_at IS NULL keeps it tight as the table
    # grows with revocations.
    (
        "CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_active "
        "ON api_keys(tenant_id) WHERE revoked_at IS NULL"
    ),
    # post-v1.0: per-job email notification. SMS deferred — needs
    # regulatory + phone-number provisioning out of band of code.
    "ALTER TABLE jobs ADD COLUMN notify_email TEXT",
    # post-v1.0: per-tenant monthly cost ceiling. One row per tenant;
    # absent row = unlimited (the default, backwards-compatible with
    # v0.x). Executor queries this on every run entry → PK lookup is
    # the perf path.
    """
    CREATE TABLE IF NOT EXISTS tenant_budgets (
        tenant_id          TEXT PRIMARY KEY,
        monthly_usd_limit  REAL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    # Cover the per-tenant current-month aggregation. Without this,
    # SUM(metrics.cost_usd) WHERE tenant_id=? AND created_at>=? is a
    # table scan; with it, an index range scan over month-of-rows.
    ("CREATE INDEX IF NOT EXISTS idx_runs_tenant_created ON runs(tenant_id, created_at)"),
    # post-v1.0: job-level retry policy. attempt_count tracks how
    # many times we've dispatched; next_retry_at lets the claim path
    # skip a job until its backoff has elapsed. NOT NULL DEFAULT 0
    # so existing rows from before this migration get a sensible
    # value (they've been "dispatched" some implicit number of times
    # but for retry purposes a fresh attempt budget is fine).
    "ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN next_retry_at TEXT",
    # Update the queue-head index to include next_retry_at so the claim
    # path can skip jobs whose retry hasn't yet elapsed without a table
    # scan. Use a separate index — keeps the original tight for jobs
    # that don't have a retry pending (next_retry_at IS NULL, the
    # common case).
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_retry_at "
        "ON jobs(tenant_id, next_retry_at) "
        "WHERE status = 'queued' AND next_retry_at IS NOT NULL"
    ),
    # v0.7.1: key expiry. NULL = no expiry (legacy keys keep working).
    # New keys written by mint_api_key get a 90-day default.
    "ALTER TABLE api_keys ADD COLUMN expires_at TEXT",
    # v0.8: permission scope. NULL = standard tenant key;
    # "fleet-admin" = admin-only endpoint access.
    "ALTER TABLE api_keys ADD COLUMN scope TEXT",
    # ADR 013 L2: least-privilege scope SET (supersedes the single
    # `scope` above). JSON-encoded list of scope strings; NULL/empty on a
    # legacy row resolves to the default {read,run,eval} at read time via
    # `effective_scopes` (no destructive backfill). sqlite has no native
    # JSON column type, so TEXT holds `json.dumps(scopes)` — same pattern
    # as runs.metrics / run_feedback.dimensions.
    "ALTER TABLE api_keys ADD COLUMN scopes TEXT",
    # 0.8.2.11: operator feedback on runs. Chainlit playground writes
    # here; the analytics dashboard reads. Sqlite uses TEXT for the
    # JSONB-equivalent dimensions column (we serialize via json.dumps
    # on save + json.loads on read, same pattern as runs.metrics).
    """
    CREATE TABLE IF NOT EXISTS run_feedback (
        feedback_id        TEXT PRIMARY KEY,
        run_id             TEXT NOT NULL,
        tenant_id          TEXT NOT NULL,
        agent              TEXT NOT NULL,
        user_id            TEXT NOT NULL,
        score              INTEGER NOT NULL,
        dimensions         TEXT,
        comment            TEXT,
        langfuse_score_id  TEXT,
        created_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_feedback_run_id ON run_feedback(run_id)",
    (
        "CREATE INDEX IF NOT EXISTS idx_run_feedback_agent_created "
        "ON run_feedback(agent, created_at DESC)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_run_feedback_tenant_created "
        "ON run_feedback(tenant_id, created_at DESC)"
    ),
    # 0.8.2.13: KB chunks for vector retrieval. Embeddings stored as
    # TEXT (JSON-encoded float arrays); no native vector index. Cosine
    # similarity computed in Python at query time. Acceptable for KBs
    # up to ~10k chunks per agent.
    """
    CREATE TABLE IF NOT EXISTS kb_chunks (
        chunk_id        TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        agent           TEXT NOT NULL,
        source          TEXT NOT NULL,
        text            TEXT NOT NULL,
        embedding       TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        content_hash    TEXT NOT NULL,
        metadata        TEXT,
        created_at      TEXT NOT NULL,
        UNIQUE(agent, tenant_id, content_hash)
    )
    """,
    ("CREATE INDEX IF NOT EXISTS idx_kb_chunks_agent_tenant ON kb_chunks(agent, tenant_id)"),
    ("CREATE INDEX IF NOT EXISTS idx_kb_chunks_source ON kb_chunks(agent, tenant_id, source)"),
    # 0.8.2.27 / PR-N: Conversation threads — group runs for multi-turn
    # agents. Runs link via the new ``runs.thread_id`` column (added
    # below). updated_at is refreshed on each appended message so
    # clients can sort threads most-recently-active first.
    """
    CREATE TABLE IF NOT EXISTS conversation_threads (
        thread_id   TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        agent       TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_threads_tenant_updated "
        "ON conversation_threads(tenant_id, updated_at DESC)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_threads_agent_tenant "
        "ON conversation_threads(agent, tenant_id)"
    ),
    # Per-run thread linkage. NULL = standalone (non-threaded) run.
    "ALTER TABLE runs ADD COLUMN thread_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id, created_at)",
    # PR-Q: jobs carry the thread linkage from queue time so the
    # worker can propagate it onto the spawned run. NULL = standalone.
    "ALTER TABLE jobs ADD COLUMN thread_id TEXT",
    # PR-AA: FTS5 virtual table for native BM25 lexical search.
    # Stored as a regular (non-content) FTS5 table — chunk_id is
    # UNINDEXED (stored but not tokenized); text is the indexed column.
    # Synced manually in save_kb_chunk / delete_kb_chunks.
    # Migration also backfills existing rows via the init() method.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts
    USING fts5(chunk_id UNINDEXED, text)
    """,
    # PR-EE: OCR provenance flag. INTEGER (0/1) in sqlite; NOT NULL so
    # existing rows default to 0 (native extraction). The migration is
    # idempotent on first-run (brand-new DB also has the column from
    # the CREATE TABLE below once the migration runs).
    "ALTER TABLE kb_chunks ADD COLUMN ocr INTEGER NOT NULL DEFAULT 0",
    # GraphRAG: knowledge-graph entities + relations layered over the KB.
    # Embeddings + source_chunk_ids + metadata stored as JSON-encoded TEXT
    # (same strategy as kb_chunks). Dedup via UNIQUE(agent, tenant_id,
    # content_hash) + ON CONFLICT in the upsert path. Bounded k-hop
    # traversal rides a recursive CTE over kb_relations.
    """
    CREATE TABLE IF NOT EXISTS kb_entities (
        entity_id        TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL,
        agent            TEXT NOT NULL,
        name             TEXT NOT NULL,
        type             TEXT NOT NULL,
        description      TEXT,
        embedding        TEXT NOT NULL,
        embedding_model  TEXT NOT NULL,
        content_hash     TEXT NOT NULL,
        source_chunk_ids TEXT,
        metadata         TEXT,
        created_at       TEXT NOT NULL,
        UNIQUE(agent, tenant_id, content_hash)
    )
    """,
    ("CREATE INDEX IF NOT EXISTS idx_kb_entities_agent_tenant ON kb_entities(agent, tenant_id)"),
    """
    CREATE TABLE IF NOT EXISTS kb_relations (
        relation_id      TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL,
        agent            TEXT NOT NULL,
        src_entity_id    TEXT NOT NULL,
        dst_entity_id    TEXT NOT NULL,
        type             TEXT NOT NULL,
        description      TEXT,
        weight           REAL NOT NULL DEFAULT 1.0,
        content_hash     TEXT NOT NULL,
        source_chunk_ids TEXT,
        metadata         TEXT,
        created_at       TEXT NOT NULL,
        UNIQUE(agent, tenant_id, content_hash)
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_kb_relations_src "
        "ON kb_relations(agent, tenant_id, src_entity_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_kb_relations_dst "
        "ON kb_relations(agent, tenant_id, dst_entity_id)"
    ),
    # BACKLOG #64: multi-model bench results. Mirrors the ``evals`` table
    # but persists a per-model comparison rather than per-case scores. The
    # ``input`` payload and the per-model ``models`` list are JSON-encoded
    # TEXT (same strategy as workflow_runs.initial_state). New table → it
    # lands here in the ordered migration list (additive, idempotent
    # CREATE TABLE IF NOT EXISTS) rather than in the base _SCHEMA.
    """
    CREATE TABLE IF NOT EXISTS bench (
        bench_id        TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        agent           TEXT NOT NULL,
        agent_version   TEXT NOT NULL,
        input           TEXT NOT NULL,
        judge_method    TEXT,
        judge_provider  TEXT,
        runs_per_model  INTEGER NOT NULL,
        gate_mode       TEXT NOT NULL,
        models          TEXT NOT NULL,
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bench_agent_created ON bench(agent, created_at DESC)",
    # ADR 014 D1: durable agent registry. One immutable row per published
    # (name, version) bundle, tenant-scoped; the ``files`` map is JSON-encoded
    # TEXT (same strategy as bench.models / workflow_runs.initial_state). New
    # table → it lands here in the ordered migration list (additive, idempotent
    # CREATE TABLE IF NOT EXISTS), never an ALTER. A new publish = a new row.
    """
    CREATE TABLE IF NOT EXISTS agent_bundles (
        name          TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        version       TEXT NOT NULL,
        created_by    TEXT,
        content_hash  TEXT NOT NULL,
        files         TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        PRIMARY KEY (tenant_id, name, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_bundles_name ON agent_bundles(tenant_id, name)",
    "CREATE INDEX IF NOT EXISTS idx_agent_bundles_name_created "
    "ON agent_bundles(tenant_id, name, created_at DESC)",
    # ADR 090 D1: agent operational lifecycle state. UNLIKE agent_bundles
    # (immutable, one row per version), this is a MUTABLE row keyed on
    # (tenant_id, name) — one current status per agent across all versions.
    # An absent row ⇒ ACTIVE, so this is additive with no backfill. Additive,
    # idempotent CREATE TABLE IF NOT EXISTS.
    """
    CREATE TABLE IF NOT EXISTS agent_runtime_state (
        tenant_id   TEXT NOT NULL,
        name        TEXT NOT NULL,
        status      TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        updated_by  TEXT,
        note        TEXT,
        PRIMARY KEY (tenant_id, name)
    )
    """,
    # ADR 060 D1: durable skills registry. Mirrors agent_bundles row-for-row
    # so the API CRUD/version surface is symmetric with agents. One immutable
    # (name, version) row per publish, tenant-scoped; ``files`` is JSON-encoded
    # TEXT (same strategy as agent_bundles.files); ``description`` is a denorm
    # column so listings don't have to parse skill.yaml. New table → lands here
    # in the ordered migration list; additive, idempotent CREATE TABLE IF NOT
    # EXISTS, never an ALTER. A new publish = a new row; rows are never mutated.
    """
    CREATE TABLE IF NOT EXISTS skills (
        name          TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        version       TEXT NOT NULL,
        created_by    TEXT,
        content_hash  TEXT NOT NULL,
        description   TEXT NOT NULL DEFAULT '',
        files         TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        PRIMARY KEY (tenant_id, name, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(tenant_id, name)",
    "CREATE INDEX IF NOT EXISTS idx_skills_name_created "
    "ON skills(tenant_id, name, created_at DESC)",
    # ADR 060 D1: durable contexts registry. Context analogue of skills/
    # agent_bundles — one immutable (name, version) row per publish,
    # tenant-scoped; the payload is a single Markdown ``body`` (the prompt
    # fragment ADR 002 injects) instead of a file bundle. Additive, idempotent
    # CREATE TABLE IF NOT EXISTS, never an ALTER.
    """
    CREATE TABLE IF NOT EXISTS contexts (
        name          TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        version       TEXT NOT NULL,
        created_by    TEXT,
        content_hash  TEXT NOT NULL,
        description   TEXT NOT NULL DEFAULT '',
        body          TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        PRIMARY KEY (tenant_id, name, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_contexts_name ON contexts(tenant_id, name)",
    "CREATE INDEX IF NOT EXISTS idx_contexts_name_created "
    "ON contexts(tenant_id, name, created_at DESC)",
    # ADR 037 D1: durable workflow registry. Mirrors agent_bundles row-for-row
    # so the API CRUD/version/publish surface is symmetric with agents. One
    # immutable (name, version) row per publish, tenant-scoped; ``files`` is
    # JSON-encoded TEXT (same strategy as agent_bundles.files). ``published``
    # is the soft promote/revert flag (ADR 037 D1) — at most one True per
    # (tenant, name) at any time, enforced by publish_workflow_version. New
    # table → lands here in the ordered migration list; additive, idempotent
    # CREATE TABLE IF NOT EXISTS, never an ALTER.
    """
    CREATE TABLE IF NOT EXISTS workflow_bundles (
        name          TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        version       TEXT NOT NULL,
        created_by    TEXT,
        content_hash  TEXT NOT NULL,
        files         TEXT NOT NULL,
        published     INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL,
        PRIMARY KEY (tenant_id, name, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflow_bundles_name ON workflow_bundles(tenant_id, name)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_bundles_name_created "
    "ON workflow_bundles(tenant_id, name, created_at DESC)",
    # ADR 016 D2: continuous-eval schedules. One row per (tenant, agent) with
    # a cadence; the scheduler tick enqueues EVAL jobs for due rows. Additive
    # + idempotent (CREATE TABLE IF NOT EXISTS) — default-off, no backfill.
    """
    CREATE TABLE IF NOT EXISTS eval_schedules (
        tenant_id            TEXT NOT NULL,
        agent                TEXT NOT NULL,
        cadence_seconds      INTEGER NOT NULL,
        enabled              INTEGER NOT NULL,
        mock                 INTEGER NOT NULL,
        runs                 INTEGER NOT NULL,
        gate_mode            TEXT NOT NULL,
        gate                 REAL NOT NULL,
        objective            TEXT,
        regression_tolerance REAL NOT NULL,
        baseline_id          TEXT,
        notify_email         TEXT,
        created_by           TEXT,
        created_at           TEXT NOT NULL,
        last_enqueued_at     TEXT,
        PRIMARY KEY (tenant_id, agent)
    )
    """,
    # ADR 017 D2: generic agent/workflow cron schedules. One row per
    # (tenant, name) with a cadence + a job payload; the scheduler tick
    # enqueues a JobKind.AGENT/WORKFLOW job for due rows. Additive +
    # idempotent (CREATE TABLE IF NOT EXISTS) — default-off, no backfill.
    # ``input`` holds json.dumps(payload), mirroring the jobs table.
    """
    CREATE TABLE IF NOT EXISTS job_schedules (
        tenant_id        TEXT NOT NULL,
        name             TEXT NOT NULL,
        kind             TEXT NOT NULL,
        target           TEXT NOT NULL,
        cadence_seconds  INTEGER NOT NULL,
        enabled          INTEGER NOT NULL,
        input            TEXT NOT NULL,
        notify_email     TEXT,
        created_by       TEXT,
        created_at       TEXT NOT NULL,
        last_enqueued_at TEXT,
        PRIMARY KEY (tenant_id, name)
    )
    """,
    # ADR 017 D2: inbound event/webhook triggers. One row per (tenant, name)
    # with a public trigger_id (in the webhook URL), a hashed-at-rest
    # per-trigger secret, and default-input merged under the event body. The
    # fire endpoint resolves by trigger_id and enqueues a JobKind.AGENT/
    # WORKFLOW job. Additive new table (CREATE TABLE IF NOT EXISTS, idempotent)
    # — default-off, no ALTER, no backfill. ``input_defaults`` is JSON-encoded
    # like jobs.input; trigger_id is uniquely indexed for the fire-path lookup.
    """
    CREATE TABLE IF NOT EXISTS triggers (
        tenant_id      TEXT NOT NULL,
        name           TEXT NOT NULL,
        trigger_id     TEXT NOT NULL,
        kind           TEXT NOT NULL,
        target         TEXT NOT NULL,
        secret_hash    TEXT NOT NULL,
        salt           TEXT NOT NULL,
        input_defaults TEXT NOT NULL,
        enabled        INTEGER NOT NULL,
        created_by     TEXT,
        created_at     TEXT NOT NULL,
        last_fired_at  TEXT,
        PRIMARY KEY (tenant_id, name)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_triggers_trigger_id ON triggers(trigger_id)",
    # item 23: trigger replay / idempotency (ADR 017 D2 follow-up). One row
    # per (trigger_id, delivery_id) recording the job_id the FIRST delivery
    # enqueued, so an at-least-once webhook retry returns the same job instead
    # of double-enqueuing. Additive new table (CREATE TABLE IF NOT EXISTS,
    # idempotent) — a row exists only when a fire request carried an
    # X-Movate-Delivery-Id header, so the no-header path is unchanged. The
    # composite PRIMARY KEY makes record_trigger_delivery's INSERT OR IGNORE
    # an atomic dedup: a concurrent double-delivery races to one winner.
    """
    CREATE TABLE IF NOT EXISTS trigger_deliveries (
        trigger_id  TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        job_id      TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (trigger_id, delivery_id)
    )
    """,
    # item 37: submission idempotency. One row per (tenant_id, idempotency_key)
    # recording the job_id the FIRST async submit enqueued, so a client retry
    # (network blip / timeout) returns the same job instead of double-
    # enqueuing. Mirrors trigger_deliveries above but is per-TENANT scoped (the
    # submit path has an AuthContext). Additive new table (CREATE TABLE IF NOT
    # EXISTS, idempotent) — a row exists only when a submit carried an
    # Idempotency-Key header, so the no-header path is unchanged. The composite
    # PRIMARY KEY makes record_run_submission's INSERT OR IGNORE an atomic
    # dedup: a concurrent retry races to one winner.
    """
    CREATE TABLE IF NOT EXISTS run_submissions (
        tenant_id       TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        job_id          TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        request_hash    TEXT,
        PRIMARY KEY (tenant_id, idempotency_key)
    )
    """,
    # ADR 016 D3: canary / champion-challenger rollout. One row per (tenant,
    # agent): a challenger version + a traffic weight (0 = kill switch), with
    # optional champion pin + auto-promote eval gate. The run/enqueue path
    # reads this to choose champion vs challenger; NO row → champion-by-latest
    # → byte-for-byte the pre-canary behavior. Additive + idempotent (CREATE
    # TABLE IF NOT EXISTS) — default-off, no backfill.
    """
    CREATE TABLE IF NOT EXISTS canary_configs (
        tenant_id          TEXT NOT NULL,
        agent              TEXT NOT NULL,
        challenger_version TEXT NOT NULL,
        champion_version   TEXT,
        weight             INTEGER NOT NULL,
        sticky             INTEGER NOT NULL,
        enabled            INTEGER NOT NULL,
        auto_promote       INTEGER NOT NULL,
        eval_gate          REAL,
        created_by         TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        PRIMARY KEY (tenant_id, agent)
    )
    """,
    # ADR 016 D5: opt-in auto-rollback. A scheduled-eval drift regression on
    # the challenger trips the kill switch (weight → 0) when set. Additive +
    # default-off (DEFAULT 0): pre-D5 canary rows read back as auto_rollback=
    # False → alert-only, byte-for-byte the pre-D5 behavior. Placed after the
    # CREATE so upgraders with an existing canary_configs table get the column.
    "ALTER TABLE canary_configs ADD COLUMN auto_rollback INTEGER NOT NULL DEFAULT 0",
    # ADR 016 D3: carry the canary-chosen agent version to the async worker.
    # The enqueue path stamps the concrete champion/challenger version it
    # picked; the worker resolves THAT version. Nullable — pre-canary rows
    # (and every job with no canary in play) read back as NULL → None → the
    # worker resolves latest, unchanged.
    "ALTER TABLE jobs ADD COLUMN target_version TEXT",
    # ADR 017 D5 (PR 1): HITL pause checkpoint on workflow_runs. When a
    # workflow pauses at a HUMAN gate the runner stamps these three columns;
    # PR 2's resume-on-signal path reads them to continue. All nullable —
    # existing rows (and every non-paused SUCCESS/ERROR run) read back as
    # NULL → None, so the schema change is additive + backward compatible.
    # sqlite has no native JSON column type, so paused_state / human_task are
    # TEXT holding json.dumps(...) — same strategy as workflow_runs.initial_state.
    "ALTER TABLE workflow_runs ADD COLUMN paused_node_id TEXT",
    "ALTER TABLE workflow_runs ADD COLUMN paused_state TEXT",
    "ALTER TABLE workflow_runs ADD COLUMN human_task TEXT",
    # ADR 062 D2: which backend owns this run. NULL on pre-migration rows and
    # native runs → None → the resume-on-signal endpoint defaults to the native
    # re-walk; only 'temporal' routes the resume to a Temporal signal. Additive
    # + nullable, same backward-compatible story as the paused_* columns above.
    "ALTER TABLE workflow_runs ADD COLUMN runtime TEXT",
    # ADR 017 D5 (PR 2): resume-on-signal. The signal endpoint enqueues a
    # JobKind.WORKFLOW continuation job carrying the workflow_run_id to resume
    # from; the worker reads this and calls WorkflowRunner.resume. Nullable —
    # pre-PR-2 rows (and every non-resume job) read back as NULL → None → the
    # worker runs from the entrypoint, unchanged. Mirrors the target_version
    # additive-column pattern above.
    "ALTER TABLE jobs ADD COLUMN resume_workflow_run_id TEXT",
    # item 17: batch inference. A batch is parent metadata over N child
    # JobKind.AGENT jobs; the submit endpoint persists one row here and
    # stamps each child job's batch_id (the column added just below).
    # Additive new table (CREATE TABLE IF NOT EXISTS, idempotent) — no row
    # exists unless a batch was submitted, so non-batch behavior is
    # unchanged.
    """
    CREATE TABLE IF NOT EXISTS batches (
        batch_id    TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        agent       TEXT NOT NULL,
        total       INTEGER NOT NULL,
        created_by  TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_batches_tenant_created ON batches(tenant_id, created_at DESC)",
    # item 17: link each enqueued dataset row back to its parent batch.
    # Nullable — pre-batch rows (and every non-batch job: single runs,
    # scheduled/triggered/threaded/workflow jobs) read back as NULL → None,
    # byte-for-byte the pre-batch JobRecord. Mirrors the target_version
    # additive-column pattern above.
    "ALTER TABLE jobs ADD COLUMN batch_id TEXT",
    # Aggregate the children of one batch (status endpoint). Partial-free:
    # most jobs have batch_id NULL, but a batch with thousands of rows wants
    # an index range scan, not a full table scan.
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_batch "
        "ON jobs(tenant_id, batch_id) WHERE batch_id IS NOT NULL"
    ),
    # item 32 (ADR 019): W3C trace-context carrier captured at enqueue so the
    # worker can continue the originating distributed trace (submit → execute
    # as ONE trace). Stored as JSON text (a dict[str,str] of
    # traceparent/tracestate) — storage never imports OTel. Nullable — pre-R2
    # rows (and any job enqueued with OTel off) read back as NULL → {} → the
    # worker starts a fresh root span, byte-for-byte the pre-R2 behaviour. The
    # duplicate-column guard in init() keeps this idempotent on re-run (mirrors
    # the target_version additive-column pattern above).
    "ALTER TABLE jobs ADD COLUMN trace_context TEXT",
    # item 24: per-dimension eval means. A JSON column ({dim: mean}) so drift
    # detection can compare per-dimension, catching a single-dimension
    # regression the aggregate mean_score would mask. Nullable — pre-item-24
    # rows read back as NULL → None, and detect_drift then falls back to the
    # aggregate-only path, byte-for-byte the old behaviour. Mirrors the
    # target_version additive-column pattern above.
    "ALTER TABLE evals ADD COLUMN dimension_means TEXT",
    # ADR 018: per-tenant BYOK provider keys. One row per (tenant, provider)
    # holding a Fernet ``ciphertext`` of the tenant's own provider key + a
    # masked ``fingerprint`` for display. The ProviderKeyResolver decrypts
    # ``ciphertext`` at run time (tenant-key-first, shared-key fallback). NO
    # row → the run path uses the provider's env-default key → byte-for-byte
    # the pre-BYOK behavior. Additive new table (CREATE TABLE IF NOT EXISTS,
    # idempotent) — default-off, no backfill. The plaintext key is NEVER
    # stored here (only its ciphertext + masked tail).
    """
    CREATE TABLE IF NOT EXISTS tenant_provider_keys (
        tenant_id   TEXT NOT NULL,
        provider    TEXT NOT NULL,
        ciphertext  TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created_by  TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (tenant_id, provider)
    )
    """,
    # item 36 (R4b): cooperative run cancellation. A RUNNING job flagged for
    # cancellation carries cancel_requested = 1; the worker honors it at its
    # terminal checkpoint and writes CANCELLED instead of the dispatch outcome
    # (a QUEUED job is flipped straight to CANCELLED by request_job_cancel and
    # never claimed). Stored as INTEGER (sqlite has no bool) NOT NULL DEFAULT 0
    # so existing rows (and every job that's never cancelled — the common case)
    # read back as 0/False and behave byte-for-byte as before. The
    # duplicate-column guard in init() keeps this idempotent on re-run (mirrors
    # the attempt_count additive-column pattern above).
    "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0",
    # item 37 (payload-conflict guard): a nullable request fingerprint on
    # run_submissions. A repeat submit reusing an Idempotency-Key but with a
    # DIFFERENT canonical payload must 409 (don't silently return the wrong
    # run) rather than dedup to the original job. NULL on pre-migration rows
    # (and on submits recorded before this column existed) means "fingerprint
    # unknown" → the app skips the conflict check and returns the prior job,
    # so the upgrade is byte-for-byte back-compatible. Idempotent via the
    # duplicate-column guard in init() (same pattern as cancel_requested).
    "ALTER TABLE run_submissions ADD COLUMN request_hash TEXT",
    # ADR 024 D2 — per-step observability retention. Both are JSON-array TEXT
    # columns (same strategy as runs.metrics): the executor populates them and
    # they round-trip through save_run / _row_to_run so `mdk explain` can
    # reconstruct the turn → skill/retrieval tree OFFLINE (no Langfuse needed).
    # Additive + idempotent (duplicate-column guard in init() swallows re-runs);
    # NULL on pre-migration rows → an empty list, so legacy records load fine.
    # ``skill_calls`` was previously not persisted by the DB providers; this
    # migration starts retaining it alongside the new ``turns`` so the offline
    # breakdown is coherent (a turn's skill children are persisted too).
    "ALTER TABLE runs ADD COLUMN skill_calls TEXT",
    "ALTER TABLE runs ADD COLUMN turns TEXT",
    # ADR 040 — Projects as a first-class cloud entity. Five additive,
    # idempotent (CREATE TABLE IF NOT EXISTS) tables. No existing column is
    # modified, no ALTER on existing tables; the front end's project UX
    # composes onto this without any back-compat hazard. ``tenant_id NOT
    # NULL`` is the invariant on ``projects``; members + junctions derive
    # the tenant via the project's row (joined when needed) so the
    # tenant-isolation story stays single-sourced. Soft delete via
    # ``archived_at`` (D6); a default-per-tenant row is created lazily by
    # ``get_or_create_default_project`` (D5) and the unique
    # ``(tenant_id, name)`` index keeps two racing creates collapsing to one.
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id           TEXT PRIMARY KEY,
        tenant_id            TEXT NOT NULL,
        name                 TEXT NOT NULL,
        description          TEXT,
        owner_principal_id   TEXT NOT NULL,
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL,
        archived_at          TEXT,
        UNIQUE (tenant_id, name)
    )
    """,
    # Listing filter (D6 — hide archived) + tenant scope.
    "CREATE INDEX IF NOT EXISTS idx_projects_tenant_archived ON projects(tenant_id, archived_at)",
    # Member CRUD: PK is (project_id, principal_id); the secondary index
    # covers the RBAC "members of project X with role Y" filter the API
    # composes on (e.g. "is this principal a project owner?").
    """
    CREATE TABLE IF NOT EXISTS project_members (
        project_id    TEXT NOT NULL,
        principal_id  TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('viewer','editor','owner')),
        added_by      TEXT NOT NULL,
        added_at      TEXT NOT NULL,
        PRIMARY KEY (project_id, principal_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_members_project_role "
    "ON project_members(project_id, role)",
    # Agent attachment junction (D2 — M:N). Indices cover both directions:
    # forward ("agents attached to project X") + reverse ("projects this
    # agent belongs to" — D5 implicit-default check).
    """
    CREATE TABLE IF NOT EXISTS project_agents (
        project_id  TEXT NOT NULL,
        agent_name  TEXT NOT NULL,
        added_at    TEXT NOT NULL,
        PRIMARY KEY (project_id, agent_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_agents_project ON project_agents(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_agents_agent_name ON project_agents(agent_name)",
    # Workflow attachment junction (D2 — M:N). Mirror of project_agents.
    """
    CREATE TABLE IF NOT EXISTS project_workflows (
        project_id     TEXT NOT NULL,
        workflow_name  TEXT NOT NULL,
        added_at       TEXT NOT NULL,
        PRIMARY KEY (project_id, workflow_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_workflows_project ON project_workflows(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_workflows_workflow_name "
    "ON project_workflows(workflow_name)",
    # KB attachment junction (D3). Carries the share mode (owned /
    # shared_reference / shared_copy). The "exactly one ``owned`` row per
    # kb_id" invariant is enforced by the API layer on share, not at the
    # storage seam — keeps the table portable to backends that don't
    # support partial unique indexes.
    """
    CREATE TABLE IF NOT EXISTS project_kbs (
        project_id  TEXT NOT NULL,
        kb_id       TEXT NOT NULL,
        mode        TEXT NOT NULL CHECK (mode IN ('owned','shared_reference','shared_copy')),
        added_at    TEXT NOT NULL,
        PRIMARY KEY (project_id, kb_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_kbs_project ON project_kbs(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_kbs_kb_id ON project_kbs(kb_id)",
    # ADR 041: Agent catalog. Three namespaces (movate / private / community)
    # in one schema, distinguished by ``source``; the read API joins
    # ``movate`` + the caller's ``private`` + (future) ``community``. The
    # ``tenant_id_key`` column is a generated computed key over (tenant_id,
    # '') so a NULL tenant for public entries still participates in the
    # composite PRIMARY KEY (sqlite's PK semantics ignore NULL otherwise).
    # ``tags`` is stored as JSON text (a list[str]) — the gin-equivalent
    # filter is "JSON_EACH" in queries. ``ratings_summary`` is JSON text.
    # Bundle bytes live in ``catalog_entry_versions.bundle_tar`` (BLOB).
    """
    CREATE TABLE IF NOT EXISTS catalog_entries (
        slug             TEXT NOT NULL,
        source           TEXT NOT NULL CHECK (source IN ('movate','private','community')),
        tenant_id        TEXT,
        tenant_id_key    TEXT NOT NULL,
        latest_version   TEXT NOT NULL,
        name             TEXT NOT NULL,
        title            TEXT NOT NULL,
        description      TEXT NOT NULL,
        tags             TEXT NOT NULL DEFAULT '[]',
        shape            TEXT,
        recommended_for  TEXT,
        ratings_summary  TEXT NOT NULL DEFAULT '{"count": 0, "avg": 0.0}',
        popularity       INTEGER NOT NULL DEFAULT 0,
        synced_at        TEXT NOT NULL,
        PRIMARY KEY (slug, source, tenant_id_key),
        CHECK (
            (source = 'private' AND tenant_id IS NOT NULL)
            OR (source IN ('movate','community') AND tenant_id IS NULL)
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_source_tenant "
    "ON catalog_entries(source, tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_shape ON catalog_entries(shape)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_synced_at ON catalog_entries(synced_at DESC)",
    # Per-version table — the bundle bytes live here, fetched lazily on add.
    # PK (slug, source, version, tenant_id_key) — re-publishing the same
    # version overwrites the prior row (idempotent re-sync).
    """
    CREATE TABLE IF NOT EXISTS catalog_entry_versions (
        slug          TEXT NOT NULL,
        version       TEXT NOT NULL,
        source        TEXT NOT NULL,
        tenant_id     TEXT,
        tenant_id_key TEXT NOT NULL,
        bundle_tar    BLOB NOT NULL,
        digest        TEXT NOT NULL,
        published_at  TEXT NOT NULL,
        deprecated_at TEXT,
        PRIMARY KEY (slug, source, version, tenant_id_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_catalog_entry_versions_slug_source "
    "ON catalog_entry_versions(slug, source)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entry_versions_published "
    "ON catalog_entry_versions(published_at DESC)",
    # One rating per tenant per entry. Re-rating overwrites in place.
    """
    CREATE TABLE IF NOT EXISTS catalog_entry_ratings (
        slug       TEXT NOT NULL,
        source     TEXT NOT NULL,
        tenant_id  TEXT NOT NULL,
        rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
        comment    TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (slug, source, tenant_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_catalog_entry_ratings_slug ON catalog_entry_ratings(slug)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entry_ratings_tenant "
    "ON catalog_entry_ratings(tenant_id)",
    # Per-source sync watermark. One row per source — the last successful
    # sync timestamp. Drives the incremental ``?since=`` request to
    # catalog.movate.io (ADR 041 D4).
    """
    CREATE TABLE IF NOT EXISTS catalog_sync_watermark (
        source           TEXT PRIMARY KEY,
        last_synced_at   TEXT NOT NULL
    )
    """,
    # ADR 047: observability insights — the overnight analyst's daily,
    # pre-aggregated telemetry summary per (tenant, project, date). APPEND-ONLY:
    # a re-run for a day INSERTs a new row (keyed by the unique ``id``); reads
    # take the latest row per (tenant, project, date). The four JSON columns
    # (anomalies / top_failures / usage_rollup / trends) are json.dumps'd on
    # save + json.loads'd on read, same strategy as runs.metrics. Additive new
    # table (CREATE TABLE IF NOT EXISTS, idempotent) — default-off, no backfill.
    """
    CREATE TABLE IF NOT EXISTS observability_insights (
        id               TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL,
        project_id       TEXT NOT NULL,
        date             TEXT NOT NULL,
        health_score     REAL NOT NULL,
        anomalies        TEXT NOT NULL,
        top_failures     TEXT NOT NULL,
        usage_rollup     TEXT NOT NULL,
        trends           TEXT NOT NULL,
        narrative_digest TEXT NOT NULL,
        created_at       TEXT NOT NULL
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_observability_insights_tpd "
        "ON observability_insights(tenant_id, project_id, date)"
    ),
    # ADR 035 D1 — events outbox. Domain events (run.completed,
    # agent.published, eval.failed, drift.detected, canary.promoted/demoted,
    # ...) are recorded at the runtime edges and served by ``GET
    # /api/v1/events``. Additive new table (CREATE TABLE IF NOT EXISTS,
    # idempotent) — no row exists unless an emit happened, so non-event
    # behavior is byte-for-byte unchanged. ``tenant_id NOT NULL`` matches
    # the rest of the schema (ADR 013/014). ``data`` holds json.dumps(...)
    # — same TEXT-for-JSON strategy as runs.metrics / kb_chunks.metadata.
    """
    CREATE TABLE IF NOT EXISTS events (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        kind        TEXT NOT NULL,
        subject     TEXT NOT NULL,
        data        TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    # Index for the common time-range list ``WHERE tenant_id=? AND
    # created_at >=? ORDER BY created_at`` — the default ``GET
    # /api/v1/events`` shape.
    ("CREATE INDEX IF NOT EXISTS idx_events_tenant_created ON events(tenant_id, created_at)"),
    # Index for the kind-filter list ``WHERE tenant_id=? AND kind=? AND
    # created_at>=?`` — supports the kind-subscribed front-end view that
    # D2/D3 will also lean on.
    (
        "CREATE INDEX IF NOT EXISTS idx_events_tenant_kind_created "
        "ON events(tenant_id, kind, created_at)"
    ),
    # ADR 035 D2 — webhook subscriptions (outbound delivery). One row per
    # configured subscriber; the secret is STORED (not hashed) so the
    # worker can re-sign every outbound POST. Additive new table — a row
    # exists only when the tenant POSTs ``/api/v1/webhooks``, so pre-D2
    # behavior is byte-for-byte unchanged. ``tenant_id NOT NULL`` matches
    # the rest of the schema; ``kind_filter`` is a JSON list (TEXT) for
    # backend portability.
    """
    CREATE TABLE IF NOT EXISTS webhooks (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        url           TEXT NOT NULL,
        kind_filter   TEXT NOT NULL,
        secret        TEXT NOT NULL,
        enabled       INTEGER NOT NULL DEFAULT 1,
        failure_count INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webhooks_tenant ON webhooks(tenant_id)",
    # ADR 035 D2 — per-attempt delivery log (one row per
    # (webhook_id, event_id, attempt_n) triple). Bounded
    # ``response_excerpt`` keeps a misbehaving subscriber from
    # ballooning the table.
    """
    CREATE TABLE IF NOT EXISTS webhook_attempts (
        id                TEXT PRIMARY KEY,
        webhook_id        TEXT NOT NULL,
        event_id          TEXT NOT NULL,
        tenant_id         TEXT NOT NULL,
        attempted_at      TEXT NOT NULL,
        status_code       INTEGER,
        response_excerpt  TEXT,
        error_kind        TEXT NOT NULL,
        attempt_n         INTEGER NOT NULL
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_webhook_attempts_tenant_at "
        "ON webhook_attempts(tenant_id, attempted_at)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_webhook_attempts_webhook_at "
        "ON webhook_attempts(webhook_id, attempted_at)"
    ),
    # ADR 035 D2 — per-webhook delivery cursor. One row per subscription
    # (``webhook_id`` PK); ``last_event_id`` is the most recently
    # processed event id. Per-webhook cursor (vs a single global one)
    # avoids re-delivering everyone's events when a new subscriber
    # signs up.
    """
    CREATE TABLE IF NOT EXISTS webhook_cursors (
        webhook_id    TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        last_event_id TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webhook_cursors_tenant ON webhook_cursors(tenant_id)",
    # Eval-generation jobs (``mdk eval generate``). Runtime-resident,
    # SSE-driven; persistence is so the status GET + commit endpoint can
    # read back what generation produced. Additive new table, idempotent
    # (CREATE TABLE IF NOT EXISTS) — no row exists unless a caller hit
    # the generate endpoint. ``result`` is a JSON-encoded
    # :class:`GenerationResult` (cases + judge + cost) populated when the
    # pipeline finishes; ``error`` is populated on failure. Both NULL
    # while ``status='running'``.
    """
    CREATE TABLE IF NOT EXISTS eval_generation_jobs (
        job_id          TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        agent_name      TEXT NOT NULL,
        status          TEXT NOT NULL,
        description     TEXT NOT NULL,
        count_requested INTEGER NOT NULL,
        categories      TEXT NOT NULL,
        include_judge   INTEGER NOT NULL DEFAULT 0,
        model           TEXT NOT NULL,
        budget_usd      REAL,
        progress        REAL NOT NULL DEFAULT 0.0,
        result          TEXT,
        error           TEXT,
        tokens_used     INTEGER NOT NULL DEFAULT 0,
        cost_usd        REAL NOT NULL DEFAULT 0.0,
        created_at      TEXT NOT NULL,
        completed_at    TEXT
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_eval_gen_jobs_tenant_created "
        "ON eval_generation_jobs(tenant_id, created_at DESC)"
    ),
    # ADR 043 D1: Failure Pattern Diagnoser results. One row per diagnose
    # request; upsert on diagnosis_id so the runtime's background task can
    # transition a row from ``running`` to ``completed`` without a separate
    # update method. ``request`` / ``result`` are JSON blobs in TEXT (no
    # native JSON type in sqlite); the typed-fix taxonomy is validated at
    # the wire edge so adding a new fix kind later doesn't require a
    # storage migration. Read-only with respect to agent state — persisting
    # a row never modifies the agent.
    """
    CREATE TABLE IF NOT EXISTS diagnoses (
        diagnosis_id  TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        agent         TEXT NOT NULL,
        status        TEXT NOT NULL,
        request       TEXT NOT NULL,
        result        TEXT,
        error         TEXT,
        tokens_used   INTEGER NOT NULL DEFAULT 0,
        cost_usd      REAL NOT NULL DEFAULT 0.0,
        model         TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        completed_at  TEXT
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_diagnoses_agent_created "
        "ON diagnoses(tenant_id, agent, created_at DESC)"
    ),
    # Claude-orchestrated audit records (read-only audit pipeline). One
    # immutable row per terminal audit, keyed by audit_id, tenant-scoped at
    # the row level. ``findings`` + ``categories`` are JSON-encoded TEXT —
    # same strategy as bench.models / workflow_runs.initial_state. Additive
    # + idempotent (CREATE TABLE IF NOT EXISTS); no backfill needed.
    """
    CREATE TABLE IF NOT EXISTS audits (
        audit_id        TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        scope_kind      TEXT NOT NULL,
        scope_id        TEXT NOT NULL,
        categories      TEXT NOT NULL,
        severity_floor  TEXT NOT NULL,
        model           TEXT NOT NULL,
        budget_usd      REAL NOT NULL,
        findings        TEXT NOT NULL,
        partial         INTEGER NOT NULL DEFAULT 0,
        tokens_used     INTEGER NOT NULL DEFAULT 0,
        cost_usd        REAL NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audits_scope_created "
    "ON audits(tenant_id, scope_id, created_at DESC)",
    # ADR 046 D1 — project-scope the knowledge graph. Additive nullable
    # column on both graph tables so the viewer / query API can window a
    # subgraph at the PROJECT grain (a project's graph across its agents),
    # not only per agent. Idempotent (the duplicate-column guard in init()
    # swallows re-runs); pre-migration rows + project-less ingests carry
    # NULL → absent from a project-filtered query, fully visible in the
    # unfiltered (per-agent) view. Backward-compatible by construction. A
    # partial index keeps project-filtered scans cheap without bloating the
    # common NULL case.
    "ALTER TABLE kb_entities ADD COLUMN project_id TEXT",
    "ALTER TABLE kb_relations ADD COLUMN project_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_kb_entities_project "
    "ON kb_entities(agent, tenant_id, project_id) WHERE project_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_kb_relations_project "
    "ON kb_relations(agent, tenant_id, project_id) WHERE project_id IS NOT NULL",
    # ADR 045 D10: stateful sessions — server-side conversation memory.
    # ``sessions`` holds the entity + the per-session rollups; the turns
    # live in ``session_messages`` (one row per message, content as JSON
    # TEXT). ``tenant_id NOT NULL`` on both (per-tenant isolation).
    # Additive + idempotent (CREATE TABLE IF NOT EXISTS); no backfill.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id       TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL,
        agent            TEXT NOT NULL,
        title            TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        turn_count       INTEGER NOT NULL DEFAULT 0,
        total_cost_usd   REAL NOT NULL DEFAULT 0,
        total_tokens_in  INTEGER NOT NULL DEFAULT 0,
        total_tokens_out INTEGER NOT NULL DEFAULT 0
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_sessions_tenant_updated "
        "ON sessions(tenant_id, updated_at DESC)"
    ),
    ("CREATE INDEX IF NOT EXISTS idx_sessions_agent_tenant ON sessions(agent, tenant_id)"),
    """
    CREATE TABLE IF NOT EXISTS session_messages (
        message_id   TEXT PRIMARY KEY,
        session_id   TEXT NOT NULL,
        tenant_id    TEXT NOT NULL,
        role         TEXT NOT NULL,
        content      TEXT NOT NULL,
        run_id       TEXT,
        cost_usd     REAL NOT NULL DEFAULT 0,
        tokens_in    INTEGER NOT NULL DEFAULT 0,
        tokens_out   INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_session_messages_session "
        "ON session_messages(session_id, tenant_id, created_at)"
    ),
    # ADR 050 D1 — voice-as-run parity: voice-specific columns on runs.
    # All nullable (None for text runs); additive, no backfill needed.
    "ALTER TABLE runs ADD COLUMN modality TEXT",
    "ALTER TABLE runs ADD COLUMN stt_latency_ms REAL",
    "ALTER TABLE runs ADD COLUMN tts_latency_ms REAL",
    "ALTER TABLE runs ADD COLUMN audio_duration_s REAL",
    "ALTER TABLE runs ADD COLUMN stt_cost_usd REAL",
    "ALTER TABLE runs ADD COLUMN tts_cost_usd REAL",
    # ADR 052: tool registry descriptors. Additive + idempotent
    # (CREATE TABLE IF NOT EXISTS); no backfill needed.
    (
        "CREATE TABLE IF NOT EXISTS tool_descriptors ("
        "  name TEXT NOT NULL,"
        "  version TEXT NOT NULL,"
        "  scope TEXT NOT NULL DEFAULT 'tenant',"
        "  tenant_id TEXT NOT NULL,"
        "  project_id TEXT,"
        "  description TEXT NOT NULL DEFAULT '',"
        "  tags TEXT NOT NULL DEFAULT '[]',"
        "  input_schema TEXT NOT NULL DEFAULT '{}',"
        "  output_schema TEXT NOT NULL DEFAULT '{}',"
        "  backend TEXT NOT NULL DEFAULT '{}',"
        "  credentials_ref TEXT,"
        "  governance TEXT NOT NULL DEFAULT '{}',"
        "  owner TEXT,"
        "  created_at TEXT,"
        "  updated_at TEXT,"
        "  PRIMARY KEY (name, version, scope, tenant_id)"
        ")"
    ),
    "CREATE INDEX IF NOT EXISTS idx_tool_descriptors_scope_tenant "
    "ON tool_descriptors(scope, tenant_id)",
]


def _fts5_escape(query: str) -> str:
    """Sanitize a free-text query for safe use in FTS5 MATCH expressions.

    FTS5 MATCH syntax has special characters (``"*^()``) that cause
    parse errors when present in user queries. We take the conservative
    approach: strip all non-alphanumeric/space characters and reassemble
    as a space-joined set of plain tokens. This gives OR-style matching
    (all tokens scored independently by BM25) which is the correct
    semantics for retrieval — AND-style would miss chunks that partially
    match.

    Returns empty string if the query has no usable tokens.
    """
    import re  # noqa: PLC0415

    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    return " ".join(tokens)


def _opt_col(r: Any, name: str) -> Any:
    """Read a column that may be absent on a pre-migration row.

    ``aiosqlite.Row`` raises ``IndexError`` for an unknown column; the
    additive ``project_id`` ALTER always runs in ``init()`` so the column
    is normally present, but reading via ``dict(row).get`` keeps this
    null-safe even if a row was selected with a column-less projection."""
    try:
        return dict(r).get(name)
    except (TypeError, ValueError):
        return None


def _row_to_entity(r: Any) -> Entity:
    return Entity(
        entity_id=r["entity_id"],
        tenant_id=r["tenant_id"],
        agent=r["agent"],
        project_id=_opt_col(r, "project_id"),
        name=r["name"],
        type=r["type"],
        description=r["description"],
        embedding=json.loads(r["embedding"]),
        embedding_model=r["embedding_model"],
        content_hash=r["content_hash"],
        source_chunk_ids=json.loads(r["source_chunk_ids"]) if r["source_chunk_ids"] else [],
        metadata=json.loads(r["metadata"]) if r["metadata"] else None,
        created_at=datetime.fromisoformat(r["created_at"]),
    )


def _tenant_key(tenant_id: str | None) -> str:
    """Encode ``tenant_id`` into a non-null PK component for catalog tables.

    SQLite's PRIMARY KEY ignores NULL when checking uniqueness, so two
    public-namespace rows with the same slug + source + NULL tenant_id
    would silently both insert. Stamping a sentinel (``__public__``)
    into a non-null companion column keeps the composite PK uniqueness
    guarantee intact for public entries while preserving ``tenant_id``
    itself as NULL for the read API."""

    return tenant_id if tenant_id is not None else "__public__"


def _enforce_catalog_namespace(source: CatalogSource, tenant_id: str | None) -> None:
    """The same invariant the DB CHECK constraint enforces — raised early
    so callers get a clear Python error instead of an opaque SQL one."""

    if source is CatalogSource.PRIVATE:
        if not tenant_id:
            raise ValueError("catalog 'private' entries require tenant_id")
    elif tenant_id is not None:
        raise ValueError(f"catalog '{source.value}' entries must have tenant_id=None")


_RATING_MIN = 1
_RATING_MAX = 5


def _row_to_catalog_entry(r: Any) -> CatalogEntry:
    summary_raw = r["ratings_summary"]
    summary = CatalogRatingsSummary.model_validate(json.loads(summary_raw))
    return CatalogEntry(
        slug=r["slug"],
        source=CatalogSource(r["source"]),
        tenant_id=r["tenant_id"],
        latest_version=r["latest_version"],
        name=r["name"],
        title=r["title"],
        description=r["description"],
        tags=json.loads(r["tags"]) if r["tags"] else [],
        shape=r["shape"],
        recommended_for=r["recommended_for"],
        ratings_summary=summary,
        popularity=int(r["popularity"]),
        synced_at=datetime.fromisoformat(r["synced_at"]),
    )


def _row_to_catalog_entry_version(r: Any) -> CatalogEntryVersion:
    return CatalogEntryVersion(
        slug=r["slug"],
        version=r["version"],
        source=CatalogSource(r["source"]),
        tenant_id=r["tenant_id"],
        bundle_tar=bytes(r["bundle_tar"]),
        digest=r["digest"],
        published_at=datetime.fromisoformat(r["published_at"]),
        deprecated_at=(datetime.fromisoformat(r["deprecated_at"]) if r["deprecated_at"] else None),
    )


def _row_to_relation(r: Any) -> Relation:
    return Relation(
        relation_id=r["relation_id"],
        tenant_id=r["tenant_id"],
        agent=r["agent"],
        project_id=_opt_col(r, "project_id"),
        src_entity_id=r["src_entity_id"],
        dst_entity_id=r["dst_entity_id"],
        type=r["type"],
        description=r["description"],
        weight=r["weight"],
        content_hash=r["content_hash"],
        source_chunk_ids=json.loads(r["source_chunk_ids"]) if r["source_chunk_ids"] else [],
        metadata=json.loads(r["metadata"]) if r["metadata"] else None,
        created_at=datetime.fromisoformat(r["created_at"]),
    )


class SqliteProvider:
    name = "sqlite"

    def __init__(self, db_path: str | Path = "~/.movate/local.db") -> None:
        self._path = Path(str(db_path)).expanduser()
        self._conn: aiosqlite.Connection | None = None

    async def ping(self) -> None:
        """``SELECT 1`` — confirms the connection is alive without
        touching any application table. Cheap enough to run on every
        ACA readiness probe (default cadence ~10s)."""
        async with self._db.execute("SELECT 1") as cur:
            await cur.fetchone()

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        # Idempotent column additions for upgraders. ALTER TABLE in sqlite
        # raises OperationalError("duplicate column name") on re-run; swallow
        # only that and re-raise everything else.
        for stmt in _MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
        await self._conn.commit()

        # PR-AA: Backfill FTS5 index for any chunks ingested before
        # this migration ran. The NOT IN subquery is safe for small KBs
        # (typical local dev / CI). On subsequent startups the subquery
        # returns nothing → no-op. Degrades gracefully if FTS5 is not
        # compiled into the sqlite binary (very rare).
        try:
            await self._conn.execute(
                """
                INSERT INTO kb_chunks_fts(rowid, chunk_id, text)
                SELECT rowid, chunk_id, text FROM kb_chunks
                WHERE chunk_id NOT IN (SELECT chunk_id FROM kb_chunks_fts)
                """
            )
            await self._conn.commit()
        except aiosqlite.OperationalError:
            pass  # FTS5 not available — lexical path degrades to Python BM25

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteProvider.init() not called")
        return self._conn

    async def save_run(self, run: RunRecord) -> None:
        # Use named columns rather than positional VALUES so column order in
        # the schema can drift without breaking inserts (and so lightweight
        # ALTER-added columns work).
        #
        # INSERT OR REPLACE so the voice-parity handler (ADR 050 D1) can
        # re-save a RunRecord with voice-specific fields (modality, stt/tts
        # latency + cost) after the Executor's initial save.
        await self._db.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, job_id, tenant_id, agent, agent_version, prompt_hash,
                provider, provider_version, pricing_version, status,
                input, output, metrics, error, created_at,
                workflow_run_id, node_id, thread_id, skill_calls, turns,
                modality, stt_latency_ms, tts_latency_ms, audio_duration_s,
                stt_cost_usd, tts_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                json.dumps(run.input),
                json.dumps(run.output) if run.output is not None else None,
                run.metrics.model_dump_json(),
                run.error.model_dump_json() if run.error else None,
                run.created_at.isoformat(),
                run.workflow_run_id,
                run.node_id,
                run.thread_id,
                # ADR 024 D2 — per-step retention as JSON arrays.
                json.dumps([c.model_dump() for c in run.skill_calls]),
                json.dumps([t.model_dump() for t in run.turns]),
                # ADR 050 D1 — voice-specific fields (all None for text runs).
                run.modality,
                run.stt_latency_ms,
                run.tts_latency_ms,
                run.audio_duration_s,
                run.stt_cost_usd,
                run.tts_cost_usd,
            ),
        )
        await self._db.commit()

    async def save_failure(self, f: FailureRecord) -> None:
        await self._db.execute(
            "INSERT INTO failures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f.failure_id,
                f.run_id,
                f.tenant_id,
                f.agent,
                f.failure_type,
                f.message,
                int(f.retryable),
                f.created_at.isoformat(),
            ),
        )
        await self._db.commit()

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
        params: list[object] = []
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if workflow_run_id:
            clauses.append("workflow_run_id = ?")
            params.append(workflow_run_id)
        sql = "SELECT * FROM runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]

    async def save_eval(self, e: EvalRecord) -> None:
        # Explicit column list (not positional) so the additive
        # ``dimension_means`` column (item 24, appended by the migration) is
        # written by name — robust to column ordering. NULL when the record
        # carries no per-dimension means (legacy / exact-match datasets).
        await self._db.execute(
            """
            INSERT INTO evals (
                eval_id, tenant_id, agent, agent_version, dataset_hash,
                judge_method, judge_provider, runs_per_case, gate_mode,
                threshold, mean_score, pass_rate, sample_count,
                total_cost_usd, created_at, dimension_means
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                e.created_at.isoformat(),
                json.dumps(e.dimension_means) if e.dimension_means is not None else None,
            ),
        )
        await self._db.commit()

    async def list_evals(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        sql = "SELECT * FROM evals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_eval(r) for r in rows]

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        # tenant_id in WHERE is the SQL-layer enforcement; a caller can't
        # read another tenant's run even by guessing the run_id.
        async with self._db.execute(
            "SELECT * FROM runs WHERE run_id = ? AND tenant_id = ? LIMIT 1",
            (run_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_run(row) if row else None

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        async with self._db.execute(
            "SELECT * FROM workflow_runs WHERE workflow_run_id = ? AND tenant_id = ? LIMIT 1",
            (workflow_run_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_run(row) if row else None

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        async with self._db.execute(
            "SELECT * FROM evals WHERE eval_id = ? AND tenant_id = ? LIMIT 1",
            (eval_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_eval(row) if row else None

    # ------------------------------------------------------------------
    # Observability insights (ADR 047) — append-only.
    # ------------------------------------------------------------------

    async def save_insight(self, insight: ObservabilityInsight) -> None:
        # INSERT-only (never UPDATE): a re-run for the same day appends a new
        # row keyed by the unique ``id``; the read methods take the latest.
        await self._db.execute(
            """
            INSERT INTO observability_insights (
                id, tenant_id, project_id, date, health_score,
                anomalies, top_failures, usage_rollup, trends,
                narrative_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                insight.id,
                insight.tenant_id,
                insight.project_id,
                insight.date.isoformat(),
                insight.health_score,
                json.dumps(insight.anomalies),
                json.dumps(insight.top_failures),
                json.dumps(insight.usage_rollup),
                json.dumps(insight.trends),
                insight.narrative_digest,
                insight.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_insight(
        self, tenant_id: str, project_id: str, day: date
    ) -> ObservabilityInsight | None:
        # Latest-per-day: ORDER BY created_at DESC LIMIT 1 collapses
        # append-only re-runs to the newest row. tenant_id in WHERE is the
        # SQL-layer no-leak enforcement.
        async with self._db.execute(
            """
            SELECT * FROM observability_insights
            WHERE tenant_id = ? AND project_id = ? AND date = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (tenant_id, project_id, day.isoformat()),
        ) as cur:
            row = await cur.fetchone()
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
        clauses = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if since is not None:
            clauses.append("date >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("date <= ?")
            params.append(until.isoformat())
        sql = (
            "SELECT * FROM observability_insights WHERE "
            + " AND ".join(clauses)
            + " ORDER BY date DESC, created_at DESC"
        )
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        # Collapse append-only re-runs to the latest row per (project, date).
        # Rows arrive newest-created first, so the first sighting of a
        # (project, date) is the latest one.
        seen: set[tuple[str, str]] = set()
        out: list[ObservabilityInsight] = []
        for r in rows:
            key = (r["project_id"], r["date"])
            if key in seen:
                continue
            seen.add(key)
            out.append(_row_to_insight(r))
            if len(out) >= limit:
                break
        return out

    async def save_bench(self, b: BenchRecord) -> None:
        await self._db.execute(
            "INSERT INTO bench VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                b.bench_id,
                b.tenant_id,
                b.agent,
                b.agent_version,
                json.dumps(b.input),
                b.judge_method.value if b.judge_method else None,
                b.judge_provider,
                b.runs_per_model,
                b.gate_mode,
                json.dumps([m.model_dump() for m in b.models]),
                b.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_bench(self, bench_id: str, *, tenant_id: str) -> BenchRecord | None:
        async with self._db.execute(
            "SELECT * FROM bench WHERE bench_id = ? AND tenant_id = ? LIMIT 1",
            (bench_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_bench(row) if row else None

    async def list_bench(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[BenchRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        sql = "SELECT * FROM bench"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_bench(r) for r in rows]

    # ------------------------------------------------------------------
    # Audit records (Claude-orchestrated read-only audit pipeline).
    #
    # One immutable row per terminal audit. Audit data is JSON-encoded
    # (categories + findings) — same strategy as bench.models /
    # workflow_runs.initial_state. Read-only on the application side:
    # the audit pipeline NEVER calls save_agent_bundle / save_kb_chunk /
    # save_eval; the only write it makes is here.
    # ------------------------------------------------------------------

    async def save_audit(self, a: AuditRecord) -> None:
        await self._db.execute(
            "INSERT INTO audits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.audit_id,
                a.tenant_id,
                a.scope_kind,
                a.scope_id,
                json.dumps(a.categories),
                a.severity_floor.value,
                a.model,
                a.budget_usd,
                json.dumps([f.model_dump() for f in a.findings]),
                1 if a.partial else 0,
                a.tokens_used,
                a.cost_usd,
                a.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_audit(self, audit_id: str, *, tenant_id: str) -> AuditRecord | None:
        async with self._db.execute(
            "SELECT * FROM audits WHERE audit_id = ? AND tenant_id = ? LIMIT 1",
            (audit_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_audit(row) if row else None

    async def list_audits(
        self,
        *,
        tenant_id: str | None = None,
        scope_id: str | None = None,
        limit: int = 20,
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if scope_id:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        sql = "SELECT * FROM audits"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_audit(r) for r in rows]

    # ------------------------------------------------------------------
    # Eval schedules (ADR 016 D2)
    # ------------------------------------------------------------------

    async def save_eval_schedule(self, schedule: EvalSchedule) -> None:
        # Upsert on the (tenant_id, agent) primary key.
        await self._db.execute(
            """
            INSERT INTO eval_schedules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, agent) DO UPDATE SET
                cadence_seconds = excluded.cadence_seconds,
                enabled = excluded.enabled,
                mock = excluded.mock,
                runs = excluded.runs,
                gate_mode = excluded.gate_mode,
                gate = excluded.gate,
                objective = excluded.objective,
                regression_tolerance = excluded.regression_tolerance,
                baseline_id = excluded.baseline_id,
                notify_email = excluded.notify_email,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                last_enqueued_at = excluded.last_enqueued_at
            """,
            (
                schedule.tenant_id,
                schedule.agent,
                schedule.cadence_seconds,
                int(schedule.enabled),
                int(schedule.mock),
                schedule.runs,
                schedule.gate_mode,
                schedule.gate,
                schedule.objective,
                schedule.regression_tolerance,
                schedule.baseline_id,
                schedule.notify_email,
                schedule.created_by,
                schedule.created_at.isoformat(),
                schedule.last_enqueued_at.isoformat() if schedule.last_enqueued_at else None,
            ),
        )
        await self._db.commit()

    async def get_eval_schedule(self, agent: str, *, tenant_id: str) -> EvalSchedule | None:
        async with self._db.execute(
            "SELECT * FROM eval_schedules WHERE agent = ? AND tenant_id = ? LIMIT 1",
            (agent, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_eval_schedule(row) if row else None

    async def list_eval_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[EvalSchedule]:
        sql = "SELECT * FROM eval_schedules"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_eval_schedule(r) for r in rows]

    async def delete_eval_schedule(self, agent: str, *, tenant_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM eval_schedules WHERE agent = ? AND tenant_id = ?",
            (agent, tenant_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def touch_eval_schedule(
        self,
        agent: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        await self._db.execute(
            "UPDATE eval_schedules SET last_enqueued_at = ? WHERE agent = ? AND tenant_id = ?",
            (last_enqueued_at.isoformat(), agent, tenant_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Job schedules (ADR 017 D2)
    # ------------------------------------------------------------------

    async def save_job_schedule(self, schedule: JobSchedule) -> None:
        # Upsert on the (tenant_id, name) primary key.
        await self._db.execute(
            """
            INSERT INTO job_schedules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, name) DO UPDATE SET
                kind = excluded.kind,
                target = excluded.target,
                cadence_seconds = excluded.cadence_seconds,
                enabled = excluded.enabled,
                input = excluded.input,
                notify_email = excluded.notify_email,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                last_enqueued_at = excluded.last_enqueued_at
            """,
            (
                schedule.tenant_id,
                schedule.name,
                schedule.kind.value,
                schedule.target,
                schedule.cadence_seconds,
                int(schedule.enabled),
                json.dumps(schedule.input),
                schedule.notify_email,
                schedule.created_by,
                schedule.created_at.isoformat(),
                schedule.last_enqueued_at.isoformat() if schedule.last_enqueued_at else None,
            ),
        )
        await self._db.commit()

    async def get_job_schedule(self, name: str, *, tenant_id: str) -> JobSchedule | None:
        async with self._db.execute(
            "SELECT * FROM job_schedules WHERE name = ? AND tenant_id = ? LIMIT 1",
            (name, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job_schedule(row) if row else None

    async def list_job_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[JobSchedule]:
        sql = "SELECT * FROM job_schedules"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_job_schedule(r) for r in rows]

    async def delete_job_schedule(self, name: str, *, tenant_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM job_schedules WHERE name = ? AND tenant_id = ?",
            (name, tenant_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def touch_job_schedule(
        self,
        name: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        await self._db.execute(
            "UPDATE job_schedules SET last_enqueued_at = ? WHERE name = ? AND tenant_id = ?",
            (last_enqueued_at.isoformat(), name, tenant_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Triggers (ADR 017 D2 — inbound event/webhook → enqueue a job)
    # ------------------------------------------------------------------

    async def save_trigger(self, trigger: Trigger) -> None:
        # Upsert on the (tenant_id, name) management key.
        await self._db.execute(
            """
            INSERT INTO triggers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, name) DO UPDATE SET
                trigger_id = excluded.trigger_id,
                kind = excluded.kind,
                target = excluded.target,
                secret_hash = excluded.secret_hash,
                salt = excluded.salt,
                input_defaults = excluded.input_defaults,
                enabled = excluded.enabled,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                last_fired_at = excluded.last_fired_at
            """,
            (
                trigger.tenant_id,
                trigger.name,
                trigger.trigger_id,
                trigger.kind.value,
                trigger.target,
                trigger.secret_hash,
                trigger.salt,
                json.dumps(trigger.input_defaults),
                int(trigger.enabled),
                trigger.created_by,
                trigger.created_at.isoformat(),
                trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
            ),
        )
        await self._db.commit()

    async def get_trigger(self, name: str, *, tenant_id: str) -> Trigger | None:
        async with self._db.execute(
            "SELECT * FROM triggers WHERE name = ? AND tenant_id = ? LIMIT 1",
            (name, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_trigger(row) if row else None

    async def get_trigger_by_id(self, trigger_id: str) -> Trigger | None:
        async with self._db.execute(
            "SELECT * FROM triggers WHERE trigger_id = ? LIMIT 1",
            (trigger_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_trigger(row) if row else None

    async def list_triggers(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[Trigger]:
        sql = "SELECT * FROM triggers"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_trigger(r) for r in rows]

    async def delete_trigger(self, name: str, *, tenant_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM triggers WHERE name = ? AND tenant_id = ?",
            (name, tenant_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def touch_trigger(self, trigger_id: str, *, last_fired_at: datetime) -> None:
        await self._db.execute(
            "UPDATE triggers SET last_fired_at = ? WHERE trigger_id = ?",
            (last_fired_at.isoformat(), trigger_id),
        )
        await self._db.commit()

    async def get_trigger_delivery(self, trigger_id: str, delivery_id: str) -> str | None:
        async with self._db.execute(
            "SELECT job_id FROM trigger_deliveries "
            "WHERE trigger_id = ? AND delivery_id = ? LIMIT 1",
            (trigger_id, delivery_id),
        ) as cur:
            row = await cur.fetchone()
        return row["job_id"] if row else None

    async def record_trigger_delivery(self, trigger_id: str, delivery_id: str, job_id: str) -> bool:
        # INSERT OR IGNORE on the (trigger_id, delivery_id) PRIMARY KEY: the
        # row is written only if absent, so a concurrent double-delivery
        # races atomically to one winner. cur.rowcount is 1 on a fresh
        # insert, 0 when the row already existed.
        cur = await self._db.execute(
            "INSERT OR IGNORE INTO trigger_deliveries "
            "(trigger_id, delivery_id, job_id, created_at) VALUES (?, ?, ?, ?)",
            (trigger_id, delivery_id, job_id, datetime.now(UTC).isoformat()),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def get_run_submission(self, tenant_id: str, idempotency_key: str) -> str | None:
        async with self._db.execute(
            "SELECT job_id FROM run_submissions "
            "WHERE tenant_id = ? AND idempotency_key = ? LIMIT 1",
            (tenant_id, idempotency_key),
        ) as cur:
            row = await cur.fetchone()
        return row["job_id"] if row else None

    async def get_run_submission_record(
        self, tenant_id: str, idempotency_key: str
    ) -> RunSubmissionRecord | None:
        from movate.storage.base import RunSubmissionRecord  # noqa: PLC0415

        async with self._db.execute(
            "SELECT job_id, request_hash FROM run_submissions "
            "WHERE tenant_id = ? AND idempotency_key = ? LIMIT 1",
            (tenant_id, idempotency_key),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return RunSubmissionRecord(job_id=row["job_id"], request_hash=row["request_hash"])

    async def record_run_submission(
        self,
        tenant_id: str,
        idempotency_key: str,
        job_id: str,
        request_hash: str | None = None,
    ) -> bool:
        # INSERT OR IGNORE on the (tenant_id, idempotency_key) PRIMARY KEY: the
        # row is written only if absent, so a concurrent retry races atomically
        # to one winner. cur.rowcount is 1 on a fresh insert, 0 when the row
        # already existed. ``request_hash`` (item 37 payload-conflict guard) is
        # the canonical-payload fingerprint; None preserves the pre-guard shape.
        cur = await self._db.execute(
            "INSERT OR IGNORE INTO run_submissions "
            "(tenant_id, idempotency_key, job_id, created_at, request_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                tenant_id,
                idempotency_key,
                job_id,
                datetime.now(UTC).isoformat(),
                request_hash,
            ),
        )
        await self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Tenant provider keys (ADR 018 — per-tenant BYOK provider credentials)
    # ------------------------------------------------------------------

    async def save_tenant_provider_key(self, key: TenantProviderKey) -> None:
        # Upsert on the (tenant_id, provider) primary key — a re-set rotates.
        await self._db.execute(
            """
            INSERT INTO tenant_provider_keys (
                tenant_id, provider, ciphertext, fingerprint,
                created_by, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, provider) DO UPDATE SET
                ciphertext = excluded.ciphertext,
                fingerprint = excluded.fingerprint,
                created_by = excluded.created_by,
                updated_at = excluded.updated_at
            """,
            (
                key.tenant_id,
                key.provider,
                key.ciphertext,
                key.fingerprint,
                key.created_by,
                key.created_at.isoformat(),
                key.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_tenant_provider_key(
        self, provider: str, *, tenant_id: str
    ) -> TenantProviderKey | None:
        async with self._db.execute(
            "SELECT * FROM tenant_provider_keys WHERE provider = ? AND tenant_id = ? LIMIT 1",
            (provider, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_tenant_provider_key(row) if row else None

    async def list_tenant_provider_keys(self, *, tenant_id: str) -> list[TenantProviderKey]:
        async with self._db.execute(
            "SELECT * FROM tenant_provider_keys WHERE tenant_id = ? ORDER BY provider ASC",
            (tenant_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_tenant_provider_key(r) for r in rows]

    async def list_all_tenant_provider_keys(
        self, *, limit: int = 100_000
    ) -> list[TenantProviderKey]:
        # item 26 (DR export) — fleet-wide, operator-only. Stable order.
        async with self._db.execute(
            "SELECT * FROM tenant_provider_keys ORDER BY tenant_id, provider LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_tenant_provider_key(r) for r in rows]

    async def delete_tenant_provider_key(self, provider: str, *, tenant_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM tenant_provider_keys WHERE provider = ? AND tenant_id = ?",
            (provider, tenant_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Canary configs (ADR 016 D3 — champion/challenger rollout)
    # ------------------------------------------------------------------

    async def save_canary_config(self, config: CanaryConfig) -> None:
        # Upsert on the (tenant_id, agent) primary key.
        await self._db.execute(
            """
            INSERT INTO canary_configs (
                tenant_id, agent, challenger_version, champion_version, weight,
                sticky, enabled, auto_promote, eval_gate, created_by,
                created_at, updated_at, auto_rollback
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, agent) DO UPDATE SET
                challenger_version = excluded.challenger_version,
                champion_version = excluded.champion_version,
                weight = excluded.weight,
                sticky = excluded.sticky,
                enabled = excluded.enabled,
                auto_promote = excluded.auto_promote,
                eval_gate = excluded.eval_gate,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                auto_rollback = excluded.auto_rollback
            """,
            (
                config.tenant_id,
                config.agent,
                config.challenger_version,
                config.champion_version,
                config.weight,
                int(config.sticky),
                int(config.enabled),
                int(config.auto_promote),
                config.eval_gate,
                config.created_by,
                config.created_at.isoformat(),
                config.updated_at.isoformat(),
                int(config.auto_rollback),
            ),
        )
        await self._db.commit()

    async def get_canary_config(self, agent: str, *, tenant_id: str) -> CanaryConfig | None:
        async with self._db.execute(
            "SELECT * FROM canary_configs WHERE agent = ? AND tenant_id = ? LIMIT 1",
            (agent, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_canary_config(row) if row else None

    async def list_canary_configs(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[CanaryConfig]:
        sql = "SELECT * FROM canary_configs"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_canary_config(r) for r in rows]

    async def delete_canary_config(self, agent: str, *, tenant_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM canary_configs WHERE agent = ? AND tenant_id = ?",
            (agent, tenant_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Agent registry (ADR 014 D1)
    # ------------------------------------------------------------------

    async def save_agent_bundle(self, bundle: AgentBundleRecord) -> None:
        await self._db.execute(
            "INSERT INTO agent_bundles VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.name,
                bundle.tenant_id,
                bundle.version,
                bundle.created_by,
                bundle.content_hash,
                json.dumps(bundle.files),
                bundle.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> AgentBundleRecord | None:
        if version is not None:
            sql = (
                "SELECT * FROM agent_bundles "
                "WHERE name = ? AND tenant_id = ? AND version = ? LIMIT 1"
            )
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            # version=None → latest by created_at.
            sql = (
                "SELECT * FROM agent_bundles WHERE name = ? AND tenant_id = ? "
                "ORDER BY created_at DESC LIMIT 1"
            )
            params = (name, tenant_id)
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return _row_to_agent_bundle(row) if row else None

    async def list_agents(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[AgentBundleRecord]:
        # Latest version per name, newest-first. The correlated subquery
        # keeps only each name's most-recently-published row.
        sql = """
            SELECT b.* FROM agent_bundles b
            WHERE b.tenant_id = ?
              AND b.created_at = (
                  SELECT MAX(b2.created_at) FROM agent_bundles b2
                  WHERE b2.tenant_id = b.tenant_id AND b2.name = b.name
              )
            ORDER BY b.created_at DESC
            LIMIT ?
        """
        async with self._db.execute(sql, (tenant_id, limit)) as cur:
            rows = await cur.fetchall()
        return [_row_to_agent_bundle(r) for r in rows]

    async def list_agent_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[AgentBundleRecord]:
        async with self._db.execute(
            "SELECT * FROM agent_bundles WHERE name = ? AND tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (name, tenant_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_agent_bundle(r) for r in rows]

    async def list_all_agent_bundles(self, *, limit: int = 100_000) -> list[AgentBundleRecord]:
        # item 26 (DR export) — every version, every tenant. Stable order.
        async with self._db.execute(
            "SELECT * FROM agent_bundles ORDER BY tenant_id, name, created_at LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_agent_bundle(r) for r in rows]

    async def delete_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        if version is not None:
            sql = "DELETE FROM agent_bundles WHERE name = ? AND tenant_id = ? AND version = ?"
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            sql = "DELETE FROM agent_bundles WHERE name = ? AND tenant_id = ?"
            params = (name, tenant_id)
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Agent runtime state (ADR 090 D1/D2) — mutable per-(tenant, name) status.
    # Upsert semantics (one current row per agent); absent ⇒ ACTIVE upstream.
    # ------------------------------------------------------------------

    async def get_agent_state(self, name: str, *, tenant_id: str) -> AgentRuntimeState | None:
        async with self._db.execute(
            "SELECT * FROM agent_runtime_state WHERE name = ? AND tenant_id = ? LIMIT 1",
            (name, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_agent_state(row) if row else None

    async def set_agent_state(self, state: AgentRuntimeState) -> None:
        await self._db.execute(
            "INSERT INTO agent_runtime_state (tenant_id, name, status, updated_at, "
            "updated_by, note) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, name) DO UPDATE SET "
            "status = excluded.status, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by, note = excluded.note",
            (
                state.tenant_id,
                state.name,
                state.status.value,
                state.updated_at.isoformat(),
                state.updated_by,
                state.note,
            ),
        )
        await self._db.commit()

    async def list_agent_states(self, *, tenant_id: str) -> list[AgentRuntimeState]:
        async with self._db.execute(
            "SELECT * FROM agent_runtime_state WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_agent_state(r) for r in rows]

    # ------------------------------------------------------------------
    # Skills registry (ADR 060 D1) — mirrors save/get/list/delete agent bundle.
    # ------------------------------------------------------------------

    async def save_skill(self, skill: SkillRecord) -> None:
        await self._db.execute(
            "INSERT INTO skills VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                skill.name,
                skill.tenant_id,
                skill.version,
                skill.created_by,
                skill.content_hash,
                skill.description,
                json.dumps(skill.files),
                skill.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_skill(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> SkillRecord | None:
        if version is not None:
            sql = "SELECT * FROM skills WHERE name = ? AND tenant_id = ? AND version = ? LIMIT 1"
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            sql = (
                "SELECT * FROM skills WHERE name = ? AND tenant_id = ? "
                "ORDER BY created_at DESC LIMIT 1"
            )
            params = (name, tenant_id)
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return _row_to_skill(row) if row else None

    async def list_skills(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[SkillRecord]:
        sql = """
            SELECT s.* FROM skills s
            WHERE s.tenant_id = ?
              AND s.created_at = (
                  SELECT MAX(s2.created_at) FROM skills s2
                  WHERE s2.tenant_id = s.tenant_id AND s2.name = s.name
              )
            ORDER BY s.created_at DESC
            LIMIT ?
        """
        async with self._db.execute(sql, (tenant_id, limit)) as cur:
            rows = await cur.fetchall()
        return [_row_to_skill(r) for r in rows]

    async def list_skill_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[SkillRecord]:
        async with self._db.execute(
            "SELECT * FROM skills WHERE name = ? AND tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (name, tenant_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_skill(r) for r in rows]

    async def delete_skill(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        if version is not None:
            sql = "DELETE FROM skills WHERE name = ? AND tenant_id = ? AND version = ?"
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            sql = "DELETE FROM skills WHERE name = ? AND tenant_id = ?"
            params = (name, tenant_id)
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Contexts registry (ADR 060 D1) — mirrors the skills surface (body, not files).
    # ------------------------------------------------------------------

    async def save_context(self, context: ContextRecord) -> None:
        await self._db.execute(
            "INSERT INTO contexts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                context.name,
                context.tenant_id,
                context.version,
                context.created_by,
                context.content_hash,
                context.description,
                context.body,
                context.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_context(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> ContextRecord | None:
        if version is not None:
            sql = "SELECT * FROM contexts WHERE name = ? AND tenant_id = ? AND version = ? LIMIT 1"
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            sql = (
                "SELECT * FROM contexts WHERE name = ? AND tenant_id = ? "
                "ORDER BY created_at DESC LIMIT 1"
            )
            params = (name, tenant_id)
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return _row_to_context(row) if row else None

    async def list_contexts(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[ContextRecord]:
        sql = """
            SELECT c.* FROM contexts c
            WHERE c.tenant_id = ?
              AND c.created_at = (
                  SELECT MAX(c2.created_at) FROM contexts c2
                  WHERE c2.tenant_id = c.tenant_id AND c2.name = c.name
              )
            ORDER BY c.created_at DESC
            LIMIT ?
        """
        async with self._db.execute(sql, (tenant_id, limit)) as cur:
            rows = await cur.fetchall()
        return [_row_to_context(r) for r in rows]

    async def list_context_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[ContextRecord]:
        async with self._db.execute(
            "SELECT * FROM contexts WHERE name = ? AND tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (name, tenant_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_context(r) for r in rows]

    async def delete_context(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        if version is not None:
            sql = "DELETE FROM contexts WHERE name = ? AND tenant_id = ? AND version = ?"
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            sql = "DELETE FROM contexts WHERE name = ? AND tenant_id = ?"
            params = (name, tenant_id)
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Projects (ADR 040)
    # ------------------------------------------------------------------

    async def create_project(self, project: Project) -> Project:
        try:
            await self._db.execute(
                "INSERT INTO projects (project_id, tenant_id, name, description, "
                "owner_principal_id, created_at, updated_at, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project.project_id,
                    project.tenant_id,
                    project.name,
                    project.description,
                    project.owner_principal_id,
                    project.created_at.isoformat(),
                    project.updated_at.isoformat(),
                    project.archived_at.isoformat() if project.archived_at else None,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            # UNIQUE(tenant_id, name) collision OR duplicate project_id.
            raise ValueError(
                f"project ({project.tenant_id!r}, {project.name!r}) already exists"
            ) from exc
        await self._db.commit()
        return project

    async def get_project(self, tenant_id: str, project_id: str) -> Project | None:
        async with self._db.execute(
            "SELECT * FROM projects WHERE project_id = ? AND tenant_id = ? LIMIT 1",
            (project_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_project(row) if row else None

    async def get_project_by_name(self, tenant_id: str, name: str) -> Project | None:
        async with self._db.execute(
            "SELECT * FROM projects WHERE tenant_id = ? AND name = ? LIMIT 1",
            (tenant_id, name),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_project(row) if row else None

    async def list_projects(
        self,
        tenant_id: str,
        *,
        include_archived: bool = False,
        limit: int = 100,
        after_id: str | None = None,
    ) -> list[Project]:
        clauses = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if not include_archived:
            clauses.append("archived_at IS NULL")
        # Keyset cursor: walk strictly past the last project_id from the
        # prior page in the (created_at DESC, project_id DESC) order. Match
        # the ORDER BY tuple in the WHERE so we never skip or repeat a row.
        if after_id is not None:
            async with self._db.execute(
                "SELECT created_at, project_id FROM projects WHERE project_id = ?",
                (after_id,),
            ) as cur:
                cursor_row = await cur.fetchone()
            if cursor_row is not None:
                clauses.append(
                    "(created_at, project_id) < (?, ?)",
                )
                params.extend([cursor_row["created_at"], cursor_row["project_id"]])
        sql = (
            "SELECT * FROM projects WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, project_id DESC LIMIT ?"
        )
        params.append(limit)
        async with self._db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [_row_to_project(r) for r in rows]

    async def update_project(
        self,
        tenant_id: str,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Project | None:
        # Return current row if no fields were provided (idempotent no-op).
        if name is None and description is None:
            return await self.get_project(tenant_id, project_id)
        sets = ["updated_at = ?"]
        params: list[object] = [datetime.now(UTC).isoformat()]
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        params.extend([project_id, tenant_id])
        try:
            cur = await self._db.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE project_id = ? AND tenant_id = ?",
                tuple(params),
            )
        except aiosqlite.IntegrityError as exc:
            # Rename collision on UNIQUE(tenant_id, name).
            raise ValueError(
                f"project name {name!r} already exists in tenant {tenant_id!r}"
            ) from exc
        await self._db.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_project(tenant_id, project_id)

    async def archive_project(self, tenant_id: str, project_id: str) -> bool:
        existing = await self.get_project(tenant_id, project_id)
        if existing is None:
            return False
        # D5 + D6: the default project is never archivable. Enforce at the
        # storage layer in addition to the API guard so a buggy code path
        # can't lose the default attachment target.
        if existing.name == _DEFAULT_PROJECT_NAME:
            raise ValueError(f"default project for tenant {tenant_id!r} cannot be archived")
        if existing.archived_at is not None:
            return False
        now = datetime.now(UTC).isoformat()
        cur = await self._db.execute(
            "UPDATE projects SET archived_at = ?, updated_at = ? "
            "WHERE project_id = ? AND tenant_id = ? AND archived_at IS NULL",
            (now, now, project_id, tenant_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

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
                "VALUES (?, ?, ?, ?, ?)",
                (
                    project_id,
                    principal_id,
                    role.value,
                    added_by,
                    datetime.now(UTC).isoformat(),
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ValueError(f"member {principal_id!r} already on project {project_id!r}") from exc
        await self._db.commit()

    async def list_project_members(self, project_id: str) -> list[ProjectMember]:
        async with self._db.execute(
            "SELECT * FROM project_members WHERE project_id = ? "
            "ORDER BY added_at ASC, principal_id ASC",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_project_member(r) for r in rows]

    async def update_project_member(
        self,
        project_id: str,
        principal_id: str,
        *,
        role: ProjectMemberRole,
    ) -> ProjectMember | None:
        cur = await self._db.execute(
            "UPDATE project_members SET role = ? WHERE project_id = ? AND principal_id = ?",
            (role.value, project_id, principal_id),
        )
        await self._db.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_project_member(project_id, principal_id)

    async def remove_project_member(self, project_id: str, principal_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM project_members WHERE project_id = ? AND principal_id = ?",
            (project_id, principal_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def get_project_member(
        self,
        project_id: str,
        principal_id: str,
    ) -> ProjectMember | None:
        async with self._db.execute(
            "SELECT * FROM project_members WHERE project_id = ? AND principal_id = ?",
            (project_id, principal_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_project_member(row) if row else None

    async def attach_agent_to_project(self, project_id: str, agent_name: str) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO project_agents (project_id, agent_name, added_at) "
            "VALUES (?, ?, ?)",
            (project_id, agent_name, datetime.now(UTC).isoformat()),
        )
        await self._db.commit()

    async def detach_agent_from_project(self, project_id: str, agent_name: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM project_agents WHERE project_id = ? AND agent_name = ?",
            (project_id, agent_name),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def list_project_agents(self, project_id: str) -> list[str]:
        async with self._db.execute(
            "SELECT agent_name FROM project_agents WHERE project_id = ? "
            "ORDER BY added_at ASC, agent_name ASC",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [r["agent_name"] for r in rows]

    async def list_projects_for_agent(self, tenant_id: str, agent_name: str) -> list[str]:
        async with self._db.execute(
            "SELECT pa.project_id FROM project_agents pa "
            "JOIN projects p ON p.project_id = pa.project_id "
            "WHERE p.tenant_id = ? AND pa.agent_name = ? "
            "ORDER BY pa.added_at ASC, pa.project_id ASC",
            (tenant_id, agent_name),
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return [r["project_id"] for r in rows]
        # D5: an agent with no explicit attachment is implicitly in the
        # tenant's default project; create it lazily so first-read after the
        # migration matches the ADR's "no backfill" contract.
        default_project = await self.get_or_create_default_project(tenant_id)
        return [default_project.project_id]

    async def attach_workflow_to_project(self, project_id: str, workflow_name: str) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO project_workflows "
            "(project_id, workflow_name, added_at) VALUES (?, ?, ?)",
            (project_id, workflow_name, datetime.now(UTC).isoformat()),
        )
        await self._db.commit()

    async def detach_workflow_from_project(self, project_id: str, workflow_name: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM project_workflows WHERE project_id = ? AND workflow_name = ?",
            (project_id, workflow_name),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def list_project_workflows(self, project_id: str) -> list[str]:
        async with self._db.execute(
            "SELECT workflow_name FROM project_workflows WHERE project_id = ? "
            "ORDER BY added_at ASC, workflow_name ASC",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [r["workflow_name"] for r in rows]

    async def list_projects_for_workflow(self, tenant_id: str, workflow_name: str) -> list[str]:
        async with self._db.execute(
            "SELECT pw.project_id FROM project_workflows pw "
            "JOIN projects p ON p.project_id = pw.project_id "
            "WHERE p.tenant_id = ? AND pw.workflow_name = ? "
            "ORDER BY pw.added_at ASC, pw.project_id ASC",
            (tenant_id, workflow_name),
        ) as cur:
            rows = await cur.fetchall()
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
        # Upsert: a duplicate attach with the same mode is a no-op; a
        # different mode updates in place (the share-promotion path).
        await self._db.execute(
            "INSERT INTO project_kbs (project_id, kb_id, mode, added_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_id, kb_id) DO UPDATE SET mode = excluded.mode",
            (project_id, kb_id, mode.value, datetime.now(UTC).isoformat()),
        )
        await self._db.commit()

    async def detach_kb_from_project(self, project_id: str, kb_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM project_kbs WHERE project_id = ? AND kb_id = ?",
            (project_id, kb_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def list_project_kbs(self, project_id: str) -> list[tuple[str, ProjectKbMode]]:
        async with self._db.execute(
            "SELECT kb_id, mode FROM project_kbs WHERE project_id = ? "
            "ORDER BY added_at ASC, kb_id ASC",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [(r["kb_id"], ProjectKbMode(r["mode"])) for r in rows]

    async def list_projects_for_kb(self, tenant_id: str, kb_id: str) -> list[str]:
        async with self._db.execute(
            "SELECT pk.project_id FROM project_kbs pk "
            "JOIN projects p ON p.project_id = pk.project_id "
            "WHERE p.tenant_id = ? AND pk.kb_id = ? "
            "ORDER BY pk.added_at ASC, pk.project_id ASC",
            (tenant_id, kb_id),
        ) as cur:
            rows = await cur.fetchall()
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
            # Concurrent create raced us; re-read the winner.
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug, source, tenant_id_key) DO UPDATE SET
                latest_version  = excluded.latest_version,
                name            = excluded.name,
                title           = excluded.title,
                description     = excluded.description,
                tags            = excluded.tags,
                shape           = excluded.shape,
                recommended_for = excluded.recommended_for,
                ratings_summary = excluded.ratings_summary,
                popularity      = excluded.popularity,
                synced_at       = excluded.synced_at
            """,
            (
                entry.slug,
                entry.source.value,
                entry.tenant_id,
                tkey,
                entry.latest_version,
                entry.name,
                entry.title,
                entry.description,
                json.dumps(list(entry.tags)),
                entry.shape,
                entry.recommended_for,
                json.dumps(entry.ratings_summary.model_dump()),
                entry.popularity,
                entry.synced_at.isoformat(),
            ),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Durable workflow registry (ADR 037 D1)
    # ------------------------------------------------------------------

    async def save_workflow_bundle(self, bundle: WorkflowBundleRecord) -> None:
        await self._db.execute(
            "INSERT INTO workflow_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.name,
                bundle.tenant_id,
                bundle.version,
                bundle.created_by,
                bundle.content_hash,
                json.dumps(bundle.files),
                1 if bundle.published else 0,
                bundle.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_catalog_entry(
        self,
        slug: str,
        *,
        source: CatalogSource,
        tenant_id: str | None = None,
    ) -> CatalogEntry | None:
        # For private, NULL tenant_id can never resolve to a stored row (the
        # PK uses a non-null tenant_id_key, and the schema CHECK forbids NULL
        # tenant_id on private). Short-circuit cleanly so callers can pass
        # tenant_id=None for the public namespaces.
        if source is CatalogSource.PRIVATE and tenant_id is None:
            return None
        tkey = _tenant_key(tenant_id if source is CatalogSource.PRIVATE else None)
        async with self._db.execute(
            "SELECT * FROM catalog_entries "
            "WHERE slug = ? AND source = ? AND tenant_id_key = ? LIMIT 1",
            (slug, source.value, tkey),
        ) as cur:
            row = await cur.fetchone()
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
        # Visibility: movate (any tenant) + private (caller) + community.
        clauses: list[str] = [
            "(source = 'movate' OR (source = 'private' AND tenant_id = ?) OR source = 'community')"
        ]
        params: list[object] = [tenant_id]

        if source_filter is not None:
            clauses.append("source = ?")
            params.append(source_filter.value)
        if shape_filter is not None:
            clauses.append("shape = ?")
            params.append(shape_filter)
        if tag_filter is not None:
            # JSON_EACH(tags) iterates the JSON list; the EXISTS subquery
            # asserts membership.
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(catalog_entries.tags) WHERE json_each.value = ?)"
            )
            params.append(tag_filter)
        if q:
            needle = f"%{q.lower()}%"
            clauses.append(
                "(LOWER(name) LIKE ? OR LOWER(title) LIKE ? OR LOWER(description) LIKE ?)"
            )
            params.extend([needle, needle, needle])
        if after_slug is not None:
            clauses.append("slug > ?")
            params.append(after_slug)

        sql = (
            "SELECT * FROM catalog_entries WHERE "
            + " AND ".join(clauses)
            + " ORDER BY slug ASC LIMIT ?"
        )
        params.append(int(limit))
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
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
        async with self._db.execute(
            "SELECT * FROM catalog_entry_versions "
            "WHERE slug = ? AND source = ? AND tenant_id_key = ? "
            "ORDER BY published_at DESC",
            (slug, source.value, tkey),
        ) as cur:
            rows = await cur.fetchall()
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
        async with self._db.execute(
            "SELECT * FROM catalog_entry_versions "
            "WHERE slug = ? AND source = ? AND version = ? AND tenant_id_key = ? "
            "LIMIT 1",
            (slug, source.value, version, tkey),
        ) as cur:
            row = await cur.fetchone()
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
        await self._db.execute(
            """
            INSERT INTO catalog_entry_versions (
                slug, version, source, tenant_id, tenant_id_key,
                bundle_tar, digest, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug, source, version, tenant_id_key) DO UPDATE SET
                bundle_tar = excluded.bundle_tar,
                digest     = excluded.digest
            """,
            (
                slug,
                version,
                source.value,
                tenant_id,
                tkey,
                bundle_tar,
                digest,
                now.isoformat(),
            ),
        )
        await self._db.commit()
        # Re-read to get the stamped published_at (a first insert uses ``now``
        # above; a subsequent upsert preserves the original published_at).
        async with self._db.execute(
            "SELECT * FROM catalog_entry_versions "
            "WHERE slug = ? AND source = ? AND version = ? AND tenant_id_key = ?",
            (slug, source.value, version, tkey),
        ) as cur:
            row = await cur.fetchone()
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
        now = datetime.now(UTC)
        await self._db.execute(
            """
            INSERT INTO catalog_entry_ratings (
                slug, source, tenant_id, rating, comment, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug, source, tenant_id) DO UPDATE SET
                rating     = excluded.rating,
                comment    = excluded.comment,
                created_at = excluded.created_at
            """,
            (slug, source.value, tenant_id, int(rating), comment, now.isoformat()),
        )
        # Recompute summary from the ratings table for this (slug, source).
        async with self._db.execute(
            "SELECT COUNT(*) AS c, AVG(rating) AS a FROM catalog_entry_ratings "
            "WHERE slug = ? AND source = ?",
            (slug, source.value),
        ) as cur:
            agg = await cur.fetchone()
        assert agg is not None  # COUNT(*) always yields a row
        count = int(agg["c"] or 0)
        avg = float(agg["a"] or 0.0)
        summary = CatalogRatingsSummary(count=count, avg=avg)
        # Update the entry's cached summary in place (public namespace
        # rows use tenant_id_key='__public__'; we update wherever the slug
        # exists for this source — both private (per-tenant) and public).
        await self._db.execute(
            "UPDATE catalog_entries SET ratings_summary = ? WHERE slug = ? AND source = ?",
            (json.dumps(summary.model_dump()), slug, source.value),
        )
        await self._db.commit()
        return summary

    async def get_catalog_sync_watermark(self, source: CatalogSource) -> datetime | None:
        async with self._db.execute(
            "SELECT last_synced_at FROM catalog_sync_watermark WHERE source = ?",
            (source.value,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["last_synced_at"])

    async def set_catalog_sync_watermark(self, source: CatalogSource, ts: datetime) -> None:
        await self._db.execute(
            """
            INSERT INTO catalog_sync_watermark (source, last_synced_at)
            VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET last_synced_at = excluded.last_synced_at
            """,
            (source.value, ts.isoformat()),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Durable workflow registry reads + publish (ADR 037 D1)
    # ------------------------------------------------------------------

    async def get_workflow_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> WorkflowBundleRecord | None:
        if version is not None:
            sql = (
                "SELECT * FROM workflow_bundles "
                "WHERE name = ? AND tenant_id = ? AND version = ? LIMIT 1"
            )
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            sql = (
                "SELECT * FROM workflow_bundles WHERE name = ? AND tenant_id = ? "
                "ORDER BY created_at DESC LIMIT 1"
            )
            params = (name, tenant_id)
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_bundle(row) if row else None

    async def list_workflows(
        self,
        *,
        tenant_id: str,
        published_only: bool = False,
        limit: int = 100,
    ) -> list[WorkflowBundleRecord]:
        # Latest version per name, newest-first. Matches the agent_bundles
        # query shape so a backend swap behaves identically.
        if published_only:
            # Narrow to names that have at least one published version, but
            # still return the *latest* row for the name so the caller can
            # detect "blessed != latest" drift.
            sql = """
                SELECT b.* FROM workflow_bundles b
                WHERE b.tenant_id = ?
                  AND b.created_at = (
                      SELECT MAX(b2.created_at) FROM workflow_bundles b2
                      WHERE b2.tenant_id = b.tenant_id AND b2.name = b.name
                  )
                  AND EXISTS (
                      SELECT 1 FROM workflow_bundles b3
                      WHERE b3.tenant_id = b.tenant_id
                        AND b3.name = b.name
                        AND b3.published = 1
                  )
                ORDER BY b.created_at DESC
                LIMIT ?
            """
        else:
            sql = """
                SELECT b.* FROM workflow_bundles b
                WHERE b.tenant_id = ?
                  AND b.created_at = (
                      SELECT MAX(b2.created_at) FROM workflow_bundles b2
                      WHERE b2.tenant_id = b.tenant_id AND b2.name = b.name
                  )
                ORDER BY b.created_at DESC
                LIMIT ?
            """
        async with self._db.execute(sql, (tenant_id, limit)) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow_bundle(r) for r in rows]

    async def list_workflow_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[WorkflowBundleRecord]:
        async with self._db.execute(
            "SELECT * FROM workflow_bundles WHERE name = ? AND tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (name, tenant_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow_bundle(r) for r in rows]

    async def list_all_workflow_bundles(
        self, *, limit: int = 100_000
    ) -> list[WorkflowBundleRecord]:
        async with self._db.execute(
            "SELECT * FROM workflow_bundles ORDER BY tenant_id, name, created_at LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow_bundle(r) for r in rows]

    async def delete_workflow_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        if version is not None:
            sql = "DELETE FROM workflow_bundles WHERE name = ? AND tenant_id = ? AND version = ?"
            params: tuple[object, ...] = (name, tenant_id, version)
        else:
            sql = "DELETE FROM workflow_bundles WHERE name = ? AND tenant_id = ?"
            params = (name, tenant_id)
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return cur.rowcount

    async def publish_workflow_version(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str,
    ) -> bool:
        # ADR 037 D1: at most one row per (tenant, name) is published. Mark
        # the target, then clear every other version of the same name in the
        # same tenant. We probe first so the False/404 case is unambiguous.
        async with self._db.execute(
            "SELECT 1 FROM workflow_bundles "
            "WHERE tenant_id = ? AND name = ? AND version = ? LIMIT 1",
            (tenant_id, name, version),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        await self._db.execute(
            "UPDATE workflow_bundles SET published = 1 "
            "WHERE tenant_id = ? AND name = ? AND version = ?",
            (tenant_id, name, version),
        )
        await self._db.execute(
            "UPDATE workflow_bundles SET published = 0 "
            "WHERE tenant_id = ? AND name = ? AND version <> ?",
            (tenant_id, name, version),
        )
        await self._db.commit()
        return True

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None:
        # Upsert on the workflow_run_id PRIMARY KEY: the runner saves a row
        # when a run reaches a terminal/paused state, and a resume (ADR 017
        # D5, PR 2) re-saves the SAME workflow_run_id when the paused run
        # continues to completion (or re-pauses). The signal endpoint also
        # persists the merged checkpoint back under the same id. ON CONFLICT
        # DO UPDATE makes save idempotent on the id — the latest write wins,
        # so a resumed run UPDATES its row rather than violating the PK.
        # A first-time SUCCESS/ERROR/PAUSED save takes the INSERT path
        # unchanged (no existing row → no conflict).
        await self._db.execute(
            """
            INSERT INTO workflow_runs (
                workflow_run_id, tenant_id, workflow, workflow_version,
                status, initial_state, final_state, error_node_id, error,
                created_at, paused_node_id, paused_state, human_task, runtime
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_run_id) DO UPDATE SET
                tenant_id        = excluded.tenant_id,
                workflow         = excluded.workflow,
                workflow_version = excluded.workflow_version,
                status           = excluded.status,
                initial_state    = excluded.initial_state,
                final_state      = excluded.final_state,
                error_node_id    = excluded.error_node_id,
                error            = excluded.error,
                created_at       = excluded.created_at,
                paused_node_id   = excluded.paused_node_id,
                paused_state     = excluded.paused_state,
                human_task       = excluded.human_task,
                runtime          = excluded.runtime
            """,
            (
                w.workflow_run_id,
                w.tenant_id,
                w.workflow,
                w.workflow_version,
                w.status.value,
                json.dumps(w.initial_state),
                json.dumps(w.final_state) if w.final_state is not None else None,
                w.error_node_id,
                w.error.model_dump_json() if w.error else None,
                w.created_at.isoformat(),
                w.paused_node_id,
                json.dumps(w.paused_state) if w.paused_state is not None else None,
                json.dumps(w.human_task) if w.human_task is not None else None,
                w.runtime,
            ),
        )
        await self._db.commit()

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if workflow:
            clauses.append("workflow = ?")
            params.append(workflow)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        sql = "SELECT * FROM workflow_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Jobs (v0.5)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Batches (item 17 — batch inference)
    # ------------------------------------------------------------------

    async def save_batch(self, batch: BatchRecord) -> None:
        await self._db.execute(
            "INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?)",
            (
                batch.batch_id,
                batch.tenant_id,
                batch.agent,
                batch.total,
                batch.created_by,
                batch.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_batch(self, batch_id: str, *, tenant_id: str) -> BatchRecord | None:
        async with self._db.execute(
            "SELECT * FROM batches WHERE batch_id = ? AND tenant_id = ?",
            (batch_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_batch(row) if row else None

    async def list_batches(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 20,
    ) -> list[BatchRecord]:
        sql = "SELECT * FROM batches"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.tenant_id,
                job.kind.value,
                job.target,
                job.status.value,
                json.dumps(job.input),
                job.result_run_id,
                json.dumps(job.error.model_dump()) if job.error else None,
                job.api_key_id,
                job.created_at.isoformat(),
                job.claimed_at.isoformat() if job.claimed_at else None,
                job.completed_at.isoformat() if job.completed_at else None,
                job.notify_email,
                job.attempt_count,
                job.next_retry_at.isoformat() if job.next_retry_at else None,
                job.thread_id,
                job.target_version,
                job.resume_workflow_run_id,
                job.batch_id,
                # item 32 (ADR 019): W3C trace-context carrier as JSON text.
                # Empty dict {} when OTel was off / no active span at enqueue.
                json.dumps(job.trace_context),
                # item 36 (R4b): cooperative-cancel flag as INTEGER (sqlite has
                # no bool). Always 0 at insert (a fresh job is never
                # pre-cancelled); set later by request_job_cancel for a RUNNING
                # job.
                int(job.cancel_requested),
            ),
        )
        await self._db.commit()

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        async with self._db.execute(
            "SELECT * FROM jobs WHERE job_id = ? AND tenant_id = ?",
            (job_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None

    # claim_next_job needs to re-fetch the just-claimed row by id (the
    # caller's tenant matches by construction), so we use this small
    # internal helper that bypasses the tenant filter. It's safe because
    # we just inserted the row id in claim_next_job's own transaction.
    async def _get_job_unchecked(self, job_id: str) -> JobRecord | None:
        async with self._db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
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
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if target is not None:
            clauses.append("target = ?")
            params.append(target)
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """Atomic claim: pick the oldest queued row, flip to RUNNING, return it.

        sqlite implementation uses ``BEGIN IMMEDIATE`` to take the
        reserved write lock up front so the SELECT-then-UPDATE pair is
        serialized across concurrent claimers. Postgres provider will
        use ``SELECT ... FOR UPDATE SKIP LOCKED`` (no IMMEDIATE needed
        — the row lock is finer-grained).

        ``tenant_id`` is optional so a single shared worker can drain
        all tenants. The HTTP layer never accepts a tenant-less claim;
        it's reserved for ``movate worker --all-tenants``.
        """
        # `aiosqlite` queues writes per connection so this whole block runs
        # serialized against other writers anyway, but BEGIN IMMEDIATE makes
        # the intent explicit (and works correctly under multi-process
        # access too, where aiosqlite's queue offers nothing).
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            now_iso = datetime.now(UTC).isoformat()
            # Retry-aware claim: skip jobs whose next_retry_at is in the
            # future. The `next_retry_at IS NULL` branch is the common
            # case (fresh jobs, jobs that have never failed); the
            # `<= now` branch is for re-queued jobs whose backoff has
            # elapsed.
            tenant_clause = "AND tenant_id = ?" if tenant_id is not None else ""
            params: tuple[object, ...] = (now_iso,)
            if tenant_id is not None:
                params = (now_iso, tenant_id)
            async with self._db.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  {tenant_clause}
                ORDER BY created_at
                LIMIT 1
                """,
                params,
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await self._db.commit()
                return None

            await self._db.execute(
                "UPDATE jobs SET status = 'running', claimed_at = ? WHERE job_id = ?",
                (now_iso, row["job_id"]),
            )
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

        # Re-fetch so the returned record reflects the updated columns.
        # _get_job_unchecked since we just claimed this row inside our
        # own transaction; the caller's tenant matches by construction
        # (claim_next_job either filtered by tenant_id or was the
        # operator drain-all path).
        return await self._get_job_unchecked(row["job_id"])

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
        now = datetime.now(UTC).isoformat()
        # tenant_id in WHERE: even a misconfigured worker can't mutate
        # another tenant's job. Silently no-ops on tenant mismatch
        # (matches the "404 not 403" cross-tenant probe defense).
        await self._db.execute(
            """
            UPDATE jobs
            SET status = ?, result_run_id = ?, error = ?, completed_at = ?
            WHERE job_id = ? AND tenant_id = ?
            """,
            (
                status.value,
                result_run_id,
                json.dumps(error) if error else None,
                now,
                job_id,
                tenant_id,
            ),
        )
        await self._db.commit()

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Transition a ``RUNNING`` job back to ``QUEUED`` for a retry.

        The job sits in the queue but ``claim_next_job`` won't pick
        it up until ``now >= next_retry_at`` — that's how exponential
        backoff is enforced. ``claimed_at`` is cleared so the next
        claim records the new attempt cleanly.

        Tenant-scoped in WHERE (defense in depth — same rationale as
        ``update_job``). Silently no-ops on tenant mismatch.
        """
        await self._db.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                claimed_at = NULL,
                attempt_count = ?,
                next_retry_at = ?
            WHERE job_id = ? AND tenant_id = ?
            """,
            (
                attempt_count,
                next_retry_at.isoformat(),
                job_id,
                tenant_id,
            ),
        )
        await self._db.commit()

    async def reclaim_stale_jobs(
        self,
        *,
        older_than: datetime,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> ReclaimResult:
        """Reclaim orphaned ``RUNNING`` jobs — cross-tenant, atomic.

        Same two-statement logic as the postgres provider, under a
        single ``BEGIN IMMEDIATE`` transaction (matching ``claim_next_job``):
        dead-letter the budget-exhausted rows FIRST, then requeue the
        remaining stale ``running`` rows. ``changes()`` after each UPDATE
        gives the affected-row counts.
        """
        effective_now = now if now is not None else datetime.now(UTC)
        now_iso = effective_now.isoformat()
        older_than_iso = older_than.isoformat()
        dead_letter_error = json.dumps(
            {
                "type": "reaper_dead_letter",
                "message": ("orphaned in running past visibility timeout; retry budget exhausted"),
            }
        )
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                """
                UPDATE jobs
                SET status = 'dead_letter',
                    completed_at = ?,
                    error = ?
                WHERE status = 'running'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < ?
                  AND attempt_count + 1 >= ?
                """,
                (now_iso, dead_letter_error, older_than_iso, max_attempts),
            ) as cur:
                dead_lettered = cur.rowcount
            async with self._db.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    claimed_at = NULL,
                    attempt_count = attempt_count + 1,
                    next_retry_at = ?
                WHERE status = 'running'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < ?
                """,
                (now_iso, older_than_iso),
            ) as cur:
                requeued = cur.rowcount
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        # aiosqlite returns -1 for rowcount on some statements; floor at 0.
        return ReclaimResult(
            requeued=max(0, requeued),
            dead_lettered=max(0, dead_lettered),
        )

    async def request_job_cancel(self, job_id: str, *, tenant_id: str) -> JobStatus | None:
        """Cooperatively cancel a job — atomic CASE UPDATE then re-fetch.

        Mirrors the postgres provider's single-statement CASE logic, run
        under ``BEGIN IMMEDIATE`` (same write-lock discipline as
        ``claim_next_job``):

        * ``queued`` → ``cancelled`` (+ ``completed_at = now``); never
          claimed, so the cancel is immediate.
        * ``running`` → status stays ``running`` but ``cancel_requested
          = 1``; the worker finalizes it as ``CANCELLED`` at its checkpoint.
        * any terminal status → CASE leaves it untouched (no-op).

        ``tenant_id`` is in WHERE so a cross-tenant id never mutates
        another tenant's row; we then re-fetch (tenant-scoped) and return
        the resulting status, or ``None`` if no row matched (missing or
        cross-tenant) — same shape as ``get_job`` (→ 404, never 403).
        """
        now_iso = datetime.now(UTC).isoformat()
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            await self._db.execute(
                """
                UPDATE jobs
                SET status = CASE
                        WHEN status = 'queued' THEN 'cancelled'
                        ELSE status
                    END,
                    completed_at = CASE
                        WHEN status = 'queued' THEN ?
                        ELSE completed_at
                    END,
                    cancel_requested = CASE
                        WHEN status = 'running' THEN 1
                        ELSE cancel_requested
                    END
                WHERE job_id = ? AND tenant_id = ?
                """,
                (now_iso, job_id, tenant_id),
            )
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        record = await self.get_job(job_id, tenant_id=tenant_id)
        return record.status if record is not None else None

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
        clauses = ["tenant_id = ?", "status = 'dead_letter'"]
        params: list[object] = [tenant_id]
        if agent is not None:
            clauses.append("target = ?")
            params.append(agent)
        params.append(limit)
        sql = f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?"
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def requeue_dead_letter_job(self, job_id: str, *, tenant_id: str) -> bool:
        """Reset a DEAD_LETTER job → QUEUED with a fresh attempt budget.

        Status-guarded ``WHERE status = 'dead_letter'`` so a live/other
        terminal job is never touched. ``changes()`` tells us whether a
        row actually flipped; returns ``True`` iff one did (so the API/CLI
        can 404 / error cleanly on a non-dead-letter or cross-tenant id).
        Runs under ``BEGIN IMMEDIATE`` (same write-lock discipline as the
        other mutating job paths)."""
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    attempt_count = 0,
                    next_retry_at = NULL,
                    claimed_at = NULL,
                    completed_at = NULL,
                    error = NULL
                WHERE job_id = ? AND tenant_id = ? AND status = 'dead_letter'
                """,
                (job_id, tenant_id),
            ) as cur:
                changed = cur.rowcount
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        return changed > 0

    async def purge_dead_letter_jobs(
        self,
        tenant_id: str,
        *,
        before: datetime | None = None,
    ) -> int:
        """Delete this tenant's DEAD_LETTER rows; returns the count deleted.

        Tenant + status scoped in WHERE so a purge can never touch another
        tenant's rows or a live/non-dead-letter job. ``before`` narrows to
        rows whose ``completed_at`` is strictly older than the cutoff (and
        non-NULL); ``None`` purges all dead-letter rows for the tenant."""
        clauses = ["tenant_id = ?", "status = 'dead_letter'"]
        params: list[object] = [tenant_id]
        if before is not None:
            clauses.append("completed_at IS NOT NULL")
            clauses.append("completed_at < ?")
            params.append(before.isoformat())
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                f"DELETE FROM jobs WHERE {' AND '.join(clauses)}",
                params,
            ) as cur:
                deleted = cur.rowcount
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        return max(0, deleted)

    # ------------------------------------------------------------------
    # API keys (v0.5 stage 2)
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO api_keys (
                key_id, tenant_id, env, secret_hash, salt, label,
                created_at, last_used_at, revoked_at, expires_at, scope, scopes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key.key_id,
                key.tenant_id,
                key.env.value,
                key.secret_hash,
                key.salt,
                key.label,
                key.created_at.isoformat(),
                key.last_used_at.isoformat() if key.last_used_at else None,
                key.revoked_at.isoformat() if key.revoked_at else None,
                key.expires_at.isoformat() if key.expires_at else None,
                key.scope,
                # Persist as a JSON array; empty list → NULL so a legacy
                # read (and round-trip) is indistinguishable from a never-
                # scoped row → resolves to the default at check time.
                json.dumps(key.scopes) if key.scopes else None,
            ),
        )
        await self._db.commit()

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        async with self._db.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_api_key(row) if row else None

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM api_keys {where} ORDER BY created_at DESC"
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_api_key(r) for r in rows]

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Set ``revoked_at`` on a key. Idempotent — re-revoking is a no-op.

        ``tenant_id`` in WHERE: a tenant can only revoke its own keys
        even if it discovers another tenant's key_id (8-char random
        suffix; not high-entropy but still secret-ish).
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE key_id = ? AND tenant_id = ? AND revoked_at IS NULL",
            (now, key_id, tenant_id),
        )
        await self._db.commit()

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Bump ``last_used_at``. Called inline after a successful verify;
        failure to touch must not fail the request (caller swallows
        exceptions). ``tenant_id`` is defense in depth — the auth path
        already cross-checks the presented key's tenant prefix against
        the looked-up record, but the storage layer enforces it
        independently."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_id = ? AND tenant_id = ?",
            (now, key_id, tenant_id),
        )
        await self._db.commit()

    async def set_api_key_expiry(
        self, key_id: str, *, tenant_id: str, expires_at: datetime
    ) -> None:
        """Set ``expires_at`` on an active key (grace window; ADR 013 D5).

        ``tenant_id`` + ``revoked_at IS NULL`` in WHERE: tenant-scoped,
        and we never re-arm a dead key. No-op on missing / cross-tenant /
        already-revoked."""
        await self._db.execute(
            "UPDATE api_keys SET expires_at = ? "
            "WHERE key_id = ? AND tenant_id = ? AND revoked_at IS NULL",
            (expires_at.isoformat(), key_id, tenant_id),
        )
        await self._db.commit()

    async def update_api_key_scopes(self, key_id: str, *, scopes: list[str]) -> None:
        """Overwrite ONLY the ``scopes`` column by ``key_id`` (bootstrap heal).

        Not tenant-scoped — the sole caller resolves the row by the parsed
        ``key_id`` from ``MOVATE_SEED_API_KEY`` at startup. Mirrors
        ``save_api_key``'s scope encoding (empty list → NULL). No-op on
        missing. Leaves every other column (secret_hash/salt/tenant_id/env/
        created_at) untouched."""
        await self._db.execute(
            "UPDATE api_keys SET scopes = ? WHERE key_id = ?",
            (json.dumps(scopes) if scopes else None, key_id),
        )
        await self._db.commit()

    async def revoke_all_api_keys(self, *, tenant_id: str, except_key_id: str | None = None) -> int:
        """Revoke every active key for ``tenant_id``; return count revoked.

        Compromise-response bulk revoke (ADR 013 D5). ``except_key_id``
        spares one key (the operator's own). ``rowcount`` is reliable here
        because each UPDATE only touches the still-active subset
        (``revoked_at IS NULL``), so re-running returns 0."""
        now = datetime.now(UTC).isoformat()
        sql = "UPDATE api_keys SET revoked_at = ? WHERE tenant_id = ? AND revoked_at IS NULL"
        params: list[object] = [now, tenant_id]
        if except_key_id is not None:
            sql += " AND key_id != ?"
            params.append(except_key_id)
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        async with self._db.execute(
            "SELECT * FROM tenant_budgets WHERE tenant_id = ?",
            (tenant_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return TenantBudget(
            tenant_id=row["tenant_id"],
            monthly_usd_limit=row["monthly_usd_limit"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        # ``INSERT ... ON CONFLICT`` preserves the original created_at
        # on updates — operators see "when was this tenant first
        # given a budget?" separately from "when did we last change
        # the limit?". updated_at refreshes either way.
        now_iso = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO tenant_budgets (
                tenant_id, monthly_usd_limit, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                monthly_usd_limit = excluded.monthly_usd_limit,
                updated_at = excluded.updated_at
            """,
            (
                budget.tenant_id,
                budget.monthly_usd_limit,
                budget.created_at.isoformat(),
                now_iso,
            ),
        )
        await self._db.commit()

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        async with self._db.execute("SELECT * FROM tenant_budgets ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [
            TenantBudget(
                tenant_id=r["tenant_id"],
                monthly_usd_limit=r["monthly_usd_limit"],
                created_at=datetime.fromisoformat(r["created_at"]),
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Run feedback (Chainlit playground writes here)
    # ------------------------------------------------------------------

    async def save_feedback(self, feedback: FeedbackRecord) -> None:
        # INSERT OR REPLACE: operators can edit their feedback. The
        # primary key is feedback_id; same-id re-saves overwrite.
        # ``dimensions`` is JSON-serialized to TEXT (sqlite has no
        # native JSON column — matches the runs.metrics pattern).
        import json as _json  # noqa: PLC0415

        dims = _json.dumps(feedback.dimensions) if feedback.dimensions is not None else None
        await self._db.execute(
            """
            INSERT OR REPLACE INTO run_feedback (
                feedback_id, run_id, tenant_id, agent, user_id,
                score, dimensions, comment, langfuse_score_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                feedback.feedback_id,
                feedback.run_id,
                feedback.tenant_id,
                feedback.agent,
                feedback.user_id,
                feedback.score,
                dims,
                feedback.comment,
                feedback.langfuse_score_id,
                feedback.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def list_feedback(
        self,
        *,
        run_id: str | None = None,
        agent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        import json as _json  # noqa: PLC0415

        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        sql = (
            "SELECT feedback_id, run_id, tenant_id, agent, user_id, score, "
            "dimensions, comment, langfuse_score_id, created_at "
            "FROM run_feedback" + where + " ORDER BY created_at DESC LIMIT ?"
        )
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            FeedbackRecord(
                feedback_id=r["feedback_id"],
                run_id=r["run_id"],
                tenant_id=r["tenant_id"],
                agent=r["agent"],
                user_id=r["user_id"],
                score=r["score"],
                dimensions=(_json.loads(r["dimensions"]) if r["dimensions"] else None),
                comment=r["comment"],
                langfuse_score_id=r["langfuse_score_id"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # KB chunks — vector retrieval (cosine in Python)
    # ------------------------------------------------------------------

    async def save_kb_chunk(self, chunk: KbChunk) -> None:
        import json as _json  # noqa: PLC0415

        embedding_json = _json.dumps(chunk.embedding)
        metadata_json = _json.dumps(chunk.metadata) if chunk.metadata is not None else None
        # INSERT OR REPLACE on the unique (agent, tenant_id, content_hash)
        # would also work but sqlite's REPLACE = DELETE+INSERT changes
        # the chunk_id. We want to PRESERVE chunk_id so anything that
        # cached it still works. Use ON CONFLICT explicitly.
        await self._db.execute(
            """
            INSERT INTO kb_chunks (
                chunk_id, tenant_id, agent, source, text, embedding,
                embedding_model, content_hash, metadata, created_at, ocr
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent, tenant_id, content_hash) DO UPDATE SET
                embedding = excluded.embedding,
                embedding_model = excluded.embedding_model,
                metadata = excluded.metadata,
                source = excluded.source,
                ocr = excluded.ocr
            """,
            (
                chunk.chunk_id,
                chunk.tenant_id,
                chunk.agent,
                chunk.source,
                chunk.text,
                embedding_json,
                chunk.embedding_model,
                chunk.content_hash,
                metadata_json,
                chunk.created_at.isoformat(),
                int(chunk.ocr),
            ),
        )
        await self._db.commit()

        # PR-AA: sync FTS5 index. Delete-then-insert is the FTS5
        # upsert pattern (INSERT OR REPLACE not supported). The
        # intermediate SELECT is needed to get the rowid of the
        # just-written chunk.
        try:
            async with self._db.execute(
                "SELECT rowid FROM kb_chunks WHERE chunk_id = ?",
                (chunk.chunk_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                rowid = row[0]
                await self._db.execute("DELETE FROM kb_chunks_fts WHERE rowid = ?", (rowid,))
                await self._db.execute(
                    "INSERT INTO kb_chunks_fts(rowid, chunk_id, text) VALUES (?, ?, ?)",
                    (rowid, chunk.chunk_id, chunk.text),
                )
                await self._db.commit()
        except aiosqlite.OperationalError:
            pass  # FTS5 not available — skip silently

    async def search_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        # Same Python-cosine ranking as Postgres path. The index on
        # (agent, tenant_id) keeps the SELECT cheap; ranking dominates.
        from movate.storage._cosine import rank_chunks_by_cosine  # noqa: PLC0415

        chunks = await self.list_kb_chunks(agent=agent, tenant_id=tenant_id, limit=100_000)
        return rank_chunks_by_cosine(chunks, query_embedding, limit)

    async def list_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
        limit: int = 1000,
    ) -> list[KbChunk]:
        import json as _json  # noqa: PLC0415

        clauses = ["agent = ?", "tenant_id = ?"]
        params: list[Any] = [agent, tenant_id]
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        params.append(int(limit))
        sql = (
            "SELECT chunk_id, tenant_id, agent, source, text, embedding, "
            "embedding_model, content_hash, metadata, created_at, ocr "
            "FROM kb_chunks WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT ?"
        )
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            KbChunk(
                chunk_id=r["chunk_id"],
                tenant_id=r["tenant_id"],
                agent=r["agent"],
                source=r["source"],
                text=r["text"],
                embedding=_json.loads(r["embedding"]),
                embedding_model=r["embedding_model"],
                content_hash=r["content_hash"],
                metadata=_json.loads(r["metadata"]) if r["metadata"] else None,
                ocr=bool(r["ocr"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def delete_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        # PR-AA: sync FTS5 — delete matching rows by rowid BEFORE
        # the main DELETE removes them from kb_chunks (so we can
        # still look up their rowids).
        try:
            if source is not None:
                fts_sql = (
                    "SELECT rowid FROM kb_chunks WHERE agent = ? AND tenant_id = ? AND source = ?"
                )
                fts_params: tuple[str, ...] = (agent, tenant_id, source)
            else:
                fts_sql = "SELECT rowid FROM kb_chunks WHERE agent = ? AND tenant_id = ?"
                fts_params = (agent, tenant_id)
            async with self._db.execute(fts_sql, fts_params) as cur:
                rowids = [r[0] for r in await cur.fetchall()]
            for rid in rowids:
                await self._db.execute("DELETE FROM kb_chunks_fts WHERE rowid = ?", (rid,))
        except aiosqlite.OperationalError:
            pass  # FTS5 not available

        if source is not None:
            async with self._db.execute(
                "DELETE FROM kb_chunks WHERE agent = ? AND tenant_id = ? AND source = ?",
                (agent, tenant_id, source),
            ) as cur:
                count = cur.rowcount
        else:
            async with self._db.execute(
                "DELETE FROM kb_chunks WHERE agent = ? AND tenant_id = ?",
                (agent, tenant_id),
            ) as cur:
                count = cur.rowcount
        await self._db.commit()
        return int(count or 0)

    async def reindex_kb(self, *, agent: str, tenant_id: str) -> int:
        # SQLite brute-forces cosine search in Python (no HNSW index to
        # rebuild), so reindex is a graceful no-op that just reports how
        # many chunks the (agent, tenant_id) scope holds. NEVER raises —
        # same contract the other backends honour.
        async with self._db.execute(
            "SELECT count(*) FROM kb_chunks WHERE agent = ? AND tenant_id = ?",
            (agent, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Knowledge graph (GraphRAG)
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> None:
        # Merge provenance with any existing row (UNION source_chunk_ids)
        # so re-ingesting from a new document adds to, rather than
        # replaces, an entity's source citations.
        async with self._db.execute(
            "SELECT source_chunk_ids FROM kb_entities "
            "WHERE agent = ? AND tenant_id = ? AND content_hash = ?",
            (entity.agent, entity.tenant_id, entity.content_hash),
        ) as cur:
            row = await cur.fetchone()
        existing = json.loads(row[0]) if row and row[0] else []
        merged = sorted(set(existing) | set(entity.source_chunk_ids))
        await self._db.execute(
            """
            INSERT INTO kb_entities (
                entity_id, tenant_id, agent, project_id, name, type, description,
                embedding, embedding_model, content_hash, source_chunk_ids,
                metadata, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent, tenant_id, content_hash) DO UPDATE SET
                project_id = COALESCE(excluded.project_id, kb_entities.project_id),
                name = excluded.name,
                type = excluded.type,
                description = excluded.description,
                embedding = excluded.embedding,
                embedding_model = excluded.embedding_model,
                source_chunk_ids = excluded.source_chunk_ids,
                metadata = excluded.metadata
            """,
            (
                entity.entity_id,
                entity.tenant_id,
                entity.agent,
                entity.project_id,
                entity.name,
                entity.type,
                entity.description,
                json.dumps(entity.embedding),
                entity.embedding_model,
                entity.content_hash,
                json.dumps(merged),
                json.dumps(entity.metadata) if entity.metadata is not None else None,
                entity.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def upsert_relation(self, relation: Relation) -> None:
        async with self._db.execute(
            "SELECT source_chunk_ids FROM kb_relations "
            "WHERE agent = ? AND tenant_id = ? AND content_hash = ?",
            (relation.agent, relation.tenant_id, relation.content_hash),
        ) as cur:
            row = await cur.fetchone()
        existing = json.loads(row[0]) if row and row[0] else []
        merged = sorted(set(existing) | set(relation.source_chunk_ids))
        await self._db.execute(
            """
            INSERT INTO kb_relations (
                relation_id, tenant_id, agent, project_id, src_entity_id,
                dst_entity_id, type, description, weight, content_hash,
                source_chunk_ids, metadata, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent, tenant_id, content_hash) DO UPDATE SET
                project_id = COALESCE(excluded.project_id, kb_relations.project_id),
                src_entity_id = excluded.src_entity_id,
                dst_entity_id = excluded.dst_entity_id,
                type = excluded.type,
                description = excluded.description,
                weight = excluded.weight,
                source_chunk_ids = excluded.source_chunk_ids,
                metadata = excluded.metadata
            """,
            (
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
                json.dumps(merged),
                json.dumps(relation.metadata) if relation.metadata is not None else None,
                relation.created_at.isoformat(),
            ),
        )
        await self._db.commit()

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

        entities = await self.list_entities(
            agent=agent, tenant_id=tenant_id, limit=100_000, project_id=project_id
        )
        return rank_entities_by_cosine(entities, query_embedding, limit)

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
        # the historical behavior). When set, only rows tagged with this
        # project participate, so traversal stays inside the project subgraph.
        proj_clause = "" if project_id is None else " AND r.project_id = ?"
        proj_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        # Recursive CTE: bounded k-hop reachability from the seed ids.
        # Undirected for reachability (follow an edge from either endpoint);
        # UNION dedups so cycles terminate, depth < hops bounds the walk.
        async with self._db.execute(
            f"""
            WITH RECURSIVE reachable(eid, depth) AS (
                SELECT value, 0 FROM json_each(?)
              UNION
                SELECT CASE WHEN r.src_entity_id = reachable.eid
                            THEN r.dst_entity_id ELSE r.src_entity_id END,
                       reachable.depth + 1
                FROM kb_relations r
                JOIN reachable
                  ON (r.src_entity_id = reachable.eid OR r.dst_entity_id = reachable.eid)
                WHERE reachable.depth < ? AND r.agent = ? AND r.tenant_id = ?{proj_clause}
            )
            SELECT DISTINCT eid FROM reachable
            """,
            (json.dumps(entity_ids), int(hops), agent, tenant_id, *proj_args),
        ) as cur:
            reachable = [r[0] for r in await cur.fetchall()]
        if not reachable:
            return Subgraph(entities=[], relations=[])
        # Edges with both endpoints reachable, strongest first, budget-capped.
        ph = ",".join("?" * len(reachable))
        rel_proj = "" if project_id is None else " AND project_id = ?"
        async with self._db.execute(
            f"""
            SELECT relation_id, tenant_id, agent, project_id, src_entity_id,
                   dst_entity_id, type, description, weight, content_hash,
                   source_chunk_ids, metadata, created_at
            FROM kb_relations
            WHERE agent = ? AND tenant_id = ?{rel_proj}
              AND src_entity_id IN ({ph}) AND dst_entity_id IN ({ph})
            ORDER BY weight DESC, relation_id LIMIT ?
            """,
            (agent, tenant_id, *proj_args, *reachable, *reachable, int(limit)),
        ) as cur:
            relations = [_row_to_relation(r) for r in await cur.fetchall()]
        keep = set(entity_ids)
        for rel in relations:
            keep.add(rel.src_entity_id)
            keep.add(rel.dst_entity_id)
        keep_ph = ",".join("?" * len(keep))
        ent_proj = "" if project_id is None else " AND project_id = ?"
        async with self._db.execute(
            f"""
            SELECT entity_id, tenant_id, agent, project_id, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities
            WHERE agent = ? AND tenant_id = ?{ent_proj} AND entity_id IN ({keep_ph})
            """,
            (agent, tenant_id, *proj_args, *keep),
        ) as cur:
            entities = [_row_to_entity(r) for r in await cur.fetchall()]
        return Subgraph(entities=entities, relations=relations)

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        async with self._db.execute(
            """
            SELECT entity_id, tenant_id, agent, project_id, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities WHERE entity_id = ? AND tenant_id = ?
            """,
            (entity_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
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
        proj_clause = "" if project_id is None else " AND project_id = ?"
        proj_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        async with self._db.execute(
            f"""
            SELECT entity_id, tenant_id, agent, project_id, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities WHERE agent = ? AND tenant_id = ?{proj_clause}
            ORDER BY created_at DESC LIMIT ?
            """,
            (agent, tenant_id, *proj_args, int(limit)),
        ) as cur:
            entities = [_row_to_entity(r) for r in await cur.fetchall()]
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
        proj_clause = "" if project_id is None else " AND project_id = ?"
        proj_args: tuple[Any, ...] = () if project_id is None else (project_id,)
        async with self._db.execute(
            f"""
            SELECT relation_id, tenant_id, agent, project_id, src_entity_id,
                   dst_entity_id, type, description, weight, content_hash,
                   source_chunk_ids, metadata, created_at
            FROM kb_relations WHERE agent = ? AND tenant_id = ?{proj_clause}
            ORDER BY created_at DESC LIMIT ?
            """,
            (agent, tenant_id, *proj_args, int(limit)),
        ) as cur:
            return [_row_to_relation(r) for r in await cur.fetchall()]

    async def delete_graph(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        if source is None:
            async with self._db.execute(
                "DELETE FROM kb_entities WHERE agent = ? AND tenant_id = ?",
                (agent, tenant_id),
            ) as cur:
                count = int(cur.rowcount or 0)
            async with self._db.execute(
                "DELETE FROM kb_relations WHERE agent = ? AND tenant_id = ?",
                (agent, tenant_id),
            ) as cur:
                count += int(cur.rowcount or 0)
            await self._db.commit()
            return count
        # Per-source delete: drop rows whose provenance is SOLELY this
        # source (source_chunk_ids ⊆ that source's chunks). Multi-source
        # rows survive — load + filter in Python (same bounded-scale
        # approach as the cosine path).
        async with self._db.execute(
            "SELECT chunk_id FROM kb_chunks WHERE agent = ? AND tenant_id = ? AND source = ?",
            (agent, tenant_id, source),
        ) as cur:
            chunk_ids = {r[0] for r in await cur.fetchall()}

        def _solely_from_source(ids: list[str]) -> bool:
            return bool(ids) and set(ids) <= chunk_ids

        entities = await self.list_entities(agent=agent, tenant_id=tenant_id, limit=10**9)
        relations = await self.list_relations(agent=agent, tenant_id=tenant_id, limit=10**9)
        doomed_entities = [e.entity_id for e in entities if _solely_from_source(e.source_chunk_ids)]
        doomed_relations = [
            r.relation_id for r in relations if _solely_from_source(r.source_chunk_ids)
        ]
        for entity_id in doomed_entities:
            await self._db.execute("DELETE FROM kb_entities WHERE entity_id = ?", (entity_id,))
        for relation_id in doomed_relations:
            await self._db.execute("DELETE FROM kb_relations WHERE relation_id = ?", (relation_id,))
        await self._db.commit()
        return len(doomed_entities) + len(doomed_relations)

    async def search_kb_chunks_lexical(
        self,
        *,
        agent: str,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """FTS5-backed BM25 lexical search.

        Falls back to the Python BM25 scorer if FTS5 is unavailable
        or the query contains no recognized terms. Empty query → [].
        """
        import json as _json  # noqa: PLC0415
        import math  # noqa: PLC0415

        if not query.strip():
            return []
        try:
            # FTS5 MATCH syntax: wrap the query in double-quotes to
            # treat it as a phrase, OR strip to plain terms. We use
            # plainto_fts5() semantics by sanitizing the query string
            # (FTS5 MATCH is picky about special characters).
            safe_query = _fts5_escape(query)
            if not safe_query:
                return []
            sql = """
                SELECT c.chunk_id, c.tenant_id, c.agent, c.source, c.text,
                       c.embedding, c.embedding_model, c.content_hash,
                       c.metadata, c.created_at,
                       kb_chunks_fts.rank AS fts_rank
                FROM kb_chunks_fts
                JOIN kb_chunks c
                    ON kb_chunks_fts.rowid = c.rowid
                WHERE kb_chunks_fts MATCH ?
                  AND c.agent = ?
                  AND c.tenant_id = ?
                ORDER BY kb_chunks_fts.rank
                LIMIT ?
            """
            async with self._db.execute(sql, (safe_query, agent, tenant_id, int(limit))) as cur:
                rows = await cur.fetchall()
            results = []
            for r in rows:
                # FTS5 rank is negative (lower = more relevant). Negate
                # and normalize to [0, 1] via tanh so the existing
                # KbChunkWithScore validator accepts the score.
                raw_rank = r["fts_rank"] or 0.0
                score = math.tanh(-raw_rank / 10.0)
                chunk = KbChunk(
                    chunk_id=r["chunk_id"],
                    tenant_id=r["tenant_id"],
                    agent=r["agent"],
                    source=r["source"],
                    text=r["text"],
                    embedding=_json.loads(r["embedding"]),
                    embedding_model=r["embedding_model"],
                    content_hash=r["content_hash"],
                    metadata=_json.loads(r["metadata"]) if r["metadata"] else None,
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                results.append(KbChunkWithScore(chunk=chunk, score=score))
            return results
        except aiosqlite.OperationalError:
            # FTS5 not available or query syntax error — fall back to
            # Python BM25 over all chunks.
            from movate.kb.lexical import bm25_search  # noqa: PLC0415

            all_chunks = await self.list_kb_chunks(agent=agent, tenant_id=tenant_id, limit=100_000)
            return bm25_search(all_chunks, query, limit=limit)

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        # First-of-the-month UTC. We do this in Python so the SQL
        # stays portable (sqlite's date functions are quirky); the
        # index on (tenant_id, created_at) covers the lookup either way.
        month_start = _first_of_month_utc().isoformat()
        async with self._db.execute(
            """
            SELECT COALESCE(SUM(
                CAST(json_extract(metrics, '$.cost_usd') AS REAL)
            ), 0.0) AS total
            FROM runs
            WHERE tenant_id = ? AND created_at >= ?
            """,
            (tenant_id, month_start),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0.0
        return float(row["total"] or 0.0)

    # ------------------------------------------------------------------
    # Conversation threads (PR-N) — multi-turn agent foundation.
    # ------------------------------------------------------------------

    async def save_conversation_thread(self, thread: ConversationThread) -> None:
        # Upsert on thread_id — INSERT OR REPLACE matches the
        # Postgres ON CONFLICT semantics. Clients call this once at
        # thread creation and again on every appended message to
        # refresh updated_at.
        await self._db.execute(
            """
            INSERT OR REPLACE INTO conversation_threads
            (thread_id, tenant_id, agent, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                thread.thread_id,
                thread.tenant_id,
                thread.agent,
                thread.title,
                thread.created_at.isoformat(),
                thread.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> ConversationThread | None:
        async with self._db.execute(
            "SELECT * FROM conversation_threads WHERE thread_id = ? AND tenant_id = ?",
            (thread_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
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
        clauses = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        sql = (
            "SELECT * FROM conversation_threads WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(int(limit))
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_thread(r) for r in rows]

    async def list_runs_for_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[RunRecord]:
        # Chronological order — earliest turn first — so the runtime
        # renders conversation history straight from the list.
        # Tenant-scoped via the WHERE clause (cross-tenant lookups
        # return []).
        async with self._db.execute(
            """
            SELECT * FROM runs
            WHERE thread_id = ? AND tenant_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (thread_id, tenant_id, int(limit)),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]

    async def delete_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        # Tenant-scoped DELETE — a thread row for a different tenant
        # is invisible (returns False / rowcount=0), matching the
        # 404-not-403 cross-tenant contract.
        cur = await self._db.execute(
            "DELETE FROM conversation_threads WHERE thread_id = ? AND tenant_id = ?",
            (thread_id, tenant_id),
        )
        await self._db.commit()
        return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Stateful sessions (ADR 045 D10)
    # ------------------------------------------------------------------

    async def save_session(self, session: Session) -> None:
        # Upsert on session_id — INSERT OR REPLACE matches the Postgres
        # ON CONFLICT semantics. Preserve created_at on update by reading
        # the existing row first (INSERT OR REPLACE would otherwise reset
        # it to the supplied value, but callers always carry the original
        # created_at on the model so this stays consistent either way).
        await self._db.execute(
            """
            INSERT OR REPLACE INTO sessions
            (session_id, tenant_id, agent, title, created_at, updated_at,
             turn_count, total_cost_usd, total_tokens_in, total_tokens_out)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                session.tenant_id,
                session.agent,
                session.title,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                session.turn_count,
                session.total_cost_usd,
                session.total_tokens_in,
                session.total_tokens_out,
            ),
        )
        await self._db.commit()

    async def get_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
    ) -> Session | None:
        async with self._db.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND tenant_id = ?",
            (session_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
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
        clauses = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        sql = (
            "SELECT * FROM sessions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(int(limit))
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def append_session_message(self, message: SessionMessage) -> None:
        # Insert-only (messages are immutable). Tenant_id is denormalized
        # onto the row so message reads stay tenant-scoped without a join.
        await self._db.execute(
            """
            INSERT INTO session_messages
            (message_id, session_id, tenant_id, role, content, run_id,
             cost_usd, tokens_in, tokens_out, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.session_id,
                message.tenant_id,
                message.role,
                json.dumps(message.content),
                message.run_id,
                message.cost_usd,
                message.tokens_in,
                message.tokens_out,
                message.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def list_session_messages(
        self,
        session_id: str,
        *,
        tenant_id: str,
        limit: int = 1000,
    ) -> list[SessionMessage]:
        # Chronological order — earliest first. Tenant-scoped via the
        # WHERE clause (cross-tenant returns []).
        async with self._db.execute(
            """
            SELECT * FROM session_messages
            WHERE session_id = ? AND tenant_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (session_id, tenant_id, int(limit)),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_session_message(r) for r in rows]

    async def delete_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        # Delete messages first, then the session row. Both tenant-scoped.
        # The bool reflects whether the SESSION row existed (a session
        # with no messages still returns True; a cross-tenant id returns
        # False without touching either table).
        await self._db.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND tenant_id = ?",
            (session_id, tenant_id),
        )
        cur = await self._db.execute(
            "DELETE FROM sessions WHERE session_id = ? AND tenant_id = ?",
            (session_id, tenant_id),
        )
        await self._db.commit()
        return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Diagnoses (ADR 043 D1 — Failure Pattern Diagnoser results)
    # ------------------------------------------------------------------

    async def save_diagnosis(self, record: DiagnosisRecord) -> None:
        # Upsert on diagnosis_id. ON CONFLICT preserves the original
        # created_at so a follow-up "completed" save doesn't reset the
        # insert timestamp — same pattern as the upsert helpers above.
        await self._db.execute(
            """
            INSERT INTO diagnoses (
                diagnosis_id, tenant_id, agent, status, request, result,
                error, tokens_used, cost_usd, model, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(diagnosis_id) DO UPDATE SET
                status       = excluded.status,
                request      = excluded.request,
                result       = excluded.result,
                error        = excluded.error,
                tokens_used  = excluded.tokens_used,
                cost_usd     = excluded.cost_usd,
                model        = excluded.model,
                completed_at = excluded.completed_at
            """,
            (
                record.diagnosis_id,
                record.tenant_id,
                record.agent,
                record.status.value,
                json.dumps(record.request),
                json.dumps(record.result) if record.result is not None else None,
                json.dumps(record.error.model_dump()) if record.error is not None else None,
                record.tokens_used,
                record.cost_usd,
                record.model,
                record.created_at.isoformat(),
                record.completed_at.isoformat() if record.completed_at else None,
            ),
        )
        await self._db.commit()

    async def get_diagnosis(self, diagnosis_id: str, *, tenant_id: str) -> DiagnosisRecord | None:
        # tenant_id in WHERE is the SQL-layer enforcement — the no-leak
        # contract (a cross-tenant lookup is indistinguishable from a
        # missing row).
        async with self._db.execute(
            "SELECT * FROM diagnoses WHERE diagnosis_id = ? AND tenant_id = ? LIMIT 1",
            (diagnosis_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
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
    # Events outbox (ADR 035 D1 — durable lifecycle events).
    # ``data`` round-trips as TEXT holding ``json.dumps(...)`` — same
    # JSON-as-text strategy as runs.metrics. Tenant-scoped on every read.
    # ------------------------------------------------------------------

    async def record_event(self, event: Event) -> None:
        await self._db.execute(
            "INSERT INTO events (id, tenant_id, kind, subject, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.tenant_id,
                event.kind,
                event.subject,
                json.dumps(event.data),
                event.created_at.isoformat(),
            ),
        )
        await self._db.commit()

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
        clauses: list[str] = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("created_at < ?")
            params.append(until.isoformat())
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        # Cursor pagination — skip rows up to and including ``after_id``
        # in oldest-first order. We look up the cursor row's
        # ``(created_at, id)`` so the comparison is stable across rows
        # sharing a timestamp (tie-break on id). An unknown after_id
        # silently falls back to "from the beginning" — no existence
        # leak across tenants either, since the cursor lookup is
        # tenant-scoped.
        if after_id is not None:
            async with self._db.execute(
                "SELECT created_at FROM events WHERE id = ? AND tenant_id = ? LIMIT 1",
                (after_id, tenant_id),
            ) as cur:
                cursor_row = await cur.fetchone()
            if cursor_row is not None:
                clauses.append("(created_at, id) > (?, ?)")
                params.extend([cursor_row["created_at"], after_id])
        sql = (
            "SELECT * FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at ASC, id ASC LIMIT ?"
        )
        params.append(limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            Event(
                id=r["id"],
                tenant_id=r["tenant_id"],
                kind=r["kind"],
                subject=r["subject"],
                data=json.loads(r["data"]) if r["data"] else {},
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Webhook subscriptions (ADR 035 D2 — outbound delivery). The
    # ``secret`` is stored verbatim because the worker must re-sign
    # every outbound delivery with the same key the subscriber
    # configured (we never echo it back on the wire after create).
    # ``kind_filter`` is json-encoded (TEXT) for portability.
    # ------------------------------------------------------------------

    async def create_webhook(self, sub: WebhookSubscription) -> WebhookSubscription:
        await self._db.execute(
            "INSERT INTO webhooks "
            "(id, tenant_id, url, kind_filter, secret, enabled, failure_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sub.id,
                sub.tenant_id,
                sub.url,
                json.dumps(sub.kind_filter),
                sub.secret,
                1 if sub.enabled else 0,
                sub.failure_count,
                sub.created_at.isoformat(),
            ),
        )
        await self._db.commit()
        return sub

    async def list_webhooks(
        self,
        tenant_id: str,
        *,
        enabled_only: bool = True,
    ) -> list[WebhookSubscription]:
        sql = "SELECT * FROM webhooks WHERE tenant_id = ?"
        params: list[object] = [tenant_id]
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at ASC, id ASC"
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_webhook(r) for r in rows]

    async def get_webhook(self, tenant_id: str, webhook_id: str) -> WebhookSubscription | None:
        async with self._db.execute(
            "SELECT * FROM webhooks WHERE id = ? AND tenant_id = ? LIMIT 1",
            (webhook_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_webhook(row) if row is not None else None

    async def update_webhook(
        self,
        tenant_id: str,
        webhook_id: str,
        *,
        enabled: bool | None = None,
        failure_count: int | None = None,
    ) -> WebhookSubscription | None:
        # Defensive no-op when neither field is set — same shape as a
        # PATCH with an empty body.
        if enabled is None and failure_count is None:
            return await self.get_webhook(tenant_id, webhook_id)
        sets: list[str] = []
        params: list[object] = []
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if failure_count is not None:
            sets.append("failure_count = ?")
            params.append(failure_count)
        params.extend([webhook_id, tenant_id])
        await self._db.execute(
            f"UPDATE webhooks SET {', '.join(sets)} WHERE id = ? AND tenant_id = ?",
            params,
        )
        await self._db.commit()
        return await self.get_webhook(tenant_id, webhook_id)

    async def delete_webhook(self, tenant_id: str, webhook_id: str) -> bool:
        cur = await self._db.execute(
            "DELETE FROM webhooks WHERE id = ? AND tenant_id = ?",
            (webhook_id, tenant_id),
        )
        await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def record_webhook_attempt(self, attempt: WebhookAttempt) -> None:
        await self._db.execute(
            "INSERT INTO webhook_attempts "
            "(id, webhook_id, event_id, tenant_id, attempted_at, status_code, "
            " response_excerpt, error_kind, attempt_n) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.id,
                attempt.webhook_id,
                attempt.event_id,
                attempt.tenant_id,
                attempt.attempted_at.isoformat(),
                attempt.status_code,
                attempt.response_excerpt,
                attempt.error_kind,
                attempt.attempt_n,
            ),
        )
        await self._db.commit()

    async def list_webhook_attempts(
        self,
        tenant_id: str,
        *,
        webhook_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[WebhookAttempt]:
        clauses: list[str] = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if webhook_id is not None:
            clauses.append("webhook_id = ?")
            params.append(webhook_id)
        if since is not None:
            clauses.append("attempted_at >= ?")
            params.append(since.isoformat())
        params.append(limit)
        sql = (
            "SELECT * FROM webhook_attempts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY attempted_at DESC, id DESC LIMIT ?"
        )
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_webhook_attempt(r) for r in rows]

    async def get_webhook_cursor(self, tenant_id: str, webhook_id: str) -> str | None:
        async with self._db.execute(
            "SELECT last_event_id FROM webhook_cursors "
            "WHERE webhook_id = ? AND tenant_id = ? LIMIT 1",
            (webhook_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        return row["last_event_id"] if row is not None else None

    async def set_webhook_cursor(self, tenant_id: str, webhook_id: str, last_event_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        # SQLite's ``INSERT ... ON CONFLICT DO UPDATE`` — same upsert
        # idiom as the trigger-deliveries dedup write.
        await self._db.execute(
            "INSERT INTO webhook_cursors (webhook_id, tenant_id, last_event_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(webhook_id) DO UPDATE SET "
            "  last_event_id = excluded.last_event_id, "
            "  updated_at = excluded.updated_at",
            (webhook_id, tenant_id, last_event_id, now),
        )
        await self._db.commit()

    # Eval-generation jobs (``mdk eval generate``)
    # ------------------------------------------------------------------

    async def save_eval_generation_job(self, job: Any) -> None:
        from movate.core.eval_generator import EvalGenerationJob  # noqa: PLC0415

        assert isinstance(job, EvalGenerationJob)
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO eval_generation_jobs (
                job_id, tenant_id, agent_name, status, description,
                count_requested, categories, include_judge, model,
                budget_usd, progress, result, error, tokens_used,
                cost_usd, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                progress = excluded.progress,
                result = excluded.result,
                error = excluded.error,
                tokens_used = excluded.tokens_used,
                cost_usd = excluded.cost_usd,
                completed_at = excluded.completed_at
            """,
            (
                job.job_id,
                job.tenant_id,
                job.agent_name,
                job.status,
                job.description,
                job.count,
                json.dumps(job.categories),
                1 if job.include_judge else 0,
                job.model,
                job.budget_usd,
                job.progress,
                json.dumps(job.result) if job.result is not None else None,
                json.dumps(job.error) if job.error is not None else None,
                job.tokens_used,
                job.cost_usd,
                job.created_at,
                job.completed_at,
            ),
        )
        await self._conn.commit()

    async def get_eval_generation_job(self, job_id: str, *, tenant_id: str) -> Any | None:
        from movate.core.eval_generator import EvalGenerationJob  # noqa: PLC0415

        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM eval_generation_jobs WHERE job_id = ? AND tenant_id = ?",
            (job_id, tenant_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return EvalGenerationJob(
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            agent_name=row["agent_name"],
            status=row["status"],
            description=row["description"],
            count=row["count_requested"],
            categories=json.loads(row["categories"]),
            include_judge=bool(row["include_judge"]),
            model=row["model"],
            budget_usd=row["budget_usd"],
            progress=row["progress"],
            result=json.loads(row["result"]) if row["result"] else None,
            error=json.loads(row["error"]) if row["error"] else None,
            tokens_used=row["tokens_used"],
            cost_usd=row["cost_usd"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
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
        # Same disk-mutation contract as the in-memory double; the SQL
        # layer doesn't store the dataset (the agent's evals/dataset.jsonl
        # lives on the runtime's filesystem). Delegated to a shared helper
        # so the three backends stay byte-identical on the commit shape.
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

    # ------------------------------------------------------------------
    # Tool registry (ADR 052)
    # ------------------------------------------------------------------

    async def save_tool_descriptor(self, descriptor: Any) -> None:
        conn = self._db
        await conn.execute(
            "INSERT OR REPLACE INTO tool_descriptors"
            " (name, version, scope, tenant_id, project_id, description,"
            "  tags, input_schema, output_schema, backend, credentials_ref,"
            "  governance, owner, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                descriptor.name,
                descriptor.version,
                descriptor.scope if isinstance(descriptor.scope, str) else descriptor.scope.value,
                descriptor.tenant_id,
                descriptor.project_id,
                descriptor.description,
                json.dumps(descriptor.tags),
                json.dumps(descriptor.input_schema),
                json.dumps(descriptor.output_schema),
                (
                    descriptor.backend.model_dump_json()
                    if hasattr(descriptor.backend, "model_dump_json")
                    else json.dumps(descriptor.backend)
                ),
                descriptor.credentials_ref,
                (
                    descriptor.governance.model_dump_json()
                    if hasattr(descriptor.governance, "model_dump_json")
                    else json.dumps(descriptor.governance)
                ),
                descriptor.owner,
                descriptor.created_at.isoformat() if descriptor.created_at else None,
                descriptor.updated_at.isoformat() if descriptor.updated_at else None,
            ),
        )
        await conn.commit()

    async def get_tool_descriptor(
        self,
        name: str,
        version: str | None,
        scope: str,
        tenant_id: str,
    ) -> Any | None:
        conn = self._db
        if version is not None:
            cursor = await conn.execute(
                "SELECT * FROM tool_descriptors"
                " WHERE name = ? AND version = ? AND scope = ? AND tenant_id = ?",
                (name, version, scope, tenant_id),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM tool_descriptors"
                " WHERE name = ? AND scope = ? AND tenant_id = ?"
                " ORDER BY updated_at DESC LIMIT 1",
                (name, scope, tenant_id),
            )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_tool_descriptor(row)

    async def list_tool_descriptors(
        self,
        scope: str | None,
        tenant_id: str,
        tags: list[str] | None,
    ) -> list[Any]:
        conn = self._db
        if scope is not None:
            cursor = await conn.execute(
                "SELECT * FROM tool_descriptors"
                " WHERE scope = ? AND tenant_id = ?"
                " ORDER BY name ASC, version DESC",
                (scope, tenant_id),
            )
        else:
            # List across all scopes visible to the tenant.
            cursor = await conn.execute(
                "SELECT * FROM tool_descriptors"
                " WHERE tenant_id = ? OR scope = 'movate'"
                " ORDER BY name ASC, version DESC",
                (tenant_id,),
            )
        rows = await cursor.fetchall()
        result = [_row_to_tool_descriptor(r) for r in rows]
        if tags:
            result = [d for d in result if all(t in d.tags for t in tags)]
        return result

    async def delete_tool_descriptor(
        self,
        name: str,
        version: str,
        scope: str,
        tenant_id: str,
    ) -> bool:
        conn = self._db
        cursor = await conn.execute(
            "DELETE FROM tool_descriptors"
            " WHERE name = ? AND version = ? AND scope = ? AND tenant_id = ?",
            (name, version, scope, tenant_id),
        )
        await conn.commit()
        return bool(cursor.rowcount > 0)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def _row_to_webhook(row: aiosqlite.Row) -> WebhookSubscription:
    return WebhookSubscription(
        id=row["id"],
        tenant_id=row["tenant_id"],
        url=row["url"],
        kind_filter=json.loads(row["kind_filter"]) if row["kind_filter"] else [],
        secret=row["secret"],
        enabled=bool(row["enabled"]),
        failure_count=row["failure_count"] or 0,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_webhook_attempt(row: aiosqlite.Row) -> WebhookAttempt:
    return WebhookAttempt(
        id=row["id"],
        webhook_id=row["webhook_id"],
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        attempted_at=datetime.fromisoformat(row["attempted_at"]),
        status_code=row["status_code"],
        response_excerpt=row["response_excerpt"],
        error_kind=row["error_kind"],
        attempt_n=row["attempt_n"],
    )


def _row_to_thread(row: aiosqlite.Row) -> ConversationThread:
    return ConversationThread(
        thread_id=row["thread_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        title=row["title"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_session(row: aiosqlite.Row) -> Session:
    return Session(
        session_id=row["session_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        title=row["title"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        turn_count=row["turn_count"],
        total_cost_usd=row["total_cost_usd"],
        total_tokens_in=row["total_tokens_in"],
        total_tokens_out=row["total_tokens_out"],
    )


def _row_to_session_message(row: aiosqlite.Row) -> SessionMessage:
    return SessionMessage(
        message_id=row["message_id"],
        session_id=row["session_id"],
        tenant_id=row["tenant_id"],
        role=row["role"],
        content=json.loads(row["content"]),
        run_id=row["run_id"],
        cost_usd=row["cost_usd"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_diagnosis(row: aiosqlite.Row) -> DiagnosisRecord:
    error_raw = row["error"]
    error_obj: ErrorInfo | None = None
    if error_raw:
        error_obj = ErrorInfo.model_validate(json.loads(error_raw))
    return DiagnosisRecord(
        diagnosis_id=row["diagnosis_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        status=DiagnosisStatus(row["status"]),
        request=json.loads(row["request"]) if row["request"] else {},
        result=json.loads(row["result"]) if row["result"] else None,
        error=error_obj,
        tokens_used=int(row["tokens_used"] or 0),
        cost_usd=float(row["cost_usd"] or 0.0),
        model=row["model"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=(datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None),
    )


def _row_to_bench(row: aiosqlite.Row) -> BenchRecord:
    return BenchRecord(
        bench_id=row["bench_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        input=json.loads(row["input"]),
        judge_method=JudgeMethod(row["judge_method"]) if row["judge_method"] else None,
        judge_provider=row["judge_provider"],
        runs_per_model=row["runs_per_model"],
        gate_mode=row["gate_mode"],
        models=[BenchModelResult.model_validate(m) for m in json.loads(row["models"])],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_agent_bundle(row: aiosqlite.Row) -> AgentBundleRecord:
    return AgentBundleRecord(
        name=row["name"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        created_by=row["created_by"],
        content_hash=row["content_hash"],
        files=json.loads(row["files"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_agent_state(row: aiosqlite.Row) -> AgentRuntimeState:
    return AgentRuntimeState(
        tenant_id=row["tenant_id"],
        name=row["name"],
        status=AgentStatus(row["status"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        updated_by=row["updated_by"],
        note=row["note"],
    )


def _row_to_skill(row: aiosqlite.Row) -> SkillRecord:
    return SkillRecord(
        name=row["name"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        created_by=row["created_by"],
        content_hash=row["content_hash"],
        description=row["description"],
        files=json.loads(row["files"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_context(row: aiosqlite.Row) -> ContextRecord:
    return ContextRecord(
        name=row["name"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        created_by=row["created_by"],
        content_hash=row["content_hash"],
        description=row["description"],
        body=row["body"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_project(row: aiosqlite.Row) -> Project:
    archived = row["archived_at"]
    return Project(
        project_id=row["project_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        description=row["description"],
        owner_principal_id=row["owner_principal_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        archived_at=datetime.fromisoformat(archived) if archived else None,
    )


def _row_to_project_member(row: aiosqlite.Row) -> ProjectMember:
    return ProjectMember(
        project_id=row["project_id"],
        principal_id=row["principal_id"],
        role=ProjectMemberRole(row["role"]),
        added_by=row["added_by"],
        added_at=datetime.fromisoformat(row["added_at"]),
    )


def _row_to_insight(row: aiosqlite.Row) -> ObservabilityInsight:
    return ObservabilityInsight(
        id=row["id"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        date=date.fromisoformat(row["date"]),
        health_score=row["health_score"],
        anomalies=json.loads(row["anomalies"]) if row["anomalies"] else [],
        top_failures=json.loads(row["top_failures"]) if row["top_failures"] else [],
        usage_rollup=json.loads(row["usage_rollup"]) if row["usage_rollup"] else {},
        trends=json.loads(row["trends"]) if row["trends"] else {},
        narrative_digest=row["narrative_digest"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_workflow_bundle(row: aiosqlite.Row) -> WorkflowBundleRecord:
    # ADR 037 D1 — sqlite stores ``published`` as INTEGER (0/1); coerce to bool
    # at the boundary so the Pydantic model carries the typed value.
    return WorkflowBundleRecord(
        name=row["name"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        created_by=row["created_by"],
        content_hash=row["content_hash"],
        files=json.loads(row["files"]),
        published=bool(row["published"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_audit(row: aiosqlite.Row) -> AuditRecord:
    raw_findings = json.loads(row["findings"]) if row["findings"] else []
    return AuditRecord(
        audit_id=row["audit_id"],
        tenant_id=row["tenant_id"],
        scope_kind=row["scope_kind"],
        scope_id=row["scope_id"],
        categories=json.loads(row["categories"]) if row["categories"] else [],
        severity_floor=AuditFindingSeverity(row["severity_floor"]),
        model=row["model"],
        budget_usd=float(row["budget_usd"]),
        findings=[AuditFinding.model_validate(f) for f in raw_findings],
        partial=bool(row["partial"]),
        tokens_used=int(row["tokens_used"]),
        cost_usd=float(row["cost_usd"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_eval(row: aiosqlite.Row) -> EvalRecord:
    # item 24 per-dimension means. init() has run the ALTER by the time we
    # read here; .get() stays defensive against a row read on a connection
    # that somehow predates the migration — such a row is a pre-item-24 eval,
    # which is exactly None (drift falls back to aggregate-only).
    raw_dim_means = dict(row).get("dimension_means")
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
        created_at=datetime.fromisoformat(row["created_at"]),
        dimension_means=json.loads(raw_dim_means) if raw_dim_means else None,
    )


def _row_to_eval_schedule(row: aiosqlite.Row) -> EvalSchedule:
    return EvalSchedule(
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        cadence_seconds=row["cadence_seconds"],
        enabled=bool(row["enabled"]),
        mock=bool(row["mock"]),
        runs=row["runs"],
        gate_mode=row["gate_mode"],
        gate=row["gate"],
        objective=row["objective"],
        regression_tolerance=row["regression_tolerance"],
        baseline_id=row["baseline_id"],
        notify_email=row["notify_email"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_enqueued_at=(
            datetime.fromisoformat(row["last_enqueued_at"]) if row["last_enqueued_at"] else None
        ),
    )


def _row_to_job_schedule(row: aiosqlite.Row) -> JobSchedule:
    return JobSchedule(
        tenant_id=row["tenant_id"],
        name=row["name"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        cadence_seconds=row["cadence_seconds"],
        enabled=bool(row["enabled"]),
        input=json.loads(row["input"]),
        notify_email=row["notify_email"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_enqueued_at=(
            datetime.fromisoformat(row["last_enqueued_at"]) if row["last_enqueued_at"] else None
        ),
    )


def _row_to_canary_config(row: aiosqlite.Row) -> CanaryConfig:
    return CanaryConfig(
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        challenger_version=row["challenger_version"],
        champion_version=row["champion_version"],
        weight=row["weight"],
        sticky=bool(row["sticky"]),
        enabled=bool(row["enabled"]),
        auto_promote=bool(row["auto_promote"]),
        eval_gate=row["eval_gate"],
        auto_rollback=bool(row["auto_rollback"]),
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_trigger(row: aiosqlite.Row) -> Trigger:
    return Trigger(
        tenant_id=row["tenant_id"],
        name=row["name"],
        trigger_id=row["trigger_id"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        secret_hash=row["secret_hash"],
        salt=row["salt"],
        input_defaults=json.loads(row["input_defaults"]),
        enabled=bool(row["enabled"]),
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_fired_at=(
            datetime.fromisoformat(row["last_fired_at"]) if row["last_fired_at"] else None
        ),
    )


def _row_to_tenant_provider_key(row: aiosqlite.Row) -> TenantProviderKey:
    return TenantProviderKey(
        tenant_id=row["tenant_id"],
        provider=row["provider"],
        ciphertext=row["ciphertext"],
        fingerprint=row["fingerprint"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_run(row: aiosqlite.Row) -> RunRecord:
    keys = row.keys() if hasattr(row, "keys") else []
    # ADR 024 D2 — per-step arrays. Columns absent on pre-migration rows (or
    # NULL when this run predates the executor populating them) → empty lists,
    # so legacy records load + render as a single node, no crash.
    skill_calls: list[SkillCallRecord] = []
    if "skill_calls" in keys and row["skill_calls"]:
        skill_calls = [SkillCallRecord.model_validate(c) for c in json.loads(row["skill_calls"])]
    turns: list[TurnRecord] = []
    if "turns" in keys and row["turns"]:
        turns = [TurnRecord.model_validate(t) for t in json.loads(row["turns"])]
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
        input=json.loads(row["input"]),
        output=json.loads(row["output"]) if row["output"] else None,
        metrics=Metrics.model_validate_json(row["metrics"]),
        error=ErrorInfo.model_validate_json(row["error"]) if row["error"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        workflow_run_id=row["workflow_run_id"] if "workflow_run_id" in keys else None,
        node_id=row["node_id"] if "node_id" in keys else None,
        thread_id=row["thread_id"] if "thread_id" in keys else None,
        skill_calls=skill_calls,
        turns=turns,
        # ADR 050 D1 — voice-specific fields. NULL / absent on pre-migration
        # or text-only rows → None, so existing records load unchanged.
        modality=_opt_col(row, "modality") if "modality" in keys else None,
        stt_latency_ms=_opt_col(row, "stt_latency_ms") if "stt_latency_ms" in keys else None,
        tts_latency_ms=_opt_col(row, "tts_latency_ms") if "tts_latency_ms" in keys else None,
        audio_duration_s=_opt_col(row, "audio_duration_s") if "audio_duration_s" in keys else None,
        stt_cost_usd=_opt_col(row, "stt_cost_usd") if "stt_cost_usd" in keys else None,
        tts_cost_usd=_opt_col(row, "tts_cost_usd") if "tts_cost_usd" in keys else None,
    )


def _row_to_workflow_run(row: aiosqlite.Row) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=row["workflow_run_id"],
        tenant_id=row["tenant_id"],
        workflow=row["workflow"],
        workflow_version=row["workflow_version"],
        status=WorkflowStatus(row["status"]),
        initial_state=json.loads(row["initial_state"]),
        final_state=json.loads(row["final_state"]) if row["final_state"] else None,
        error_node_id=row["error_node_id"],
        error=ErrorInfo.model_validate_json(row["error"]) if row["error"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        # ADR 017 D5 (PR 1): HITL checkpoint. NULL on pre-migration / non-paused
        # rows → None, so old records load unchanged.
        paused_node_id=row["paused_node_id"],
        paused_state=json.loads(row["paused_state"]) if row["paused_state"] else None,
        human_task=json.loads(row["human_task"]) if row["human_task"] else None,
        # ADR 062 D2: backend owner. NULL on pre-migration / native rows → None.
        runtime=row["runtime"],
    )


def _row_to_job(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        kind=JobKind(row["kind"]),
        target=row["target"],
        status=JobStatus(row["status"]),
        input=json.loads(row["input"]),
        result_run_id=row["result_run_id"],
        error=ErrorInfo.model_validate_json(row["error"]) if row["error"] else None,
        api_key_id=row["api_key_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        claimed_at=datetime.fromisoformat(row["claimed_at"]) if row["claimed_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        notify_email=row["notify_email"],
        # Defensive: rows from pre-retry-migration schemas could
        # theoretically lack the column on a fresh open. aiosqlite.Row
        # KeyError on missing column, so we use bracket access only —
        # the migrations have run by the time we get here.
        attempt_count=row["attempt_count"],
        next_retry_at=(
            datetime.fromisoformat(row["next_retry_at"]) if row["next_retry_at"] else None
        ),
        # PR-Q thread linkage. init() has run the migration by the
        # time we read here, so the column is guaranteed to exist.
        thread_id=row["thread_id"],
        # ADR 016 D3 canary-chosen version. init() has run the ALTER by
        # the time we read here; .get() stays defensive against a row
        # read on a connection that somehow predates the migration —
        # such a row is a pre-canary job, which is exactly None.
        target_version=dict(row).get("target_version"),
        # ADR 017 D5 (PR 2) HITL resume target. init() has run the ALTER by
        # the time we read here; .get() stays defensive against a pre-PR-2
        # row, which is a non-resume job — exactly None.
        resume_workflow_run_id=dict(row).get("resume_workflow_run_id"),
        # item 17 batch linkage. init() has run the ALTER by the time we read
        # here; .get() stays defensive against a pre-batch row, which is a
        # non-batch job — exactly None.
        batch_id=dict(row).get("batch_id"),
        # item 32 (ADR 019): W3C trace-context carrier (JSON text). init() has
        # run the ALTER by the time we read here; NULL (pre-R2 row, or a job
        # enqueued with OTel off) → {} so the worker starts a fresh root span,
        # byte-for-byte the pre-R2 behaviour. .get() stays defensive against a
        # row predating the migration.
        trace_context=_loads_trace_context(dict(row).get("trace_context")),
        # item 36 (R4b): cooperative-cancel flag stored as INTEGER (0/1).
        # init() has run the ALTER by the time we read here; .get() stays
        # defensive against a row predating the migration (NULL/missing → 0 →
        # False), which is exactly a never-cancelled job.
        cancel_requested=bool(dict(row).get("cancel_requested") or 0),
    )


def _row_to_batch(row: aiosqlite.Row) -> BatchRecord:
    return BatchRecord(
        batch_id=row["batch_id"],
        tenant_id=row["tenant_id"],
        agent=row["agent"],
        total=row["total"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_api_key(row: aiosqlite.Row) -> ApiKeyRecord:
    # expires_at, scope, and scopes may be absent on rows from pre-migration
    # schemas (before the ALTER TABLE migrations ran). dict() access raises
    # KeyError for missing columns; use .get() via the keys() approach instead.
    row_dict = dict(row)
    return ApiKeyRecord(
        key_id=row_dict["key_id"],
        tenant_id=row_dict["tenant_id"],
        env=ApiKeyEnv(row_dict["env"]),
        secret_hash=row_dict["secret_hash"],
        salt=row_dict["salt"],
        label=row_dict["label"],
        created_at=datetime.fromisoformat(row_dict["created_at"]),
        last_used_at=(
            datetime.fromisoformat(row_dict["last_used_at"]) if row_dict["last_used_at"] else None
        ),
        revoked_at=(
            datetime.fromisoformat(row_dict["revoked_at"]) if row_dict["revoked_at"] else None
        ),
        expires_at=(
            datetime.fromisoformat(row_dict["expires_at"]) if row_dict.get("expires_at") else None
        ),
        scope=row_dict.get("scope"),
        # NULL / missing column → empty list (legacy default applies at
        # check time via effective_scopes). Stored as a JSON array.
        scopes=_decode_scopes(row_dict.get("scopes")),
    )


def _decode_scopes(raw: object) -> list[str]:
    """Decode the JSON-encoded ``scopes`` column → list of scope strings.

    Tolerant of NULL/empty (legacy rows → ``[]``) and of a bare-string
    legacy value (defensive — should always be a JSON array)."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(decoded, list):
            return [str(s) for s in decoded]
    return []


def _loads_trace_context(raw: object) -> dict[str, str]:
    """Decode the JSON-encoded ``jobs.trace_context`` column → carrier dict.

    Tolerant of NULL/empty (pre-R2 rows, or a job enqueued with OTel off →
    ``{}`` so the worker starts a fresh root span) and of a malformed value
    (defensive — should always be a JSON object of ``str → str``)."""
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if isinstance(decoded, dict):
            return {str(k): str(v) for k, v in decoded.items()}
    return {}


def _row_to_tool_descriptor(row: Any) -> Any:
    """Convert a sqlite row to a ToolDescriptor."""
    from movate.core.tool_registry.models import (  # noqa: PLC0415
        ToolBackendConfig,
        ToolDescriptor,
        ToolGovernance,
    )

    def _json_or_raw(val: Any) -> Any:
        return json.loads(val) if isinstance(val, str) else val

    backend_data = _json_or_raw(row["backend"])
    governance_data = _json_or_raw(row["governance"])
    tags_data = _json_or_raw(row["tags"])
    input_schema_data = _json_or_raw(row["input_schema"])
    output_schema_data = _json_or_raw(row["output_schema"])

    created_at = None
    if row["created_at"]:
        created_at = datetime.fromisoformat(row["created_at"])
    updated_at = None
    if row["updated_at"]:
        updated_at = datetime.fromisoformat(row["updated_at"])

    backend = ToolBackendConfig(**backend_data) if isinstance(backend_data, dict) else backend_data
    governance = (
        ToolGovernance(**governance_data) if isinstance(governance_data, dict) else governance_data
    )

    return ToolDescriptor(
        name=row["name"],
        version=row["version"],
        scope=row["scope"],
        description=row["description"],
        tags=tags_data,
        input_schema=input_schema_data,
        output_schema=output_schema_data,
        backend=backend,
        credentials_ref=row["credentials_ref"],
        governance=governance,
        owner=row["owner"],
        created_at=created_at,
        updated_at=updated_at,
        tenant_id=row["tenant_id"],
        project_id=_opt_col(row, "project_id"),
    )


def _first_of_month_utc() -> datetime:
    """Midnight UTC on the 1st of the current month.

    The boundary for ``sum_tenant_cost_current_month``. The postgres
    backend has its own equivalent in :mod:`movate.storage.postgres`
    so the two implementations stay in lockstep on month-boundary
    semantics (operator's local time treating Jan 1 UTC as start of
    January).
    """
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
