# ADR 078 — Self-hosted Temporal server on Azure Container Apps (durable workflows inside the customer's tenant)

**Status:** Accepted
**Date:** 2026-06-05
**Deciders:** Engineering (infra / runtime) — **no new shipped dependency; the
`temporalio` opt-in (ADR 054/065) is already adopted. This ADR is infra only.**
**Builds on / composes with (changes nothing in any of them):**
ADR 054 (Temporal as the durable workflow backend — **D5 BYOK connection**:
`TEMPORAL_HOST` / `TEMPORAL_NAMESPACE` / `TEMPORAL_TLS_CERT` resolved by
`runtime/workflow_backend._resolve_temporal_connection`; Phase 2 named a
"Temporal-on-AKS" Bicep module — this ADR chooses **Azure Container Apps**
instead and supersedes that wording),
ADR 062 (durable HITL HUMAN node — the headline workload that needs a
reachable, durable Temporal frontend; pause/resume + signal landed 2026-06-05),
ADR 065 (Temporal as the *optional* durable-execution seam — native is the
floor, forever; this is opt-in infra for those who want durability),
ADR 018 (BYOK credential autoload — Temporal connection rides the same seam as
provider keys), and the existing `infra/azure` Bicep stack (the shared
Container Apps Environment, `postgres.bicep` Flexible Server, `keyvault.bicep`,
the UAI pre-creation + two-pass deploy + opt-in-feature-flag patterns — Langfuse
is the closest precedent: a self-hosted third-party service on the shared CAE +
shared Postgres).

**Defining gap.** Durable Temporal today requires either a developer's local
`temporal server start-dev` (non-durable, SQLite/in-memory — fine for tests,
useless in production) or **Temporal Cloud** (a third-party SaaS: egress out of
the tenant, per-action billing, customer data transiting an external service).
Neither fits a customer who wants durable workflows — refund approvals that
survive for weeks (ADR 062), multi-step incident procedures (ADR 077) — running
**entirely inside their own Azure tenant**: data residency, no SaaS dependency,
predictable cost. The application side is already done: the worker and API
connect to *whatever* `TEMPORAL_HOST` points at (ADR 054 D5), so a self-hosted
frontend just needs its FQDN passed in. What is missing is the **deployment
seam** — a way to stand the Temporal server up next to the rest of mdk in Azure.

This is a **deployment-lifecycle / infra** ADR (rule 2). It adds one optional
Bicep module and one `enableTemporal` flag; it changes deploy behavior
**additively** (rule 5 flagged) and touches **zero application code** — the
elegance is that ADR 054 D5 already drew the seam.

---

## Decision

### D1 — Temporal frontend as a single Container App (`temporalio/auto-setup`)

Run the Temporal server as one Container App in the existing
`movate-${env}-cae` environment, from the official **`temporalio/auto-setup`**
image (a pinned tag, never `latest`). That image bundles the four Temporal
services (frontend / history / matching / internal-worker) plus a schema
auto-setup step into one container — the pragmatic single-process topology for
SMB / demo / single-tenant scale. Schema creation is idempotent (auto-setup
creates the schema if absent, no-ops if present), so a restart is safe.

The container exposes the frontend's **gRPC port 7233**. A new module
`infra/azure/modules/containerapp-temporal.bicep` mirrors `langfuse.bicep`
(self-hosted third-party service, shared CAE, shared Postgres, UAI for ACR pull
+ KV read).

> The production multi-service split (separate frontend/history/matching/worker
> Container Apps for horizontal scale + HA) is **out of scope** here (D6,
> Boundaries) — v1 is the single-container cluster.

### D2 — Reached over the CAE by internal FQDN; no application change

The Temporal Container App uses **internal ingress** (`external: false`) with
**`transport: 'tcp'`**, `targetPort: 7233`, `exposedPort: 7233` — raw gRPC on a
fixed port, never published to the internet. The API and worker Container Apps
reach it at its internal FQDN and set, via the same env wiring the rest of
`main.bicep` uses:

```bicep
{ name: 'TEMPORAL_HOST',      value: enableTemporal ? '${temporal!.outputs.internalFqdn}:7233' : '' }
{ name: 'TEMPORAL_NAMESPACE', value: enableTemporal ? 'default' : '' }
{ name: 'TEMPORAL_TLS_CERT',  value: '' }   // empty inside the CAE boundary (D5)
```

`_resolve_temporal_connection` reads exactly these (ADR 054 D5). When
`enableTemporal=false` the vars are empty and selecting `runtime: temporal`
fails loud (the existing fail-loud-availability rule, ADR 055 D6) — never a
silent downgrade. **No code changes**; this is pure wiring. (`http2` internal
ingress on 443 is a viable alternative to TCP/7233 and is noted in Alternatives;
TCP keeps the canonical `host:7233` gRPC shape the SDK and local dev already use.)

### D3 — Datastore on the shared Postgres Flexible Server, separate databases

Temporal needs a SQL datastore. Reuse the existing `postgres.bicep` Flexible
Server and add two databases — `temporal` (default store) and
`temporal_visibility` (standard SQL visibility store) — exactly as the
`langfuse` database is added today. The DB password is a Key Vault secret
(reuse `pg-admin-password`, or a dedicated `temporal` Postgres role for least
privilege) referenced by the Temporal Container App via `secretRef` + UAI, the
same pattern as every other app. Standard SQL visibility is sufficient for
mdk's `mdk runs` / list-paused use; **advanced visibility (Elasticsearch) is out
of scope.** A dedicated Postgres server for isolation at scale is an operator
escape hatch (Boundaries), not the v1 default.

### D4 — Opt-in module, two-pass deploy, UAI pre-creation (reuse, don't invent)

The whole module is gated `if (enableTemporal && enableApiWorker)` and emits
**zero bytes** when false — identical to `enableScheduler` / `deployLangfuse` /
`enablePlayground`. A `temporalUai` user-assigned identity is **pre-created
unconditionally** in `main.bicep` with `AcrPull` + `Key Vault Secrets User`
role assignments landing on pass 1 (the established cold-deploy-safety pattern),
so the app lands cleanly on pass 2 after the operator populates the Temporal DB
secret between passes. No new deploy mechanics — the two-pass `enable*` flow is
unchanged.

### D5 — TLS / auth posture: network boundary first, mTLS available

Inside the CAE, Temporal traffic is environment-internal and never publicly
exposed (D2), so **plaintext gRPC on internal ingress is acceptable for v1** —
the internal-ingress network boundary is the control. For stricter postures
(regulated tenants, defense-in-depth) the Temporal frontend can run with mTLS:
the CA cert lands in Key Vault and rides into `TEMPORAL_TLS_CERT`, which
`get_temporal_client` already honors (`TLSConfig(server_root_ca_cert=…)`) — no
code change, a config flip. Temporal **authorization** (namespace RBAC, API
keys, the authorizer plugin) is **out of scope for v1**; the boundary is the
network, not in-cluster authz.

### D6 — Optional Web UI; HA + scaling limits documented honestly

An optional **Temporal Web UI** (`temporalio/ui` image) ships as a separate
Container App, gated on its own sub-flag. It is internal by default; exposing it
externally reuses the playground's Entra SSO pattern (ADR 053) — never an
unauthenticated public console.

**HA limitation (flagged per CLAUDE.md §11):** the single `auto-setup`
container is **one logical Temporal cluster** and runs at `minReplicas:
maxReplicas: 1`. Durable state is safe in Postgres across a restart, but the
frontend is briefly unavailable while the container recycles (in-flight
workflows resume automatically once it returns — that is the whole point of
durability). This is acceptable for SMB / demo / single-tenant; it is **not**
horizontally scalable or zero-downtime. Higher throughput / HA requires the
multi-service split (separate frontend/history/matching apps), deferred to a
follow-up. Operators must not read `maxReplicas` as Temporal horizontal scale.

---

## Phasing

| Phase | Scope | Size |
|---|---|---|
| **Phase 1 (this ADR + the module)** | `containerapp-temporal.bicep` (auto-setup single container, internal TCP ingress :7233) + `temporal`/`temporal_visibility` Postgres DBs + `enableTemporal` flag + `temporalUai` + API/worker env wiring + optional internal Web UI. Native remains default; temporal is opt-in. | M |
| **Phase 2** | Frontend mTLS (KV cert → `TEMPORAL_TLS_CERT`); Web UI behind Entra SSO; Temporal metrics → the in-cluster OTel collector (ADR 020). | S–M |
| **Phase 3** | Multi-service HA topology (frontend/history/matching/worker as separate Container Apps); optional dedicated Postgres; namespace authz. | L |

---

## Consequences

**Positive**
- **Durable workflows entirely within the customer's Azure tenant** — data
  residency, no third-party SaaS, no per-action egress billing, predictable cost.
- **Zero application code change.** ADR 054 D5's BYOK seam already abstracts the
  connection; this is purely a new Bicep module + flag.
- **Reuses every established infra pattern** (shared CAE, shared Postgres,
  Key Vault secrets, UAI pre-creation, two-pass deploy, opt-in feature flag) —
  low blast radius, additive, Langfuse is the working precedent.
- **Composes with native + Temporal Cloud.** A deployment can choose: no
  Temporal (native floor), Temporal Cloud (point `TEMPORAL_HOST` at the cloud
  namespace), or this self-hosted module — all behind the same seam.

**Negative / risks**
- **Operational ownership.** Self-hosting means owning image-tag upgrades,
  schema migrations (pin the tag; auto-setup runs on start), and incident
  response — vs Temporal Cloud's managed SLA. Backups are covered by the
  Postgres Flexible Server backup policy.
- **Not HA in v1.** The single auto-setup container is a single point of
  restart-availability (D6). Durable state survives; the frontend blips.
- **Shared-Postgres coupling.** Runtime + KB + Langfuse + Temporal now share one
  Flexible Server — capacity planning matters; the dedicated-server escape hatch
  is documented (Phase 3).
- **gRPC over ACA TCP ingress** is a less-trodden path than HTTP ingress and
  needs validation that ACA internal TCP ingress + the Temporal frontend
  interoperate cleanly (a Phase-1 spike item).

## Alternatives considered

1. **Temporal Cloud (managed SaaS).** Already supported by the BYOK seam — point
   `TEMPORAL_HOST` at the cloud namespace + an mTLS cert in `TEMPORAL_TLS_CERT`.
   Best operability + HA. Rejected as the *only* option because it is a
   third-party dependency with egress and per-action billing and takes customer
   workflow data out of the tenant. **Kept as the recommended default** for
   customers who accept SaaS; this ADR serves those who cannot.
2. **AKS + the Temporal Helm chart (ADR 054 Phase 2's original wording).**
   Production-grade HA out of the box, but a full Kubernetes operational burden
   and a *second* orchestration substrate alongside mdk's ACA model. Rejected
   for v1; revisit only if throughput forces the multi-service split at scale.
3. **`temporal server start-dev` in a container.** Uses SQLite / in-memory and
   is not durable across restarts — defeats the entire purpose. Rejected.
4. **Azure Container Instances (ACI).** No managed environment, service
   discovery, KEDA scaling, or Key-Vault-secret integration the rest of mdk
   relies on. Rejected — ACA is the established vessel.

## Boundaries (out of scope)

- Multi-service production topology + true HA (Phase 3).
- Advanced visibility (Elasticsearch); standard SQL visibility only.
- Temporal authorization plugin / namespace RBAC (network boundary is the v1
  control).
- `mdk auth login temporal` / Temporal Cloud onboarding UX — that is ADR 054
  Phase 2, a separate item.
- Automating the operator's secret-population step — the two-pass deploy stands.

## New surfaces (CLAUDE.md §5 — all additive)

- New Bicep module `infra/azure/modules/containerapp-temporal.bicep` +
  `enableTemporal` param (default `false`). Zero effect on the template when
  false.
- Two new Postgres databases (`temporal`, `temporal_visibility`) + one new
  Key Vault secret, all gated on the flag.
- New `TEMPORAL_HOST` / `TEMPORAL_NAMESPACE` / `TEMPORAL_TLS_CERT` env vars on
  the API + worker Container Apps, populated only when enabled (empty otherwise
  → native default, unchanged).
- A new `temporalUai` managed identity + its AcrPull / KV-read role assignments.

No change to `agent.yaml` / `workflow.yaml` schema, the `/api/v1` surface,
storage schema, CLI flags, or any `MOVATE_*` / `MDK_*` env var. Native-only and
Temporal-Cloud deployments are byte-for-byte unaffected.
