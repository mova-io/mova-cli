# Movate fleet dashboards — illustrative, pending ADR 039 sign-off

The six JSON files in this directory are **illustrative dashboards** for the
**Movate-side fleet view** of MDK deployments described in
[`docs/adr/039-movate-product-telemetry.md`](../../../docs/adr/039-movate-product-telemetry.md).

They are **NOT** customer-tenant imports. For customer-side, single-deployment
dashboards see the sibling files in `dashboards/grafana/*.json` (PR #518), which
target the customer's own Prometheus / Azure Monitor over a single workspace.
The audiences and the data scopes are different:

| Surface | Audience | Data scope | Hosted in |
| --- | --- | --- | --- |
| `dashboards/grafana/*.json` (PR #518) | The customer's own ops team | One MDK deployment, one tenant | Customer's Grafana / Managed Grafana |
| `dashboards/grafana/movate/*.json` (this dir) | Movate product team + SRE | The MDK fleet (all customers) | Movate's Azure Managed Grafana (per ADR 039 D1) |

## Status

**Illustrative only — pending ADR 039 sign-off.** The dashboards reference only
metric and span names confirmed on `origin/main` against
[`docs/observability.md`](../../../docs/observability.md) (the PR #518 catalog)
+ `METRIC_NAMES` in `src/movate/tracing/metrics.py`. Where a panel needs an
instrument that does not exist today (e.g. eval pass-rate, queue depth without
the in-flight workaround), the panel is left as a **scaffold with a Markdown
banner** flagging the gap and pointing at ADR 039 Open Questions / ADR 016 D2.

## Layout

| File | Pane | Notes |
| --- | --- | --- |
| `adoption.json` | Adoption + version posture | deployments-by-env gauge, active agents timeseries, new-agents/week stat, top template table, CalVer-running-where table, upgrade-lag heatmap |
| `usage.json` | Usage + latency | runs/day per customer, runs by `agent.execute.kind`, provider mix from `agent.execute.provider`, p50/p95/p99 from `mdk.job.duration_ms`, top agents by volume |
| `health.json` | Health + reliability | error rate by job-kind, deploy success rate by CalVer, active alerts, revision stability grid |
| `cost.json` | Cost | $/customer ranked bar, tokens by provider, $/run trend, cost outliers with z-score |
| `quality.json` | Quality + eval | eval pass-rate heatmap, drift detections, canary success rate — **mostly scaffold-with-banner today**: eval signals are Langfuse scores (ADR 031 D1), not OTel metrics |
| `capacity.json` | Capacity + saturation | `mdk.jobs.in_flight`, `mdk.db.pool.*`, worker saturation, hot-tenants bubble (latency × volume) |

Each dashboard:

- Carries the tag `"adr-039"` + `"illustrative"`.
- Has `customer`, `deployment`, `environment` template variables.
- Has a top-row **prescriptive runbook** Markdown panel (What / Normal / If
  red, do…) matching PR #518's pattern, cross-referencing alert rules + ADRs.
- Has annotation queries scaffolded for **CalVer releases** (placeholder —
  source becomes the GitHub Releases webhook once Phase 1 is live; see ADR
  039 Open Questions) + **incidents** (placeholder — source TBD).
- Uses **only** metric/span names that exist on `origin/main`. Where a name
  doesn't exist, the panel is replaced with a Markdown banner pointing at
  the open instrumentation question.

## How to import (Phase 1: Lighthouse)

Per ADR 039 D2 Phase 1, Movate's Azure Managed Grafana queries each customer's
Log Analytics workspace **in place** via the
`grafana-azure-monitor-datasource` plugin, authorized by Azure Lighthouse
delegation (`Reader` + `Monitoring Reader` on the customer's workspace; no
other roles).

1. Sign in to the Managed Grafana instance (Entra ID SSO, `movate.com` only).
2. *Dashboards → New → Import* and upload each JSON file in this directory.
3. When prompted for the data source, pick the Azure Monitor data source bound
   to Movate's managed identity. The Lighthouse delegations show up as
   additional **subscription scopes** the data source can query — multi-select
   the active customer subscriptions in the `customer` template variable.
4. The `customer` variable resolves against the **subscription display name**
   (Phase 1). The `deployment` + `environment` variables resolve against the
   `Resource` / `ResourceId` columns on `AppMetrics`.

There is **no data copy** in Phase 1. The dashboards query each workspace
directly. Customer retention / privacy posture is unchanged.

## How the layout changes under Phase 2 (deferred)

Per ADR 039 D2 Phase 2, when fleet-width KQL latency becomes the bottleneck,
deployments that opt in (`MDK_TELEMETRY_ENDPOINT` set, default unset) ship a
second OTLP stream to a Movate-operated Collector → Movate Log Analytics
workspace.

When that happens:

- The `customer` template variable re-binds from "Lighthouse subscription
  scope" to "the `customer` OTel resource attribute" (D4 — hashed). All other
  variables (`deployment`, `environment`) re-bind to OTel resource attributes
  the deployment stamps.
- The dashboards do **not** change shape — they continue to reference the
  same metric/span names. Only the data source UID + the variable definitions
  swap.
- The redaction allow-list (ADR 039 D3) is enforced at the customer
  Collector. The fleet workspace never sees prompt content, completion
  content, chunk text, or un-hashed `tenant` / `tenant_id`.

Phase 2 ships under a separate ADR + a separate PR; this README will get a
follow-up that adds the alternate `templating` block as a per-dashboard
overlay.

## Cross-reference

- ADR 039 — `docs/adr/039-movate-product-telemetry.md` (this surface's
  source of truth).
- ADR 031 — `docs/adr/031-reporting-and-dashboards.md` (dashboards-as-code
  pattern these dashboards extend).
- ADR 020 — `docs/adr/020-otel-collector-azure-monitor.md` (the per-customer
  OTLP → Azure Monitor pipe these dashboards read from).
- PR #518 dashboards — `dashboards/grafana/*.json` (the per-customer
  single-deployment view; different audience, different scope; documented
  alongside `dashboards/README.md`).
- Metric / span catalog — `docs/observability.md` (PR #518); MDK source of
  truth for instrument names is `src/movate/tracing/metrics.py:METRIC_NAMES`.

## Drift guard

The repo-wide drift guard `tests/test_dashboards_metric_names.py` validates
the **per-customer** dashboards under `dashboards/grafana/*.json` against
`METRIC_NAMES`. That test does **not** recurse into this `movate/` subdirectory
because the fleet dashboards intentionally include scaffold-with-banner panels
for instruments that do not exist yet (per ADR 039's Open Questions).

A lighter sibling test, `tests/test_movate_dashboards_parse.py`, validates that
every JSON file in this directory parses cleanly and references only metric
names from `METRIC_NAMES` — but does not require the strict "every emitted
metric is covered" coupling the per-customer guard enforces. This lets the
fleet dashboards evolve ahead of the instrumentation without silently going
broken.
