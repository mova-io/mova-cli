# ADR 047 — Observability Intelligence layer (insights store + overnight analyst + NL query)

**Status:** Accepted — **v1 shipped** (this PR). Nightly cron wiring is a
documented follow-up.
**Date:** 2026-05-28
**Deciders:** Engineering (Movate)
**Builds on / related:** `StorageProvider` Protocol (ADR 009 seam), `BaseLLMProvider`
seam, ADR 016 (judge/drift), ADR 017 (durable queue + scheduler + JobKind),
ADR 024 (per-step spans), ADR 031/032 (reporting + front-end monitor feed),
ADR 036 (usage metering — `build_usage`), the Failure Pattern Diagnoser (#542,
`core/diagnoser.py`).

## Context
MDK already *captures* rich telemetry — runs (cost/latency/status), evals
(pass rates), failures, per-step spans — and *renders* it (`mdk report`, the
`/api/v1/report` monitor feed). What it does **not** do is *interpret* it.
Operators must know which dashboard to open and what "normal" looks like before
a number means anything. Two gaps:

1. **No preprocessing.** Every "how are things?" question re-scans raw runs.
   There's no daily, pre-aggregated, anomaly-annotated summary an operator (or
   a downstream tool) can read in one lookup.
2. **No natural-language interface.** "Why did costs spike yesterday?" /
   "triage is timing out — what changed?" require a human to manually correlate
   deploys + drift + failure clusters.

The trap is text-to-SQL: letting an LLM author queries against the production
store. That is unbounded and mutation-capable — unacceptable for a framework
embedded in customer deliverables.

## Decision
Add a self-contained **Observability Intelligence layer** in a new
`core/observability/` package, one append-only storage table, and new
`/api/v1/observability/*` endpoints + `mdk observability` CLI. Three facets of
one feature:

- **D1 — Append-only insights store (one table).** `ObservabilityInsight(id,
  tenant_id, project_id, date, health_score, anomalies, top_failures,
  usage_rollup, trends, narrative_digest, created_at)`. `anomalies` /
  `top_failures` / `usage_rollup` / `trends` are JSON; `tenant_id` is NOT NULL.
  Three additive `StorageProvider` methods — `save_insight`, `get_insight`,
  `list_insights` — implemented on SQLite + Postgres + InMemory. **Append-only:**
  no update method exists; a re-run of a day INSERTs a new row, reads take the
  latest per `(tenant, project, date)`. The daily history is itself an audit
  trail.

- **D2 — The overnight analyst (preprocessing; an MDK agent dogfooding MDK).**
  `analyst.analyze(tenant, project, window, *, storage, llm, budget_usd)`:
  1. pull telemetry via the Protocol (runs / evals / failures; `build_usage`
     and the #542 diagnoser used opportunistically via `getattr`);
  2. **anomaly detection (pure Python, no LLM):** z-score of
     cost / latency / error-rate / volume vs a trailing baseline drawn from
     prior insights, emitting typed `{metric, severity, value, baseline, z}`
     records (`info`/`warning`/`critical` at z ≥ 2/3/4);
  3. **health score (pure Python):** composite 0–100,
     `100 × (0.40·(1−error_rate) + 0.30·eval_pass_rate +
     0.15·(drift?0:1) + 0.15·cost_trend_factor)`;
  4. **top failure clusters:** the #542 diagnoser when present, else
     un-clustered grouping by `failure_type`;
  5. **narrative digest:** the ONE LLM call, budget-capped — a short markdown
     "Yesterday: … Watch: …" digest;
  6. persist via `save_insight`.
  Exposed as `JobKind.OBSERVABILITY_ANALYZE` (worker dispatch) + an admin
  `POST /api/v1/observability/analyze` on-demand trigger. Nightly cron via a
  `JobSchedule` reusing the ADR 017 scheduler is a documented follow-up.

- **D3 — NL query + troubleshoot (grounded, cited).** `query.ask` reads the
  insights store (fast path) and may run a BOUNDED detail query; `query.troubleshoot`
  correlates failure clusters + anomalies + recent failed runs into a root-cause
  narrative. Both return `GroundedAnswer{answer, evidence[], confidence,
  suggested_action}`. **Citations are mandatory:** every answer carries
  `evidence[]` citing source kind + reference (insight date, query template,
  run id, event, failure signature).

- **D4 — SQL-SAFETY CONTRACT (the load-bearing invariant).** The detail path is
  **text-to-PARAMETERIZED-TEMPLATE, never text-to-arbitrary-SQL.** A CLOSED
  registry of named, read-only query templates
  (`cost_by_agent`, `failed_runs`, `latency_percentiles`, `usage_by_provider`)
  each calls *typed* `StorageProvider` methods with a hard row cap. The LLM's
  only influence is to PICK a template name from the closed set and FILL typed,
  clamped params (window, agent). There is NO `execute_sql` / `raw_query` helper
  and no string-interpolation of LLM output into SQL — so the path is
  mutation-proof and bounded by construction. A test enforces no arbitrary-SQL
  symbol exists.

- **D5 — Budget caps + read-mostly.** The analyst's narrative call and the NL
  query's synthesis call are each budget-capped (cost computed from returned
  token usage via the versioned pricing table). Only `save_insight` + the
  analyst write; `ask`/`troubleshoot`/`insights`/`health` are pure reads.

- **D6 — Boundary discipline.** `core/observability/` depends only on the
  `StorageProvider` + `BaseLLMProvider` Protocols and core models — never a
  concrete backend, never `cli`/`runtime`. The CLI talks to the runtime over
  HTTP (`cli ⊥ runtime`). Tracing is wired at the edges, not inside the analyst.

## Endpoints (compat surface — additive)
- `GET  /api/v1/observability/insights?project_id=&since=&until=` (read)
- `GET  /api/v1/observability/health[?project_id=]` (read)
- `POST /api/v1/observability/ask` (read; budget-capped)
- `POST /api/v1/observability/troubleshoot` (read; budget-capped)
- `POST /api/v1/observability/analyze` (admin; on-demand trigger → 202 + job_id)

## Failure modes considered
- **LLM unavailable / over budget:** the digest is empty + the NL answer falls
  back to a deterministic, still-grounded summary; the structured insight is the
  source of truth, never blocked by prose.
- **#542 diagnoser / ADR 036 helpers not on `main`:** resolved via `getattr`;
  degrade to un-clustered failures and live-computed baselines.
- **Cold start (no prior insights):** anomaly detection emits nothing (no
  anomaly invented from zero/flat history); health falls back to neutral terms;
  `health` returns `has_insight=false` (200, not 404).
- **Dirty data (error_rate > 1):** the health score is clamped to [0, 100].
- **Duplicate analyst runs:** append-only INSERT; reads dedupe to the latest.

## Alternatives rejected
- **Text-to-SQL** (LLM authors SQL we exec): unbounded + mutation-capable.
  Rejected for D4.
- **Mutable per-day insight row** (upsert): loses the daily audit trail and
  races a concurrent re-run. Rejected for append-only.
- **A new adapter Protocol for "insights":** unnecessary — insights are just
  another persisted record behind the existing `StorageProvider`.

## Status of work
v1 in this PR: the store (3 methods × 3 backends), the analyst (pure stages +
budget-capped digest + JobKind + admin trigger), the NL query + troubleshoot
with the template set, the five endpoints + schemas, the `mdk observability`
CLI, and tests (storage append-only, analyst formula/anomaly/budget/degrade,
query SQL-safety/citations, runtime endpoints/scopes, contract routes).
Follow-up: nightly cron `JobSchedule` wiring; richer event/deploy correlation
once an events table lands.
