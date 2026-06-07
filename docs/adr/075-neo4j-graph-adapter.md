# ADR 075 — Neo4j graph adapter behind StorageProvider Protocol (opt-in `[neo4j]` extra)

**Status:** Accepted
**Date:** 2026-06-03
**Deciders:** Engineering
**Builds on:** ADR 010 (GraphRAG extraction), ADR 046 (graph query API)

## Context

The existing GraphRAG knowledge graph is stored in relational tables
(`kb_entities` / `kb_relations`) on Postgres or SQLite, with k-hop
traversal via recursive CTEs. This works well for small-to-medium graphs
(sub-100k nodes) but dedicated graph databases offer better traversal
performance at scale and native Cypher/GQL query expressiveness.

Customers with existing Neo4j infrastructure want to route graph operations
to Neo4j without replacing the relational store for runs, jobs, API keys,
and other non-graph data.

## Decision

1. **New adapter**: `src/movate/storage/neo4j.py` — a `Neo4jStorageProvider`
   implementing the graph-related slice of the `StorageProvider` Protocol
   using Cypher against the Neo4j Python driver.

2. **Graph-only delegate**: the adapter implements `upsert_entity`,
   `upsert_relation`, `list_entities`, `list_relations`, `search_entities`,
   `expand_neighbors`, `get_entity`, and `delete_graph`. All non-graph
   methods raise `NotImplementedError`.

3. **`MOVATE_GRAPH_BACKEND=neo4j` env var**: selects the Neo4j adapter for
   graph operations. The relational backend (Postgres/SQLite) handles
   everything else. `build_graph_storage()` returns the graph delegate;
   callers fall back to the primary backend when `None`.

4. **`[neo4j]` optional extra**: `neo4j>=5.0,<6` (Apache-2.0). Lazy-
   imported — never loaded unless `NEO4J_URI` is configured.

5. **Credentials**: `NEO4J_URI` + `NEO4J_USER` + `NEO4J_PASSWORD` env vars,
   autoloaded from `~/.movate/credentials` via `mdk auth login neo4j`.

6. **Vector search**: loads entities from Neo4j, ranks by cosine similarity
   in Python (same `_cosine.rank_entities_by_cosine` the other backends
   use). A future optimization could use Neo4j's vector index (5.11+).

7. **k-hop expansion**: BFS in Python with Cypher per-hop (fetch incident
   edges per frontier). Portable across Neo4j versions without APOC/GDS
   dependency.

## Cypher patterns

- **Entity upsert**: `MERGE (n:Entity {agent, tenant_id, content_hash})
  ON CREATE SET ... ON MATCH SET ...` with `source_chunk_ids` UNION via
  `REDUCE`.
- **Relation upsert**: `MATCH (src), (dst) MERGE (src)-[r:RELATION {agent,
  tenant_id, content_hash}]->(dst) ON CREATE/MATCH SET ...`
- **Delete graph**: `MATCH (n:Entity {agent, tenant_id}) DETACH DELETE n`
- **List**: `MATCH (n:Entity {agent, tenant_id}) RETURN n ORDER BY
  n.created_at DESC LIMIT $limit`

## Consequences

- Graph operations can be routed to a dedicated Neo4j instance at scale.
- No change to the relational storage path (backward-compatible).
- The `neo4j` package is never imported unless explicitly configured.
- Source-scoped `delete_graph` (the `source` parameter) is not fully
  supported because chunk-to-source resolution requires the relational
  store; it falls back to a full-agent delete with a warning.
