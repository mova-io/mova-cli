# ADR 087 — Prometheus augmentation for high-resolution operational metrics

- Status: Accepted
- Date: 2026-06-07
- Supersedes: none
- Related: ADR 001 (vendor-neutral), ADR 015 (trace-sink selector), ADR 020
  (collector → App Insights), ADR 031 (dashboards-as-code), ADR 039 (managed
  Grafana posture), ADR 082 (Temporal observability)

## Context

The cloud deployment ships OTel metrics through one pipeline (ADR 020):

```
app → OTel collector → azuremonitor exporter → App Insights / Log Analytics
                                                      ↑ Grafana (Azure Monitor datasource, KQL)
```

This is solid for **traces, logs, audit, and long retention**, but it is a poor
fit for *real-time operational metrics*:

- **Ingestion lag.** App Insights → Log Analytics is minutes (observed 5-10 min
  in dev). Useless for a live operator dashboard.
- **KQL is clumsy for metric math.** `rate()` and `histogram_quantile()` have no
  clean KQL equivalent; our `mdk.job.duration_ms` / `mdk.workflow.duration_ms`
  histograms reduce to a `Sum/ItemCount` mean approximation, not real p50/p95.
- **Alerting cost/ergonomics.** `scheduledQueryRules` (KQL) are coarse and
  per-query priced vs. Prometheus recording rules + Alertmanager.

Meanwhile the repo already ships a **complete PromQL surface** built for the
local demo stack (`infra/otel-collector/` docker-compose): the golden-signals /
cost / queue-and-pool / runtime-overview / exec-summary dashboards
(`dashboards/grafana/*.json`) and the recording + alerting rules
(`dashboards/prometheus/mdk-rules.yaml`). In the cloud these are dead — there is
no Prometheus datasource — so the most polished dashboards we own render nothing.

## Decision

**Augment** (not replace) the Azure Monitor pipeline with Prometheus, by fanning
the collector's metrics out to a second exporter. Traces/logs/audit continue to
App Insights; metrics ALSO flow to Prometheus, which Grafana queries for the
real-time operational dashboards.

```
app → OTel collector ─┬─ azuremonitor          → App Insights      (traces, logs, audit)
                      └─ prometheusremotewrite  → Prometheus (TSDB) (real-time metrics)
                                                      ↑ Grafana (Prometheus datasource, PromQL)
```

### D1 — Push (remote-write), not pull (scrape)

ACA apps scale to zero and have rotating replica IPs, so Prometheus *scraping*
them is unreliable. Instead the **collector pushes** via its
`prometheusremotewrite` exporter to Prometheus's `--web.enable-remote-write-receiver`
endpoint. The collector is a single stable internal target; nothing scrapes
ephemeral replicas.

### D2 — Self-hosted single-replica Prometheus on ACA *for dev* (not Managed yet)

The architecturally "correct" cloud target is **Azure Monitor Managed
Prometheus** (an Azure Monitor Workspace). We do NOT adopt it yet because its
remote-write ingestion requires Entra (Azure AD) auth on the write path, and the
stock `otel/opentelemetry-collector-contrib` distro does not bundle an Azure AD
auth extension for `prometheusremotewrite` — wiring it needs a custom collector
build, which is friction we will not pay during dev.

For now we run a **single-replica `prom/prometheus` Container App** (internal
ingress, remote-write receiver, in dev an ephemeral TSDB — history is
disposable). This is the exact, proven pattern from the local demo stack, just
on ACA. Acceptable in dev where retention/HA do not matter.

**The collector exporter is the seam.** Migrating to Managed Prometheus later is
a one-line collector change (point `prometheusremotewrite` at the Azure Monitor
Workspace's Data Collection Endpoint + add the AAD auth extension) plus an `AMW`
resource — no app or dashboard change. That migration is a follow-up ADR.

### D3 — Gated + default-off (additive, no regression)

A new `enablePrometheus` Bicep param (default `false`) gates the whole stack: the
Prometheus Container App module AND the collector's second exporter. With it off,
the compiled collector config and the deployed topology are **byte-for-byte
unchanged** — the App Insights path is untouched. The api/worker/temporal-worker
need no change (they already emit OTLP to the collector).

### D4 — Dashboards-as-code, both backends

The existing PromQL dashboards (`dashboards/grafana/*.json`) and rules
(`dashboards/prometheus/mdk-rules.yaml`) become live in the cloud once the
Prometheus datasource exists. They stay under the ADR-031 drift guard. Grafana is
deployed out-of-band today (not in the Azure bicep), so the **Prometheus
datasource + PromQL dashboard import** is an operator step (Grafana API /
provisioning), documented in `docs/movate-telemetry-onboarding.md` — same model
as the `mdk-temporal` / `mdk-live-runtime` imports. Codifying Grafana itself in
bicep is tracked separately.

## Consequences

- **Pro:** the polished PromQL dashboards light up with ~zero authoring;
  real-time (≈15-30s) operational metrics + proper percentiles; Alertmanager-grade
  alerting; the demo gains a second, snappier observability surface.
- **Pro:** purely additive + gated — App Insights (traces/logs/audit) is the
  source of truth and is untouched; dev can flip Prometheus on/off freely.
- **Con / accepted:** two metric backends to reason about (App Insights +
  Prometheus) and two Grafana datasources — the duplication ADR 020 consolidated
  away from, accepted deliberately for the real-time + alerting win.
- **Con / dev-only:** self-hosted Prometheus is stateful-on-ACA (single replica,
  ephemeral TSDB in dev). NOT for production — production goes to Managed
  Prometheus (follow-up ADR) before any customer deployment.

## Out of scope (follow-ups)

- Azure Monitor Managed Prometheus (AMW) + AAD remote-write auth (the production
  target; D2 migration).
- Codifying the Grafana Container App + its datasources/dashboards in bicep
  (today both Grafana and the PromQL import are out-of-band).
- Persistent TSDB (Azure Files volume) + retention/HA for any non-dev use.
