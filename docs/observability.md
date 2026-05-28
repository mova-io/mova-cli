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
