# ADR 079 — Deterministic graph-assert seam (guarantee high-value nodes without waiting on LLM extraction)

**Status:** Accepted — shipped (deterministic graph-assert seam; /api/v1 graph assert). _(status reconciled to shipped reality 2026-06-08)_
**Date:** 2026-06-05
**Deciders:** Engineering (kb / storage / runtime) — **no new shipped
dependency; no new storage Protocol method** (reuses the `upsert_entity` /
`upsert_relation` seam that already exists on `StorageProvider`).
**Builds on / composes with (changes nothing in any of them):**
ADR 017 (agent orchestration),
ADR 046 (graph project-scope `project_id` + `min_confidence` confidence floor),
ADR 075 (the Neo4j graph adapter — one more `StorageProvider` impl behind the same seam),
ADR 077 (agent→workflow dispatch — the dispatched workflow's *outcome* is a future assert producer),
and the existing knowledge-graph stack (`kb/graph_extract.py` builds records,
`kb/ingest.py` persists them via `upsert_entity` / `upsert_relation`, the
read-only runtime graph endpoints render them).

**Defining observation (empirical, from the POS-reboot voice demo).** When a
call resolves, the demo ingests a transcript and an **LLM extractor** turns it
into graph nodes/edges. That path is excellent for *broad recall* (it surfaces
firmware versions, switches, symptoms nobody hand-labelled) but it is
**probabilistic**: in testing, the ServiceNow incident (`INC0042217`) — the one
node a user most wants to *search the graph by* — was sometimes not extracted,
or extracted under a paraphrase ("ticket 42217", "the incident"). So
"search-by-incident" on the Knowledge-Graph tab is only as reliable as the
extractor's mood. We strengthened the ingest document to cue the incident hard
(it now names `Incident INC0042217` four times with explicit relations), which
raises the hit rate — but a *demo where typing the ticket number sometimes finds
nothing* is the wrong contract for a known, structured fact the system itself
generated. Known-shape, system-generated facts (an incident number, a dispatched
workflow's outcome, a reboot command id) should land in the graph
**deterministically**, not be re-discovered by an LLM reading prose we just
wrote from structured data.

This is a **strategic / design** ADR (rule 1/2): it introduces a thin seam and a
rollout order; each piece ships in its own PR against this contract. It changes
no storage Protocol method and deprecates nothing.

---

## Decision

### D1 — A deterministic *assert* record-builder, sibling to `graph_extract`

Add a pure module (`kb/graph_assert.py`, no I/O — mirrors `graph_extract.py`'s
"build records, never touch storage" rule) that turns a **structured fact set**
into `Entity` / `Relation` records and persists them through the **already
existing** `StorageProvider.upsert_entity` / `upsert_relation` seam. No new
storage method, so it works on **every** backend (SQLite / Postgres / Neo4j —
ADR 075) for free.

Two properties make it deterministic and idempotent:

* **Canonical stable IDs.** An asserted node's identity is derived from
  `(type, canonical-key)` — e.g. `incident:INC0042217`, `lane:118/5`,
  `store:118` — and written into `content_hash` (the field the upsert layer
  already dedups on). Re-asserting the *same* fact **merges** into the same node
  instead of spawning a duplicate. This is the entity-resolution guarantee the
  LLM path can only approximate.
* **Confidence + provenance.** Asserted records carry `confidence = 1.0` and
  `metadata = {source: "assert", call_id/run_id, asserted_at}`. They therefore
  **survive the ADR 046 `min_confidence` floor** unconditionally and are
  distinguishable from inferred nodes at read time.

Asserted nodes are **still embedded** (via the configured embedder) so they join
the same vector space — entity-resolution against extracted nodes and GraphRAG
retrieval both keep working. (Embedding is a required `Entity` field; asserting
does not get to skip it.)

### D2 — A guarded runtime write endpoint: `POST …/projects/{project}/graph/assert`

Expose the assert path over the runtime API so the execution plane (and the demo
ingest, and — later — an ADR 077 workflow) can write structured facts:

```
POST /api/v1/projects/{project_id}/graph/assert      # write scope, tenant-scoped
{ "nodes": [{ "key": "incident:INC0042217", "type": "Incident",
              "name": "INC0042217", "attributes": {...} }, ...],
  "edges": [{ "src": "incident:INC0042217", "dst": "lane:118/5",
              "type": "affects" }, ...] }
→ 200 { "applied": {"nodes": N, "edges": M}, "skipped": [...] }
```

* **Write scope.** Gated by `_scope("kb:write")` (ADR 013) — the graph is part
  of the KB, so it reuses the same `kb:write` scope as `POST …/agents/{name}/kb`
  rather than inventing a new scope name. Every other graph endpoint today is
  `_scope("read")`; this is the first graph **writer**. Records are stamped with
  the caller's tenant, so a write can never land in another tenant's graph (same
  no-leak rule as the read endpoints).
* **Order + atomicity.** Nodes upserted before edges; an edge referencing a
  missing/!asserted node is **skipped and reported**, not fatal (partial-write
  failure mode is explicit, not silent). Per-request best-effort with a report.

### D3 — Coexistence with extraction (assert is opt-in, extraction stays the floor)

Extraction remains the **default** and the broad-recall workhorse. Assert is an
**opt-in** for the small set of high-value, known-shape facts the system itself
produces. They share one graph:

* **Merge semantics.** When an asserted node and an extracted node resolve to the
  same canonical id, the **asserted attributes are authoritative** (identity,
  type, `confidence = 1.0`); extraction may *enrich* (add edges, a description)
  but cannot demote an asserted node's identity or confidence.
* **First producers.** (1) the demo call-ingest asserts the `Incident` node +
  its `Store` / `Lane` / `Symptom` edges; (2) — composing with ADR 077 — a
  dispatched workflow asserts its **outcome** (resolved/escalated, command id) so
  the graph reflects what *actually happened*, not just what the transcript said.

### D4 — Provenance surfaced + live-growth parity

`source: "assert"` (D1) is rendered in the node-detail panel as an
**"asserted vs inferred"** badge, so an operator can see which facts are
guaranteed vs probabilistic. Asserted writes flow through the **same
graph-growth stream** (the `node.added` frames the read side already emits), so
an asserted incident **animates into the graph live** exactly like an extracted
node — the demo's "grew with your last call" beat is preserved.

---

## Consequences

* "Search-by-incident" (and any search-by-system-generated-key) becomes a
  **contract**, not a coin flip: the node is guaranteed present under a
  predictable id the moment the call resolves.
* Duplicate incident/lane/store nodes from paraphrase drift go away for asserted
  facts — canonical ids dedup deterministically, improving the ADR 079 *and* the
  recurrence/root-cause insight counts (degree stops being inflated by dupes).
* The graph gains a **trust dimension**: asserted (1.0, sourced) vs inferred
  (probabilistic). Buyers asking "where did this come from / can I rely on it"
  get a real answer.
* ADR 077's handoff gets a place to record outcomes — the durable workflow can
  assert its terminal state into the same graph the voice front-door reads.

## Boundaries

* **No new storage Protocol method** — reuses `upsert_entity` / `upsert_relation`
  (rule 7: extend via the existing seam, not a new one). All three backends work
  unchanged.
* **New `/api/v1` surface** (`…/graph/assert`) — additive, write-scoped; flagged
  per rule 5. No change to `agent.yaml` / `project.yaml` schema, CLI `--json`
  shapes, storage schema, or `MOVATE_*` / `MDK_*` env vars.
* `kb` builds records and calls the `StorageProvider` Protocol — never a concrete
  `postgres` / `sqlite` / `neo4j` (rule 6).
* Control plane (cli) ⊥ execution plane (runtime): the assert **endpoint** lives
  in the runtime; the CLI / ingest / a skill *call* it. Tracing stays at the
  edges.

## Alternatives considered

* **Keep cueing the LLM ingest doc harder (status quo+).** Rejected as the
  *guarantee*: better recall, still probabilistic — the wrong contract for a key
  a user types verbatim. (It stays as the recall floor for everything *else*.)
* **Add a new `StorageProvider.assert_graph(...)` method.** Rejected: the write
  seam already exists (`upsert_entity` / `upsert_relation`); a new method means
  three new backend impls for zero new capability.
* **Client-side incident registry in the voiceapp.** Rejected: not persisted, not
  cross-session, not tenant-scoped, invisible to GraphRAG and to the real graph —
  a demo-only band-aid, not a platform seam.
* **Skip the embedding on asserted nodes (write a sentinel vector).** Rejected:
  they'd fall out of entity-resolution and GraphRAG retrieval — present in the
  picture but dead to search/merge. Asserting embeds like everything else.

## Scope / rollout

1. **D1 `kb/graph_assert.py` record-builder + canonical-id/idempotency tests** —
   pure, no I/O; smallest, standalone PR.
2. **D2 `POST …/graph/assert` endpoint (write scope, tenant-scoped, node→edge
   ordering, skip-report)** — the runtime surface.
3. **D3 wire the demo call-ingest** to assert the `Incident` (+ Store/Lane/
   Symptom edges) deterministically; extraction continues for everything else.
4. **D4 provenance badge in node-detail + growth-stream parity**, then (composing
   with ADR 077) assert dispatched-workflow outcomes.

Each step ships against this contract; none is a big-bang migration (ADR 065 D4
discipline).
