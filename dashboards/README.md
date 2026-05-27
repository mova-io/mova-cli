# mdk dashboards-as-code (ADR 031 D2)

In-repo, versioned dashboards customers **import** into the infra they already
run — there is no bespoke mdk dashboard server (CLAUDE.md rule 8 / ADR 031). They
render the OTel metrics mdk **already emits** from the runtime/worker edges
(`src/movate/tracing/metrics.py`); nothing here changes execution logic.

| File | Surface | Source of the data |
| --- | --- | --- |
| `grafana/mdk-golden-signals.json` | Grafana dashboard | Prometheus scraping the OTLP → Prometheus stream |
| `prometheus/mdk-rules.yaml` | Prometheus recording + alerting rules | same Prometheus |
| `azure/mdk-golden-signals.workbook.json` | Azure Monitor workbook | OTLP → Azure Monitor (App Insights `App*` tables) |

A test (`tests/test_dashboards_metric_names.py`) parses every file here and
asserts each metric it references exists in `movate.tracing.metrics.METRIC_NAMES`
— so a future metric rename can't silently break these dashboards.

## The metrics these dashboards cover

The instrument names are defined **once** in
`src/movate/tracing/metrics.py` (the `METRIC_*` constants / `METRIC_NAMES`):

| OTel instrument | Type | Attributes (labels) | Golden signal |
| --- | --- | --- | --- |
| `mdk.jobs.completed` | counter | `kind`, `status`, `tenant` | throughput + error rate (incl. dead-letter = `status=dead_letter`) |
| `mdk.job.duration_ms` | histogram (ms) | `kind`, `status` | latency p50/p95/p99 |
| `mdk.jobs.in_flight` | up-down counter | `tenant` | saturation (in-flight proxy) |
| `mdk.run.tokens` | counter | `tenant` | token volume |
| `mdk.run.cost_usd` | counter (usd) | `tenant` | per-run + cumulative cost |
| `mdk.db.pool.size` | observable gauge | — | DB pool: open connections (per pod) |
| `mdk.db.pool.idle` | observable gauge | — | DB pool: idle (checked-in) connections |
| `mdk.db.pool.in_use` | observable gauge | — | DB pool: checked-out connections (`size - idle`) |
| `mdk.db.pool.waiting` | observable gauge | — | DB pool: callers blocked on the acquire queue |
| `mdk.db.pool.max` | observable gauge | — | DB pool: configured per-pod ceiling (saturation denom) |

`status` values: `success` / `error` / `safety_blocked` / `dead_letter`.

The `mdk.db.pool.*` gauges (ADR 034 D3) are sampled from the **live per-pod
asyncpg pool** at metric-collection time (Postgres backend only; flat/zero on the
local SQLite backend). Under KEDA autoscale `N_pods x pool_max` can exceed Azure
Postgres `max_connections` → connection exhaustion; `in_use` rising toward `max`
and a sustained non-zero `waiting` are the early-warning signals. The
`mdk doctor` connection-ceiling check (ADR 034 D1) does the static capacity math
(`pods x pool_max <= max_connections - headroom`). These gauges carry **no**
Prometheus unit/`_total` suffix (`mdk_db_pool_in_use`, not `..._total`). They are
currently panelled on the **Grafana** dashboard only.

> **Queue depth** (`mdk.queue.depth`) and **eval pass-rate / drift** are golden
> signals too, but mdk does not yet export them as OTel *metrics*: queue depth is
> deferred to item #27 (needs a storage-count query off the `StorageProvider`
> seam) and eval/drift surface today as Langfuse scores (ADR 031 D1), not
> metrics. When they land as instruments in `metrics.py`/`METRIC_NAMES`, add the
> matching panels here. The dashboards intentionally use `mdk.jobs.in_flight` as
> the in-flight/saturation proxy until then.

## How the instrument name maps per backend

| Backend | Transform | Example: `mdk.jobs.completed` | Example: `mdk.job.duration_ms` |
| --- | --- | --- | --- |
| **Prometheus** | dots→underscores, lowercase, unit suffix, `_total` for monotonic counters | `mdk_jobs_completed_total` | `mdk_job_duration_ms_milliseconds_bucket` / `_sum` / `_count` |
| **Azure Monitor** | dot-name preserved verbatim in `AppMetrics.Name`; counter value in `Sum`; attributes in `Properties[...]` | `AppMetrics \| where Name == "mdk.jobs.completed"` | (latency cross-checked via the `agent.execute` span `DurationMs` in `AppDependencies`) |

If your Prometheus/OTLP exporter is configured **without** unit-suffix
normalization, drop the `_milliseconds` segment (`mdk_job_duration_ms_bucket`)
and/or the `_total` suffix and re-import; the panel/rule logic is identical.

## What mdk must emit for these to populate

Set the standard OTel env on the runtime + worker (these are
auto-injected on Azure Container Apps via ACA managed OpenTelemetry — see
`infra/azure/`):

- `MOVATE_TRACE_SINK=otlp` (or `both`) — turns mdk's OTel metrics on
  (`_otlp_metrics_enabled`); `none` keeps them off.
- `OTEL_EXPORTER_OTLP_ENDPOINT` — your collector endpoint.
- `OTEL_EXPORTER_OTLP_PROTOCOL` — `http/protobuf` (default) or `grpc`.
- `OTEL_SERVICE_NAME` — defaults to `movate-runtime` (the `service.name`
  resource attr; also the `AppRoleName` Azure assigns).
- `MOVATE_ENV` / `OTEL_DEPLOYMENT_ENVIRONMENT` — sets `deployment.environment`
  so you can split prod from dev.

Your collector must then fan the metrics out to Prometheus (e.g. the
`prometheus`/`prometheusremotewrite` exporter) and/or Azure Monitor (the
`azuremonitor` exporter, ADR 020).

## Import — Grafana + Prometheus

**Grafana dashboard**

- UI: *Dashboards → New → Import → Upload JSON file* →
  `grafana/mdk-golden-signals.json`, then pick your Prometheus data source for
  the `DS_PROMETHEUS` input.
- Or provision it as code (mount under your dashboards provider path):

  ```yaml
  # /etc/grafana/provisioning/dashboards/mdk.yaml
  apiVersion: 1
  providers:
    - name: mdk
      type: file
      options:
        path: /var/lib/grafana/dashboards/mdk
  ```

  and drop `mdk-golden-signals.json` into `/var/lib/grafana/dashboards/mdk/`.

**Prometheus rules**

Reference the file from your Prometheus config and reload:

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/rules/mdk-rules.yaml
```

```bash
cp prometheus/mdk-rules.yaml /etc/prometheus/rules/
promtool check rules /etc/prometheus/rules/mdk-rules.yaml   # validate
curl -X POST http://localhost:9090/-/reload                  # hot reload
```

Wire the alerts to your Alertmanager as usual. Thresholds (error >10%, p95 >30s,
any dead-letter, throughput stall) mirror the Azure SLO alerts so both surfaces
page on the same conditions.

## Import — Azure Monitor workbook

The workbook queries the **workspace-based App Insights `App*` tables** (the
OTel Collector's `azuremonitor` exporter writes there — see
`infra/azure/modules/monitor-alerts.bicep`), so scope it to the **Log Analytics
workspace** the deployment uses (`logs.outputs.workspaceId` in
`infra/azure/main.bicep`).

- UI: *Azure Monitor → Workbooks → New → </> (Advanced Editor)* → paste
  `azure/mdk-golden-signals.workbook.json` → *Apply* → *Done editing* → *Save*,
  choosing your subscription / resource group / the Log Analytics workspace as
  the resource. The KQL items are scoped to
  `microsoft.operationalinsights/workspaces`.
- The **"Open Azure Monitor Alerts"** link item jumps to the Alerts blade where
  the item-27 SLO rules (`*-deadletter-spike`, `*-high-error-rate`,
  `*-high-latency-p95`, `*-availability-no-traffic`) surface.

> The workbook's KQL assumes the `azuremonitor` exporter lands counters in
> `AppMetrics` with the dot-name in `Name`, the value in `Sum`, and attributes in
> `Properties[...]` (the same assumption the item-27 alert rules document). If
> your collector instead lands them in `customMetrics`, swap the table name; the
> filters/aggregations are unchanged.

## Don't hand-edit metric names here

Every metric name in these files must match
`movate.tracing.metrics.METRIC_NAMES`. If you rename an instrument in
`metrics.py`, the drift test fails until you update these files (and vice-versa).
That coupling is intentional — it's the anti-drift guard ADR 031 calls for.
