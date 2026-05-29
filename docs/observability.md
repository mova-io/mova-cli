# mdk observability — spans + metrics catalog

> Reviewer-grade reference for the architecture review. Single page covering
> what mdk emits, where it lives in the code, how it flows out, and how to
> import the in-repo dashboards. Everything in the tables below was extracted
> directly from `src/` — no fabricated names.

## Overview — two planes, one Tracer Protocol

mdk separates the **control plane** (`src/movate/cli/`) from the **execution
plane** (`src/movate/runtime/` + `src/movate/core/`). Both planes emit
observability via the `Tracer` Protocol (`src/movate/tracing/base.py`) and the
OTel metrics module (`src/movate/tracing/metrics.py`). Boundary rule 6
(`CLAUDE.md`) keeps tracing **wired at the edges only** — `core/` never
imports `tracing/`; the tracer/meter handle is injected through the executor
and dispatched-from-the-edge.

Tracer Protocol implementations under `src/movate/tracing/`:

| Impl | File | Purpose |
| --- | --- | --- |
| `NullTracer` | `null.py` | No-op default; zero-cost when observability is off |
| `StdoutTracer` | `stdout.py` | Local debugging: prints span lifecycle to stderr |
| `OtelTracer` | `otel.py` | OTLP push to the configured Collector (the prod path) |
| `LangfuseTracer` | `langfuse.py` | Langfuse session/trace tree (ADR 015) |
| `CompositeTracer` | `composite.py` | Fan a span out to multiple backends |
| `AuditTracer` | `audit.py` | Append spans to a local JSONL audit log |

The OTel **metrics** half (`metrics.py`) is OTel-only — no Langfuse mirror;
counters/histograms ship over the same OTLP endpoint the spans use.

## Metrics catalog

Source of truth: `src/movate/tracing/metrics.py` (`METRIC_*` constants /
`METRIC_NAMES` frozenset). Anything *not* in this table is not currently
emitted as an OTel metric.

| Instrument (OTel dot-form) | Kind | Unit | Attributes | Recorded by (source) |
| --- | --- | --- | --- | --- |
| `mdk.jobs.completed` | counter | 1 | `kind`, `status`, `tenant` | `src/movate/tracing/metrics.py:439` (`record_job_completed`) — called from `src/movate/runtime/worker.py` on every terminal job |
| `mdk.job.duration_ms` | histogram (ms) | ms | `kind`, `status` | same call site as `mdk.jobs.completed` (paired) |
| `mdk.jobs.in_flight` | up-down counter | 1 | `tenant` | `src/movate/tracing/metrics.py:472` (`inc_in_flight` / `dec_in_flight`) bracketed in `src/movate/runtime/worker.py:164` |
| `mdk.run.tokens` | counter | 1 | `tenant` | `src/movate/tracing/metrics.py:451` (`record_run_usage`) — runtime edge after a run terminates with usage |
| `mdk.run.cost_usd` | counter | usd | `tenant` | same call as `mdk.run.tokens` |
| `mdk.db.pool.size` | observable gauge | 1 | — | `src/movate/tracing/metrics.py:390` (`register_pool_metrics`) — callback samples the live asyncpg pool from `src/movate/storage/postgres.py:947` (ADR 034 D3) |
| `mdk.db.pool.idle` | observable gauge | 1 | — | same callback |
| `mdk.db.pool.in_use` | observable gauge | 1 | — | same callback |
| `mdk.db.pool.waiting` | observable gauge | 1 | — | same callback |
| `mdk.db.pool.max` | observable gauge | 1 | — | same callback |

**`status`** values: `success` / `error` / `safety_blocked` / `dead_letter`.
Dead-letter rate is *not* a separate instrument — it is `mdk.jobs.completed`
filtered to `status=dead_letter`.

**Cardinality rules** (encoded in the call sites): `tenant` is allowed as an
attribute; `job_id` / `run_id` are **never** attributes (they would explode
cardinality and are recoverable from spans). The DB pool gauges carry **no**
attributes — per-pod identity comes from the OTel `Resource` (e.g.
`service.instance.id` stamped by the Collector / Resource SDK).

**Not yet emitted as metrics** (these surface as Langfuse scores or are
deferred):

| Concept | Why not metric-side today |
| --- | --- |
| Queue depth (`mdk.queue.depth`) | Needs a `StorageProvider` count query — deferred to item #27 to keep the metrics module off the storage seam (`src/movate/tracing/metrics.py` header) |
| Eval pass-rate / drift | Surface as Langfuse scores (ADR 031 D1), not OTel metrics |
| Per-route HTTP latency | mdk's `/api/v1` is instrumented via spans, not a per-route metric; latency lives in `agent.execute` span `DurationMs` |
| Per-provider cost split | `mdk.run.cost_usd` is keyed only on `tenant` today; per-provider lives on the `agent.execute` span `provider` attribute — query Jaeger/Langfuse to slice that way |

## Spans catalog

Span names are inline string literals at the `start_span` call sites. Audited
from the source under `src/movate/`:

| Span name | Attributes (on creation) | Where started | Notes |
| --- | --- | --- | --- |
| `workflow.execute` | `workflow`, `workflow_version`, `workflow_run_id`, `tenant_id` | `src/movate/core/workflow/runner.py:266` | ADR 024 D4 — workflow root; every node's `agent.execute` nests under it |
| `agent.execute` | `agent`, `agent_version`, `provider`, `tenant_id`, `job_id`, `run_id`, `model_override` | `src/movate/core/executor.py:259` | Run root |
| `agent.turn[N]` | `turn`, `model` | `src/movate/core/executor.py:1257` | One per agent turn under `agent.execute` |
| `retrieval.<skill>` | `skill`, `turn=0`, `auto_into` | `src/movate/core/executor.py:933` | ADR 024 D1 — pre-retrieval ("turn 0") |
| `skill.<name>` | `skill`, `turn` | `src/movate/core/executor.py:1387` | One per dispatched tool call, parented under the turn |
| `kb_search` | `stage_count`, `total_ms`, plus per-stage children | `src/movate/kb/trace.py:249` | KB retrieval root; children are stage-named (e.g. `embed`, `retrieve`, `rerank`) |

The `kb_search` children carry: `duration_ms`, `input_count`, `output_count`,
`chunk_count`, `chunk_ids_preview` (capped at 10) — see
`src/movate/kb/trace.py:258-267`.

## Export paths

mdk emits OTLP — vendor-neutral (ADR 001). The destination is controlled by
the standard OTel env vars + the mdk `MOVATE_TRACE_SINK` gate.

```
                       (control gate)                  (OTel SDK env)
MOVATE_TRACE_SINK={otlp|both}  +  OTEL_EXPORTER_OTLP_ENDPOINT  +  OTEL_EXPORTER_OTLP_PROTOCOL
                       |
                       v
              OTLP /  HTTP|gRPC
                       |
                       v
   +---------------------------------------+
   |          OpenTelemetry Collector       |
   +---------------------------------------+
       |             |              |
       v             v              v
  traces           metrics         logs
       |             |
       |             |
   PROD path:        |
       +-------------+--> exporter `azuremonitor`
                          (App Insights `App*` tables;
                           ADR 020; infra/azure/modules/
                           containerapp-otel-collector.bicep)
   LOCAL DEMO:
       +--> Jaeger          +--> Prometheus
            (otlp/jaeger)        (prometheus exporter :8889)
            (infra/otel-collector/)
```

The local demo lives at `infra/otel-collector/` — runnable via
`docker compose up`, see its README. The mdk side of the pipe is identical
between local and prod; only the Collector's exporter pipelines differ.

### Sink gate

The `_otlp_metrics_enabled()` check in `src/movate/tracing/metrics.py` mirrors
the tracer's:

| `MOVATE_TRACE_SINK` | Metrics on? |
| --- | --- |
| `otlp` | yes |
| `both` | yes |
| `langfuse` | no (Langfuse is tracer-only; no metric mirror) |
| `none` | no (operator-explicit off) |
| (unset) | yes iff `OTEL_EXPORTER_OTLP_ENDPOINT` is set (legacy auto-detect) |

A failure to build the MeterProvider degrades to no-op + one stderr line
(never crashes the runtime — fail-soft, `src/movate/tracing/metrics.py:217`).

## Correlations (logs ↔ traces ↔ jobs)

- **Span → log**: `src/movate/tracing/log_correlation.py` installs a logging
  filter that stamps `record.trace_id` / `record.span_id` (32-hex / 16-hex)
  on every `LogRecord` via the OTel context. Format the deployed log line
  with `%(trace_id)s` and the trace id joins straight back to the trace.
- **Trace context across the queue**: ADR 019 — the worker pulls the W3C
  trace-context off the enqueued job and reinjects it before dispatch, so a
  `mdk serve` HTTP request and the `mdk worker` dispatch of the resulting
  job sit in **one** trace (`src/movate/tracing/propagation.py`).
- **Eval ↔ run**: Langfuse scores from `mdk eval` link to the same Langfuse
  trace tree the runtime emits (ADR 031 D1), keyed off `run_id`.

The Jaeger search-by-trace-id flow in the local demo is the same shape as the
prod Application Insights → trace flow in Azure (`AppDependencies` /
`AppTraces` keyed by `OperationId`).

## Prescriptive layer + persona Workbooks

The four Grafana dashboards now carry a **prescriptive triage layer**: each one
opens with a triage-flow text panel (Mermaid graph + numbered-list fallback),
every chart has a sibling "Sub-panel: triage notes" text panel using the
**What / Normal / If red, do** pattern, the chart thresholds match the
item-27 SLO alert thresholds from `infra/azure/modules/monitor-alerts.bicep`,
and every chart has drill-down links to the local Jaeger demo (`http://localhost:16686/...`)
and to the corresponding Azure Monitor Workbook page. Anti-drift coverage is
unchanged: `tests/test_grafana_dashboards.py` walks `targets[*].expr` only,
so the new `text` panels are transparent to the gate.

The same metrics are also exposed Azure-side as four **persona-scoped
Workbooks** under `infra/azure-monitor/workbooks/`: `operator`, `platform`,
`eval-and-drift` (scaffolded - eval scores live in Langfuse today per ADR 031
D1; the workbook documents the shape for when those instruments land), and
`tenant-ops` (per-tenant slice with a `Tenant` workbook parameter). See
[`docs/azure-monitor-workbooks.md`](azure-monitor-workbooks.md) for portal
import + persona-to-on-call-flow mapping.

### Choosing between Grafana and Workbooks

Both surface the same OTel catalog. Pick **Grafana** when you want
open-source / multi-cloud, the local demo stack
(`infra/otel-collector/docker-compose.yml`), or live trace-search drill-downs
into Jaeger (Workbooks can't replace those without leaving the Portal). Pick
**Workbooks** when you want native Azure (shares auth with the Portal), KQL
(the right language when you need to pivot to Activity Log / Resource Graph),
and the same identity that owns the alert rules in
`infra/azure/modules/monitor-alerts.bicep`. The two surfaces are anti-drift
tested against the same `METRIC_NAMES`, so the choice is ergonomic, not
semantic.

## How to import the in-repo dashboards

### Local (Grafana + Prometheus, via the demo stack)

`docker compose -f infra/otel-collector/docker-compose.yml up`. The four
in-repo Grafana JSON files auto-provision under the **mdk** folder; the
Prometheus rules under `dashboards/prometheus/mdk-rules.yaml` auto-load. No
manual import. See `infra/otel-collector/README.md`.

### Production (Grafana Cloud / managed Grafana over Azure Monitor)

If your prod path is OTel Collector → Azure Monitor (ADR 020), point your
managed Grafana / Grafana Cloud at **Azure Monitor as the data source**
(`grafana-azure-monitor-datasource`), then:

1. Import the JSON under `dashboards/grafana/*.json` via *Dashboards → New →
   Import*.
2. Replace the panel queries with their KQL equivalents from the
   `dashboards/azure/mdk-golden-signals.workbook.json` workbook (the
   workbook ships the canonical KQL — counters land in `AppMetrics.Sum` with
   the dot-name in `AppMetrics.Name`, attributes in `Properties[...]`; see
   `dashboards/README.md` for the per-backend transform table).

The metric *names* are identical across both paths — only the query language
differs (PromQL on Prometheus, KQL on Azure Monitor). The drift guard
`tests/test_dashboards_metric_names.py` keeps both surfaces honest against
`METRIC_NAMES`.

### Standalone Grafana (your own Prometheus)

If you already run Prometheus scraping the Collector elsewhere:

1. *Dashboards → New → Import* each `dashboards/grafana/*.json` and pick
   your Prometheus data source for the `DS_PROMETHEUS` input.
2. Reference `dashboards/prometheus/mdk-rules.yaml` from your Prometheus
   config (`rule_files:`) and reload.

See `dashboards/README.md` for the full import flow including the Azure
workbook.

---

# Observability — telemetry surface, sinks, and dual export

MDK emits OpenTelemetry metrics + spans. The default posture is **single
in-tenant sink** (the customer's Azure Monitor / Application Insights, via the
ADR 015 OTLP exporter); ADR 039 Phase 2 adds an **opt-in second exporter** that
sends a minimized copy of metrics + spans to a Movate-operated central
Collector.

This doc is the operator-facing catalog of what's emitted, how to turn each
sink on, and the privacy contract of the Phase 2 dual stream.

## Sinks at a glance

| Sink | Selector | What gets exported | Where it lands |
| --- | --- | --- | --- |
| In-tenant OTLP (primary) | `MOVATE_TRACE_SINK=otlp` + `OTEL_EXPORTER_OTLP_ENDPOINT=...` | All metrics + all span attributes | Customer's Azure Monitor / App Insights / Tempo / SigNoz |
| Langfuse | `MOVATE_TRACE_SINK=langfuse` + `LANGFUSE_*` | LLM-shaped spans | Langfuse Cloud or self-hosted |
| Both | `MOVATE_TRACE_SINK=both` | Both of the above, fanning out | Both |
| **Phase 2 dual export** (ADR 039) | `MDK_TELEMETRY_ENDPOINT=...` + `MDK_TELEMETRY_CUSTOMER_ID=...` | **Minimized** metrics + span metadata only (see below) | Movate-operated central Collector — in **addition** to the primary sink |

The Phase 2 dual export is **additive** to whichever primary you choose — it
does not replace it. Setting `MDK_TELEMETRY_ENDPOINT` without setting
`MOVATE_TRACE_SINK`/`OTEL_EXPORTER_OTLP_ENDPOINT` is a misconfiguration: the
runtime will keep working but no telemetry pipeline is active at all (Phase 2
runs through the same `TracerProvider` / `MeterProvider` the primary sink
builds; no primary, no provider, nothing to dual-export).

## ADR 039 Phase 2 — opt-in dual export

Phase 2 is **off by default** and reversed unilaterally by unsetting one env.
The contract:

* The customer keeps full per-tenant visibility — the primary export is
  unchanged.
* Movate gets a **minimized** copy: metrics and span *metadata* only. No
  prompt content, no completion content, no chunk text, no tool I/O
  payloads, no user identifiers (see the allow-list below).
* Failure of the dual stream — unreachable endpoint, SDK init failure,
  half-configured env — **never** breaks the primary stream. The runtime
  logs one stderr warning at startup and continues; the primary stream is
  unaffected.

### Env vars

| Var | Purpose | Default | Notes |
| --- | --- | --- | --- |
| `MDK_TELEMETRY_ENDPOINT` | OTLP endpoint URL for Movate's central Collector | *unset* | Unset = Phase 2 disabled. Same shape as `OTEL_EXPORTER_OTLP_ENDPOINT`. |
| `MDK_TELEMETRY_CUSTOMER_ID` | Opaque per-customer identifier — a hash, not a name | *unset* | **Required** when `MDK_TELEMETRY_ENDPOINT` is set. Becomes the `customer` Resource attribute on the dual stream. If absent, the dual exporter is disabled and one warning is logged. |
| `MDK_TELEMETRY_INSECURE` | Allow plaintext OTLP (dev / self-signed clusters) | `false` | Truthy values: `1` / `true` / `yes` / `on`. Production = leave unset (TLS-required). |

### What crosses the boundary (the allow-list)

ADR 039 D3 is the canonical allow-list; the Phase 2 wiring enforces it in two
places:

1. **Metrics** — only the instruments in `METRIC_NAMES` (the same set that
   feeds the per-tenant dashboards) are emitted by MDK at all, so the dual
   stream sees exactly the same low-cardinality counters / histograms /
   gauges:
   * `mdk.jobs.completed`, `mdk.job.duration_ms`, `mdk.jobs.in_flight`
   * `mdk.run.tokens`, `mdk.run.cost_usd`
   * `mdk.db.pool.size` / `.idle` / `.in_use` / `.waiting` / `.max`
   No metric label carries prompt content / completions / chunk text.
2. **Spans** — the dual stream applies a defense-in-depth allow-list on top
   of MDK's already narrow span surface, dropping any non-allow-listed
   attribute *before* the span leaves the process. The primary stream is
   **unaffected** — customer dashboards still see the full attribute set
   inside their tenant. Allow-listed attributes today:
   * `workflow`, `workflow_version` (`workflow.execute`)
   * `agent`, `agent_version`, `provider`, `model_override`, `status`
     (`agent.execute`)
   * `turn`, `model` (`agent.turn[N]`)
   * `skill`, `auto_into` (`retrieval.<skill>` / `skill.<name>`)
   * `stage_count`, `total_ms`, `duration_ms`, `input_count`,
     `output_count`, `chunk_count` (`kb_search` + stages)
   * `exception.type` (NOT the message — exception messages can embed
     user-supplied values)

### What never crosses the boundary

The contract is explicit and absolute:

* No prompt content, no completion content, no retrieved chunk text, no
  tool I/O payloads.
* No user identifiers, no IP addresses, no HTTP request URLs with path
  segments.
* No log bodies. The fleet stream is metrics + span metadata only;
  `AppTraces` log bodies stay per-customer.
* No `chunk_ids_preview`, no `description`, no `input_text` (and similar
  free-form attributes that have been a leak vector elsewhere in the
  industry). The Phase 2 processor is **allow-list** (keep-only), not
  deny-list — a future attribute added in `src/movate/core/` defaults to
  *dropped* from the dual stream, not *leaked*.

### Failure modes

| Scenario | Effect on primary | Effect on dual |
| --- | --- | --- |
| `MDK_TELEMETRY_ENDPOINT` unset | none | disabled |
| `MDK_TELEMETRY_ENDPOINT` set, `MDK_TELEMETRY_CUSTOMER_ID` unset | none | disabled + one stderr warning |
| Endpoint set + customer-id set + OTel SDK missing | depends on primary | disabled silently — the primary needs OTel too, so the warning has already fired |
| Endpoint set + customer-id set + endpoint unreachable | none | one stderr warning at startup ("Phase-2 telemetry endpoint unreachable; primary export unaffected"); subsequent collection cycles silently retry |
| Endpoint set, dual exporter raises during export | none | OTel's `BatchSpanProcessor` / `PeriodicExportingMetricReader` swallow + drop the failed batch |

### Reversing opt-in

Unset `MDK_TELEMETRY_ENDPOINT` and redeploy. The primary stream continues.
There is no Movate-side cooperation required to opt out — this is intentional
and is the trust posture ADR 039 D5 promises.

### Verifying with `mdk doctor`

`mdk doctor` includes a `phase 2 telemetry` row that reports one of:

* `ok (off)` — `MDK_TELEMETRY_ENDPOINT` unset; default + recommended for
  customers who haven't opted in.
* `ok <endpoint> customer=<8-char-prefix>…` — Phase 2 is fully configured
  and the runtime will dual-export. The full customer-id hash is never
  printed in doctor output even though it's already hashed.
* `missing — endpoint set but MDK_TELEMETRY_CUSTOMER_ID is unset` — the
  half-configured state; fix by setting the customer-id env or unsetting
  the endpoint.

Use `mdk doctor --explain` to see the full env-var documentation alongside
the row.

## Related ADRs

* ADR 015 — OTLP sink + Langfuse v3 (the primary in-tenant sink).
* ADR 020 — OTel Collector → Azure Monitor in Azure deployments.
* ADR 031 — reporting + dashboards.
* ADR 034 — data-plane scalability + pool metrics.
* ADR 039 — Movate product telemetry (the Phase 1 Lighthouse path + Phase 2
  dual export described here).
