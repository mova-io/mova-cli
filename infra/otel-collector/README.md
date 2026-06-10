# mdk local observability stack (Collector + Jaeger + Prometheus + Grafana)

A runnable docker-compose stack that exercises mdk's real OTel emit path
(`src/movate/tracing/otel.py` + `src/movate/tracing/metrics.py`) without
any Azure footprint. Use it to:

- Validate the in-repo dashboards under `dashboards/grafana/*.json` against
  live data before pushing changes.
- See the spans + metrics mdk actually emits during a `mdk dev` /
  `mdk serve` / `mdk worker` session.
- Iterate on the `dashboards/prometheus/mdk-rules.yaml` rules with a real
  Prometheus rule engine + alert evaluator.

Everything here is **dev-only**: anonymous Grafana admin, no TLS, no auth on
Prometheus or Jaeger. Don't mirror it in production.

## Topology

```
              +-----------------+
              |       mdk       |   OTLP (HTTP :4318 default, gRPC :4317)
              | (runtime/worker)|----------------------+
              +-----------------+                      |
                                                       v
+-----------+              +----------------------------------------+
|  Grafana  | <----------- |          OpenTelemetry Collector       |
|  :3000    |   PromQL     |          (otel/opentelemetry-collector-|
|           |              |           contrib)                     |
+-----------+              +-------------------+--------------------+
      ^                            traces      |       metrics
      |                                        |
      |                                        v
      |                       +------------+   +-----------------+
      |                       |   Jaeger   |   |   Prometheus    |
      +---- proxy / scrape -- |   :16686   |   |   :9090         |
                              |  (UI only) |   |  (scrapes :8889)|
                              +------------+   +-----------------+
```

The exporter half is the only difference between this stack and the Azure
prod path (`infra/azure/modules/containerapp-otel-collector.bicep`): there,
the Collector's pipelines terminate at the `azuremonitor` exporter (ADR 020).
Here, traces go to Jaeger and metrics go to Prometheus. The mdk side is
identical -- the same env vars, the same OTLP payload, the same instrument
names.

## Quickstart

```bash
docker compose -f infra/otel-collector/docker-compose.yml up
```

Then in another shell, point mdk at the local Collector:

```bash
export MOVATE_TRACE_SINK=otlp                          # gate: turns mdk OTel metrics on
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf       # default; gRPC also fine via :4317
export OTEL_SERVICE_NAME=movate-runtime
# Optional but recommended -- splits prod/dev on the workbook + dashboards:
export MOVATE_ENV=local
export OTEL_DEPLOYMENT_ENVIRONMENT=local

# Now exercise mdk:
mdk dev
# or:
mdk serve
mdk worker
```

These env vars are read by `src/movate/tracing/otel.py` and
`src/movate/tracing/metrics.py`. `MOVATE_TRACE_SINK=none` (or unset with no
`OTEL_EXPORTER_OTLP_ENDPOINT`) keeps mdk in no-op mode and the dashboards
will stay empty.

## Open the UIs

- Grafana  http://localhost:3000  — the in-repo dashboards under
  `dashboards/grafana/*.json` auto-provision into the **mdk** folder. No
  manual import.
- Jaeger UI  http://localhost:16686  — pick `movate-runtime` from the
  Service dropdown to see spans (`agent.execute`, `agent.turn[N]`,
  `retrieval.<skill>`, `skill.<name>`, `workflow.execute`, `kb_search`).
- Prometheus  http://localhost:9090  — *Status -> Targets* should show
  `otel-collector:8889` UP. *Status -> Rules* shows the recording +
  alerting rules from `dashboards/prometheus/mdk-rules.yaml`.

## Tear down

```bash
docker compose -f infra/otel-collector/docker-compose.yml down -v
```

(`-v` also wipes the Prometheus TSDB so a new run starts clean.)

## What you should see

When mdk is doing work (any `mdk dev` chat turn, a `mdk serve` request, or a
`mdk worker` dispatch cycle):

| Metric | Where to look | What it should do |
| --- | --- | --- |
| `mdk_jobs_completed_total` | mdk - runtime overview | jumps with every terminal job |
| `mdk_job_duration_ms_milliseconds_*` | same dashboard, latency row | histogram populates; p50/p95/p99 lines render |
| `mdk_jobs_in_flight` | mdk - queue & pool | rises on dispatch, drops on terminal |
| `mdk_run_tokens_total`, `mdk_run_cost_usd_total` | mdk - cost | rises per executed run (only when the run records usage) |
| `mdk_db_pool_*` | mdk - queue & pool | flat/zero on local SQLite (no pool); populated only against a Postgres backend, ADR 034 D3 |

If you don't see traces, double-check `MOVATE_TRACE_SINK` is set to `otlp`
or `both` -- that's the gate `_otlp_metrics_enabled` checks in
`src/movate/tracing/metrics.py`.

## Adding a new dashboard

Drop a new `*.json` into `dashboards/grafana/`, then either restart Grafana
(`docker compose restart grafana`) or wait for the file provider's 30s
re-scan. Make sure every metric the new dashboard references actually exists
in `movate.tracing.metrics.METRIC_NAMES` -- the
`tests/test_grafana_dashboards.py` drift guard fails otherwise (and so does
the existing `tests/test_dashboards_metric_names.py`).

## License note

Grafana OSS is AGPLv3, which is normally excluded from shipped dependencies.
This compose file is a **dev tool** -- it is not bundled into the Python
package, not a shipped dependency, and not subject to
`scripts/check_licenses.py` (which only checks `pyproject.toml`). See
`docs/license-posture.md` for the full posture.

## Unified observability store (ADR 095) — ClickHouse + Postgres

This stack now also prototypes the **unified observability store** (ADR 095):

- **`clickhouse`** — the unified TELEMETRY store. The collector's `clickhouse`
  exporter writes **traces + metrics + logs** into the `otel` database
  (`otel_traces` / `otel_metrics_*` / `otel_logs`), alongside the existing
  Jaeger + Prometheus fanout (additive — nothing breaks).
- **`postgres`** — the BUSINESS OF RECORD (runs, cost ledger, governance),
  seeded from `seed/postgres-bor.sql`. In prod this is the shared Azure Postgres
  `movate` DB; repoint the `postgres-bor` datasource at it (read-only role).
- **Grafana datasources** — `clickhouse-otel` (plugin) + `postgres-bor` (built-in),
  auto-provisioned.
- **`dashboards/grafana/mdk-unified-observability.json`** — one pane over both
  stores; the bottom table is the **cross-store join on `trace_id`** (Postgres
  cost/outcome ⨯ ClickHouse span volume).

Run it, point mdk at the collector (same env as above), make some runs, and open
Grafana → **mdk - unified observability**. The Postgres panels show the seeded
business-of-record; the ClickHouse panels fill as OTLP arrives; the cross-store
join lights up when a run's `trace_id` appears in both stores.

**Azure rollout (ADR 095 D6):** mirror the `clickhouse` exporter into
`infra/azure/modules/containerapp-otel-collector.bicep`, point it at a reachable
ClickHouse (reuse the Langfuse VM's or a managed cluster), and add the two
datasources to the deployed Grafana.
