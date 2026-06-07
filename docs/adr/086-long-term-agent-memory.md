# ADR 086 — Long-term agent memory as a governed StorageProvider resource

- Status: Proposed
- Date: 2026-06-07
- Deciders: platform-team (human-owned direction; this ADR scopes the phase)

## Context

mdk already covers most of the "agent memory" surface — and an audit (2026-06-07)
shows the proposal-of-the-week "4 memory layers" is, for mdk, **3 layers shipped
+ 1 gap**:

| Layer | mdk today |
| --- | --- |
| **Run memory** | ✅ `MemoryStore` (`last_run`, 3 backends, TTL/evict) + durable run state on Temporal (ADR 062) |
| **Conversation memory** | ✅ `Session`/`SessionMessage`, last-N-turn injection, char budget, rolling LLM summary (ADR 045 D10) |
| **Procedural memory** | ✅ already first-class — skills, prompts, eval datasets/examples |
| **Long-term semantic memory** | ⚠️ **the gap** — `MemoryStore` is key-value, **per-agent only** (no user/session scope), **no vector recall** |

The retrieval substrate the gap needs is *also* already built: pgvector + hybrid
(vector + BM25) + rerank + multi-hop (ADR 009/023) and GraphRAG entity/relation
extraction (ADR 010/046), all behind the `StorageProvider` Protocol.

So the work is **not** "add vector memory" — it's: add the **long-term semantic
memory layer** on the seams we already have, with the governance mdk is known
for. The framing we adopt:

> **mdk Memory is governed agent state: scoped, recallable, auditable,
> deletable, and eval-tested** — not "we have a vector store."

## Decision

Build long-term agent memory as a **first-class `StorageProvider` resource**
(rule 6/7: extend the Protocol + reuse the existing pgvector/hybrid pipeline — do
NOT add Pinecone/Chroma or a parallel retrieval stack). Fold it into the
already-planned **knowledge-graph phase** (`graphrag-storage-protocol`): durable
facts and graph entities share the same substrate (Postgres+pgvector, SQLite,
Neo4j-optional) and the same multi-backend, cross-cloud posture.

Decisions:

- **D1 — `memory_items` on the StorageProvider Protocol** (additive; SQLite +
  Postgres/pgvector): `tenant_id / user_id / agent_id / session_id / kind /
  content / embedding / keywords / importance / confidence / source_run_id /
  created_at / last_used_at / usage_count / expires_at / deleted_at`. This fixes
  the **scoping gap** (today memory is agent-only) → tenant+user+agent isolation.
- **D2 — `memory:` block in `agent.yaml` that COMPOSES with `retrieval:`**, not a
  parallel config. `short_term` already exists as `retrieval.history_turns` /
  `history_summarize` — the new block adds only `long_term:` (kinds, scope,
  max_items, recall_top_k, recall_strategy, pii_policy) and points `procedural:`
  at the existing `skills/`/`evals/`.
- **D3 — picky, opt-in extraction** (post-run): extract `preference / fact /
  decision / instruction` with **PII-reject-by-default**. Default is NOT
  "remember everything" — that is expensive, creepy, and hard to debug.
- **D4 — hybrid recall, reusing the KB pipeline** (vector + keyword + recency),
  scoped tenant/user/agent, top-k, injected via the same pre-retrieval seam as
  ADR 023 auto-retrieval (so prompts/templates don't change shape).
- **D5 — auditable injection**: a recall event (recalled ids, scores, injected
  text, tokens added, reason) emitted through the existing tracer seam (Langfuse/
  OTel) AND a queryable recall row — memory is **product state in
  StorageProvider**, the trace is observability (keep them separate).
- **D6 — deletion + export (GDPR-grade)**: per-user / per-tenant purge +
  bulk export, on top of today's per-key delete + age eviction.
- **D7 — memory evals + CI gate**: `mdk eval memory` (recall@1/3, stale rate,
  wrong-user-leakage rate, token overhead, extraction precision), gated like the
  existing eval/license/roadmap freshness gates.

### Compat (rule 5)
Additive throughout: one new Protocol resource (`memory_items`, nullable/defaulted
→ old rows + native path unchanged), one new optional `agent.yaml` block (absent →
serializes identically), opt-in extraction + recall (off → byte-for-byte
unchanged). No change to `/api/v1` shapes, existing `MemoryStore`, or env vars.

### Non-goals (this ADR scopes the phase; humans own the calls below)
- The **default aggressiveness** of extraction, the **scoping default**
  (tenant_user_agent vs narrower), and the **retention policy** are human
  decisions to lock at PR time, not pre-decided here.
- "Agent remembers everything" — explicitly rejected as the default.
- A new vector DB dependency — rejected (reuse pgvector + StorageProvider).

## Consequences
- mdk's memory story becomes a governance differentiator (scoped/auditable/
  deletable/eval-tested) rather than table-stakes "vector memory."
- Long-term memory and GraphRAG converge on one durable substrate — less surface,
  one multi-backend seam, consistent cross-cloud story.
- Each layer ships as its own PR behind the existing seams (see roadmap items
  `ltm-*`), so blast radius stays small and reviewable.
