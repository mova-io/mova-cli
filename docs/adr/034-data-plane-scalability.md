# ADR 034 — Data-plane scalability: connection pooling under autoscale + read replicas

**Status:** Accepted
**Date:** 2026-05-27
**Deciders:** Engineering — **infra-shaped decisions (D1 PgBouncer provisioning) require Deva sign-off (ADR 001)**
**Builds on / related:** `storage/postgres.py` (per-pod asyncpg pool), ADR 014
(registry), ADR 017 (KEDA worker), ADR 031 (metrics/dashboards), ADR 001
(portability).

## Context
Per-pod `asyncpg` pools exist and agent-bundle serving is already cached
(version-keyed per-pod, ADR 021) — both good. The remaining scale risks:
**(a)** under KEDA autoscale, `N_pods × pool_max` can exceed Azure Postgres
`max_connections` → connection exhaustion (invisible until load); **(b)** all
reads hit the single primary.

## Decision
- **D1 — Server-side connection pooling [Deva sign-off].** Front Postgres with
  **PgBouncer** (or Azure Postgres Flexible Server built-in connection pooling)
  in transaction-pooling mode. Size per-pod pools against KEDA max-replicas via a
  documented formula (`pods × pool_max ≤ max_connections − headroom`), and add a
  `mdk doctor` check that flags the ceiling. asyncpg behind pgbouncer
  transaction-mode needs `statement_cache_size=0` — encode in the pool config.
- **D2 — Read replicas behind `StorageProvider`.** Route lag-tolerant reads
  (lists, dashboards, history) to an optional replica connection; writes always
  to primary; **falls back to primary when no replica is configured** (opt-in,
  portable). Read-your-writes caveats documented.
- **D3 — Pool observability.** Emit pool in-use / idle / wait metrics → ADR 031
  dashboards + `mdk doctor` surface saturation *before* exhaustion.

## Consequences
**Positive:** safe horizontal scale (no connection-exhaustion cliff), read
fan-out for the read-heavy front end, early-warning observability — behind the
existing `StorageProvider` seam, portable.
**Risks:** PgBouncer transaction-mode quirks (prepared statements / session
state); replica lag → stale reads (route only lag-tolerant queries; document).

## Boundaries
`StorageProvider` seam owns read/write routing (D2); infra (bicep) owns PgBouncer
(D1); no execution-logic change. Portable — PgBouncer is generic; Azure built-in
pooling is the Azure option.

## Scope / rollout
**D3** (pool metrics) + the **D1 doctor check + pool-sizing** are buildable now
(no infra dep). **D1 PgBouncer provisioning** + **D2 replica** are infra-shaped →
Deva sign-off (bicep + env). Non-infra pieces ship first.

### Shipped (non-infra slice)
- **D3 pool metrics** — `PostgresProvider.pool_stats()` snapshots the live
  asyncpg pool (size / idle / in-use / waiting / max); OTel observable gauges
  `mdk.db.pool.{size,idle,in_use,waiting,max}` (registered at the `mdk serve` /
  `mdk worker` edge via `tracing.metrics.register_pool_metrics`, sampling the
  pool each collection cycle — storage never imports tracing). Panelled on the
  Grafana dashboard; names added to `METRIC_NAMES` (dashboards anti-drift guard
  stays green).
- **D1 doctor check** — `mdk doctor` "db pool capacity" row computes the
  connection-ceiling headroom against the sizing formula
  `pods x pool_max <= max_connections - headroom`, probing `max_connections` /
  `pool_max` from a reachable Postgres (else `MOVATE_DB_MAX_CONNECTIONS` /
  `MOVATE_DB_POOL_MAX_SIZE` / `MOVATE_KEDA_MAX_REPLICAS` /
  `MOVATE_DB_CONNECTION_HEADROOM`, else assumed defaults). Warns over the
  ceiling; informational on assumed inputs; never crashes doctor without a DB.
  Remediation points at lower pool_max / cap replicas / PgBouncer (the
  Deva-gated D1 infra). Formula also documented in `docs/runbooks/load-soak.md`.

**NOT in this slice** (Deva-gated, unchanged): D1 PgBouncer provisioning + the
`statement_cache_size=0` pool config, and D2 read-replica routing.
