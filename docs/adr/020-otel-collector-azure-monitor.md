# ADR 020 — OTel Collector → Azure Monitor (in-cluster App Insights export)

**Status:** Proposed
**Date:** 2026-05-24
**Deciders:** Engineering (observability + infra)
**Context window:** v1.0 Azure operability — get the runtime's OTLP traces into Application Insights on live ACA
**Related / constrained by:** ADR 001 (cloud-portability — the runtime emits generic OTLP only, no cloud SDK), ADR 015 (self-hosted observability — the OTLP sink this export rides on), ADR 019 (trace-context propagation — the distributed trace that lands in App Insights),
`infra/azure/main.bicep`, `infra/azure/modules/containerapp-env.bicep`, `infra/azure/modules/containerapp-otel-collector.bicep` (new), `infra/azure/modules/containerapp-{api,worker}.bicep`, `infra/azure/modules/appinsights.bicep`

---

## Decision

Export the runtime's OpenTelemetry traces to **Application Insights** through an
**in-cluster OpenTelemetry Collector**, not through the Container Apps
Environment's *managed* OpenTelemetry. Specifically:

1. **(D1) Keep the app generic-OTLP (ADR 001 preserved).** The api/worker emit
   standard OTLP/HTTP — no Azure Monitor SDK in the runtime, no cloud lock-in.
   The data flow is `api/worker (OTLP) → otel-collector (azuremonitor exporter)
   → App Insights`.
2. **(D2) Run the collector as a Container App** (new
   `modules/containerapp-otel-collector.bicep`): the **contrib** distro image
   (`otel/opentelemetry-collector-contrib:0.115.1` — the only distro shipping
   the `azuremonitor` exporter), **internal** ingress on `:4318` (OTLP/HTTP),
   reachable only from the api/worker inside the environment. Its `azuremonitor`
   exporter forwards traces/metrics/logs to App Insights.
3. **(D3) Remove the broken managed-OTel App Insights destination from the CAE.**
   `modules/containerapp-env.bicep` goes back to its baseline `properties`
   (`appLogsConfiguration` + `workloadProfiles` + `zoneRedundant`) at the stable
   `2024-03-01` API version, with **no** `openTelemetryConfiguration`.
4. **(D4) Wire the apps to the collector, gated default-off.** The api/worker get
   `MDK_TRACE_SINK=otlp` **and** `OTEL_EXPORTER_OTLP_ENDPOINT=https://<collector
   internal fqdn>`, both gated on the *same* condition
   (`enableAppInsights && !empty(appInsightsConnectionString)`) so the fail-loud
   `OtelTracer` never gets `otlp` without an endpoint. Off by default → byte-for-byte
   the pre-#62 baseline.
5. **(D5) Connection string is a PLAIN (not `@secure()`) Bicep param.** It carries
   a write-only ingestion key (low sensitivity), is never an output, and — the
   load-bearing reason — `@secure()` params are **omitted during ARM preflight**,
   which is exactly when ACA validated the value (a contributor to the failure
   below). The collector reads it at runtime via
   `${env:APPLICATIONINSIGHTS_CONNECTION_STRING}`.

In one sentence: **the runtime stays generic-OTLP and ships to an in-cluster
OTel Collector whose `azuremonitor` exporter does the App Insights translation —
because ACA's managed-OTel cannot export to App Insights on the live RP.**

---

## Context

Item 4 / #62 configured the CAE's **managed** OpenTelemetry with an
`appInsightsConfiguration` destination to send traces to App Insights. **This
does not work on live ACA.** Six real `az deployment group create` attempts
failed with a `Microsoft.App/managedEnvironments` preflight error:

> `AppInsightsConfiguration.ConnectionString can not be empty`

— even when passed a valid 240-char connection string via a JSON params file
(ruling out CLI parsing, `@secure()` omission, and empty values). The root cause:
ACA managed-OTel only supports `dataDogConfiguration` and `otlpConfigurations`
destinations. That is *why* `appInsightsConfiguration` triggered `BCP037` (it is
not in the RP's Bicep type defs at all); the RP then rejects it at runtime with
the misleading "ConnectionString can not be empty".

The trap: **`az bicep build` AND `az deployment group validate` both PASS the
broken config** — only a real `create` exposes it. So the standard local gates
gave false confidence.

The fix is the canonical OTel pattern for a destination the platform doesn't
natively support: run a **Collector** that receives generic OTLP and uses a
destination-specific exporter (here `azuremonitor`) to translate to the backend.
This keeps the runtime vendor-neutral (ADR 001) while still landing data in App
Insights.

| Force | Weight |
|-------|--------|
| **Works on live ACA** — the managed-OTel path provably does not | HIGH |
| **Vendor-neutral runtime (ADR 001)** — app emits generic OTLP; only the collector knows Azure | HIGH |
| **Default-off / back-compat** — `enableAppInsights=false` is byte-for-byte the pre-#62 baseline | HIGH |
| **Boundary discipline** — translation lives at the infra edge (a collector), never in execution code | HIGH |
| **Fail-loud safety** — the otlp sink only ever gets an endpoint when one exists | HIGH |
| Operational footprint — one extra (cheap, 1-replica, internal) Container App | LOW |

---

## Decisions in detail

### D2 — The collector module
- **Image:** `otel/opentelemetry-collector-contrib:0.115.1` (a real, stable tag,
  published 2024-12-04). The contrib distro is required — the `azuremonitor`
  exporter is not in the core distro. Public Docker Hub image → no ACR pull, so
  (like `langfuse.bicep`) the module has **no `registries` block** and **no
  identity** (nothing to read from Key Vault either — see D5).
- **Ingress:** `external: false`, `targetPort: 4318`, `transport: 'http'`. Only
  the api/worker reach it, over the environment's internal network.
- **Config via env, not a file mount.** The container runs
  `--config=env:OTELCOL_CONFIG` and gets the pipeline YAML in the `OTELCOL_CONFIG`
  env var; the collector's `env:` config provider expands the
  `${env:APPLICATIONINSIGHTS_CONNECTION_STRING}` reference inside it at runtime
  against the second env var. Pipelines: `traces`, `metrics`, `logs`, each
  `otlp → azuremonitor`.

  **Bicep escaping gotcha (the #1 implementation risk, resolved):** a literal
  `${...}` in a *single-quoted* Bicep string is interpolation (would compile to
  empty), so one would reach for the `${'$'}{env:...}` escape. But the YAML lives
  in a **multi-line (`'''...'''`) Bicep string**, which is **verbatim/raw — no
  interpolation, no escaping**. There, the plain `${env:...}` form is already
  literal, and the `${'$'}` escape would *wrongly* leak the characters `${'$'}`
  into the deployed value. This was verified empirically and the compiled ARM
  carries the correct literal `${env:APPLICATIONINSIGHTS_CONNECTION_STRING}`.

### D3 — CAE revert
`modules/containerapp-env.bicep` drops the `appInsightsConnectionString` param,
the `otelFragment`/`openTelemetryConfiguration` block, and the
`#disable-next-line BCP037`. The API version reverts `2024-10-02-preview` →
`2024-03-01` (the preview was only needed for the now-removed
`openTelemetryConfiguration` surface). Compiled ARM confirms the env carries
exactly `appLogsConfiguration` + `workloadProfiles` + `zoneRedundant` and no
`openTelemetryConfiguration` anywhere; `BCP037` is gone.

### D4 — App wiring + default-off invariant
The api/worker modules gain an `otelExporterEndpoint string = ''` param; when
non-empty they emit `OTEL_EXPORTER_OTLP_ENDPOINT` (in addition to
`MDK_TRACE_SINK=otlp`). `main.bicep` sets both from a single
`appInsightsExportEnabled = enableAppInsights && !empty(appInsightsConnectionString)`
gate, so a pod can never get `MDK_TRACE_SINK=otlp` without an endpoint. With the
gate false (default): no collector, no `OTEL_EXPORTER_OTLP_ENDPOINT`, no
`MDK_TRACE_SINK`, no `openTelemetryConfiguration`, CAE at `2024-03-01` —
byte-for-byte the pre-#62 baseline (verified in compiled ARM).

**Internal-ingress OTLP endpoint format.** ACA internal ingress serves on
`:443` and forwards to `targetPort 4318`; the OTLP/HTTP exporter appends the
signal path (`/v1/traces`, `/v1/metrics`, `/v1/logs`) itself. So the endpoint is
the bare base URL `https://<collector-fqdn>` — **no port, no path**.

### D5 — Two-pass deploy (connection-string ordering)
The connection string only exists *after* the App Insights component is created.
So enabling export is two passes:
1. **Pass 1:** `enableAppInsights=true`, `appInsightsConnectionString=''` →
   creates the App Insights component; the collector + app endpoint wiring stay
   gated off (empty connection string).
2. Read the connection string from the component
   (`az monitor app-insights component show ... --query connectionString`).
3. **Pass 2:** set `appInsightsConnectionString=<that value>` and re-deploy →
   the collector comes up and the api/worker start shipping OTLP to it.

(This mirrors the existing two-pass patterns — `enableApiWorker`,
`deployLangfuse` — and is documented in `docs/azure-bootstrap.md`.)

---

## Consequences

**Positive**
- App Insights export actually works on live ACA (the managed-OTel path did not).
- Runtime stays vendor-neutral generic-OTLP (ADR 001) — only the collector knows
  Azure; swapping backends is an exporter change at the edge, not an app change.
- Fully additive + default-off: `enableAppInsights=false` is byte-for-byte the
  pre-#62 baseline. Two-pass-safe; needs no Key Vault secrets.
- Boundary-clean: backend-specific translation lives in an infra component, never
  in execution code or the runtime image.

**Negative / risks**
- One extra Container App (cheap: 1 replica, 0.5 vCPU / 1 GiB, internal). A single
  collector replica is a soft single-point for *telemetry* (not for the app);
  acceptable at v1.0 single-team volume, scalable later via `maxReplicas`.
- The connection string is a plain (not `@secure()`) param. Mitigated: write-only
  ingestion key, never an output; plain is also *required* for preflight to see
  it. Documented prominently.
- The contrib image is a pinned external dependency; bump deliberately (verify the
  `azuremonitor` exporter and OTLP receiver remain stable across collector
  releases).

**Net-new:** `modules/containerapp-otel-collector.bicep`, the
`otelExporterEndpoint` param on the api/worker modules, the
`appInsightsConnectionString` param + `appInsightsExportEnabled` gate +
`otelCollector` module + `otelCollectorFqdn` output in `main.bicep`. **Removed:**
the CAE's managed-OTel `openTelemetryConfiguration` (+ its preview API version +
`appInsightsConnectionString` param + `BCP037` suppression). **No new runtime
dependency** — the collector is an infra container, not a Python dep.

## Alternatives considered
- **CAE managed-OTel `appInsightsConfiguration` (the status quo of #62)** —
  rejected: provably fails on live ACA (six failed `create`s); the destination is
  unsupported by the RP.
- **In-app Azure Monitor OpenTelemetry exporter** (azure-monitor-opentelemetry in
  the runtime) — rejected: puts a cloud-specific SDK in the runtime image,
  violating ADR 001 portability; the whole point of the collector is to keep that
  translation out of the app.
- **Point the app's OTLP exporter directly at App Insights** — rejected: App
  Insights has no native generic-OTLP ingestion endpoint; the collector's
  `azuremonitor` exporter is what does the protocol/schema translation.
- **Managed-OTel `otlpConfigurations` → the collector** (let the CAE inject the
  endpoint, pointing it at our collector) — rejected as unnecessary indirection:
  the CAE managed-OTel surface needs the preview API version, and we already set
  the endpoint env var explicitly. Going straight from the app to the collector
  keeps the CAE at the stable baseline.
