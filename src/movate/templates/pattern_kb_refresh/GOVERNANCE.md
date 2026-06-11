# Knowledge-Base Refresh pattern — governance

Topology: `TOOL ingest → validate → DECISION(ok) → {TOOL publish | [HUMAN ack]} → notify`

Validated, auditable KB refresh, durable on Temporal — **nothing reaches the
knowledge base without a validated, auditable ingest**. The entry `ingest`
TOOL node (ADR 097) runs the workflow-local `sim-ingest` python skill: a
FIXED chunking rule (one chunk per 40 words, ceil; an empty document yields 0
chunks and counts in `empty_docs`), one auditable `{system: kb, action:
ingest}` ledger row per run. The `validate` agent judges the ingest SUMMARY
(never the raw documents) against three explicit rules and emits `{ok,
note}`; a deterministic `decision` node (ADR 094) routes `ok eq true` into
the `sim-kb-publish` TOOL (the `{system: kb, action: publish}` ledger row no
validated refresh can skip) and everything else into the `escalate` HUMAN
gate (ADR 099) — which has **NO retry route by design**: ack (or any
wording — fallback) lands on notify, the graph stays acyclic, and a failed
refresh is fixed at the source and resubmitted as a NEW run. Both paths
converge on ONE notify agent (exclusive convergence, ADR 098) whose prompt
guards the path-exclusive keys.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic ingestion as a step | the `ingest` TOOL node (ADR 097) — fixed chunking rule, schema-validated in and out, SKILL-gate governed, no LLM. |
| LLM judges the summary, never the documents | the `validate` agent's input schema lists ONLY `ingest_result` — the one model call before the gate sees counts, not content. |
| Deterministic quality routing | the `quality-gate` `decision` node — a pure predicate over the validator's `ok`; no second LLM in the control path. |
| Durable human acknowledgement | the `escalate` HUMAN node pauses durably (survives worker restarts) until a `POST /api/v1/workflow-runs/{id}/signal`. |
| Fail-safe gate vocabulary (ADR 099) | `routes {ack: notify}` + `fallback: notify` — ANY response acknowledges; only silence holds the run, and no response can publish. |
| Audited publication | the `publish` TOOL records the `{system: kb, action: publish}` ledger row — reachable ONLY via the validated route (the graph, not a convention, guarantees it). |
| Guarded shared tail (ADR 098) | the `notify` prompt wraps `publish_result` and `decision` in `{% if ... is defined %}` — StrictUndefined-safe on both routes. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles / no auto-retry | the escalate gate routes ONLY forward (ack→notify, fallback notify); the compiler rejects cyclic graphs | a poisoned refresh can never loop back into ingest — resubmission is a deliberate NEW run with fixed sources. |
| Chunking is deterministic | the fixed one-chunk-per-40-words rule in `sim-ingest` | the same documents always produce the same `ingest_result`, so the validation verdict is reproducible and replay-safe on Temporal. |
| Publication is unreachable without validation | the `publish` TOOL has exactly one inbound edge — the decision node's `ok eq true` leg | an unvalidated refresh structurally cannot write the publish row. |
| Publication is governed | the TOOL node's skill declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) statically and at runtime | the external write cannot hide behind a prompt. |

## Customize

- Point `sim-ingest` / `sim-kb-publish` at your real KB pipeline (vector
  store, search index, CMS): swap the impl.py bodies, keep the schema
  contracts and the ledger rows.
- Tighten the validation rules: the `validate` prompt's three rules are the
  quality bar — add corpus-specific checks (minimum chunk counts, required
  document ids) as new explicit rules.
- Wire the escalation: `escalate`'s prompt is what the operator sees — add
  your runbook link; aim `fallback` at a second gate if a bare ack is too
  permissive for your process.
- Chunking strategy: `_CHUNK_WORDS` in sim-ingest's impl.py is the knob;
  keep whatever rule you pick FIXED so validation stays reproducible.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls on every path**
(validate + notify — ingest, the quality-gate routing, the escalate gate's
routing, and the publish TOOL node are all deterministic, zero-cost). Cap
absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093); the eval-gate below is the quality budget.
