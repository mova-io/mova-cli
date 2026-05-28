# Movate product telemetry — onboarding runbook (Phase 1) — BLOCKED ON ADR 039

> **Status: placeholder.** This PR is intentionally a **draft** and contains
> only this note. It is parked pending ADR 039 (Movate product telemetry).
>
> See the PR description for the full scope this branch *will* deliver once
> the dependency lands.

## Why this PR is empty

The Phase 1 deliverables for ADR 039 — Azure Managed Grafana provisioned in
Movate's tenant + an Azure Lighthouse delegation offer + a dashboard-import
script + this onboarding runbook — were planned to be built on top of:

1. **ADR 039 itself** (`docs/adr/039-movate-product-telemetry.md`) — defines
   the data-scope allow-list, the trust model (read-only Lighthouse
   delegation, no data egress), and the Phase 1 / Phase 2 split.
2. **Illustrative Movate-tenant dashboards** at
   `dashboards/grafana/movate/*.json` — what
   `scripts/import-movate-dashboards.sh` would post into the Managed Grafana
   instance.

Neither exists yet on `origin/main` or on a `docs/adr-039-movate-product-telemetry`
branch on `origin`. The latest accepted ADR on `main` is **ADR 038**
(`docs/adr/038-governable-agent-pattern-library.md`).

Per the build instructions for this branch:

> If the ADR-039 branch isn't on origin yet, STOP and open a draft PR
> explaining the dependency rather than referencing dashboards that don't
> exist.

This file is that explanation. It will be replaced by the full runbook (see
PR description for the complete planned layout) in the same PR — promoted out
of draft — once ADR 039 and its dashboards are on `origin`.

## What will replace this file

Two-section runbook:

- **Movate side (one-time):** apply
  `infra/movate-telemetry/managed-grafana.bicep` in a Movate-owned subscription,
  capture the Grafana endpoint and the managed-identity application ID, run
  `scripts/import-movate-dashboards.sh`, then ship the Lighthouse offer
  template + the managed-identity app ID to each customer.
- **Customer side (per-customer):** review the offer, apply
  `infra/movate-telemetry/lighthouse-offer.json` scoped to the Log Analytics
  workspace's resource group, accept the delegation. Movate's Grafana then
  queries the workspace read-only.

Plus three appendix sections this PR will also add:

- **Revoke** — how a customer rescinds the Lighthouse delegation.
- **What flows to Movate** — table mirroring ADR 039's data-scope allow-list
  (only Azure Monitor metrics/spans Movate's Grafana queries; no data egress;
  no PII).
- **Phase 2 (future) — dual export** — pointer noting Phase 2 requires an MDK
  runtime change + customer opt-in env var, and is out of scope for this
  branch.

## Unblocking

Land ADR 039 (the doc + the `dashboards/grafana/movate/*.json` set) on `main`
(or push the `docs/adr-039-movate-product-telemetry` branch to `origin` for
this PR to reference), then re-run the agent task that produced this draft.
