# ADR 046 — Knowledge Graph surface: store, query API, growth stream, and sigma.js visualization

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x — GraphRAG **extraction** is wired (entities + typed
relationships are extracted and **persisted** when KB content is ingested), but
the graph is **invisible and unqueryable**: there is no read API, no retrieval
surface in `RetrievalConfig`, and no way to *see* the graph. This ADR turns the
already-extracted graph into a **first-class, queryable, visualizable** entity —
project-scoped, served over `/api/v1`, streamed as it grows, and rendered as a
drillable WebGL graph.
**Builds on / related:**
ADR 010 (GraphRAG behind the `StorageProvider` Protocol — extraction +
`upsert_entity`/`upsert_relation` persistence + `expand_subgraph` traversal
already exist; **this ADR completes ADR 010's unsurfaced retrieval + adds the
read/viz surface it never specified**),
ADR 035 D3 (tenant-scoped SSE event stream — **reused verbatim** for the graph
growth stream; the graph stream is a *typed projection* of the same SSE infra),
ADR 040 (Projects as a first-class cloud entity — the graph is **project-scoped**,
not just agent-scoped; nodes/edges gain a `project_id`),
ADR 023 (opt-in auto-RAG pre-retrieval — the place graph retrieval plugs in:
`retrieval.graph` sits beside the existing `retrieval` block),
ADR 009 (pgvector KB storage — nodes may carry embeddings in the same `vector(N)`
convention, enabling hybrid graph+vector retrieval),
and the **unified KB ingest** path (PR #537, `POST /api/v1/agents/{name}/kb`
text/url/generated/upload kinds → `kb.ingest.ingest_text`) — **the ingest call
that triggers extraction**, and therefore the event source that drives D6's
growth stream.
**Related but separate (do NOT bundle):** the Neo4j `StorageProvider` adapter
(future), the *implementation* of GraphRAG multi-hop retrieval (follow-up — this
ADR only **reserves** the `RetrievalConfig` shape), and any graph-**editing** UI
(the v1 viz is read-only).

---

## Context

### What exists today (the extraction half)

ADR 010 shipped (as "Proposed", reference impl on PR #341) the **extraction +
persistence** half of GraphRAG:

- `kb.graph_extract.extract_graph` turns KB chunks into deduped, embedded
  `Entity` / `Relation` records via an injected `complete_fn`.
- `kb.ingest.ingest_text(..., build_graph=True)` calls extraction at ingest time
  and **persists** the result: `_build_graph_for_chunks` loops
  `storage.upsert_entity` / `storage.upsert_relation`. **This is the load-bearing
  fact for this ADR: the extracted graph is durable rows, not a transient
  in-memory computation.** It lives in `kb_entities` / `kb_relations` on
  SQLite + Postgres behind the `StorageProvider` Protocol, scoped by
  `(agent, tenant_id)`. Each `Entity` already carries `source_chunk_ids`
  (provenance back to the source passages) and `embedding` + `embedding_model`.
- `storage.search_entities` (vector seed), `storage.expand_subgraph` (the one
  bounded k-hop traversal), `get_entity`, `list_entities`, `list_relations`,
  `delete_graph` all exist on the Protocol across InMemory / SQLite / Postgres.

So the data is there. **A read API has rows to read on day one.**

### What does NOT exist (the surface half)

- **No read/query API.** There is no `/api/v1` endpoint to fetch a subgraph, a
  node's detail+provenance, a node's neighbors, or to search nodes. The graph is
  write-only from the outside.
- **No retrieval surface.** `RetrievalConfig` (ADR 023) has **no** `graph` block.
  ADR 010's `graph_retrieve` exists in `kb/` but is **not surfaced** in agent
  config — so agents cannot actually opt into multi-hop graph reasoning. This is
  ADR 010's stated-but-unshipped gap.
- **No visualization.** Nothing renders the graph. Deva's explicit ask is a
  **drillable** graph viz in the spirit of the **sigma.js flagship demo** — WebGL
  rendering, instant search/fly-to, click-to-focus-neighborhood,
  double-click-for-node-detail, and **watch-it-grow** as content is ingested.
- **No project scoping.** Today the graph is per-`(agent, tenant)`. ADR 040 makes
  Projects first-class; the graph should be queryable at the **project** grain
  (a project's whole knowledge graph across its agents/KBs), not only per agent.
- **No layout.** The stored entities have no coordinates, so any viewer would have
  to run a full client-side force layout on every load — which is exactly what
  makes naive graph UIs slow and janky.

### The two graph modes the surface must serve

1. **KNOWLEDGE graph** — entities + typed relationships extracted from KB content
   (the GraphRAG graph). "SAML SSO → governed-by → Security Policy v3."
2. **TOPOLOGY graph** — the *platform's own* structure: agents, workflows, KBs,
   contexts, skills, and how they connect ("Agent A uses KB X; Workflow W calls
   Agent A; Context C is shared into Project P"). This view is essentially free
   once we have a node/edge store and a serializer — it reads from the existing
   registries.

Both are the **same node/edge shape**, distinguished by node `type`. One store,
one API, one viewer; two lenses.

### The drillable-viz target (why serialization + layout are the crux)

A graph UI feels instant or feels broken based on two decisions: **what shape the
server sends** and **who computes the layout**. If the server emits an arbitrary
JSON the client must transform, and the client must lay out every node on load,
the first paint is slow and every interaction stutters. The two resolved
decisions below (R1 graphology-native serialization, R2 persisted coordinates)
exist specifically to make the sigma viewer's first paint *instant* and its live
growth *smooth*. They are the heart of this ADR.

---

## Decision

Make the knowledge graph a **first-class, project-scoped, queryable, streamable,
visualizable** entity behind the existing `StorageProvider` Protocol, served over
`/api/v1`, with **sigma.js + graphology as the reference viewer** and a
graphology-native API contract that any WebGL/Canvas graph library can consume.

### D1 — Graph store behind `StorageProvider`

Generalize ADR 010's `kb_entities`/`kb_relations` into a viz- and
topology-capable node/edge store, still behind the Protocol (no new graph DB):

- **`graph_nodes`**: `id`, `tenant_id` (**NOT NULL**), `project_id`, `type`,
  `label`, `properties` (JSON), `x`, `y`, `size`, `color`, `community`,
  `source_provenance` (JSON), and (optional) `embedding` + `embedding_model`.
- **`graph_edges`**: `id`, `tenant_id` (**NOT NULL**), `project_id`, `source`,
  `target`, `type`/`label`, `weight`, `properties` (JSON).

SQLite + Postgres use **adjacency** (the recursive-CTE traversal ADR 010 already
implements for `expand_subgraph`); **pgvector** powers hybrid graph+vector when
nodes carry embeddings (same `vector(N)` convention as ADR 009). A **Neo4j /
Apache AGE / Neptune adapter is an optional future `StorageProvider` impl**, never
required (R5). The existing `Entity`/`Relation` rows map onto `graph_nodes`/
`graph_edges` (an `Entity` is a `type="entity"`-class node; `source_chunk_ids`
becomes `source_provenance`); extraction confidence, where present, rides in
`properties.confidence` so the viz can filter low-confidence nodes.

> **Compat note (storage schema — flagged per CLAUDE.md rule 5):** this evolves
> the ADR-010 graph schema (adds `project_id`, layout/render columns, and the
> generalized node/edge naming). It is **additive**: existing `(agent, tenant)`
> graph rows migrate forward; `project_id` is backfilled from the agent's project
> (nullable until ADR 040 projects exist). The *implementation* of this migration
> is a follow-up PR — this ADR fixes the target shape only.

### D2 — Two graph modes, one schema, distinguished by node `type`

(a) **KNOWLEDGE** nodes/edges come from KB extraction (GraphRAG); (b)
**TOPOLOGY** nodes/edges are agents/workflows/KBs/contexts/skills and their
connections, projected from the existing registries into the same store (or
assembled on read). The `mode` query param and the node `type` select the lens.
No second store, no second API.

### D3 — graphology-native API serialization (zero client transform)

Every graph-returning endpoint emits **graphology's import JSON shape** directly:

```jsonc
{
  "attributes": { "name": "project-42 knowledge graph" },
  "nodes": [
    { "key": "n_saml_sso",
      "attributes": { "label": "SAML SSO", "x": 0.42, "y": -1.13,
                      "size": 6, "color": "#4f86c6",
                      "type": "entity", "community": 3 } }
  ],
  "edges": [
    { "key": "e_1", "source": "n_saml_sso", "target": "n_sec_policy_v3",
      "attributes": { "label": "governed-by", "weight": 0.8 } }
  ]
}
```

so the client does **`graph.import(response)`** with **zero transform** and hands
the populated graphology instance straight to sigma. The contract is
deliberately library-agnostic (it is *graphology's* documented serialization, not
sigma's internal model), so cytoscape / react-force-graph adapters are a thin
mapping, not a rewrite (R3).

### D4 — Persisted layout coordinates (instant first paint + cheap live growth)

`x`, `y` (and `size`, `color`, `community`) are **stored on the node**, computed
once by **ForceAtlas2** after a graph build and updated **incrementally**
thereafter:

- **First paint is instant** — the server already shipped coordinates; the client
  never runs a full-graph layout on load.
- **Incremental layout** — when the growth stream (D6) adds a node, only the *new*
  node needs placement. The client runs FA2 **on the new node + its
  neighborhood** in a **web worker** (graphology's `forceatlas2.worker`), seeded
  at its neighbors' centroid, leaving the settled bulk of the graph fixed. The
  server periodically (debounced, off the request path) recomputes a full FA2 pass
  and persists fresh coordinates so drift from incremental placement self-heals.
- **Fallback** — if a node has no stored coordinates yet (build hasn't run), the
  client runs a bounded FA2 pass over the returned window only (see Failure
  modes). The API marks `attributes.layout: "computed" | "pending"` so the client
  knows whether to lay out.

### D5 — Query API (read scope, tenant + project scoped)

All endpoints are **read** scope, tenant + project scoped (D10):

- `GET /api/v1/projects/{id}/graph?mode=knowledge|topology&type=&root=&depth=&limit=`
  — a **windowed** subgraph in graphology JSON (never the whole graph; D5/R6).
  `root` + `depth` bound a neighborhood; `type` filters node class; `limit` caps
  node count.
- `GET /api/v1/graph/nodes/{node_id}` — node detail including **PROVENANCE**
  (source chunk + source URL + extraction confidence), `neighbors_count`,
  `referenced_by_agents`, and `_links.expand` (ADR 045 D6 hypermedia) →
  drives the double-click detail panel.
- `GET /api/v1/graph/nodes/{node_id}/neighbors?depth=1&limit=` —
  **expand-on-demand** (powers drill-in and cluster expansion); reuses ADR 010's
  `expand_subgraph` under the hood, re-serialized to graphology JSON.
- `GET /api/v1/graph/search?q=&type=&limit=` — node search; powers the viz's
  search box and **fly-to**.
- `POST /api/v1/graph/query` — **bounded** traverse / shortest-path / subgraph
  extraction for programmatic callers (every traversal is depth- and
  fanout-capped, per ADR 010's budget guard).

> **Compat note (`/api/v1` surface — flagged per rule 5):** these are **new,
> additive** read endpoints; no existing route changes shape. They are
> contract-tested like the rest of the `/api/v1` surface and follow ADR 045's
> error envelope + `_links` conventions.

### D6 — Growth stream (reuses ADR 035 D3 SSE)

`GET /api/v1/projects/{id}/graph/stream` (SSE) emits `node.added` /
`edge.added` / `node.updated` events **as ingest extraction persists them**, so
the viewer **animates the graph growing in real time** while a crawl/ingest runs.
This is a **typed projection of the ADR 035 D3 stream**, not new transport: the
same SSE machinery, filtered to graph lifecycle events scoped to the project.
**Each event payload is itself graphology-importable** (a one-node/one-edge
fragment), so the client does `graph.mergeNode(...)` / `graph.import(fragment)`
and the FA2 worker (D4) places only the delta.

### D7 — Scale ladder

sigma renders ~10k nodes raw comfortably (WebGL). Beyond that:

- **Window + expand-on-demand** is the floor (D5/R6) — never ship the whole
  graph; load a neighborhood and expand on interaction.
- **Cluster nodes (LOD)** — a `community` collapses to a single **super-node**
  server-side; double-click / zoom expands it via the **same neighbors endpoint**
  at cluster granularity. The community assignment (D4's `community` column,
  from a server-side modularity pass) drives both the cluster grouping and node
  color. **Documented here; not necessarily built in v1** — the windowing floor
  is sufficient for the first cut, and the API shape already supports clusters as
  "just nodes with a higher granularity."

### D8 — sigma.js + graphology as the reference viz

`mdk graph serve` ships a **self-contained, vendored viewer** (sigma.js +
graphology, both **MIT** — air-gapped / customer-tenant safe; license-gate clean
per `scripts/check_licenses.py`): WebGL graph, search box (fly-to), **click →
neighborhood focus**, **double-click → detail panel with provenance**, and a
**live-growth toggle** that subscribes to D6. The vendored builds are static
assets, not a new shipped Python/runtime dependency. The **API stays
viz-library-agnostic** (D3) — sigma is the *reference*, not the *only* possible
client; the same endpoints could feed cytoscape or react-force-graph.

> **Compat note (new CLI verb — flagged per rule 5):** `mdk graph serve` is a new
> command under the existing `mdk` surface. Per the project memory, graph is a
> capability surfaced through existing front doors where natural; `graph serve` is
> the viewer launcher (analogous to other `mdk <noun> serve` patterns) and does
> not change any existing verb.

### D9 — Completes ADR 010 (surfaces graph retrieval in `RetrievalConfig`)

Add a `graph` block to `RetrievalConfig` (ADR 023) so agents can opt into
multi-hop reasoning:

```yaml
retrieval:
  graph:
    enabled: true
    hops: 2          # bounded neighborhood expansion (ADR 010 expand_subgraph)
    max_relations: 50
    # ...
```

This is exactly the GraphRAG retrieval ADR 010 left **unsurfaced** — `kb.graph_
retrieval.graph_retrieve` already exists; this wires it into agent config and the
executor's pre-retrieval step (ADR 023). **This ADR reserves the config shape; the
executor wiring is a follow-up PR** (see Boundaries). `agent.yaml` stays
backward-compatible — agents with no `retrieval.graph` block behave exactly as
today.

### D10 — Security (tenant + project scoped; no cross-tenant leakage)

The graph is **tenant + project scoped** end to end. `tenant_id` is **NOT NULL**
on both tables; **no edge ever crosses a tenant boundary** (an edge's endpoints
must share the edge's `tenant_id`); **provenance never exposes another tenant's
chunks or URLs**; and the viewer renders **only what the caller's scope permits**
(every query is filtered by the caller's `(tenant_id, project_id)` and ADR 013
scopes before serialization). Cross-tenant `node_id`s in `POST /graph/query`
contribute nothing rather than raising (ADR 010's existing convention).

---

## Resolved decisions (locked upfront)

| # | Decision | Why |
|---|---|---|
| **R1** | **graphology-JSON-native API** (zero client transform). | The client does `graph.import(response)` straight into sigma; no mapping layer to drift. |
| **R2** | **Persisted layout coordinates + incremental FA2 for new nodes.** | Instant first paint (server ships `x,y`) + smooth live growth (only the delta is laid out, in a worker). |
| **R3** | **sigma.js + graphology reference viz, vendored MIT builds; API stays library-agnostic.** | WebGL scale + built-in drill events; MIT → air-gapped-safe; library-agnostic API keeps cytoscape/react-force-graph open. |
| **R4** | **Two modes (knowledge + topology) on one schema, distinguished by node `type`.** | One store, one API, one viewer; topology view comes nearly for free. |
| **R5** | **Neo4j is an OPTIONAL future adapter behind `StorageProvider` — never required.** | SQLite/Postgres adjacency is the floor; sovereignty + infra burden of a required graph DB is unacceptable (ADR 001/010). |
| **R6** | **Expand-on-demand (never ship the whole graph) + cluster-node LOD for large graphs.** | Bounded payloads + interactive scale to >10k nodes. |

---

## Failure modes

- **Huge graph.** A project graph can dwarf the viewport / sigma's comfort zone.
  *Mitigation:* D5/R6 windowing (never ship the whole graph) + D7 cluster-node LOD
  + bounded traversals (ADR 010 fanout/depth budget). The API caps `limit` and
  rejects unbounded `POST /graph/query`.
- **Low-confidence extracted nodes.** The extractor LLM produces shaky entities.
  *Mitigation:* extraction confidence rides on the node (`properties.confidence`);
  the API can filter and the viz can dim/hide below a threshold — bad nodes are
  *visible and filterable*, not silently load-bearing.
- **Layout not yet computed.** A node/window has no stored `x,y` (build hasn't run,
  or brand-new nodes mid-stream). *Mitigation:* `attributes.layout: "pending"`
  signals the client to run a **bounded client-side FA2** over the returned window
  only; the server's debounced full FA2 pass (D4) then backfills persisted
  coordinates so subsequent loads are instant.
- **Extraction never ran for a project.** Empty graph. *Mitigation:* endpoints
  return a valid empty graphology doc (`nodes: [], edges: []`); the viewer shows an
  empty-state nudging `mdk kb ingest --build-graph` / the ingest API.
- **SSE stream drops mid-ingest.** *Mitigation:* the stream is additive eye-candy
  over the durable store — on reconnect the client re-fetches the windowed subgraph
  (D5) to reconcile; no event is load-bearing for correctness (ADR 035's
  at-least-once + dedupe-on-id semantics apply).

---

## Consequences

**Positive**
- A **drillable knowledge-graph viz** (search → fly-to, click → focus, double-click
  → provenance, live-growth) — the flagship-demo experience Deva asked for, on the
  backends we already ship.
- **GraphRAG multi-hop retrieval finally surfaced** (D9) — ADR 010's unshipped half
  becomes reachable from `agent.yaml`.
- **Topology view nearly for free** (D2/R4) — the same store/API/viewer renders the
  platform's own structure.
- **Library-agnostic, zero-transform contract** (D3) — the front end (Mova iO) and
  any third-party client consume the graph without a mapping layer.
- **No new infra / no new shipped dep** — adjacency on SQLite/Postgres; vendored
  MIT static assets; Protocol-pure.

**Negative / costs (what to watch)**
- **Layout compute cost** — the server-side full FA2 pass is O(n·e); debounce it
  off the request path and cap graph size before recompute (D4/D7).
- **Graph-store growth → retention** — every ingest grows `graph_nodes`/
  `graph_edges`; needs a retention / `delete_graph`-on-reingest story (ADR 010's
  `delete_graph` already supports source-scoped deletion; project-level retention is
  a follow-up).
- **Schema evolution** — D1 evolves the ADR-010 graph schema; the migration is a
  flagged, additive follow-up (see D1 compat note).

**Neutral**
- Adds a new CLI verb (`mdk graph serve`) and a vendored static viewer bundle.
- `community` requires a modularity pass; cheap at v1 graph sizes, batched off the
  request path.

---

## Alternatives considered

- **D3 force-directed graph as the reference viz** — *rejected as the reference.*
  D3's SVG/Canvas force layout degrades past ~2k nodes and would require building
  the drill interactions (focus, expand, fly-to, LOD) by hand. sigma's WebGL
  renderer scales to ~10k+ and ships the drill events. **D3 remains possible** —
  the API is library-agnostic (D3/R3) — it is just not the reference.
- **Neo4j (or a required graph DB)** — *rejected as a requirement.* It is a hard
  dependency + a second storage path that bypasses the Protocol (the coupling ADR
  001/010 forbid) and a sovereignty/infra burden for customer-tenant deploys.
  Kept as an **optional future adapter** behind `StorageProvider` (R5).
- **Ship the whole graph to the client and lay it out there** — *rejected.* Slow
  first paint, janky interaction, unbounded payloads. Replaced by persisted
  coordinates (R2) + expand-on-demand (R6).
- **A bespoke (non-graphology) JSON shape** — *rejected.* Forces a client
  transform that drifts; graphology's documented serialization is the
  lingua-franca that keeps sigma/cytoscape/react-force-graph all cheap (R1).

---

## Boundaries

**In scope:** the graph **surface** — the generalized node/edge store shape (D1),
the read/query API (D5), the growth stream (D6), the graphology contract (D3), the
persisted-layout strategy (D4), the reference viewer (D8), the `RetrievalConfig.
graph` **shape** (D9), and the security model (D10).

**Out of scope (explicitly):**
- The **Neo4j/AGE/Neptune `StorageProvider` adapter** — future, optional (R5).
- The **implementation** of GraphRAG multi-hop retrieval wiring into the executor
  — follow-up; this ADR only **reserves** the `retrieval.graph` config shape (D9).
- **3D visualization** — the v1 viewer is 2D WebGL.
- **Graph editing via the viz** — the v1 viewer is **read-only**; mutations happen
  through ingest/extraction, not by dragging nodes.
- The **D1 schema migration** itself — flagged, additive, follow-up PR.

---

## Implementation note (for the follow-up PRs — not this docs-only ADR)

ADR 010's extracted graph **is already persisted** (`kb_entities` /
`kb_relations`, written by `kb.ingest._build_graph_for_chunks` →
`storage.upsert_entity` / `upsert_relation`), so the D5 read API has real data to
read from day one. The first implementation PR is therefore mostly **read-path +
serialization + layout**, not a new write path: (1) generalize the schema (D1,
add `project_id` + layout/render columns), (2) serialize existing rows into
graphology JSON (D3), (3) compute + persist FA2 coordinates (D4), (4) wire the
read endpoints (D5) + the SSE projection (D6), (5) vendor the sigma viewer (D8).
`RetrievalConfig.graph` (D9) and cluster-LOD (D7) are independent follow-ups.
