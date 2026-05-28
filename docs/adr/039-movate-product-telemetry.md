# ADR 039 — Movate product telemetry: a fleet view across per-tenant MDK deployments

**Status:** Proposed
**Date:** 2026-05-27
**Deciders:** Engineering + Deva (Movate)
**Builds on / related:** ADR 015 (Langfuse v3, OTLP → Azure Monitor), ADR 020
(OTel Collector → Azure Monitor in Azure deployments), ADR 031 (reporting +
dashboards), ADR 034 (data-plane scalability + pool metrics), ADR 036 (usage
metering + quotas), `src/movate/tracing/metrics.py` (`METRIC_NAMES` — the
canonical instrument list), `docs/observability.md` (the metric/span catalog
from PR #518).

## Context

Movate (the vendor) ships **MDK** (`movate-cli` / `mdk`) as a per-customer
*embedded* product: each customer deploys their own MDK instance into their own
Azure tenant, with their own Log Analytics workspace, their own Application
Insights resource, and (today) their own OTel Collector destination per ADR 020
(`infra/azure/modules/containerapp-otel-collector.bicep`). The control-plane
telemetry — every metric in `METRIC_NAMES` plus the spans catalogued in
`docs/observability.md` from PR #518 — lands in the customer's Azure Monitor and
**stays there**.

That is the right default for the customer (data residency, blast-radius,
zero-trust with the vendor). It is the wrong default for Movate-the-product-team,
which today has **no central view** of:

- **Adoption** — how many active deployments, how many active agents per
  deployment, which templates are picked up, which CalVer versions are running
  where, and how stale each fleet is.
- **Usage** — runs/day per customer, agent-shape mix (chat / workflow / batch),
  provider mix, p50/p95/p99 latency, top-N agents by volume.
- **Health** — error rate by route/job-kind, deploy success rate by CalVer,
  active alerts, revision stability.
- **Cost** — `$`/customer (`mdk.run.cost_usd`), tokens by provider
  (`mdk.run.tokens` × `agent.execute.provider` span attribute), `$`/run trend,
  cost outliers.
- **Quality** — eval pass-rate per customer × agent, drift detections, canary
  success rate (today these surface as **Langfuse scores**, ADR 031 D1, not as
  OTel metrics — see Open Questions).
- **Capacity** — `mdk.jobs.in_flight`, `mdk.db.pool.*`, worker saturation,
  hot-tenants view.

The structural pressure is real, on both sides. Movate cannot make confident
product decisions (which templates to invest in, which providers to certify,
where to put SDK polish) without a fleet view. Customers must not be surprised
by a vendor exfiltrating prompt content, PII, or any token-level data from their
tenant; the trust posture is the wedge MDK competes on.

We therefore need an **architectural decision**: how Movate observes the MDK
fleet *without* eroding the per-tenant data-residency boundary that makes
embedded MDK acceptable inside regulated customer estates in the first place.

## Decision

Movate operates a **central observability surface** in Movate's Azure tenant
that queries each customer's existing telemetry by *delegation*, with an
optional *additive* second OTLP stream introduced only when the
delegation-only path becomes a scale/latency bottleneck.

### D1 — Hosting: Azure Managed Grafana in Movate's tenant

- Resource: `Microsoft.Dashboard/grafana` (Azure Managed Grafana, "Standard"
  SKU; the cheaper "Essential" tier omits SLA + zone redundancy and is not a
  fit for an internal product surface). Bicep'd alongside the rest of
  Movate's internal infra — **out of scope for this ADR's PR** (docs +
  illustrative JSON only); the Bicep lands when the ADR is signed off.
- **AuthN:** Entra ID SSO restricted to `movate.com` directory; no anonymous
  access; viewer-default RBAC, editor for the platform team, admin for two
  named operators.
- **Data source:** the Managed Grafana instance's **system-assigned managed
  identity** authenticates to Azure Monitor via the
  `grafana-azure-monitor-datasource` plugin (the same data source PR #518's
  `dashboards/README.md` documents for the production import path). The
  managed identity is granted role assignments **in each customer's tenant**
  via Azure Lighthouse (D2).
- **Versioning:** the illustrative dashboards in this PR live under
  `dashboards/grafana/movate/` and are imported via Grafana's
  *Dashboards → New → Import* flow once Phase 1 is live. They are JSON,
  versioned in this repo, reviewed in PRs, drift-checked the same way the
  per-customer dashboards from PR #518 are.

### D2 — Data flow: a two-phase plan

#### Phase 1 — Lighthouse delegation (default, MVP)

Each onboarded customer accepts a Movate-published **Azure Lighthouse**
service-provider offer (a `Microsoft.ManagedServices/registrationDefinitions`
template + a per-customer `registrationAssignment`). The offer delegates two
built-in Azure roles, **scoped narrowly** to the customer's MDK Log Analytics
workspace + Application Insights resource group:

- **`Reader`** (built-in `acdd72a7-3385-48ef-bd42-f606fba81ae7`) on the
  workspace's resource group — enough to enumerate the workspace + the AI
  resource via Resource Manager.
- **`Monitoring Reader`** (built-in `43d0d8ad-25c7-4714-9337-8ba259a9fe05`) on
  the workspace itself — KQL read on `AppMetrics` / `AppTraces` /
  `AppDependencies` / `AppRequests`. **Reader of telemetry only.** Crucially
  this does **not** grant write to any customer resource, does **not** grant
  read of secrets / Key Vault / storage data, and does **not** grant access
  outside the delegated scope.

Movate's Managed Grafana queries each customer's workspace **in place** via
the Azure Monitor data source — the data never leaves the customer tenant.
The `customer` template variable on every illustrative dashboard binds, in
Phase 1, to the **Lighthouse subscription scope** the operator selects.

**Why this is the MVP:** zero customer-side code change, zero new MDK
instrumentation, zero new env var. The customer accepts an offer (one-click
in the Azure Portal *or* `New-AzManagedServicesAssignment` via the deploy
runbook) and Movate is in.

#### Phase 2 — Dual-export over OTLP (opt-in, additive, deferred)

Phase 1's failure modes are: (a) per-customer-workspace cross-tenant KQL is
slow at fleet width once we exceed ~50 customers (the AM data source fan-out
is sequential per subscription); (b) cross-workspace KQL joins (e.g.
"which CalVer is running everywhere?") get clumsy.

When (a) or (b) becomes a real constraint — *not* speculatively — Phase 2
adds, as an **additive** capability that defaults off:

- A new env var, `MDK_TELEMETRY_ENDPOINT`. Default unset. When set on a
  customer deployment, the existing OTLP exporter (ADR 020) is configured
  with a **second** OTLP destination — a Movate-operated OTel Collector — in
  addition to the customer's primary Azure Monitor sink. The customer
  retains their full per-tenant view; Movate gets a minimized fleet stream.
  - Implementation seam (when wired): the OTLP exporter chain inside the
    customer's Collector pipeline (`infra/azure/modules/containerapp-otel-collector.bicep`),
    not the application code. `cli ⊥ runtime` boundary is preserved.
- A second `MDK_TELEMETRY_REDACT` env, default `strict`, applies the
  attribute allow-list in D3 at the customer's Collector — so the dropped
  fields never traverse the customer/vendor boundary. The Collector
  processor is `attributes/keep` (drop-not-in-list), not `transform`.
- A Movate-side OTel Collector → Movate Log Analytics workspace ingests the
  stream. The `customer` template variable on the dashboards then resolves
  to the `customer` OTel resource attribute (D3) — the same dashboards
  re-bind cleanly.

Phase 2 is **declared deferred**, not approved-by-this-ADR. The decision to
flip it on is a separate ADR (likely ADR 04x) once Phase 1 metrics tell us
that the latency / scale pain is real and isn't solved by query caching.

### D3 — Scope of data: an explicit allow-list (no prompts, no PII)

Movate observes **metrics and span metadata only**. Prompt content, completion
content, retrieved chunk text, tool I/O payloads, user identifiers, and any
free-form attribute that has historically been a leak vector — **never leave
the customer tenant**, in either phase. The list below is the canonical
allow-list and is the single source of truth the Phase-2 Collector processor
will enforce.

**Metrics** (verbatim from `METRIC_NAMES` in `src/movate/tracing/metrics.py`
and the catalog in PR #518's `docs/observability.md`):

| Instrument | Attributes kept | Attributes redacted |
| --- | --- | --- |
| `mdk.jobs.completed` | `kind`, `status` | `tenant` → **hashed** (D4) |
| `mdk.job.duration_ms` | `kind`, `status` | — |
| `mdk.jobs.in_flight` | — | `tenant` → **hashed** (D4) |
| `mdk.run.tokens` | — | `tenant` → **hashed** (D4) |
| `mdk.run.cost_usd` | — | `tenant` → **hashed** (D4) |
| `mdk.db.pool.size` | — | — |
| `mdk.db.pool.idle` | — | — |
| `mdk.db.pool.in_use` | — | — |
| `mdk.db.pool.waiting` | — | — |
| `mdk.db.pool.max` | — | — |

**Spans** (names + a subset of attributes from PR #518's catalog):

| Span | Attributes kept | Attributes redacted/dropped |
| --- | --- | --- |
| `workflow.execute` | `workflow`, `workflow_version`, duration | `workflow_run_id` → **truncated to 8-char prefix**, `tenant_id` → **hashed** (D4) |
| `agent.execute` | `agent`, `agent_version`, `provider`, `model_override`, duration, status | `job_id` / `run_id` → **truncated to 8-char prefix**, `tenant_id` → **hashed** (D4) |
| `agent.turn[N]` | `turn`, `model`, duration | — |
| `retrieval.<skill>` | `skill`, `turn`, `auto_into`, duration | — |
| `skill.<name>` | `skill`, `turn`, duration | — |
| `kb_search` (+ stage children) | `stage_count`, `total_ms`, per-stage `duration_ms`, `input_count`, `output_count`, `chunk_count` | `chunk_ids_preview` → **dropped** (already capped at 10 upstream, but ID-shaped — drop entirely from the fleet stream) |

**Hard rules:**

- **No log bodies** ever cross the boundary. The fleet stream is metrics +
  span *metadata* only; `AppTraces` log bodies stay per-customer.
- **No `chunk_ids_preview`**, **no tool I/O payloads**, **no exception
  messages with embedded values** — only the exception *type* + status code.
- **No HTTP request URLs with path segments that could be customer IDs** —
  `agent.execute` already does not record URLs; we keep that boundary.
- The Phase-2 Collector processor MUST be `keep-only` (allow-list), not
  `delete-by-pattern` (deny-list) — a new attribute added to a span in
  `src/movate/core/` MUST default to *dropped* at the boundary, not
  *leaked*.

### D4 — `tenant` / `tenant_id` redaction

The `tenant` (metric attribute) and `tenant_id` (span attribute) fields can
encode a customer-internal identifier (a department, a team, an environment
slug — sometimes the customer's own end-user). They MUST NOT pass to Movate
verbatim.

- **Hashing:** `HMAC-SHA256(key = <per-deployment-salt>, msg = tenant)`,
  truncated to the first 16 hex chars. Per-deployment salt — so the same
  end-user across customers is **not joinable** across customers; within a
  single customer the hash is **stable enough** to track a cohort over time.
- The salt is rotated on a cadence we decide in implementation; the runbook
  in `dashboards/grafana/movate/README.md` records the rotation policy. Salt
  rotation is intentionally lossy across the rotation boundary — that is the
  point.
- The hash transform lives in the Phase-2 Collector pipeline (an `attributes`
  processor with `action: hash` is insufficient because OTel-Collector's
  built-in `hash` is unsalted SHA1; we ship a tiny `transform` rule that
  computes the keyed HMAC from a Key-Vault-mounted secret). Phase 1 does not
  need this — Phase 1 reads from the customer's workspace where the raw
  `tenant` value is the customer's own and never replicates centrally.

### D5 — Consent + retention

- **Consent.** Phase 1's customer-side action is an *explicit* Lighthouse
  delegation acceptance — a customer Azure admin clicks "accept offer" (or
  the engagement runbook does it with documented consent). That click is
  the audited opt-in. Phase 2's `MDK_TELEMETRY_ENDPOINT` is a second,
  independent opt-in — setting the env var on a deployment is the audited
  signal; default unset means default no-export.
- **Retention.** Phase 1 inherits the customer's per-workspace retention
  policy (Azure Monitor defaults — 30 to 730 days, customer-configured). We
  add nothing centrally. Phase 2's central workspace defaults to **90 days
  retention** on the Movate Log Analytics workspace; longer windows for the
  fleet view defeat the data-minimization posture this ADR is built on.
- **Customer opt-out / withdrawal.** Phase 1 is reversed by removing the
  Lighthouse registration — instantaneous, customer-side, no Movate
  cooperation required. Phase 2 is reversed by unsetting
  `MDK_TELEMETRY_ENDPOINT` and redeploying the Collector — same property.
  *We MUST preserve this property* — any future change that makes opt-out
  require Movate cooperation is a regression on the trust posture and is
  out of scope of this ADR.
- **PII boundary.** Explicit and absolute: no prompt content, no completion
  content, no chunk text, no user identifiers, no IP addresses (the customer
  Collector strips `client.address` before the fleet stream in Phase 2).
  The allow-list in D3 is the contract.

### D6 — Cost model

- **Movate-side**, Phase 1: Azure Managed Grafana Standard SKU is the
  primary line item. Public price (US East, 2026-05) lands around **\$0.69/hr
  per Grafana instance ≈ \$500/month** including the Standard tier's bundled
  data-source query quota. Cross-tenant KQL via Lighthouse incurs **zero
  Movate-side data charges** — each customer pays the per-workspace ingestion
  / retention on their own bill, which is the *intended* posture. Movate's
  marginal cost per onboarded customer in Phase 1 is therefore ~\$0 + a
  small share of Grafana's bundled query quota.
- **Movate-side**, Phase 2: adds a Movate-owned Log Analytics workspace
  ingest cost. Estimate using the metric/span shape: a per-run telemetry
  envelope from `docs/observability.md` is roughly **6 metric data points +
  ~5 spans × ~12 attributes** ≈ 1–2 KB raw / 0.3–0.6 KB compressed
  post-Collector. At Azure Monitor's commitment tier pricing (~\$2/GB at 100
  GB/day commit, 2026), a fleet doing **1 M runs/day across all customers**
  ingests ~300–600 GB/month → **\$600–1,200/month**. The 90-day retention
  default keeps the on-disk bill bounded. These numbers are a back-of-envelope
  for the decision record; the implementation ADR refreshes them.
- **Customer-side:** Phase 1 = **\$0** (no new resource on their bill, the
  reads they already pay for are the reads Movate runs). Phase 2 = a small
  egress + a small Collector CPU bump on the customer's existing ContainerApp
  Collector pod, but **no extra customer-side AM ingestion**.

## Consequences

**Positive**

- **Trust-first.** Phase 1 is the strongest possible privacy posture short
  of "no central view at all": data never moves, scope is narrow, opt-out is
  unilateral and instantaneous. This is the wedge story we sell to regulated
  customers.
- **Decision-grade product data.** Movate gets adoption / usage / health /
  cost / quality / capacity signals across the fleet — the six illustrative
  dashboards in this PR show what's answerable on day one.
- **Zero new mdk surface area in Phase 1.** No new env var, no schema
  change, no `MOVATE_*` / `MDK_*` env (CLAUDE.md rule 5 compat contract is
  preserved). Phase 2's env (`MDK_TELEMETRY_ENDPOINT`) is additive +
  default-off, also compat-preserving.

**Negative / risks**

- **Lighthouse onboarding friction.** Some customers will refuse on
  principle; the offer + runbook need to be tight, and we need a "we
  understand if you say no" answer that does not break the customer
  relationship.
- **Fleet-width KQL latency.** Phase 1's cross-subscription KQL fan-out is
  sequential per subscription via the AM data source — this gets uncomfortable
  past ~50 customers. The mitigation is Phase 2, which is *prepared* by
  this ADR but not *approved* by it.
- **Salt management.** D4's HMAC salt is per-deployment, in Key Vault.
  Operational burden is real (rotation cadence, key-vault access in Phase 2
  Collector); the runbook documents it.
- **Hidden assumption: `customer` label.** The fleet dashboards assume a
  single canonical `customer` template variable. In Phase 1 it's the
  Lighthouse subscription scope; in Phase 2 it's an OTel resource attribute.
  Whichever, MDK itself has to *consistently* tag emissions so the variable
  resolves — see Open Questions on which CalVer first emits it.

**Operational burden on Movate**

- Lighthouse offer + per-customer assignment template (one-time per
  customer, ~10 lines of Bicep).
- Managed Grafana instance + identity + dashboard JSON CI (existing repo
  pattern — these JSONs review like the per-customer dashboards already do).
- An on-call SRE rotation for the central instance — but it is read-only
  on customer data, so blast-radius of an outage is "Movate loses fleet
  visibility for an hour," not "customer telemetry is gone."

## Alternatives considered

- **Self-hosted Grafana** (a VM or AKS pod Movate runs itself).
  *Rejected.* Managed Grafana is the same product Movate would build, but
  with Microsoft running it; Movate gains nothing operational by self-hosting
  and loses managed-identity integration + zone redundancy. The infra-ops
  cost is not a Movate differentiator.
- **Grafana Cloud (Grafana Labs SaaS).**
  *Rejected for now.* A third-party SaaS in front of Azure Monitor adds a
  cross-cloud data egress (read traffic Azure-Monitor → Grafana Cloud), a
  second vendor in the data path, and a new SOC-2 conversation with each
  customer. Managed Grafana keeps the data path Azure-internal.
- **Single shared Movate-hosted Log Analytics that all customers ship
  into directly.**
  *Rejected.* This is the data-residency nightmare. Mixing 50 customers'
  telemetry into one workspace violates the per-tenant boundary embedded
  MDK is sold on, makes per-customer retention/policy impossible, and turns
  a single Movate access incident into a 50-customer disclosure event. Phase
  2's central workspace is a *minimized, allow-listed, opt-in* version of
  this idea — not a substitute for it.
- **No central view; rely on customer screenshots + support tickets.**
  *Rejected.* Status quo. Movate cannot make confident product investment
  decisions on anecdotes; this ADR exists precisely because that posture
  has run out.
- **Push per-customer aggregated rollups upward via `mdk report --json`
  on a cron** (the offline CLI from ADR 031 D3, scheduled per deployment,
  shipped over HTTP to Movate).
  *Considered, deferred.* This is essentially a hand-rolled, lower-fidelity
  Phase 2 with worse instrumentation hygiene. If Phase 2 turns out to be
  rejected by customers for posture reasons, this becomes the fallback —
  but its rollup granularity makes p95-latency and per-run cost outliers
  unanswerable, which are exactly the questions we want a fleet view for.

## Migration / compatibility

- **Phase 1 lands with zero customer-side code change** and **zero MDK
  schema change.** Onboarding is a Lighthouse offer the customer accepts;
  no `agent.yaml` / `project.yaml` change, no `/api/v1` change, no
  `MOVATE_*` / `MDK_*` env change, no CalVer break.
- **Phase 2** introduces **one new env var**, `MDK_TELEMETRY_ENDPOINT`,
  additive + default-off + deprecation-free. Per CLAUDE.md rule 5 the env
  is documented in the next release notes when it lands.
- **CalVer floor for the `customer` attribute.** The Phase 1 `customer`
  template variable resolves against a subscription-scope label that
  already exists in Azure Monitor (the workspace's parent subscription
  display name), so Phase 1 works on every currently-deployed CalVer with
  no MDK-side change. The Phase 2 `customer` *OTel resource attribute*
  needs an MDK release that stamps it — the floor for that is decided in
  the Phase 2 ADR.

## Open questions

- **Customer-id label CalVer.** Phase 2 needs an MDK-emitted, allow-listed
  `customer` OTel resource attribute on every metric and span. Which CalVer
  introduces it, and how is it sourced? Options: (a) the deploy environment
  stamps it via `MDK_CUSTOMER_ID` (operator-set), hashed at the Collector
  per D4; (b) MDK derives it from the workspace name at install. (a) is
  cleaner — operator-set, no MDK code change, hashing happens at the
  egress boundary not at the source.
- **Eval surfacing.** Eval pass-rate / drift live in **Langfuse scores**
  today (ADR 031 D1) — not in OTel. The `quality.json` dashboard in this
  PR therefore ships *panel shape only* with a banner pointing at the gap.
  Do we (a) widen ADR 016 D2 to emit eval pass-rate as an OTel metric too,
  (b) integrate Langfuse-as-Grafana-data-source for the fleet (Langfuse
  exposes a metrics endpoint), or (c) leave quality off the central view
  entirely? Decision deferred to a follow-up ADR — does not block Phase 1.
- **CalVer release annotations on Grafana.** The illustrative dashboards
  set up an annotation source labelled `annotation source: GitHub Releases
  API webhook — TODO post-ADR`. The implementation is: a Logic App
  subscribed to the `movate-cli` GitHub Releases webhook, posting a single
  AM custom-log row per release, which the Grafana annotation query reads.
  Out of scope for this PR; tracked when Phase 1 stands up.

## Boundaries

- **No `src/` change in this PR.** Pure docs + illustrative JSON dashboards.
- **No `infra/` Bicep in this PR.** The Managed Grafana + Lighthouse offer
  templates land in a follow-up under `infra/movate/` when the ADR is
  signed off.
- **`cli ⊥ runtime`** preserved — neither phase touches application code.
- **Tracing wired at the edges only** preserved — Phase 2 dual-export is
  configured on the customer's *Collector*, not in `src/movate/tracing/`.
- The per-customer dashboards in PR #518 (`dashboards/grafana/*.json`) are a
  **different surface for a different audience** (the customer's own ops
  team, viewing only their own workspace) and are unaffected by this ADR.
