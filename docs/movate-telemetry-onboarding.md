# Movate telemetry — Phase 1 onboarding runbook (ADR 039)

> **Status:** Phase 1 of ADR 039 ([`docs/adr/039-movate-product-telemetry.md`](adr/039-movate-product-telemetry.md)).
> Movate operates a central Managed Grafana instance in Movate's own Azure
> tenant; each customer delegates read-only access on their MDK Log
> Analytics workspace via Azure Lighthouse. Phase 1 reads telemetry **in
> place**; nothing replicates centrally. No customer-side code change, no
> new MDK env var, no `src/` changes.

This runbook has two halves: a one-time **Movate ops** setup, and a
repeatable **per-customer onboarding** flow.

---

## Movate ops (one-time)

Prerequisite: an Azure subscription owned by Movate (the "Movate fleet
observability" subscription) and a resource group inside it
(`movate-telemetry-rg` below).

### 1. Provision Managed Grafana + role assignments

```bash
az deployment group create \
  --resource-group movate-telemetry-rg \
  --template-file infra/movate-telemetry/managed-grafana.bicep \
  --parameters \
      grafanaName=movate-fleet-grafana \
      adminPrincipalIds='["<engineer-A-objectId>","<engineer-B-objectId>"]'
```

The Bicep module:

- creates the `Microsoft.Dashboard/grafana@2023-09-01` instance with a
  system-assigned managed identity,
- grants that managed identity **Monitoring Reader** on Movate's own
  subscription (symmetric to what each customer's Lighthouse offer
  delegates into their RG),
- grants the listed Entra principals **Grafana Admin** scoped to this
  Grafana instance (not subscription-wide),
- emits three outputs you need below.

### 2. Capture the outputs

```bash
az deployment group show \
  --resource-group movate-telemetry-rg \
  --name managed-grafana \
  --query 'properties.outputs.{endpoint:grafanaEndpoint.value, oid:managedIdentityApplicationId.value, tid:managedIdentityTenantId.value}'
```

Record the three values:

| Output | Where it's used |
| --- | --- |
| `grafanaEndpoint` | Movate operators bookmark this. |
| `managedIdentityApplicationId` | The customer pastes this into `movateApplicationId` on their Lighthouse parameters file. |
| `managedIdentityTenantId` | The customer pastes this into `movateTenantId` on their Lighthouse parameters file. |

> **On the naming:** the Lighthouse `authorizations[].principalId` field
> accepts the **object ID** of the delegated principal in the
> service-provider tenant; the historical `*ApplicationId` naming in the
> customer-facing parameters is what's documented across the Lighthouse
> ecosystem, so we preserve it. `managedIdentityApplicationId` and
> `managedIdentityPrincipalId` outputs return the same value — use either.

### 3. After PR #523 (ADR 039 dashboards) merges to main: import dashboards

Phase 1 dashboards (`adoption`, `usage`, `health`, `cost`, `quality`,
`capacity`) live on the ADR 039 PR branch. Once that merges to main, run:

```bash
MOVATE_GRAFANA_NAME=movate-fleet-grafana \
MOVATE_GRAFANA_RG=movate-telemetry-rg \
  bash scripts/import-movate-dashboards.sh
```

The script is idempotent (upserts by dashboard `uid`) and prints
`[imported|skipped|failed]` per file. Until the PR merges, the script
detects the missing `dashboards/grafana/movate/` directory and exits with
a clear "Dependency on PR #523" message.

---

## Per-customer onboarding (repeat per customer)

Prerequisite: the customer has an MDK deployment with an existing Log
Analytics workspace + Application Insights (the default
`infra/azure/main.bicep` deployment provisions both per ADR 020).

### 1. Send the customer the offer + Movate IDs

Email / Slack the customer:

- A link / copy of `infra/movate-telemetry/lighthouse-offer.json`.
- A link / copy of `infra/movate-telemetry/lighthouse-offer.parameters.example.json`.
- The `managedIdentityApplicationId` and `managedIdentityTenantId` values
  recorded above.
- This one-paragraph "what / why / how to revoke":

  > Movate ships a fleet observability surface (Azure Managed Grafana in
  > Movate's tenant) that needs read-only access to your MDK
  > telemetry — **metrics and span metadata only**, never prompts, never
  > completion content, never PII, never your Key Vault. The attached
  > Azure Lighthouse offer delegates `Monitoring Reader` (Azure built-in
  > role) on the resource group containing your Log Analytics workspace.
  > It grants no write, no secret read, no access outside that RG. You
  > revoke at any time with `az managedservices assignment delete` (see
  > below); revocation takes effect immediately, on your side, with no
  > Movate cooperation needed.

### 2. Customer reviews the offer

The customer's Azure admin should confirm:

- The role granted is `Monitoring Reader` (`43d0d8ad-25c7-4714-9337-8ba259a9fe05`),
  exactly that, and only that.
- The scope is **the resource group containing the Log Analytics
  workspace** (parameter on the deploy command — *not* the full
  subscription).
- The `movateTenantId` / `movateApplicationId` values match what Movate
  sent.

### 3. Customer deploys the offer

```bash
# Customer side — run in their Azure tenant
az deployment group create \
  --resource-group <their-LA-workspace-rg> \
  --template-file lighthouse-offer.json \
  --parameters @lighthouse-offer.parameters.example.json
```

(Or via the Azure Portal: *Service providers → Service provider offers →
Add offer → upload `lighthouse-offer.json`*.)

### 4. Verify (within ~15 minutes)

Movate operator, from the Grafana endpoint:

- Open *Configuration → Data sources → Azure Monitor*.
- Confirm the customer's subscription appears in the subscription picker
  (Lighthouse-delegated subscriptions show up automatically once the MI
  has the role).
- Open a fleet dashboard (e.g. *Movate fleet — health + reliability*) and
  set the `customer` template variable to the new subscription. Panels
  should populate from the customer's `AppMetrics` / `AppDependencies`
  tables within a couple of minutes.

---

## What flows to Movate — explicit allow-list (ADR 039 D3)

Mirror of the canonical allow-list in ADR 039 §D3. **This is the
contract.** Anything not listed here MUST NOT cross the boundary; the
Phase-2 Collector processor will enforce this as `keep-only`, but in
Phase 1 it's enforced by the role itself — `Monitoring Reader` cannot
read anything outside Azure Monitor.

### Metrics (from `METRIC_NAMES` in `src/movate/tracing/metrics.py`)

| Instrument | Attributes kept | Attributes redacted |
| --- | --- | --- |
| `mdk.jobs.completed` | `kind`, `status` | `tenant` -> hashed (D4, Phase 2) |
| `mdk.job.duration_ms` | `kind`, `status` | — |
| `mdk.jobs.in_flight` | — | `tenant` -> hashed (D4, Phase 2) |
| `mdk.run.tokens` | — | `tenant` -> hashed (D4, Phase 2) |
| `mdk.run.cost_usd` | — | `tenant` -> hashed (D4, Phase 2) |
| `mdk.db.pool.size` | — | — |
| `mdk.db.pool.idle` | — | — |
| `mdk.db.pool.in_use` | — | — |
| `mdk.db.pool.waiting` | — | — |
| `mdk.db.pool.max` | — | — |

### Spans (names + the allow-listed attributes only)

| Span | Attributes kept | Attributes redacted/dropped |
| --- | --- | --- |
| `workflow.execute` | `workflow`, `workflow_version`, duration | `workflow_run_id` -> 8-char prefix, `tenant_id` -> hashed (D4) |
| `agent.execute` | `agent`, `agent_version`, `provider`, `model_override`, duration, status | `job_id` / `run_id` -> 8-char prefix, `tenant_id` -> hashed (D4) |
| `agent.turn[N]` | `turn`, `model`, duration | — |
| `retrieval.<skill>` | `skill`, `turn`, `auto_into`, duration | — |
| `skill.<name>` | `skill`, `turn`, duration | — |
| `kb_search` (+ stage children) | `stage_count`, `total_ms`, per-stage `duration_ms`, `input_count`, `output_count`, `chunk_count` | `chunk_ids_preview` -> dropped |

### Hard rules

- **No log bodies.** `AppTraces` log content stays per-customer.
- **No prompts, no completions, no retrieved chunk text, no tool I/O
  payloads, no user identifiers, no IP addresses.**
- **No `chunk_ids_preview`** (capped to 10 upstream, but still ID-shaped
  — dropped at the boundary).
- **No exception messages with embedded values** — only the exception
  *type* + status code.

---

## Revoke (customer-side, immediate)

Revocation is customer-controlled and takes effect immediately — no
Movate cooperation required (this is the trust-posture contract from
ADR 039 D5; any future change that breaks this property is a regression).

```bash
# 1. Find the assignment name (or grab it from the deployment outputs).
az managedservices assignment list \
  --scope /subscriptions/<sub-id>/resourceGroups/<their-LA-workspace-rg> \
  --query '[].{name:name, definition:properties.registrationDefinitionId}' \
  -o table

# 2. Delete it.
az managedservices assignment delete \
  --assignment <assignment-name> \
  --scope /subscriptions/<sub-id>/resourceGroups/<their-LA-workspace-rg>

# 3. (optional) Delete the registration definition too:
az managedservices definition delete \
  --definition <registration-definition-name>
```

Movate's Grafana will lose access on the next token refresh
(typically <15 min). No state remains on the customer side.

---

## Phase 2 (future) — additive, deferred

ADR 039 §D2 Phase 2 describes a future opt-in OTLP dual-export path:

- A new env, `MDK_TELEMETRY_ENDPOINT`, **default unset**. When set on a
  customer deployment, the existing OTel Collector (ADR 020) gains a
  second OTLP exporter alongside the customer's primary Azure Monitor
  sink.
- A second env, `MDK_TELEMETRY_REDACT=strict`, enforces the §D3
  allow-list at the customer's Collector as a `keep-only` attribute
  processor before the stream leaves the customer tenant.
- A Movate-owned Log Analytics workspace ingests the stream centrally.

**Phase 2 is declared deferred, not approved.** Turning it on requires a
separate ADR (likely ADR 04x), once Phase 1 metrics demonstrate that
cross-tenant KQL fan-out is the real scale bottleneck. **Do not** add
`MDK_TELEMETRY_*` envs to `src/` until that ADR lands. This onboarding
runbook will get a Phase 2 section at that time.
