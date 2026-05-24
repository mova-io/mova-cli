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
from datetime import UTC, datetime
from typing import Any

import asyncpg

from movate.core.models import (
    AgentBundleRecord,
    ApiKeyEnv,
    ApiKeyRecord,
    BenchModelResult,
    BenchRecord,
    CanaryConfig,
    ConversationThread,
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
    Relation,
    RunRecord,
    Subgraph,
    TenantBudget,
    Trigger,
    WorkflowRunRecord,
    WorkflowStatus,
)
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
        """
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for version, migrate in _MIGRATIONS:
            if version in applied:
                continue
            async with conn.transaction():
                await migrate(conn)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)

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
                workflow_run_id, node_id, thread_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18
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
                total_cost_usd, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15
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
        )

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        row = await self._db.fetchrow(
            "SELECT * FROM evals WHERE eval_id = $1 AND tenant_id = $2",
            eval_id,
            tenant_id,
        )
        return _row_to_eval(row) if row else None

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

    # ------------------------------------------------------------------
    # Canary configs (ADR 016 D3 — champion/challenger rollout)
    # ------------------------------------------------------------------

    async def save_canary_config(self, config: CanaryConfig) -> None:
        await self._db.execute(
            """
            INSERT INTO canary_configs (
                tenant_id, agent, challenger_version, champion_version, weight,
                sticky, enabled, auto_promote, eval_gate, created_by,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
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
                updated_at = EXCLUDED.updated_at
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
    # Workflow runs
    # ------------------------------------------------------------------

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO workflow_runs (
                workflow_run_id, tenant_id, workflow, workflow_version,
                status, initial_state, final_state, error_node_id, error,
                created_at, paused_node_id, paused_state, human_task
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
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
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM workflow_runs {where} ORDER BY created_at DESC LIMIT ${len(params)}"
        rows = await self._db.fetch(sql, *params)
        return [_row_to_workflow_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO jobs (
                job_id, tenant_id, kind, target, status, input,
                result_run_id, error, api_key_id,
                created_at, claimed_at, completed_at,
                notify_email, attempt_count, next_retry_at, thread_id,
                target_version
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17
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
                entity_id, tenant_id, agent, name, type, description,
                embedding, embedding_model, content_hash, source_chunk_ids,
                metadata, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (agent, tenant_id, content_hash) DO UPDATE
                SET name = EXCLUDED.name,
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
                relation_id, tenant_id, agent, src_entity_id, dst_entity_id,
                type, description, weight, content_hash, source_chunk_ids,
                metadata, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (agent, tenant_id, content_hash) DO UPDATE
                SET src_entity_id = EXCLUDED.src_entity_id,
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
    ) -> list[EntityWithScore]:
        # No pgvector yet — load matching entities and rank in Python,
        # same primitive (and future swap point) as search_kb_chunks.
        rows = await self._db.fetch(
            """
            SELECT entity_id, tenant_id, agent, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities WHERE agent = $1 AND tenant_id = $2
            """,
            agent,
            tenant_id,
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
    ) -> Subgraph:
        if not entity_ids:
            return Subgraph(entities=[], relations=[])
        # Recursive CTE: bounded k-hop reachability from the seeds.
        # Undirected for reachability; UNION dedups so cycles terminate,
        # depth < hops bounds the walk.
        reach_rows = await self._db.fetch(
            """
            WITH RECURSIVE reachable(eid, depth) AS (
                SELECT e, 0 FROM unnest($1::text[]) AS e
              UNION
                SELECT CASE WHEN r.src_entity_id = reachable.eid
                            THEN r.dst_entity_id ELSE r.src_entity_id END,
                       reachable.depth + 1
                FROM kb_relations r
                JOIN reachable
                  ON (r.src_entity_id = reachable.eid OR r.dst_entity_id = reachable.eid)
                WHERE reachable.depth < $2 AND r.agent = $3 AND r.tenant_id = $4
            )
            SELECT DISTINCT eid FROM reachable
            """,
            entity_ids,
            int(hops),
            agent,
            tenant_id,
        )
        reachable = [r["eid"] for r in reach_rows]
        if not reachable:
            return Subgraph(entities=[], relations=[])
        rel_rows = await self._db.fetch(
            """
            SELECT relation_id, tenant_id, agent, src_entity_id, dst_entity_id,
                   type, description, weight, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_relations
            WHERE agent = $1 AND tenant_id = $2
              AND src_entity_id = ANY($3::text[]) AND dst_entity_id = ANY($3::text[])
            ORDER BY weight DESC, relation_id LIMIT $4
            """,
            agent,
            tenant_id,
            reachable,
            int(limit),
        )
        relations = [_row_to_relation(r) for r in rel_rows]
        keep = set(entity_ids)
        for rel in relations:
            keep.add(rel.src_entity_id)
            keep.add(rel.dst_entity_id)
        ent_rows = await self._db.fetch(
            """
            SELECT entity_id, tenant_id, agent, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities
            WHERE agent = $1 AND tenant_id = $2 AND entity_id = ANY($3::text[])
            """,
            agent,
            tenant_id,
            list(keep),
        )
        return Subgraph(
            entities=[_row_to_entity(r) for r in ent_rows],
            relations=relations,
        )

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        row = await self._db.fetchrow(
            """
            SELECT entity_id, tenant_id, agent, name, type, description,
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
    ) -> list[Entity]:
        rows = await self._db.fetch(
            """
            SELECT entity_id, tenant_id, agent, name, type, description,
                   embedding, embedding_model, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_entities WHERE agent = $1 AND tenant_id = $2
            ORDER BY created_at DESC LIMIT $3
            """,
            agent,
            tenant_id,
            int(limit),
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
    ) -> list[Relation]:
        rows = await self._db.fetch(
            """
            SELECT relation_id, tenant_id, agent, src_entity_id, dst_entity_id,
                   type, description, weight, content_hash, source_chunk_ids,
                   metadata, created_at
            FROM kb_relations WHERE agent = $1 AND tenant_id = $2
            ORDER BY created_at DESC LIMIT $3
            """,
            agent,
            tenant_id,
            int(limit),
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


# ---------------------------------------------------------------------------
# Row → model converters
# ---------------------------------------------------------------------------


def _row_to_run(row: asyncpg.Record) -> RunRecord:
    metrics_data = dict(row["metrics"])
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


def _row_to_eval(row: asyncpg.Record) -> EvalRecord:
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
