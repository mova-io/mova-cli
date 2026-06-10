# ADR 096 — `observability_facts`: one relational summary table for platform integration

Status: Accepted
Date: 2026-06-09
Accepted: 2026-06-09 — locked in by Jeremy with the explicit framing that the
polyglot stores are kept BY DESIGN (each retains full fidelity); Postgres
`observability_facts` is the unified reporting/integration surface; durability
hardening (ClickHouse persistence + TTL, Temporal retention window, Langfuse VM
volumes) is the companion work, and facts retention is effectively forever.
Deciders: Engineering + Deva (Movate) — this is the integration contract the
mova-io platform builds on; the shape is a public-ish surface once the platform
reads it, so it needs product sign-off.
Builds on: ADR 095 (unified observability store — OTel hub → ClickHouse +
Postgres, `trace_id` correlation), ADR 036/087 (metrics), ADR 093 (governance
audit), ADR 082 (Temporal completion signals), ADR 024 (trace propagation).

## Context

ADR 095 centralizes **telemetry** (traces/metrics/logs → ClickHouse) and keeps
the **business of record** in Postgres. That solves dashboards-for-operators,
but it leaves the mova-io platform integration problem open: to replicate any
dashboard or activity view we have in **Temporal, Grafana, or Langfuse**, the
platform would today have to understand four internals —

| To show… | The platform would need… |
|---|---|
| run cost / tokens / latency | `runs.metrics` (a nested JSON blob, schema churns with mdk) |
| workflow outcomes, HITL pauses, runtime | `workflow_runs` (+ status semantics) |
| governance effects | the audit stream / `mdk.governance.decisions` metric |
| trace drill-down | ClickHouse `otel_traces` / Langfuse / Temporal UI URLs |

Coupling the platform to those internals means every mdk schema change breaks
the platform, and cross-system facts (a run's governance effect + its cost + its
Temporal link) need joins the platform shouldn't own.

**The ask (user-stated):** "build out robust telemetry and observability
tracking in Postgres so everything is unified, so the mova-io platform can
easily integrate and replicate any dashboards or activity we have in Temporal,
Grafana, or Langfuse."

**The trap (restated from ADR 095):** that must NOT mean moving trace bodies
into Postgres — volume kills a row-store. The resolution is **summary in
Postgres, detail in ClickHouse**: Postgres gains ONE denormalized, stable,
queryable fact table; deep telemetry stays in ClickHouse/Langfuse/Temporal and
is reachable from each fact row via `trace_id` + deep-links.

## Decision

### D1 — One denormalized fact table: `observability_facts`

A new table in the `movate` DB, one row per **terminal execution event** (an
agent run completing; a workflow run reaching a terminal or paused state):

```
fact_id          TEXT PK          -- deterministic: "<kind>:<source_id>" (idempotent upsert)
kind             TEXT             -- run | workflow_run
source_id        TEXT             -- run_id / workflow_run_id (FK back to the authoritative row)
trace_id         TEXT             -- the universal correlation key (ADR 095 D4)
tenant_id        TEXT
workflow         TEXT NULL        -- workflow name (NULL for standalone agent runs)
agent            TEXT NULL        -- agent name (NULL for workflow-level facts)
node_id          TEXT NULL
status           TEXT             -- success | error | paused | safety_blocked | …
runtime          TEXT             -- native | temporal
route            TEXT NULL        -- decision/router outcome (e.g. tier) when present in state
cost_usd         DOUBLE PRECISION
tokens_in        BIGINT
tokens_out       BIGINT
latency_ms       BIGINT
governance_effect TEXT NULL       -- allow | warn | deny (most severe effect on the run)
error_type       TEXT NULL
created_at       TIMESTAMPTZ
attributes       JSONB            -- bounded escape hatch (provider, model, pricing_version…)
```

The platform reads **this one table** (or its API projection, D5) and can
rebuild any summary view we have in Grafana, and link out to everything else.

### D2 — `trace_id` is the join key out to the deep stores

Every fact carries `trace_id` (mdk already stamps `RunRecord.metrics.trace_id`).
From a fact row the platform can construct: the ClickHouse span query, the
Langfuse trace URL (`langfuse_link.langfuse_trace_url`), and the Temporal Web
deep-link (workflow facts; via the existing `_temporal_web_url` helper). The
links are **derived at read time** from the ids — not stored — so URL/base
changes don't invalidate rows.

### D3 — Written fail-soft at the existing edges, never in `core`

Facts are recorded exactly where metrics already are (boundary rule — tracing/
metering wired at the edges):

- agent-run facts: the dispatch edge (`runtime/dispatch.py`, where
  `record_run_usage` fires) after the run record persists;
- workflow facts: the workflow persist edges — native runner's terminal
  `save_workflow_run` and the Temporal `persist_workflow_result_activity` (+
  the HITL pause write, so paused inventory is visible).

Same posture as metrics: a fact-write failure logs and never fails the run.

### D4 — Facts are DERIVED; `runs`/`workflow_runs` stay authoritative

`observability_facts` is a projection for integration, not a system of record.
`fact_id = "<kind>:<source_id>"` makes writes idempotent upserts, and a backfill
command (`mdk admin backfill-facts`) can rebuild the table from the
authoritative rows at any time. Nothing reads facts inside mdk's execution
paths.

### D5 — Storage seam + API projection

The table is added behind the `StorageProvider` Protocol
(`save_observability_fact`, `list_observability_facts`) with both Postgres and
SQLite implementations (additive migration; rule 5). A read-only
`GET /api/v1/observability/facts` (filters: kind/workflow/agent/status/since;
keyset pagination) is the platform's contract, so even direct-DB coupling is
optional. Phase 2 (optional, deferred): a projector that folds **external**
signals into facts on completion — Langfuse scores, Temporal close status — so
cross-system reconciliation also lands in the one table.

## Alternatives considered

- **SQL views over `runs`/`workflow_runs`** — no new writes, but couples the
  platform to internal schemas anyway (views churn with them), can't cheaply
  flatten `metrics` JSON at query time at volume, and has nowhere to put
  cross-system fields (governance effect, future Langfuse scores). Rejected.
- **Platform queries ClickHouse directly** — telemetry is sampled/derived, the
  wrong source for business numbers (ADR 095), and CH durability is weaker.
  Rejected as the primary integration; remains the drill-down path.
- **CDC/ETL pipeline into a warehouse** — heavier ops than this deployment
  needs; revisit at scale. Deferred.

## Consequences

- **Compat:** additive storage schema (new table + Protocol methods, defaulted
  migration), one new read-only API route. No change to existing tables, the
  emit paths' behavior, or env contracts.
- The platform integration surface becomes **one table + one endpoint**, stable
  under mdk-internal churn (the writer adapts; the fact shape holds).
- Every scenario in the 30-template program lands in the unified table from its
  first run — the certification matrix, ITSM family, and ADO flows inherit it.
- Write amplification is one small row per run — negligible against the
  existing per-run persistence.
