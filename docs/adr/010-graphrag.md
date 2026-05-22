# ADR 010 — GraphRAG behind the StorageProvider Protocol

**Status:** Proposed
**Date:** 2026-05-22
**Deciders:** Engineering
**Context window:** Knowledge-graph phase (the "next phase" after pgvector)
**Supersedes:** N/A
**Related:** [ADR 001 — Cloud portability](001-cloud-portability.md) (the
StorageProvider-Protocol precedent); [ADR 009 — pgvector KB storage](009-pgvector-kb-storage.md)
(entity embeddings reuse the same `vector(N)` convention);
`src/movate/kb/graph_extract.py`, `src/movate/kb/graph_retrieval.py`,
`src/movate/storage/base.py` (graph methods), `src/movate/core/models.py`
(`Entity` / `Relation`)

---

## Decision

Add **GraphRAG** — entity/relation extraction + graph-aware retrieval — as an
**optional, additive** capability that lives **behind the existing
`StorageProvider` Protocol**, implemented across all current backends
(InMemory / SQLite / Postgres), with no new hard dependency.

Concretely:

1. **Extraction** (`kb.graph_extract.extract_graph`): KB chunks → a merged,
   deduped, embedded knowledge graph (`Entity` / `Relation`) via an **injected
   LLM completion function** (`complete_fn`, default LiteLLM-backed).
2. **Storage**: the Protocol gains `upsert_entity`, `upsert_relation`,
   `search_entities` (vector), `get_entity`, `list_entities`,
   `list_relations`, `delete_graph` — implemented in `InMemoryStorage`,
   `SqliteProvider`, and `PostgresProvider`. Entity vectors use the **same
   `vector(N)` + embedding-model convention as ADR 009**.
3. **Retrieval** (`kb.graph_retrieval.graph_retrieve`): seed by entity vector
   search → expand `hops` → cap `max_relations` → a `GraphContext` rendered
   into the prompt. It **complements** (does not replace) vector chunk
   retrieval.
4. **Optional everywhere**: graph build at ingest and graph retrieval at run
   time are both opt-in; `agent.yaml` stays backward-compatible.

In one sentence: **"a knowledge graph is just more rows behind the same storage
Protocol — extracted by an injectable LLM, embedded like KB chunks, and
retrieved as an optional complement to vector search."**

---

## Context

Vector KB retrieval (ADR 009) answers "which chunks are semantically near the
query?" It struggles with **multi-hop / relational** questions — "which
incidents did the vendor that owns service X cause?" — where the answer spans
entities connected by relations rather than living in one chunk. GraphRAG
addresses this by extracting a typed entity/relation graph from the same
corpus and retrieving a connected subgraph as additional prompt context.

The standing direction (project memory): *GraphRAG behind the StorageProvider
Protocol, multi-backend (SQLite + Postgres), Neo4j optional later; cross-cloud,
flexible for internal + customer deploys.* This ADR commits that direction.

The risk this ADR guards against: GraphRAG is exactly the kind of feature that
invites a heavy new dependency (a graph DB) and a parallel storage path that
bypasses the Protocol — both of which would break the cloud-portability
contract. The decision is to resist that.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Cloud portability** — must work on the same backends we already ship (ADR 001); no Neo4j hard dependency | HIGH |
| **Protocol stability** — graph storage is *additive*; existing KB/runs/jobs methods unchanged | HIGH |
| **Backward compatibility** — `agent.yaml`, ingest, and vector retrieval keep working untouched when graph is off | HIGH |
| **Reuse over reinvention** — entity embeddings reuse the ADR 009 `vector(N)` infra + the configured embedding model | HIGH |
| **Testability** — extraction must not require a live LLM in tests (injected `complete_fn`) | HIGH |
| **Cost control** — LLM extraction is per-chunk and not free; must be opt-in | MED |

---

## Architecture

```
mdk kb ingest (--build-graph, opt-in)
        │ chunks (already embedded, ADR 009)
        ▼
  extract_graph(chunks, complete_fn=…)        ── LLM extraction
        │  entities + relations (deduped, provenance, embedded)
        ▼
  storage.upsert_entity / upsert_relation     ── behind the Protocol
        │
        ▼
  InMemory · SQLite · Postgres(pgvector)       ── same backends as KB

agent run (graph retrieval, opt-in)
  graph_retrieve(query, hops, …)
        │  seed = search_entities(query_vec)  → expand relations → cap
        ▼
  GraphContext → render_graph_context() → prompt   (alongside vector chunks)
```

`search_entities` is the only vector-touching graph method; on Postgres it uses
the same `<=>` / HNSW path as `search_kb_chunks` (ADR 009). Everything else is
ordinary row CRUD scoped by `(agent, tenant_id)`.

---

## Decisions

### Decision 1 (D1): GraphRAG lives behind the `StorageProvider` Protocol

Graph persistence is expressed as new Protocol methods, implemented in every
backend — not a separate graph-DB client. This keeps the cloud-portability
boundary (ADR 001) intact: a customer deploying on SQLite or Azure Postgres
gets GraphRAG with zero new infrastructure.

**Why not Neo4j (or a dedicated graph DB) now?** It would be a hard dependency
and a second storage path that bypasses the Protocol — the exact coupling ADR
001 forbids. A Neo4j (or AGE / Neptune) backend can be added **later as another
`StorageProvider` implementation** behind the same methods, with no caller
changes. Deferred, not precluded.

### Decision 2 (D2): LLM extraction via an injected `complete_fn`

`extract_graph` takes a `complete_fn` (prompt → text); the default is
LiteLLM-backed (`DEFAULT_EXTRACTION_MODEL`), so any provider works and tests
inject a stub returning canned JSON — no live LLM in CI. Extraction is
tolerant (strips code fences, defensive JSON parse) and **never raises**: a
failing chunk is skipped, not fatal to the batch.

Entities are deduped by normalized `(name, type)`, relations by
`(resolved endpoints, type)`; provenance (`source_chunk_ids`) is unioned across
the chunks a record came from. Relations whose endpoints didn't resolve to an
extracted entity are dropped (no dangling edges).

### Decision 3 (D3): Entity embeddings reuse the ADR 009 vector convention

Entity text is embedded with the **same model the KB chunks used**
(`embedding_model`), so query-time cosine between the question vector and entity
vectors is comparable, and Postgres stores them in the same `vector(N)` shape
with the same HNSW path. No second embedding pipeline, no dim drift — the
`MOVATE_EMBED_MODEL` / `MOVATE_EMBED_DIM` config (ADR 009 Task 5) governs both.

### Decision 4 (D4): Graph retrieval complements vector retrieval; both opt-in

`graph_retrieve` seeds from entity vector search, expands a bounded neighborhood
(`hops`, `max_relations`), and returns a `GraphContext` rendered into the
prompt **alongside** the existing vector chunks — it does not replace them. The
graph is built only when ingest is asked to (`--build-graph`-style opt-in) and
retrieved only when the agent opts in, so the default path and cost profile are
unchanged.

### Decision 5 (D5): Additive, backward-compatible surface

The Protocol *grows* but nothing existing changes; `agent.yaml` / `project.yaml`
schemas stay valid with no graph config; an agent with no graph simply gets no
graph context. This is a non-breaking addition.

---

## Consequences

**Positive**
- Multi-hop / relational retrieval on the backends we already ship — no new infra.
- Reuses the pgvector + embedding-config investment (ADR 009) rather than forking it.
- A future Neo4j/AGE backend is "just another `StorageProvider`," not a rewrite.

**Negative / costs**
- The Protocol surface grows by ~7 methods × 3 backends — more conformance
  surface to test (mitigated: the parametrized `storage` fixture covers all three).
- LLM extraction has real $ + latency cost per chunk (mitigated: opt-in; cheap
  default model; tolerant/skip-on-failure).
- Graph quality depends on the extractor LLM + prompt — needs its own eval
  rubric over time (out of scope here).

**Neutral**
- Adds an extraction model default (`DEFAULT_EXTRACTION_MODEL`); overridable.
- Postgres-gated graph-storage tests need a real PG run (CI's postgres job).

---

## Implementation status

A reference implementation exists on the GraphRAG branch (PR #341): extraction,
retrieval, the seven Protocol methods across all three backends, `Entity` /
`Relation` models, and tests (55 passing locally; the graph-storage tests are
postgres-gated). This ADR is the design gate for landing it; the playground
py314-warning bit bundled in that branch should be split into its own PR before
merge.
