# ADR 009 — pgvector KB storage on Azure Postgres

**Status:** Accepted
**Date:** 2026-05-22
**Deciders:** Engineering
**Context window:** Knowledge-graph phase (durable KB vector store)
**Supersedes:** N/A
**Related:** [ADR 001 — Cloud portability](001-cloud-portability.md) for the
StorageProvider-Protocol precedent; `src/movate/storage/base.py`
(`StorageProvider` Protocol, KB methods); `src/movate/storage/postgres.py`
(`PostgresProvider`); `src/movate/storage/_cosine.py` (`rank_chunks_by_cosine`);
`src/movate/kb/embed.py` (`embed_texts`)

---

## Decision

Make Postgres the durable vector store for KB embeddings using **pgvector**,
replacing the brute-force Python cosine that runs over JSONB-stored arrays
today. Concretely:

1. **`kb_chunks.embedding` becomes a `vector(N)` column** (was `JSONB`), where
   `N` is a fixed, explicitly-configured embedding dimension
   (`MOVATE_EMBED_DIM`, default `1536`).
2. **Vector search runs in SQL** — `ORDER BY embedding <=> $query LIMIT k` with
   an **HNSW** index — instead of loading all rows and ranking in Python.
3. **The `StorageProvider` Protocol does not change.** pgvector is an internal
   detail of `PostgresProvider`. SQLite and the in-memory double keep the
   Python-cosine engine (`_cosine.py`).
4. **The JSONB→`vector` migration casts existing data in place** (lossless),
   gated by a new minimal `schema_migrations` runner. Re-embedding is a
   separate, explicit, paid operation.

In one sentence: **"swap the Postgres KB column to `vector(N)` + HNSW and push
similarity into SQL, behind the unchanged storage Protocol, casting existing
JSONB embeddings in place."**

---

## Context

`mdk kb ingest` chunks documents, embeds them (`embed_texts`,
default `text-embedding-3-small`, 1536-dim), and persists each chunk via
`storage.save_kb_chunk`. Retrieval (`kb/search.py`) embeds the query and calls
`storage.search_kb_chunks` (vector) or fuses it with lexical search via RRF.

The deployed Azure runtime already uses Postgres (`build_storage` selects
`PostgresProvider` when `MOVATE_DB_URL` is a `postgres://` URL, which the bicep
sets). But **vectors are stored as JSONB and similarity is computed in Python**:
`PostgresProvider.search_kb_chunks` loads every `(agent, tenant_id)` row and
calls `rank_chunks_by_cosine`. The code says so itself
(`postgres.py`: *"cosine similarity computed in Python over JSONB-stored float
arrays; pgvector will swap in later"*; `models.py` `KbChunk`: *"the storage
protocol is shaped so a later pgvector swap is the only diff"*).

This O(n)-per-query scan does not scale past ~10k chunks and wastes the durable
Postgres backend we already deploy. This ADR cashes the standing "pgvector
later" promise.

Two real constraints shape the design:

* **A `vector(N)` column needs a fixed `N`**, but `embed_texts` can route
  through LiteLLM to models of varying dimension. Mixing dims in one KB is
  already semantically unsupported — `_cosine.py` hard-errors on a dim
  mismatch, and similarity scores are only comparable within one model.
* **Existing tests seed 2-, 3-, and 4-dimensional embeddings** for fast unit
  coverage. A fixed-dim Postgres column cannot hold these, so the small-dim
  paths must stay on the in-memory/SQLite backends.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Protocol stability** — KB method signatures must not change; callers (RRF fusion, CLI score thresholds) untouched | HIGH |
| **Scale** — replace O(n) Python cosine with an indexed ANN query | HIGH |
| **Safe migration** — no data loss moving JSONB→`vector` on a populated prod DB | HIGH |
| **Azure-correctness** — works on Azure Postgres Flexible Server (extension allow-list, PG 16) | HIGH |
| **Minimal new surface** — reuse `_cosine.py` for SQLite/InMemory; introduce the smallest viable migration mechanism | MED |
| **Multi-model future** — leave room for per-collection dims later without paying for it now | LOW |

---

## Architecture

```
mdk kb ingest                         agent run (retrieval)
      │                                       │
  embed_texts ─► save_kb_chunk          embed query ─► search_kb_chunks
      │                                       │
      ▼                                       ▼
 PostgresProvider                       PostgresProvider
   embedding vector(N)                    SELECT 1-(embedding <=> $q) AS score
   HNSW (vector_cosine_ops)               ORDER BY embedding <=> $q LIMIT k
                                          │
 SqliteProvider / InMemoryStorage  ──────┘ (unchanged)
   embedding JSON  ──►  rank_chunks_by_cosine (_cosine.py)
```

`search_kb_chunks(*, agent, tenant_id, query_embedding, limit) ->
list[KbChunkWithScore]` is identical across all three backends. Only the body of
the Postgres implementation changes.

---

## Decisions

### Decision 1 (D1): Fixed embedding dimension per column, from explicit config

The `kb_chunks.embedding` column is `vector(N)` where `N` comes from a new
`MOVATE_EMBED_DIM` setting (default `1536`, matching `text-embedding-3-small`).
Writes whose vector length ≠ `N` are rejected at the boundary with the same
error shape `_cosine.py` already raises on a dim mismatch.

**Why fixed dim, not per-agent collections or a multi-column discriminator?**
One embedding model per deployment is the product reality
(`MOVATE_EMBED_MODEL`). Per-collection tables multiply the schema / index /
migration surface for a multi-model future that has no concrete requirement
(the `KbChunk` docstring explicitly defers cross-agent KBs). A multi-column
discriminator is the worst of both — every query branches on dim and HNSW can't
span columns. HNSW also requires `dims <= 2000` and a literal `N`, so
"store any dim in one indexed column" is not achievable regardless.

**Consequence:** the small-dim (2/3/4) unit tests stay on InMemory/SQLite;
Postgres KB tests run at the configured dim (1536 by default, overridable for
test speed).

### Decision 2 (D2): The storage Protocol is unchanged; pgvector is internal

`StorageProvider`'s KB methods keep their exact signatures. `PostgresProvider`
internally stores `vector(N)` and runs `ORDER BY embedding <=> $1 LIMIT k`,
mapping the pgvector cosine **distance** operator `<=>` back to the existing
**similarity** convention as `score = 1 - distance`. Callers that depend on a
0–1 similarity (RRF fusion in `kb/search.py`, the CLI's score-color thresholds)
see no change. SQLite and `InMemoryStorage` keep `rank_chunks_by_cosine`
untouched, which also remains the reference oracle for tests.

**Why not push pgvector into the Protocol?** The Protocol is the cloud-portability
boundary (ADR 001). Leaking a Postgres-specific type into it would force every
backend (including the in-memory test double) to model `vector`, defeating the
abstraction.

### Decision 3 (D3): HNSW index, not ivfflat

Use `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`. HNSW gives
better recall/latency at query time and **does not need a pre-populated table**
to build well (ivfflat needs representative data for good centroids and benefits
from periodic rebuilds). Azure Postgres Flexible Server supports pgvector HNSW
on PG 16 (our pinned default). This explicitly overrides the older "ivfflat"
note left in `postgres.py`.

### Decision 4 (D4): Backfill casts JSONB→`vector` in place; never auto-re-embeds

The stored JSONB float arrays *are* the model output, so casting
`embedding::text::vector` (or array→vector) is lossless and free. Re-embedding
costs money, needs the provider key in the pod, and can drift if the model
version changed. Therefore `mdk kb reindex` casts in place by default; a model
change is the separate, explicit `mdk kb reembed`. The migration validates that
every existing row's array length equals the configured `N` and aborts — without
dropping the old column — if any row mismatches (the one-way-door safety check).

### Decision 5 (D5): A minimal `schema_migrations` runner (Postgres only)

There is no migration framework today — `PostgresProvider.init()` runs one
`CREATE TABLE IF NOT EXISTS` blob. The JSONB→`vector` column change can't be
expressed as `IF NOT EXISTS`, so we introduce a small
`schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ)` table plus
an ordered list of migrations applied transactionally in `init()`. This is the
first migration framework; it is deliberately minimal (no Alembic — too heavy
for this codebase's `IF NOT EXISTS` style). SQLite keeps its existing ad-hoc
`_MIGRATIONS` list; the divergence is intentional and documented here.

---

## Consequences

**Positive**

* KB search becomes an indexed ANN query — scales past the ~10k-chunk wall the
  current code warns about.
* The durable Azure Postgres we already deploy now actually serves vectors.
* No caller changes: the Protocol and the 0–1 score contract are preserved.

**Negative / costs**

* **The embedding dimension is a one-way door.** Changing `N` later requires a
  full re-embed + migration. Mitigated by explicit `MOVATE_EMBED_DIM`, a doctor
  check that the configured dim matches the live column, and `mdk kb reembed`.
* **Azure requires `azure.extensions = VECTOR`** to be allow-listed (a
  server-config change that may require a restart) *before* the runtime boots
  with the `vector` column. Sequencing-sensitive; covered by the deploy plan.
* **The first prod migration is destructive at the `DROP COLUMN` step.**
  Mitigated by a single transaction, validate-before-drop, abort-on-mismatch,
  `schema_migrations` idempotency, and a snapshot before first apply (Flexible
  Server has 7-day PITR).
* SQLite/Postgres now diverge in both vector engine and migration mechanism —
  acceptable, since SQLite is the local/dev path and Postgres is the durable
  deployment path.

**Neutral**

* Adds `pgvector>=0.3` to the `runtime` extra (ships
  `pgvector.asyncpg.register_vector`); not installed for the default CLI.
