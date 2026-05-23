# ADR 015 — Self-hosted observability on Azure (off Langfuse Cloud)

**Status:** Proposed
**Date:** 2026-05-23
**Deciders:** Engineering (observability + infra — Deva sign-off for the infra footprint / data-residency posture)
**Context window:** v1.0 Azure operability — keep trace data in-tenant
**Related / constrained by:** ADR 001 (cloud-portability — OTel is the blessed, vendor-neutral path),
`src/movate/tracing/` (`base.Tracer` Protocol, `langfuse.py`, `otel.py`, `composite.py`, `null.py`, `stdout.py`, `__init__.build_tracer`),
`pyproject.toml` `langfuse` + `otel` extras, ADR 012/013 (auth — traces carry sensitive payloads), the PII guardrail (`movate.guardrails`)

---

## Decision

Stop sending observability data to **Langfuse Cloud** and run it **self-hosted
inside the Azure tenant**, **without coupling the code to any one backend** —
the existing `Tracer` Protocol seam already makes the sink a deployment choice.
Specifically:

1. **(D1) Reaffirm the seam: this is a deployment/config change, not a code
   change.** Tracing is wired at the edges behind the `Tracer` Protocol
   (OTel-shaped spans), with `langfuse` / `otel` / `composite` / `null` /
   `stdout` adapters selected by `build_tracer()`. Moving off Langfuse Cloud is
   *where traces go*, not *how the runtime emits them*.
2. **(D2) Default self-hosted sink: Langfuse on Azure** — for the LLM-native UI
   (prompt / completion / cost / eval-score / session views the team relies on),
   deployed in-tenant on Container Apps, backed by the **existing Azure
   Postgres** (+ the components Langfuse v3 needs). Traces never leave the
   tenant.
3. **(D3) Keep OTLP first-class as the portable co-equal / escape hatch** — the
   `OtelTracer` already exists; emit OTLP to **Azure Monitor / Application
   Insights** (OTLP-native, lightest infra) or any OSS OTLP backend
   (Tempo / SigNoz). Honors ADR 001 so we are **not re-locked to
   Langfuse-the-company**; `CompositeTracer` can fan out to *both*.

In one sentence: **"trace data stays in our Azure tenant — self-hosted Langfuse
for the rich LLM UI and/or OTLP to Azure Monitor for ops — chosen per
deployment behind the existing `Tracer` seam, with Langfuse Cloud
decommissioned."**

---

## Context

Today the runtime can emit traces to **Langfuse Cloud** via `LangfuseTracer`
(the adapter targets the **Langfuse v2 SDK**), or to an OTLP backend via
`OtelTracer`, or both via `CompositeTracer` — `build_tracer()` picks the sink
from the environment. Traces capture **prompts, completions, tool I/O, costs,
and eval scores** — i.e. potentially **sensitive / PII-bearing** data.

Three forces push us off Langfuse Cloud:

* **Data residency / sovereignty.** For customer-VPC and regulated deployments,
  prompt+completion payloads **cannot egress** to a third-party SaaS. They must
  stay in the customer's (or Movate's) Azure tenant.
* **Control + retention + cost.** Self-hosting gives full control of retention,
  access (AAD/scopes), and avoids per-event SaaS pricing at scale.
* **Portability (ADR 001).** We should not trade one lock-in (Langfuse Cloud)
  for another; OTLP is the vendor-neutral standard and is already wired.

The good news: the adapter seam means this is **primarily infra + config**. The
real engineering cost is (a) standing up Langfuse's self-host stack on Azure and
(b) a **Langfuse v2 → v3 SDK** bump (current self-hosted Langfuse is v3).

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Data residency** — prompts/completions/PII must not egress to SaaS in regulated/VPC deploys | HIGH |
| **Portability (ADR 001)** — OTLP-first; no re-lock to a single vendor; sink is a deployment choice | HIGH |
| **Keep the team's LLM-native UX** — prompt/cost/eval/session views are a real agent-dev productivity asset | MED |
| **Operational burden** — Langfuse v3 self-host pulls in ClickHouse + Redis + workers; weigh vs. OTLP→Azure Monitor | MED |
| **Minimal code change** — reuse the `Tracer` seam; no execution-logic edits | MED |
| **Security** — secrets in Key Vault, private networking, AAD-gated access to the trace UI | HIGH |

---

## Architecture

```
  runtime/executor (edge only)                       ADR-001 portable seam
        │ Tracer Protocol (OTel-shaped spans)
        ▼
  build_tracer()  ── env-selected sink(s) ──┐
        ├─ LangfuseTracer ──▶ self-hosted Langfuse (Azure, in-tenant)   ◀── D2
        │                       web + worker (Container Apps)
        │                       ├─ Azure Postgres (existing)  ── txn
        │                       ├─ ClickHouse (container/managed) ── trace analytics
        │                       ├─ Azure Cache for Redis ── queue/cache
        │                       └─ Azure Blob ── event uploads
        ├─ OtelTracer (OTLP) ─▶ Azure Monitor / App Insights (OTLP-native) ◀── D3
        │                       └ or OSS: Grafana Tempo / SigNoz (portable)
        └─ CompositeTracer ─▶ BOTH (LLM UI + ops APM)                    ◀── dual-sink
   (null / stdout for dev + off-by-default)
```

Nothing above the `build_tracer()` line changes — the executor/runtime keep
emitting Protocol calls; only the deployed sink + its infra change.

---

## Decisions

### Decision 1 (D1): It's a sink/deployment decision — preserve the seam

No execution-plane code changes. `build_tracer()` selects the sink from env
(`MOVATE_TRACE_SINK=langfuse|otlp|both|none`, plus per-sink host/keys/endpoint),
defaulting to `null`/`stdout` (off) — unchanged behavior when unconfigured. The
`Tracer` Protocol stays OTel-shaped (it already is), which is what keeps the
backend swappable.

### Decision 2 (D2): Self-host Langfuse on Azure (default rich sink)

Deploy Langfuse **in-tenant** so trace payloads never egress:
- **Compute:** Langfuse `web` + `worker` containers on **Azure Container Apps**
  (mirrors how we run the runtime/worker; an AKS chart is the portable
  equivalent), behind a Bicep module gated by an **`enableLangfuse`** flag
  (mirroring `enableTeamsBot`).
- **Data:** reuse the **existing Azure Postgres Flexible** (transactional) +
  **ClickHouse** (trace analytics — a container with a persistent volume, or a
  managed ClickHouse) + **Azure Cache for Redis** (queue/cache) + **Azure Blob**
  (event uploads; Langfuse supports Azure Blob natively).
- **Secrets** (Langfuse `SALT`, encryption key, S3/blob creds, public/secret
  API keys) live in **Key Vault**, injected via managed identity (no static
  secrets in env). **Private networking** (internal ingress / VNet); the trace
  **UI is AAD-gated** (ties to ADR 013).
- **SDK bump:** the current `LangfuseTracer` targets the **v2 SDK**; self-hosted
  Langfuse is **v3** → bump the `langfuse` extra to `>=3` and update the adapter.
  This is the main code change and is isolated to `tracing/langfuse.py`.

Rejected: keeping Langfuse **v2** self-hosted (Postgres-only, simpler) — v2 is
EOL; not worth building on.

### Decision 3 (D3): OTLP stays first-class (portability + the lighter path)

Keep `OtelTracer` as a **co-equal** sink, per ADR 001:
- **Azure Monitor / Application Insights** ingests **OTLP natively** → the
  lightest-infra self-hosted-on-Azure option (Azure runs the backend; **no
  ClickHouse/Redis to operate**). Trade-off: it's generic APM, not LLM-native —
  you lose Langfuse's prompt/cost/eval UI and build dashboards instead.
- **OSS OTLP backends** (Grafana Tempo / SigNoz) for non-Azure / fully-portable
  deploys.
This is the **escape hatch** that prevents re-locking to Langfuse, and the
**recommended choice for regulated/minimal deploys** that don't need the LLM UI.

### Decision 4 (D4): `CompositeTracer` enables dual-sink (LLM UI + ops APM)

A deployment can run **both** — Langfuse (self-hosted) for agent-dev/eval
introspection *and* OTLP→Azure Monitor for ops/SRE dashboards + alerting —
via the existing `CompositeTracer`. The two audiences (agent authors vs.
operators) are served without choosing one tool.

### Decision 5 (D5): Data-residency + security posture

- Trace payloads (prompts/completions/tool I/O) **never egress the tenant** in
  the self-hosted config. Langfuse Cloud is **decommissioned** (no `*.langfuse.com`
  host in deployed config).
- **Redaction hook:** offer optional payload redaction at the tracer edge
  (reuse the PII guardrail, `movate.guardrails`) so even in-tenant traces can be
  scrubbed for the most sensitive fields — config-gated.
- Secrets in **Key Vault** + managed identity; trace UI behind **AAD/private
  ingress**; retention configurable.

### Decision 6 (D6): Migration is a config flip (+ the SDK bump)

Cutover: stand up self-hosted Langfuse (D2) → point `MOVATE_LANGFUSE_HOST` +
keys at it (env/KV) → verify traces land → **decommission the Cloud project**.
Optionally export historical Cloud data first (Langfuse export API). The runtime
needs **no execution-logic change** — only the `build_tracer` config + the v2→v3
adapter bump. Local dev stays on `null`/`stdout` (off by default).

### Decision 7 (D7): Operational-cost trade-off is explicit

Self-hosted Langfuse v3 = **ClickHouse + Redis + worker** to operate (real ops
weight + cost). If that's too heavy for a given deployment, **OTLP→Azure Monitor
(D3)** is the sanctioned lighter path (lose the LLM-native UI). The default for
**Movate's own** Azure env is self-hosted Langfuse (keep the UI); customer/
regulated deploys choose per their constraints. Documented in the deploy runbook.

---

## Consequences

**Positive**
- Trace data (prompts/PII) **stays in-tenant** — unblocks regulated / customer-VPC deployments and removes third-party egress.
- Full control of retention, access (AAD/scopes), and cost; no per-event SaaS pricing.
- Portability preserved (OTLP first-class) — not re-locked to Langfuse; sink is a per-deployment choice; dual-sink possible.
- Near-zero execution-plane change (the seam already exists); the work is infra + an isolated adapter SDK bump.

**Negative / costs**
- Self-hosted Langfuse v3 brings **ClickHouse + Redis + workers** to run + monitor + back up — meaningful ops weight + cost.
- A **Langfuse v2 → v3 SDK** migration in `tracing/langfuse.py` (isolated, but real).
- Two viable sinks (Langfuse vs. OTLP→Azure Monitor) = a deployment decision to document + support.

**Neutral**
- New infra (Bicep `enableLangfuse` module + ClickHouse/Redis/Blob) and config (`MOVATE_TRACE_SINK`, per-sink host/keys) — all additive, default-off.

---

## Implementation plan (separate PRs, after this ADR is accepted)

1. **OTLP→Azure Monitor path first (lowest lift, immediate residency win).**
   Wire `OtelTracer` to App Insights via OTLP; `MOVATE_TRACE_SINK=otlp`;
   document. Gets trace data in-tenant *now* without standing up Langfuse.
2. **Langfuse v3 SDK bump** in `tracing/langfuse.py` (+ bump the `langfuse`
   extra to `>=3`); keep the `Tracer` Protocol surface unchanged; tests.
3. **Self-hosted Langfuse Bicep module** (`enableLangfuse`): web+worker on
   Container Apps, ClickHouse + Redis + Blob, KV secrets, private ingress,
   AAD-gated UI; reuse existing Azure Postgres. Deploy runbook.
4. **`build_tracer` sink-selection polish** (`MOVATE_TRACE_SINK=langfuse|otlp|both|none`)
   + the optional **redaction hook** at the tracer edge.
5. **Cutover + decommission Cloud** — config flip, verify, optional historical
   export, remove `*.langfuse.com` from deployed config.

## Risks / open questions

- **ClickHouse on Azure** — self-managed container (persistent volume, backups)
  vs. a managed offering; sizing for trace volume. The single biggest ops
  decision; if unattractive, lean on D3 (Azure Monitor) instead.
- **v2→v3 SDK** behavior differences (Generation/Span API shape) — keep the
  adapter's Protocol surface stable; cover with tests.
- **Cost** — self-host infra (ClickHouse/Redis/compute) vs. Langfuse Cloud
  subscription; model it before committing to D2 over D3.
- **Historical data** — whether to migrate existing Cloud traces or start fresh.

---

## Addendum 015.1 — OTLP sink shipped (implementation-plan step 1)

*Status: implemented (branch `feat/otlp-sink`). Code-only; live Azure Monitor
validation is gated on a subscription and is out of scope for this step.*

This delivers step 1 of the plan: the runtime can emit traces over **generic
OTLP** to a self-hosted-in-tenant backend, selected per deployment, **default-off
and backward compatible**. No execution-plane code changed — only
`build_tracer()` and the OTLP provider builder in `tracing/`.

### The sink selector: `MOVATE_TRACE_SINK`

`build_tracer()` now reads `MOVATE_TRACE_SINK` first. When it is set it wins and
the sink is treated as an **explicit deployment choice** — a missing optional
dependency raises a loud, actionable `TraceSinkError` (with an install hint)
rather than silently falling back, so a misconfigured deploy is obvious.

| `MOVATE_TRACE_SINK` | Result |
|---|---|
| *(unset)* | **Unchanged** — legacy `MOVATE_TRACER` + auto-detect path, byte-for-byte. |
| `none` | `SilentTracer` (off; trace data goes nowhere). |
| `langfuse` | `LangfuseTracer` (self-hosted or Cloud — D2). |
| `otlp` | `OtelTracer` over the generic OTLP exporter (D3). |
| `both` | `CompositeTracer([langfuse, otlp])` — LLM UI + ops APM at once (D4). |

Default-off / backward-compat: with `MOVATE_TRACE_SINK` unset, behavior is
identical to before this change — the existing `MOVATE_TRACER` override and the
`LANGFUSE_SECRET_KEY` / `OTEL_EXPORTER_OTLP_ENDPOINT` auto-detect rules are
preserved exactly (and still fail *soft* to silent/stdout, since they are not an
explicit deployment choice).

### OTLP exporter config (portable — ADR 001, no Azure-specific SDK)

The `otlp` sink uses the **generic** `opentelemetry-exporter-otlp` (already in
the `otel` extra — no new dependency). It is configured entirely via the
standard OpenTelemetry env vars, which the SDK reads natively:

- `OTEL_EXPORTER_OTLP_ENDPOINT` — the OTLP receiver URL (required).
- `OTEL_EXPORTER_OTLP_HEADERS` — auth/metadata headers.
- `OTEL_EXPORTER_OTLP_PROTOCOL` — `http/protobuf` (default) or `grpc`.

Resource attributes set on the tracer provider: `service.name=movate-runtime`
(override via `OTEL_SERVICE_NAME`), `service.version` (from `movate.__version__`),
and `deployment.environment` (from `MOVATE_ENV` / `OTEL_DEPLOYMENT_ENVIRONMENT`,
when set).

#### Azure Monitor / Application Insights (in-tenant, recommended on Azure)

App Insights ingests OTLP natively — point the generic exporter at its OTLP
ingestion endpoint and pass the ingestion key as a header. **No
`azure-monitor-opentelemetry` or any azure-specific package is added** — Azure
Monitor is just an OTLP endpoint (the portable path).

```bash
export MOVATE_TRACE_SINK=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT="https://<region>.in.applicationinsights.azure.com/v2.1/track"
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<app-insights-ingestion-key>"
# optional:
export OTEL_SERVICE_NAME=mdk-prod
export MOVATE_ENV=production
```

> Exact App Insights OTLP endpoint/header shape depends on the tenant's
> ingestion config; the operator points the standard OTel vars at it. Secrets
> (the ingestion key/header) come from Key Vault via managed identity per D5 —
> never static in env in deployed config.

#### OSS alternative (fully portable — Grafana Tempo / SigNoz / collector)

Same selector, different endpoint — e.g. a local/in-cluster OTLP collector:

```bash
export MOVATE_TRACE_SINK=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318"
```

### Caveat (🔒)

The code and tests are entirely local: tests assert span emission via OTel's
`InMemorySpanExporter` (no network). **Live validation against a real Azure
Monitor / App Insights endpoint is gated on an Azure subscription and is
external to this change.** Steps 2–5 of the plan (Langfuse v3 bump, the
`enableLangfuse` Bicep module, redaction hook, Cloud decommission) remain
follow-ups.
