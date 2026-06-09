# ADR 095 unified observability — dev rollout (live state + reproduction)

This records the **live `movate-dev` state** of the unified observability stack
(ADR 095). These were applied **imperatively** to move fast; the follow-up is to
fold them into bicep (`main.bicep` + the modules below) so they survive a full
stack redeploy. Until then, this file is the source of truth for what's live.

## What is live on `movate-dev`

| Piece | State |
|---|---|
| **ClickHouse** (unified telemetry store) | `movate-dev-clickhouse` Container App, internal TCP ingress `:9000`, db `otel`. Holds `otel_traces` / `otel_metrics` / `otel_logs` (auto-created by the exporter). **Verified: real spans landing** (`SELECT count() FROM otel.otel_traces` > 0, service `movate-runtime`). Storage is **ephemeral** (durability = follow-up). |
| **OTel collector** | `movate-dev-otelcol` `OTELCOL_CONFIG` now has a `clickhouse` exporter in **all 3 pipelines** (traces/metrics/logs), additive alongside `azuremonitor`. |
| **Grafana datasources** | `Postgres (movate)` (uid `postgres-movate` → real `movate` DB, business of record) + `ClickHouse (otel)` (uid `clickhouse-otel` → telemetry). Plugin `grafana-clickhouse-datasource` installed via `GF_INSTALL_PLUGINS`. |
| **Dashboards** | `mdk - business of record` (live Postgres) imported; `dashboards/grafana/mdk-unified-observability.json` is the cross-store template (Postgres ⨯ ClickHouse on `trace_id`). |

## Reproduce (the exact commands)

```bash
# 1. ClickHouse container app (internal TCP 9000)
az containerapp create -g movate-dev-rg -n movate-dev-clickhouse \
  --environment movate-dev-cae --image clickhouse/clickhouse-server:24.3 \
  --ingress internal --transport tcp --target-port 9000 --exposed-port 9000 \
  --cpu 2 --memory 4Gi --min-replicas 1 --max-replicas 1 \
  --env-vars CLICKHOUSE_DB=otel CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1

# 2. Collector: add a `clickhouse` exporter to OTELCOL_CONFIG, endpoint
#    tcp://movate-dev-clickhouse:9000?dial_timeout=10s, db otel, create_schema:true,
#    and append `clickhouse` to traces+metrics+logs pipelines (keep azuremonitor).
#    (Rebuild the YAML with a yaml-capable python — NOT system python — and set via
#     `az containerapp update --set-env-vars "OTELCOL_CONFIG=$VAL"`.)

# 3. Grafana: install the plugin + provision datasources
az containerapp update -g movate-dev-rg -n movate-dev-grafana-oss \
  --set-env-vars GF_INSTALL_PLUGINS=grafana-clickhouse-datasource
#   then POST /api/datasources for postgres-movate (movateadmin / pg-password,
#   sslmode=require) and clickhouse-otel (host movate-dev-clickhouse, native :9000).
```

## Follow-ups (to productionize)
1. **Bicep**: add a `containerapp-clickhouse.bicep` module + a `clickhouseEndpoint`
   param on `containerapp-otel-collector.bicep` that injects the exporter; wire
   both into `main.bicep` so a full redeploy reproduces this.
2. **Durability**: ClickHouse is on ephemeral container storage — move to an Azure
   Files mount or a managed/replicated CH before this is more than a demo.
3. **Least privilege**: the Grafana Postgres datasource uses `movateadmin`; create
   a read-only PG role for Grafana + rotate (the admin password is currently in
   the datasource config).
4. **Grafana provisioning as code**: the datasources were created via the API;
   move them to file-based provisioning (like `infra/otel-collector/grafana/...`)
   so they survive a Grafana redeploy.
