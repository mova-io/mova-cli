# Grafana dashboards — mdk (movate-cli)

Five versioned Grafana dashboards shipped with the repo. Import them into any
Grafana instance that scrapes mdk's OTel metrics via Prometheus.

## Prerequisites

- **Grafana** 9.0+ (OSS or Cloud).
- **Prometheus** configured to scrape the OTLP → Prometheus stream from
  the mdk runtime (`MOVATE_TRACE_SINK=otlp` + `OTEL_EXPORTER_OTLP_ENDPOINT`
  set on the runtime/worker). See `dashboards/README.md` for the full env-var
  list.
- **Infinity datasource** (plugin ID `yesoreyeram-infinity-datasource`) for
  the `mdk-exec-summary` fleet-health and insights panels. Install via
  *Administration → Plugins → search "Infinity"*.

## Importing a dashboard

1. Open Grafana → **Dashboards → New → Import**.
2. Click **Upload JSON file** and select the file you want, or paste the JSON.
3. Map the `Prometheus` input to your Prometheus datasource.
4. For `mdk-exec-summary`, also map `Insights API` to your Infinity datasource
   (point it at the mdk runtime base URL, e.g. `http://mdk-runtime:8000`).
5. Click **Import**.

To provision dashboards as code, drop the JSON files into your Grafana
dashboards directory and reference them from a `provisioning/dashboards/`
YAML file (see `dashboards/README.md` → "Import — Grafana + Prometheus").

## Which dashboard to open first (demo order)

| Order | File | What it shows |
| ----- | ---- | ------------- |
| 1 | `mdk-exec-summary.json` | Fleet health, spend, SLO error budget, health-trend insight tables |
| 2 | `mdk-golden-signals.json` | Latency p50/p95/p99, error rate, throughput, dead-letter |
| 3 | `mdk-cost.json` | Per-agent / per-tenant cost breakdown |
| 4 | `mdk-runtime-overview.json` | Queue depth, DB pool saturation, pod autoscale |
| 5 | `mdk-dead-letter.json` | Dead-letter rate / share / backlog (operate with `mdk jobs dead-letter`) |

Open exec-summary first for the leadership "one screen", then drill down via
the top-right links.

## Seeding demo data

```bash
# 7-day window (default) with voice-turn rows for Deepgram/Cartesia panels:
mdk demo seed --with-voice

# 30-day richer history + voice:
mdk demo seed --days 30 --with-voice

# Reproducible reset (same seed every time):
mdk demo seed --clear-first --seed 1337 --with-voice
```

After seeding, point Grafana at the Prometheus that scrapes the mdk runtime
and the dashboards light up immediately. The exec-summary fleet-health gauge
and insight tables also populate once you run the observability analyst
(`mdk dev analyze` or the nightly cron — see ADR 047).

## Clearing demo data

```bash
mdk demo clear      # interactive prompt
mdk demo clear --yes  # skip the confirmation
```

Deletes all rows whose `tenant_id` starts with `demo-` (runs, evals,
failures, voice turns). Real tenant data is never touched.
