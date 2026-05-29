# ADR 047 — Observability Intelligence: overnight analysis, an insights store, and natural-language query

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Builds on / depends on:**
ADR 015 (self-hosted observability — OTLP → Azure Monitor + Langfuse v3 in-tenant; the substrate this ADR reads),
ADR 016 (the continuous-improvement loop — harvest + continuous eval + **drift** signals; this ADR's anomaly detector consumes ADR 016's drift detections directly, it does not re-derive them),
ADR 017 (the native scheduler + the Postgres job queue + the KEDA worker — the overnight analyst is just another scheduled job on this engine),
ADR 035 (outbound events / outbox — the analyst's completion + every anomaly is a typed lifecycle event on the existing outbox; deploy events the troubleshooter correlates against are already here),
ADR 036 (usage metering + quotas — the analyst's per-night spend and the NL-query per-question spend are metered + capped exactly like every other LLM job),
ADR 039 (Movate fleet telemetry / Lighthouse / Managed Grafana — the **only** channel by which any per-tenant signal reaches Movate; this ADR's fleet scope rides ADR 039's allow-list and never widens it),
**Failure Pattern Diagnoser** (#542 — `POST /api/v1/diagnose/cluster_and_propose`; read-only failure clustering over an agent's failed runs/eval-misses/drift; this ADR's analyst + troubleshooter **call** the diagnoser for error-cluster evidence rather than re-implementing clustering),
**#518 observability assets** (in-flight — the Grafana dashboards + Azure Monitor workbooks + the `docs/observability.md` metric/span catalog; this ADR *feeds* those dashboards narrative panels + anomaly annotations + health gauges, it does not replace them).
**Flagship context:** This is the **intelligence** layer over the telemetry +
dashboard substrate. Where ADR 015 decides *where traces go*, #518 decides *how
they are visualized*, and ADR 039 decides *how Movate sees the fleet*, ADR 047
decides *how the telemetry is turned into morning-ready understanding and
answers to ad-hoc questions* — by an MDK agent, grounded in a queryable
insights store, with cited evidence.

---

## Context

MDK already emits rich telemetry and visualizes it well:

- **Runs + per-run cost/tokens** (`mdk.run.cost_usd`, `mdk.run.tokens`, ADR 024)
  and **job metrics** (`mdk.jobs.completed`, `mdk.job.duration_ms`,
  `mdk.jobs.in_flight`, pool gauges) land in Azure Monitor / Langfuse per ADR
  015, catalogued in `docs/observability.md` (#518).
- **Evals + drift** run on a schedule (ADR 016 D2) and write `EvalRecord`s +
  drift detections against durable baselines.
- **Usage rollups** are computed per tenant per period (ADR 036 D1,
  `GET /api/v1/usage`).
- **Lifecycle events** (run/eval/drift/canary/deploy) are persisted on the
  outbox (ADR 035 D1).
- **Deploys** carry CalVer (ADR 021) and emit `agent.published` events.
- **Dashboards** (#518: Grafana golden-signals/cost/queue + Azure Monitor
  workbooks; #523/#525 fleet via ADR 039 Managed Grafana) render all of the
  above.
- **Failure clustering** (#542) groups an agent's failures and proposes typed
  fixes.

What is **missing is the layer between the telemetry and the human**. Today,
to know "did anything go wrong last night, and why," an operator opens four
dashboards, eyeballs the golden-signals panel, cross-references the cost panel,
checks whether a deploy landed, opens Langfuse to read a few failed traces, and
mentally correlates the lot. That is **a 30-minute forensic ritual per project
per morning**, it does not scale across a fleet, and it is exactly the kind of
work that an LLM with the right context windows does well and a human does
tediously.

Two gaps, specifically:

1. **No preprocessing.** Every question today is a fresh, expensive,
   slow query against raw telemetry (KQL over Azure Monitor, or Langfuse
   filters, or a `core/reporting` aggregation). There is no nightly-computed,
   cheap-to-read, time-series-friendly **digest** that says "project X is
   healthy, here are the two anomalies, here is the narrative." Answering the
   same recurring questions ("what's our error rate trend," "which agent is
   the cost outlier") re-pays the full query cost every time.
2. **No natural-language interface.** A customer's ops lead — or Movate's Deva
   — cannot *ask* "why did latency spike on the billing agent on Tuesday?" and
   get a grounded, cited answer. The dashboards show *what*; nobody answers
   *why* in plain language, with the evidence attached.

There are **two audiences**, and they have **different data-residency
rights**:

- **The customer tenant** — the customer's own ops/dev team, querying their own
  telemetry inside their own runtime. They may see everything: prompts,
  completions, traces, costs, evals.
- **The Movate fleet** — Deva + the platform team, who must answer fleet-level
  questions ("which customers are seeing rising error rates this week?")
  **without** the customer's prompt/completion content ever leaving the tenant.
  ADR 039 already drew this line: Movate sees **metrics + (with this ADR)
  per-tenant insight digests it is explicitly authorized to see**, never raw
  payloads.

In one sentence: *"MDK has the telemetry and the dashboards; ADR 047 adds an
overnight MDK agent that preprocesses the telemetry into an append-only,
queryable insights store, and a natural-language query interface that answers
'what happened and why' with cited, grounded evidence — tenant-scoped for the
customer, aggregate-only for Movate."*

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Close the telemetry→understanding gap** — turn four dashboards + a Langfuse dig into a morning digest + a question box | HIGH |
| **Grounded, auditable answers** — every NL answer cites its evidence; an ungrounded answer is a bug, not a feature | HIGH |
| **Data sovereignty** — customer prompt/completion content NEVER leaves the tenant; the fleet scope sees only ADR 039-authorized aggregates + digests | HIGH |
| **Dogfood the agent platform** — the analyst is an MDK agent, not bespoke analytics code; it reuses scheduling, budgets, KB, skills, tracing | HIGH |
| **NL-query SQL safety** — the detail path runs LLM-shaped SQL against telemetry; it must be provably read-only, scoped, capped, and timed out | HIGH |
| **Cheap, time-series-friendly preprocessing** — append-only daily insights amortize recurring questions; reading a digest is O(1), not a full-table scan | HIGH |
| **Budget discipline** — both the nightly analyst and per-question NL query spend LLM tokens; both are metered + capped per ADR 036 | MED |
| **Feed, don't replace, the dashboards** — the insights store is a contract #518's dashboard pack consumes (narrative panels, anomaly annotations, health gauges) | MED |
| **Resilience** — a failed analyst run must preserve partial insights + alert; an NL query over a day with no digest yet must degrade gracefully | MED |

---

## Architecture

```
            ┌──────────────────────── TELEMETRY SUBSTRATE (exists) ──────────────────────┐
            │  runs + cost/tokens (ADR 024)   eval + drift (ADR 016)   usage (ADR 036)     │
            │  lifecycle + deploy events (ADR 035 / CalVer ADR 021)   traces (ADR 015)     │
            │  failure clusters (#542 diagnoser)                                            │
            └───────────────────────────────────┬────────────────────────────────────────┘
                                                 │  (read-only; bounded queries + skills)
                            ┌────────────────────▼─────────────────────┐
   nightly cron            │   D1  OVERNIGHT ANALYST = an MDK agent      │   budget-capped
   (ADR 017 scheduler) ───▶│   prompt + runbook KB + telemetry-query     │◀── (ADR 036)
   per tenant, per project │   skills; runs per (tenant, project)        │
                            └────────────────────┬─────────────────────┘
                                                 │ writes one append-only row / (tenant,project,day)
                            ┌────────────────────▼─────────────────────┐
                            │   D2  INSIGHTS STORE                       │   D3 anomalies
                            │   observability_insights (append-only)     │   D4 health_score
                            │   health_score · anomalies[] · clusters[]  │   (typed, evidence-bearing)
                            │   usage_rollup{} · trends{} · narrative    │
                            └───────┬──────────────────────────┬────────┘
                                    │                           │
              D8 feeds #518 ◀───────┘                           │
              dashboards (narrative                             │
              panels, anomaly                ┌──────────────────▼──────────────────┐
              annotations, health gauges)    │   D5  NL QUERY  POST .../ask          │
                                             │   fast path: read insights store      │
                            ┌────────────────│   detail path: BOUNDED read-only SQL  │── R6 SQL safety
                            │                │   D9 every answer carries evidence[]   │   contract
                            │                └──────────────────┬──────────────────┘
                            │                                   │
              D6 troubleshoot POST .../troubleshoot             │
              root-cause correlation across deploys +           ▼
              drift + #542 clusters + traces →          grounded, CITED answer
              narrative with evidence

   D7  TWO SCOPES:  tenant-level (customer runtime, full data)
                    fleet-level  (Movate side, ADR 039 aggregates + authorized digests ONLY —
                                  customer prompt/completion content NEVER leaves the tenant)
```

Every shaded substrate box already exists or is in flight. The net-new code is:
the analyst **agent bundle** (a prompt + a runbook KB + a small set of typed
telemetry-query skills — authored, not hardcoded), the `observability_insights`
table behind the `StorageProvider` Protocol, the bounded-read-only SQL gateway
(R6), the four `/observability/*` endpoints, and the three `mdk observability`
CLI verbs. No new orchestration engine, no new tracer, no new dashboard tool.

---

## Decisions

### D1 — The overnight analyst is an MDK agent (dogfooding)

The analyst is **not bespoke analytics code**. It is a first-class MDK agent —
a bundle with a **prompt**, a **runbook KB** (how to read MDK telemetry, what a
healthy project looks like, what each anomaly kind means), and a small set of
**typed telemetry-query skills** (the read-only, parameterized queries of D5 /
R6, exposed as agent tools). It runs as a **scheduled job on the ADR 017
scheduler** (a cron primitive enqueuing into the existing Postgres queue; the
KEDA worker executes it), **nightly, once per (tenant, project)**.

Why an agent and not a script:

- It **reuses the entire platform** — scheduling (ADR 017), the job queue +
  retry/dead-letter, budget metering (ADR 036), tracing (ADR 015 — the analyst
  is itself observable), the KB/retrieval stack for its runbook, and the typed
  skill mechanism. Net-new is a *bundle + a table + a SQL gateway*, not an
  analytics service.
- It is **the strongest dogfooding story MDK has** — Movate's own product
  observability is produced by an agent built on Movate's own platform.
- It is **tunable like any agent** — the analyst's prompt + runbook can be
  iterated, evaluated (its digests can be harvested + graded, ADR 016 D1), and
  versioned (ADR 014) without a code deploy.

Each nightly run is **budget-capped** (D6/R5; default ceiling per project,
overridable per tenant via ADR 036 quotas). The run reads the last day's
telemetry (plus a trailing baseline window for D3), computes the insight
payload, and writes **exactly one** `observability_insights` row for that
(tenant, project, day). Completion emits an `observability.digest.completed`
event on the ADR 035 outbox; failure emits `observability.digest.failed` with a
partial-payload reference (see *Failure modes*).

### D2 — The insights store is an append-only daily digest table

A new table, `observability_insights`, holds **one row per (tenant, project,
day)**, **append-only** — never mutated, never deleted except by an explicit
retention sweep (Consequences). Append-only is deliberate: it is **cheap**
(insert, no update contention), **auditable** (the digest a human read on
2026-05-12 is bit-stable forever), and **time-series-friendly** (trend queries
are a range scan over `date`, the NL fast path is a single-row read).

The payload is a single `JSONB` column holding the structured digest:
`health_score` (D4), `anomalies[]` (D3), `top_failure_clusters[]` (from #542),
`usage_rollup{}` (from ADR 036 D1), `trends{}`, and `narrative_digest`
(human-readable markdown — the "here's your morning" paragraph). Full DDL in
*Schema*.

The store is the **fast path** for the NL query (D5) and the **contract** the
dashboard pack consumes (D8). A re-run for the same day (e.g. after a fixed
analyst bug) appends a **new row** with a higher `generation` — readers take
the latest `generation` per (tenant, project, day); old generations remain for
audit. The store is **never** the source of truth for raw telemetry — it is a
derived, queryable summary; the raw runs/evals/traces remain authoritative.

### D3 — Anomaly detection: z-score vs trailing baseline + ADR 016 drift

The analyst computes anomalies over four core series — **cost**, **latency**
(p50/p95), **error rate**, and **volume** — using a **z-score against a
trailing baseline** (default 14-day trailing window, configurable). A series
point with `|z| ≥ threshold` (default 3.0) becomes a typed `anomaly` record:

```jsonc
{
  "kind": "cost_spike" | "latency_spike" | "error_rate_spike" | "volume_drop"
          | "volume_spike" | "drift",        // drift is sourced, not z-scored
  "metric": "mdk.run.cost_usd",              // the instrument / series
  "severity": "info" | "warning" | "critical",
  "z_score": 4.2,                            // null for drift-sourced anomalies
  "observed": 184.50, "baseline_mean": 41.2, "baseline_stdev": 33.9,
  "window": { "from": "2026-05-27", "to": "2026-05-28" },
  "evidence": [ /* D9 evidence[] — which runs/events/queries support this */ ]
}
```

**Drift is not re-derived.** ADR 016 D2 already detects eval/score drift
against durable baselines. The analyst **consumes ADR 016's drift detections
directly** and surfaces them as `kind: "drift"` anomaly records (with the ADR
016 detection id in `evidence[]`). The analyst does z-score detection only for
the operational series ADR 016 does not cover (cost/latency/volume/error-rate).
This is a deliberate **no-duplicate-detector** boundary: drift is owned by ADR
016; operational anomalies are owned here; the analyst correlates both into one
digest. Severity is a documented, tunable function of `|z|` (or the ADR 016
drift magnitude) and the metric's business weight (cost/error are weighted
above volume).

### D4 — Health score: a composite 0–100 per project, formula documented + tunable

Each digest carries a `health_score` ∈ [0, 100], a weighted composite of four
sub-scores, each itself 0–100, each derived from a series the substrate already
emits:

| Component        | Source                                   | Default weight |
|------------------|------------------------------------------|---------------:|
| Error-rate score | `mdk.jobs.completed{status}` (ADR 024)   | 0.35 |
| Eval-pass score  | `EvalRecord` pass-rate vs baseline (016) | 0.30 |
| Drift score      | ADR 016 drift detections (inverse)       | 0.20 |
| Cost-trend score | `mdk.run.cost_usd` slope vs baseline     | 0.15 |

```
health_score = 100 * Σ_i ( w_i * subscore_i / 100 )      with Σ_i w_i = 1
```

Each `subscore_i` maps its raw signal onto [0,100] via a documented,
monotonic transform (e.g. error-rate score = `100 * (1 - clamp(error_rate /
error_rate_ceiling, 0, 1))`). The **weights + ceilings are tunable per tenant**
(stored alongside the analyst bundle config, not hardcoded). The score is
**explained, not opaque**: the digest records each `subscore_i` + its inputs in
`trends{}`, so a 72/100 always decomposes into "evals are fine, but cost is
trending up and one drift fired." A health score with no decomposition is a
bug.

### D5 — Natural-language query: `POST /api/v1/observability/ask`

`POST /api/v1/observability/ask { "question": "...", "time_window"?: {...} }`
→ Claude answers the question by reading the insights store (**fast path**) and,
when the digest does not contain the answer, running **bounded, read-only SQL**
against the telemetry tables (**detail path**) — and **always** returns a
grounded answer with **citations** (D9).

The flow:

1. **Fast path (default).** Claude reads the relevant `observability_insights`
   rows (a single-row read for "today," a range scan for "this week's trend").
   Most recurring questions ("what's our error-rate trend," "biggest cost
   outlier yesterday") are answered entirely from the digest — cheap, fast, no
   raw-telemetry query.
2. **Detail path (only when needed).** If the question requires detail the
   digest does not pre-compute ("show me the five slowest runs on the billing
   agent on Tuesday"), Claude may run **bounded read-only SQL** against the
   telemetry tables through the **SQL gateway (R6)** — a constrained surface
   that is provably read-only, tenant-scoped, row-capped, and timeout-bounded.
   Claude never holds a raw DB connection; it calls a tool that enforces R6.
3. **Answer.** A grounded natural-language answer plus an `evidence[]` array
   (D9) naming every insight row, SQL query, and event/run that supports the
   answer. **Low-confidence answers say so** ("I don't have enough signal to be
   sure; here's what the digest does show").

The endpoint is **`read` scope**, **tenant-scoped** (the question can only
touch the caller's tenant's data), and **budget-capped per question** (ADR 036;
a small default per-question ceiling, overridable per tenant). It **cannot
mutate anything** — see R6.

### D6 — Troubleshoot: `POST /api/v1/observability/troubleshoot`

`POST /api/v1/observability/troubleshoot { "symptom": "...", "time_window":
{...} }` → a **root-cause correlation** across the signals that explain
operational incidents, returned as a narrative + evidence:

- **Deploys** — CalVer `agent.published` events (ADR 035 / ADR 021) inside the
  window ("error rate stepped up right after `2026.5.27.12` shipped").
- **Drift** — ADR 016 detections in the window.
- **Error clusters** — the **#542 Failure Pattern Diagnoser**
  (`POST /api/v1/diagnose/cluster_and_propose`) for the affected agent, to get
  the *shape* of the failures (and, incidentally, the diagnoser's proposed
  typed fix — surfaced as a pointer, not auto-applied; applying it is ADR 043's
  job, explicitly out of scope here).
- **Traces** — representative slow/failed traces (ADR 015) as concrete
  exemplars.

The output is a narrative ("latency spiked at 02:10 UTC, 8 minutes after
`billing@2026.5.27.12` deployed; the failure cluster is a timeout on the
pricing tool; here are three exemplar traces") with every claim backed by a D9
`evidence[]` entry. Same scope/budget/tenant rules as D5. The troubleshooter
**reuses #542 for clustering** rather than re-implementing it — see *Cross-
references*.

### D7 — Two scopes: tenant-level and fleet-level, with a hard sovereignty line

The same intelligence runs at two scopes with **different data rights**:

- **Tenant scope** (the customer's own runtime, over their own data). Full
  access: the analyst, `ask`, and `troubleshoot` may read prompts, completions,
  traces, costs, evals — everything in the tenant. This is the primary surface.
- **Fleet scope** (Movate side — Deva + the platform team). The fleet analyst
  + fleet `ask` run in **Movate's** tenant and query **only** what ADR 039
  authorizes to flow to Movate: **aggregated metrics** (via the ADR 039
  Lighthouse `Monitoring Reader` delegation / Phase-2 dual OTLP) **plus
  per-tenant insight *digests*** (the D2 `narrative_digest` + `health_score` +
  `anomalies[]` — *the summary, not the raw payload*) for tenants whose ADR 039
  allow-list authorizes digest sharing.

**The hard line (R4):** customer **prompt/completion content NEVER leaves the
tenant**. The fleet scope sees metrics + insight digests Movate is explicitly
authorized to see — never raw prompts, completions, or traces. The digest that
flows to the fleet is **scrubbed by construction** (the analyst writes the
fleet-shareable digest with no payload content — see *Schema* / the
`fleet_shareable` projection), and the channel is ADR 039's allow-list, which
this ADR **rides, never widens**. A tenant not on the ADR 039 digest allow-list
contributes **nothing** to the fleet view beyond the aggregate metrics ADR 039
already governs.

### D8 — Insight-fed dashboards (extends #518)

The insights store is the **data contract** the #518 dashboard pack consumes
for its **intelligence panels**:

- **Narrative panels** — a Grafana/Workbook text panel renders the latest
  `narrative_digest` for the selected (tenant, project) — the morning summary,
  in the dashboard.
- **Anomaly annotations** — each `anomalies[]` record becomes a dashboard
  annotation on the relevant time series (a cost spike annotated on the cost
  panel), so the *what* (the #518 chart) and the *why* (the anomaly's
  evidence) sit together.
- **Health gauges** — a `health_score` gauge per project, decomposed (D4) on
  hover into its sub-scores.

This ADR **defines the contract** (the JSONB shape of D2 + a stable
`GET /observability/insights` read API the dashboards query); the actual
dashboard JSON changes are a follow-up PR against the #518 pack, out of scope
here. The insights store **feeds** #518; it does not replace Grafana / Azure
Monitor (see *Boundaries*).

### D9 — Citation / grounding contract: every answer carries `evidence[]`

Every NL answer from `ask` (D5) and every narrative from `troubleshoot` (D6),
and every `anomalies[]` record (D3), carries an `evidence[]` array — a typed
list of the sources that ground the claim:

```jsonc
"evidence": [
  { "kind": "insight",  "ref": { "tenant_id": "t1", "project_id": "p1",
                                 "date": "2026-05-27", "generation": 2 } },
  { "kind": "sql",      "ref": { "query_id": "q_01H...", "sql": "SELECT ...",
                                 "row_count": 5, "elapsed_ms": 220 } },
  { "kind": "event",    "ref": { "event_id": "evt_01H...", "type": "agent.published" } },
  { "kind": "run",      "ref": { "run_id": "run_01H..." } },
  { "kind": "drift",    "ref": { "detection_id": "drf_01H..." } },
  { "kind": "cluster",  "ref": { "diagnoser_cluster_id": "cl_01H..." } }
]
```

**Contract (R3):** an answer with an empty `evidence[]` is a bug, not a valid
answer. Claude is instructed (and the endpoint enforces) that any factual
claim must trace to at least one evidence entry; when the underlying signal is
thin, the answer's `confidence` field is `low` and the prose says so explicitly
rather than fabricating. The `sql` evidence kind carries the **exact query
that ran** (and its row count + elapsed time) so a reviewer can re-run it and
confirm the answer — auditable, not hallucinated.

---

## Schema (DDL-ish — illustrative, behind the `StorageProvider` Protocol)

All tables carry `tenant_id NOT NULL` and are filtered by it on every read
(the same per-tenant boundary ADR 040 establishes). The KB/runtime layer
reaches these only through the `StorageProvider` Protocol — never `postgres`/
`sqlite` directly (CLAUDE.md boundary rule 6).

```sql
-- The append-only daily digest. One logical digest per (tenant, project, day);
-- a re-run appends a new generation rather than mutating.
CREATE TABLE observability_insights (
  id              TEXT PRIMARY KEY,             -- ULID
  tenant_id       TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  date            DATE NOT NULL,                -- the day the digest summarizes
  generation      INTEGER NOT NULL DEFAULT 1,   -- bumped on re-run; readers take MAX
  health_score    INTEGER,                      -- 0..100 (D4); NULL if not computable
  payload         JSONB NOT NULL,               -- the structured digest (below)
  fleet_shareable BOOLEAN NOT NULL DEFAULT FALSE,-- D7: TRUE only if ADR 039 allow-list authorizes
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, project_id, date, generation)
);
-- Time-series read pattern: latest generation per (tenant, project), by date range.
CREATE INDEX ix_insights_lookup
  ON observability_insights (tenant_id, project_id, date DESC, generation DESC);

-- payload JSONB shape (documented, validated at the storage seam):
-- {
--   "health_score": 72,
--   "health_components": { "error_rate": 95, "eval_pass": 88, "drift": 60, "cost_trend": 40 },
--   "anomalies":      [ <typed anomaly record, D3, each with evidence[]> ],
--   "top_failure_clusters": [ <#542 cluster summary + diagnoser_cluster_id> ],
--   "usage_rollup":   { "runs": 1240, "tokens": 9.1e6, "cost_usd": 41.2, ... },  -- ADR 036 D1
--   "trends":         { "cost_usd": {"slope": ..., "window": ...}, "error_rate": {...}, ... },
--   "narrative_digest": "## Morning digest — billing-faq\n\nHealthy overall ..."  -- markdown
-- }
-- The `fleet_shareable` projection (D7) is the payload with prompt/completion
-- content stripped by construction — the analyst never writes payload content
-- into a fleet-shareable row. (The digest is summaries + metrics, not payloads,
-- so the projection is the narrative + health + anomalies, never raw text.)
```

`observability_anomalies` is **a view, not a second table** — anomalies live
inside the digest `payload` (they are produced together, read together, and
audited together; a separate write would risk divergence). The view flattens
`payload->'anomalies'` for the dashboard-annotation query (D8) and the
`GET /observability/insights?kind=anomaly` filter:

```sql
CREATE VIEW observability_anomalies AS
SELECT i.tenant_id, i.project_id, i.date, i.generation,
       a->>'kind'      AS kind,
       a->>'metric'    AS metric,
       a->>'severity'  AS severity,
       (a->>'z_score')::numeric AS z_score,
       a               AS anomaly         -- full typed record incl. evidence[]
FROM   observability_insights i,
       LATERAL jsonb_array_elements(i.payload->'anomalies') a;
```

No raw-telemetry tables are added or changed — this ADR **reads** the existing
run/eval/event/usage tables and **writes** only `observability_insights`.

---

## API surface (additive under `/api/v1`; ADR 033 hardening applies; OpenAPI contract-tested)

| Method | Path                                          | Scope | Purpose |
|--------|-----------------------------------------------|-------|---------|
| GET    | `/api/v1/observability/insights`              | read  | The digests. Query by `project_id`, `date` / date range, optional `kind=anomaly`. Returns latest generation per (project, date). The dashboard read contract (D8). |
| GET    | `/api/v1/observability/health`                | read  | The current `health_score` (+ decomposition, D4) per project. The gauge contract (D8). |
| POST   | `/api/v1/observability/ask`                   | read  | NL query (D5). Body `{ question, time_window? }`. Returns `{ answer, confidence, evidence[] }`. Fast path + bounded detail path (R6). Budget-capped, tenant-scoped. |
| POST   | `/api/v1/observability/troubleshoot`          | read  | Root-cause correlation (D6). Body `{ symptom, time_window }`. Returns `{ narrative, evidence[] }`. Budget-capped, tenant-scoped. |

**Scopes.** All four are **`read`** — none of the four can write any agent,
KB, config, or telemetry record. (The analyst itself writes `observability_
insights`, but it does so as a scheduled internal job, not through these
public endpoints.) The fleet-scoped variants (D7) are the same paths served by
**Movate's** runtime over the ADR 039-authorized aggregate + digest data; they
are not separate endpoints — the scope is the deployment's data boundary.

**The citation shape** is D9's `evidence[]`, returned on every `ask` and
`troubleshoot` response and embedded in every `anomalies[]` record.

---

## CLI

```
mdk observability ask "why did latency spike on billing on Tuesday?"   # → grounded answer + evidence
mdk observability health [--project <id>]                              # → health_score + decomposition
mdk observability digest [--date YYYY-MM-DD] [--project <id>]          # → the day's narrative digest
```

All three are thin clients over the `/observability/*` endpoints (the
control-plane `mdk` talks to the runtime; `cli ⊥ runtime` preserved). `ask`
streams the answer and then prints the `evidence[]` list; `digest` renders the
`narrative_digest` markdown; `health` renders the gauge + decomposition. New
commands only — no existing CLI shape changes (CLAUDE.md rule 5). `--json` on
each emits the structured response (the citation shape included) for scripting.

---

## Resolved decisions (locked in upfront)

- **R1 — The analyst is an MDK agent (dogfooding), not bespoke code.** It
  reuses scheduling (ADR 017), budgets (ADR 036), KB/retrieval, typed skills,
  tracing (ADR 015), and the registry (ADR 014). Net-new is a bundle + a table
  + a SQL gateway, not an analytics service. (D1.)
- **R2 — The insights store is append-only.** Auditable, time-series-friendly,
  cheap (insert-only). Never mutated; a re-run appends a higher `generation`;
  shrinkage is an explicit retention sweep, not in-place deletion. (D2.)
- **R3 — NL answers MUST cite evidence.** An answer with an empty `evidence[]`
  is a bug. Low-confidence answers say so rather than fabricate. The `sql`
  evidence kind carries the exact query that ran, so any answer is re-runnable
  + auditable. (D9.)
- **R4 — Customer content never leaves the tenant.** The fleet scope (D7) sees
  ADR 039-authorized aggregate metrics + fleet-shareable insight digests only —
  never raw prompts/completions/traces. The fleet-shareable digest is scrubbed
  by construction; the channel is ADR 039's allow-list, which this ADR rides
  and never widens. (D7.)
- **R5 — Both the analyst and the NL query are budget-capped (ADR 036).** The
  nightly analyst has a per-run ceiling; `ask`/`troubleshoot` have a
  per-question ceiling; both default low and are overridable per tenant via ADR
  036 quotas. A budget-exhausted run/query degrades gracefully (partial
  insights / fast-path-only answer) rather than failing hard. (D1, D5, D6.)
- **R6 — The NL detail path is BOUNDED READ-ONLY SQL.** The single riskiest
  surface in this ADR. The detail path can **NEVER** mutate, and every query is
  scoped + row-capped + timeout-bounded. The full contract is below. (D5, D6.)

### R6 in full — the bounded-read-only SQL contract

The NL detail path lets Claude run SQL against telemetry tables. This is
powerful and dangerous; the contract makes it **provably safe**:

1. **Read-only transaction, enforced at the connection.** Every detail-path
   query runs in a transaction opened with `SET TRANSACTION READ ONLY` on a
   **dedicated, least-privilege DB role** (`mdk_observability_ro`) that has
   been `GRANT SELECT`-ed on the telemetry tables/views **only** — no `INSERT`/
   `UPDATE`/`DELETE`/`DDL` privilege exists for that role at all. Even if a
   prompt-injection attack produced a `DROP TABLE`, the role cannot execute it
   and the read-only transaction would reject it twice over. This is the
   primary, defense-in-depth control: **safety does not depend on the LLM
   behaving** — it depends on a Postgres role grant.
2. **Statement allowlist.** The gateway parses the statement and rejects
   anything that is not a single `SELECT` (no `WITH ... DELETE`, no multiple
   statements, no `;`-chaining, no DDL/DML keywords, no `pg_*`/`information_
   schema` introspection beyond an allowlisted set, no `COPY`, no
   function-call side-effects). One statement, one `SELECT`, parsed and
   validated before it reaches the DB.
3. **Mandatory tenant scope.** The gateway injects `WHERE tenant_id = $caller`
   (and rejects any query that references another tenant_id literal). A query
   can **never** read across tenants — the same boundary as every other read
   in the platform.
4. **Row cap, always.** Every query is wrapped with a hard `LIMIT` (default
   1000, never unbounded). A query that would return more is truncated and the
   answer notes the truncation. **No full-table scan without a cap** — the
   gateway rejects a query whose plan estimate exceeds a configurable row/cost
   ceiling (an `EXPLAIN` guard), so a pathological query cannot tie up the DB.
5. **Statement timeout.** `SET LOCAL statement_timeout = <ms>` (default a few
   seconds) on every detail-path query. A slow query is killed by Postgres,
   not left running; the NL layer falls back to the fast-path (insight-store)
   answer and notes that the detail was unavailable (see *Failure modes*).
6. **Allowlisted tables/columns.** The role + the gateway expose only the
   telemetry tables/views needed to answer operational questions (runs, evals,
   events, usage rollups, `observability_insights`/`_anomalies`) — **not**
   secrets, credentials, tenant-provider-keys (ADR 018), or any auth table.
7. **Every detail-path query is logged + cited.** The exact SQL, the row
   count, and the elapsed time become a `kind: "sql"` evidence entry (D9), so
   every detail-path read is auditable after the fact.

In short: the NL query **cannot mutate** (role grant + read-only txn +
statement allowlist), **cannot cross tenants** (injected scope), **cannot
scan unbounded** (row cap + EXPLAIN guard), and **cannot hang** (statement
timeout) — and every query it runs is recorded as citable evidence. The
safety is structural (DB role + gateway), not a matter of trusting the model's
output.

---

## Failure modes

- **Analyst run fails (partial insights preserved).** A nightly run that fails
  partway (provider timeout, budget exhausted mid-run, a query error) writes
  whatever it computed as a **partial digest** (`payload.partial = true`, with
  the completed sections present and the failed sections noted) at a new
  `generation`, and emits `observability.digest.failed` on the ADR 035 outbox
  (→ Teams/email/webhook alert). The morning is not silently blank — the human
  gets a partial digest + an explicit "the analyst couldn't finish; here's what
  it got." The next nightly run retries cleanly.
- **NL query over a day with no insights yet.** If `ask`/`troubleshoot` is
  invoked for a (project, day) the analyst hasn't summarized yet (e.g. mid-day,
  before tonight's run, or a brand-new project), the query **falls back to a
  live bounded query** (R6) over the raw telemetry and **says so**: "no digest
  exists for today yet; this answer is from a live query." Correctness is
  preserved (the live path is the same bounded-read-only path); only the
  cheap-fast-path optimization is unavailable.
- **SQL detail-path timeout / cap hit.** If a detail-path query times out
  (R6.5) or hits the row cap (R6.4), the NL layer **returns the insight-store
  (fast-path) answer** and **notes that the detailed breakdown was unavailable**
  ("I can tell you the trend from the digest, but the per-run detail query
  timed out"). The answer is still grounded (on the digest), still cited, and
  marked `confidence: "partial"`. A timeout never produces a hard error or an
  ungrounded guess.
- **Budget exhausted.** Per R5: the analyst writes a partial digest + alerts;
  `ask`/`troubleshoot` answer from the fast path only (no detail-path spend)
  and note the limit. Degrade, don't fail.
- **Fleet digest unavailable / tenant not on allow-list.** The fleet view
  (D7) simply omits that tenant's digest and shows only the ADR 039 aggregate
  metrics for it — no error, no leakage; the sovereignty default is "show
  less," never "ask the tenant for raw data."

---

## Consequences

**Positive.**
- **Morning-ready understanding.** The four-dashboard forensic ritual becomes a
  one-paragraph digest + a health gauge + annotated anomalies — per project,
  ready by morning, for free (the analyst ran overnight).
- **Ad-hoc questions get grounded answers.** "Why did latency spike on
  Tuesday?" returns a cited narrative in seconds instead of a 30-minute manual
  correlation across Grafana + Langfuse + the deploy log.
- **Strongest dogfooding story MDK has.** Movate's own product observability is
  produced by an agent built on Movate's own platform — scheduled, budgeted,
  evaluable, versioned like any customer agent.
- **Auditable by construction.** Every answer cites its evidence (D9); every
  detail-path query is logged + re-runnable (R6.7); the insights store is
  append-only (R2). No "the dashboard said so, trust me."
- **Feeds, doesn't fork, the dashboards.** #518's panels gain narrative +
  anomaly annotations + health gauges from one contract (D8); no second viz
  tool.
- **Sovereignty preserved.** The fleet view (D7) is built entirely from ADR
  039-authorized aggregates + scrubbed digests; customer content never leaves
  the tenant.

**Risks / watch items.**
- **Insight-store growth → retention policy.** One row per (tenant, project,
  day) (× generations) grows unbounded without a retention sweep. Mitigation:
  a configurable retention window (default keep daily digests for N months,
  then roll up to weekly/monthly summaries before pruning) on the existing
  scheduler tick. Append-only makes pruning a clean range-delete; the
  roll-up preserves long-range trends. **Document the default + the knob.**
- **NL-query SQL safety is the riskiest surface.** R6 is the mitigation, and
  it is layered (DB role grant + read-only txn + statement allowlist + tenant
  scope injection + row cap + EXPLAIN guard + statement timeout + audit log).
  The safety is structural, not model-trust-based. **Watch:** any future
  widening of the `mdk_observability_ro` grant or the table allowlist is a
  security-sensitive change and must be reviewed as one.
- **Analyst-cost at fleet scale.** One budgeted agent run per (tenant, project)
  per night scales linearly with the fleet; the per-run ceiling (R5) bounds it,
  but very large fleets should batch/sample low-traffic projects (e.g.
  every-other-night for projects with < N runs/day). **Watch the aggregate
  nightly spend** (metered via ADR 036) and tune the cadence.
- **Over-trust in the narrative.** If operators read the `narrative_digest` and
  stop opening the dashboards entirely, a gap the analyst's prompt doesn't
  cover could go unseen. Mitigation: the narrative always links to the relevant
  #518 panels; the digest is a lens onto the dashboards, not a replacement.
  Harvest + grade the analyst's digests (ADR 016 D1) to keep its coverage
  honest.
- **Anomaly false-positives.** A z-score detector over a noisy/low-volume
  series will over-fire. Mitigation: severity weighting (D3), a minimum-volume
  floor before z-scoring, and the tunable threshold; track the
  flagged-but-dismissed rate as a tuning signal.

**Neutral.**
- One new table (`observability_insights`) + one view, four additive `read`
  endpoints, three new `mdk observability` verbs, one new scheduled agent
  bundle, one new least-privilege DB role. All additive; no change to existing
  telemetry capture, dashboards, or the runtime API.

---

## Alternatives considered

- **A bespoke analytics service** (a standalone microservice that ingests
  telemetry and computes insights). **Rejected.** It would re-implement
  scheduling, budgeting, retrieval, and tracing that the agent platform already
  provides, and it would forgo the dogfooding story. The analyst-as-an-MDK-agent
  (D1) reuses the whole platform and is itself observable, evaluable, and
  versioned. (R1.)
- **Live-query-only, no preprocessing** (answer every question by querying raw
  telemetry on demand, no insights store). **Rejected.** Recurring questions
  re-pay the full query cost every time (slow + expensive, especially over
  Azure Monitor KQL), and there is no cheap morning digest. Preprocessing
  **amortizes** the recurring cost into one nightly run and makes the fast path
  O(1). The detail path (D5) preserves on-demand live query for the long tail —
  best of both. (R2.)
- **Ship raw logs to an external SIEM** (Splunk / Sentinel / Datadog) and query
  there. **Rejected.** It breaks the data-sovereignty posture (ADR 015 — raw
  payloads must stay in-tenant), adds per-event SaaS cost at scale, and still
  doesn't give a grounded NL answer with MDK-aware evidence. The intelligence
  belongs **inside** the tenant, on the platform that produced the telemetry.
- **A free-form SQL agent with full DB access.** **Rejected, emphatically.**
  An agent with a writable or unscoped DB connection is a prompt-injection
  catastrophe waiting to happen. R6's bounded-read-only contract (least-priv
  role + read-only txn + statement allowlist + tenant scope + row cap + EXPLAIN
  guard + timeout) is the non-negotiable alternative: the model proposes SQL, a
  structural gateway enforces safety, and the DB role makes mutation impossible
  regardless of what the model emits.
- **Mutate one digest row per day in place** (instead of append-only +
  generation). **Rejected.** It loses auditability (the digest a human read
  yesterday could silently change) and adds update contention. Append-only with
  a `generation` counter (R2) is cheaper and auditable. (D2.)
- **Re-implement drift/clustering inside the analyst.** **Rejected.** ADR 016
  already owns drift detection and #542 already owns failure clustering; the
  analyst **consumes** both as evidence (D3, D6) rather than duplicating
  detectors. One detector per concern; the analyst correlates, it does not
  re-derive. (See *Cross-references*.)

---

## Boundaries (explicitly NOT in scope)

- **Replacing Grafana / Azure Monitor / Langfuse.** This ADR **feeds** the
  #518 dashboards (D8) and reads the ADR 015 telemetry; it does not replace any
  visualization or storage backend. The dashboards remain the *what*; this ADR
  adds the *why* on top.
- **A general BI / analytics tool.** The NL query is **operational
  observability intelligence** over MDK telemetry, not a general-purpose
  business-intelligence query engine over arbitrary customer data. The R6
  table allowlist deliberately scopes it to telemetry.
- **Cross-tenant analysis beyond ADR 039's allow-list.** The fleet scope (D7)
  sees exactly what ADR 039 authorizes — aggregate metrics + allow-listed
  digests — and not one byte more. Any widening of cross-tenant visibility is
  an ADR 039 change, not this ADR.
- **Applying fixes.** `troubleshoot` (D6) may *surface* the #542 diagnoser's
  proposed typed fix as a pointer, but **applying** it is the Self-Improving
  Loop's job (ADR 043), explicitly out of scope here. This ADR observes and
  explains; it does not mutate agents.
- **New telemetry instrumentation.** This ADR reads the existing
  `METRIC_NAMES` + spans + records (ADR 024 / #518); it adds no new emission.
  If a question needs a metric MDK doesn't emit, that's a #518 / ADR 024
  follow-up, not this ADR.
- **Changes to the existing telemetry capture, the dashboard JSON, or the
  `/api/v1` runtime API shape.** All additive; the actual #518 dashboard-panel
  changes (D8) are a separate follow-up PR against the dashboard pack.
- **CI / boundary changes.** `cli ⊥ runtime` is unchanged; the analyst runs on
  the existing ADR 017 worker; tracing stays wired at the edges
  (CLAUDE.md rule 6).

---

## Cross-references / composition notes

### Reusing ADR 016 (continuous-improvement loop) as the drift substrate

The analyst does **not** detect drift — ADR 016 D2 already runs continuous eval
against durable baselines and emits drift detections. The analyst **consumes
those detections** and surfaces each as a `kind: "drift"` anomaly record (D3)
with the ADR 016 detection id in `evidence[]`. The analyst's own z-score
detector covers **only** the operational series ADR 016 does not (cost,
latency, volume, error-rate). This is a deliberate one-detector-per-concern
boundary: **flag** — if a future change blurs this (e.g. the analyst starts
re-deriving eval drift), the two detectors will disagree and the digest's drift
story will fork. Keep drift owned by ADR 016; the analyst correlates, it does
not re-derive. ADR 016 D1 (harvest) is also the mechanism by which the
**analyst's own digests can be graded** — harvest a sample of digests, have a
human grade their accuracy, feed that back into the analyst's eval set — so the
analyst improves like any other agent.

### Reusing #542 (Failure Pattern Diagnoser) as the error-cluster substrate

The analyst's `top_failure_clusters[]` (D2) and the troubleshooter's error-
cluster evidence (D6) come from the **#542 diagnoser**
(`POST /api/v1/diagnose/cluster_and_propose`), which is **read-only with
respect to agent state** (it clusters failures + proposes a typed fix, it does
not mutate). The analyst/troubleshooter call it for the *shape* of the failures
and carry the `diagnoser_cluster_id` as `kind: "cluster"` evidence (D9). They
deliberately **do not** apply the diagnoser's proposed fix — that is ADR 043's
closed-loop job (Boundaries). **Flag:** the diagnoser was built for the
self-improving loop's *propose* step; this ADR reuses it as a pure
read-only analysis substrate. That reuse is sound precisely *because* #542 is
read-only — but it means the troubleshooter's quality is bounded by the
diagnoser's clustering quality, and a future change that made the diagnoser
write-capable (or coupled it to ADR 043's apply step) would break the
read-only assumption this ADR relies on. The dependency is on #542's
**read-only `cluster_and_propose`** contract specifically; that contract must
stay read-only for this ADR's reuse to remain safe.

### Reusing ADR 039 (fleet telemetry / Lighthouse) as the fleet channel

The fleet scope (D7) does **not** open any new cross-tenant channel. It reads
exactly what ADR 039 already governs: aggregate metrics via the Lighthouse
`Monitoring Reader` delegation (Phase 1) / dual OTLP (Phase 2), **plus**
per-tenant insight digests for tenants whose ADR 039 allow-list authorizes
digest sharing. The new artifact this ADR contributes to the fleet is the
**`fleet_shareable` digest projection** (D7 / Schema) — a scrubbed, payload-
free summary that flows through ADR 039's existing allow-list machinery. This
ADR **rides** ADR 039's sovereignty boundary and **never widens** it; a tenant
off the allow-list contributes only the aggregate metrics ADR 039 already
permits.

### Reusing ADR 035 + ADR 021 as the deploy-correlation substrate

The troubleshooter (D6) correlates symptoms against **deploys** by reading the
`agent.published` lifecycle events (ADR 035 D1) carrying CalVer (ADR 021) — no
new deploy-tracking is added. The analyst's own completion/failure are likewise
typed events on the same outbox (`observability.digest.completed/failed`),
consumed by the existing webhook/SSE machinery (ADR 035 D2/D3) for alerting.
