# ADR 095 — Unified observability store: OTel hub → ClickHouse (telemetry) + Postgres (business of record), correlated by trace_id

Status: Proposed
Date: 2026-06-09
Deciders: Engineering + Deva (Movate) — observability is a customer-facing
operational surface; the storage seam + the "central collector" direction follow
the ADR 039 multi-tenant telemetry posture and need product sign-off.
Builds on: ADR 020 (OTel tracing), ADR 031 (dashboards-as-code), ADR 036/087
(operational metrics), ADR 039 (per-customer telemetry export / central
collector seam), ADR 024 (trace propagation), ADR 082 (Temporal metrics).

## Context

Observability data today is **fragmented across four stores** that grew
independently:

| Data | Where it lives now |
|---|---|
| mdk operational records (runs, jobs, workflow_runs, governance audit, agent state) | Postgres `movate` DB |
| Temporal durable state + event history + visibility | Postgres `temporal` / `temporal_visibility` DBs |
| Langfuse trace **metadata** (projects, scores, datasets) | Postgres `langfuse` DB |
| Langfuse trace **events** (per-LLM-call spans, the bulk data) | ClickHouse (Langfuse VM, local volume) |
| mdk metrics (cost, latency, completions, governance, certification matrix) | Prometheus / Azure Monitor (OTLP → `azuremonitor` exporter) |

The deployed Postgres flexible server already consolidates the *transactional*
state of all three systems (a deliberate "reuse one Postgres" choice), but the
**high-volume telemetry is split** (Langfuse → ClickHouse, mdk metrics →
Prometheus/Azure Monitor, mdk/Temporal traces → Jaeger locally / App Insights in
Azure). The goal is **one centralized telemetry store feeding unified Grafana
dashboards** — a cost spike should pivot to the trace, the LLM spans, the
governance decision, and the workflow run, in one pane.

**The trap to avoid:** "centralize" must not mean "one physical schema for
everything." Metrics (time-series), traces (span trees, very high write volume),
logs (append-heavy), and business records (relational, transactional) have
fundamentally different access patterns; forcing them into one table — especially
Postgres — collapses under trace volume and kills query performance.

**The leverage we already have:** the **OTel Collector** (`movate-dev-otelcol`)
is already the single ingestion choke point — mdk spans + metrics, Temporal (via
`TracingInterceptor`), and agent traces all emit OTLP through it. And mdk already
carries a **dual-export seam** (ADR 039: `MDK_TELEMETRY_ENDPOINT` /
`MDK_TELEMETRY_CUSTOMER_ID`) anticipating a central collector. The foundation
exists; this ADR decides where it points.

## Decision

### D1 — Centralize *ingestion* + *query*, not the physical schema
The unit of centralization is the **OTel Collector** (one ingestion hub, OTLP as
the wire format) and **Grafana** (one query pane). Each signal type keeps a
fit-for-purpose store. We explicitly reject a single-store-for-everything design.

### D2 — ClickHouse is the unified **telemetry** store (traces + logs + metrics)
Add a **ClickHouse exporter** to the OTel Collector so traces, logs, and metrics
land in one columnar store. Rationale:
- **We already run ClickHouse** (Langfuse v3) — reuse the operational competency
  and bring Langfuse's LLM traces into the same store rather than standing up 3+
  new services.
- One store ⇒ one backup story, one query language (SQL), excellent compression
  for high-volume trace/cost data, and the high-cardinality analytics the unified
  dashboards need.

### D3 — Postgres remains the **business of record**
Runs, the cost ledger, governance decisions, `workflow_runs`, and agent lifecycle
state stay in the Postgres `movate` DB — relational, transactional, low-volume,
exact drill-down. Grafana reads it via the Postgres datasource. (Telemetry is
derived + sampled; the business of record is authoritative and must not live in a
columnar telemetry store.)

### D4 — `trace_id` is the universal correlation key
What makes this *unified* rather than merely *co-located*: every signal carries
`trace_id` — spans, metric exemplars, the `RunRecord` (mdk already stamps
`metrics.trace_id`), the cost row, and the governance audit. A single dashboard
can then pivot across ClickHouse (telemetry) and Postgres (business) on one key.
Enforcing end-to-end `trace_id` propagation (ADR 024) is part of this decision.

### D5 — Grafana is the single pane (multi-datasource, not multi-tool)
One Grafana with two primary datasources — **ClickHouse** (telemetry) and
**Postgres** (business of record) — plus the existing Temporal UI / Langfuse UI
for deep drill-down. Unified dashboards (cost ⨯ trace ⨯ governance ⨯ run) are
built as dashboards-as-code (ADR 031) and join the drift guard.

### D6 — Migration is additive + incremental (no big-bang)
1. Add the ClickHouse exporter to the Collector **alongside** today's outputs —
   nothing breaks; telemetry starts landing in CH.
2. Add Grafana datasources (ClickHouse + Postgres); build the first cross-store
   panel.
3. Enforce `trace_id` propagation end-to-end (mostly there).
4. Decide Langfuse: read its CH tables directly, or dual-export into the shared CH.
5. Once unified dashboards are proven, retire the redundant metric path
   (Azure Monitor/Prometheus) if a single source is wanted.

## Alternatives considered

- **Everything in Postgres** — rejected: traces/metrics volume collapses a
  row-store; kills query latency; wrong tool for time-series + span trees.
- **Grafana LGTM (Tempo + Loki + Mimir)** — viable and best-in-class for
  Grafana-native trace/log UX + exemplars, but it adds 3–4 stateful services +
  object storage. Deferred: revisit at larger scale or if native trace
  exploration becomes a hard requirement. ClickHouse-unified is lower-friction
  for a deployment that already runs ClickHouse + an OTel collector.
- **Status quo (fragmented)** — rejected: no unified dashboards, no cross-store
  pivot, four backup/retention stories.

## Consequences

- **Compat (additive):** a new Collector exporter + new Grafana datasources;
  nothing is removed on day one. No change to mdk's emit path, the `/api/v1`
  surface, storage schema, or env contracts. The ADR 039 central-collector seam
  is the forward path.
- **Durability note (carry-over):** the managed Postgres is backed up; a
  ClickHouse on a single VM volume is not. Productionizing this means a managed
  or replicated ClickHouse (or object-storage-backed retention) — tracked as a
  follow-on, out of scope for the prototype.
- **Telemetry is sampled/derived; Postgres is authoritative.** Dashboards must
  source exact business numbers (cost ledger, governance outcomes) from Postgres,
  using ClickHouse for high-cardinality exploration + trace bodies.

## Prototype (this PR's follow-on, D6 steps 1–2)
Add the ClickHouse exporter to `infra/otel-collector/otel-collector-config.yaml`
(+ the Azure collector module), provision Grafana ClickHouse + Postgres
datasources, and ship one cross-store panel (workflow cost from Postgres ⨯ trace
volume from ClickHouse, joined on `trace_id`) as dashboards-as-code.
