# ADR 036 — Per-tenant usage metering + quotas

**Status:** Accepted
**Date:** 2026-05-27
**Deciders:** Engineering — **quota *policy* (D2) is a commercial decision → Deva sign-off**
**Builds on / related:** ADR 024 (per-run `cost_usd` + tokens), ADR 013 / item
25 (per-tenant rate-limiting), ADR 032 D2 (`core/reporting` aggregation), the
`StorageProvider` run/eval records.

## Context
Cost is tracked **per run** (`cost_usd`, tokens) and rate-limiting is
**requests/sec** — but there is no **aggregate usage metering** or **quota
enforcement** (tokens / cost / runs per tenant per period). Both are required to
commercialize a multi-tenant platform: billing visibility + hard abuse ceilings
beyond burst rate-limiting.

## Decision
- **D1 — Usage metering.** Per-tenant rollup (runs, tokens, `cost_usd`, KB
  storage, ingest volume) by time window, computed from the per-run records
  already captured (ADR 024) — reuse the ADR-032-D2 `core/reporting` aggregation.
  Expose `GET /api/v1/usage` (per-tenant, by period; `read`).
- **D2 — Quotas [Deva sign-off on policy].** Per-tenant ceilings
  (monthly cost / token / run) enforced **at admission** — before a run/ingest is
  accepted — returning `402`/`429` with the limit + reset window. **Soft** warn at
  80% (event via ADR 035 + header), **hard** block at 100%. Distinct from burst
  rate-limiting (which is requests/sec).
- **D3 — Billing export.** Usage export (CSV/JSON) per tenant/period for a
  downstream billing integration. The billing *integration* itself (Stripe, etc.)
  is out of scope — we expose the metered data.

## Consequences
**Positive:** billing-grade usage visibility + enforceable ceilings, reusing the
per-run cost records + the reporting aggregation — no new measurement plumbing.
**Risks:** cost is **estimated** from `pricing.yaml`, not the actual provider
bill — document the estimate↔actual gap; near-ceiling concurrency (many parallel
runs at the limit) needs atomic counters or an accepted slight overage; per-tenant
quota config storage.

## Boundaries
Metering reads existing run/cost records (no new capture); quota enforcement at
the admission edge (a dependency/middleware), not in `core`; config in the
tenant/registry store. `cli ⊥ runtime`.

## Scope / rollout
**D1 metering + `/usage`** is buildable now (reuses `core/reporting`; depends on
ADR 032 D2 landing first). **D2 quota policy + thresholds** is the commercial
decision → Deva sign-off before enforcement ships. **D3 export** follows D1.
