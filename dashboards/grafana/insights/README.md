# mdk insight-fed dashboards (Observability Intelligence, ADR 047)

These dashboards are the **intelligence layer** on top of the raw-metric
golden-signal dashboards shipped in **#518** (`dashboards/grafana/*.json` +
`infra/azure-monitor/workbooks/*.workbook.json`). Where #518 answers *"what are
the numbers?"*, these answer **"what happened, which project is unhealthy, and
what are the top 3 things to look at today?"**

They **complement, not replace** #518: the narrative/health/Top-3 rows read the
**ADR 047** Observability Intelligence API; the time-series charts read the
**same OTel metrics** #518 uses, so the two surfaces stay consistent.

| File | Surface | Reads from |
| --- | --- | --- |
| `mdk-mission-control.json` | Grafana dashboard | **Single-pane operator wall** — Insights API (health gauge + plain-English anomalies + open-anomaly count) + Prometheus (live spend / throughput / error-rate / p95 latency / in-flight). Wired to the **real** response shapes; lights up against `mdk demo seed`. |
| `mdk-daily-insights.json` | Grafana dashboard | Insights API (narrative, health, Top 3, anomaly annotations) + Prometheus (golden-signal charts) |
| `mdk-project-health.json` | Grafana dashboard | Insights API (score history, trends, failure clusters) + Prometheus (ground-truth panel) |
| `../../infra/azure-monitor/workbooks/insights.workbook.json` | Azure Monitor workbook | Insights API (custom-endpoint / pasted digest) + `AppMetrics`/`AppDependencies` (raw) |

> ## ADR 047 is now on `main` — `mdk-mission-control.json` is wired to the real shapes
>
> The legacy gap banner below was written when the `/observability` endpoints
> were still unmerged; the older dashboards in this pack carry per-panel
> "documented-gap" notes from that era. **`mdk-mission-control.json` is wired to
> the *actual* response shapes** of the now-shipped API and verified against
> `movate.runtime.schemas` + `movate.core.observability.analyst`:
>
> - `GET /api/v1/observability/health` returns a **flat** object
>   `{project_id, date, health_score, narrative_digest, anomaly_count,
>   has_insight}` → the health gauge + open-anomaly tile use `root_selector: ""`.
> - `GET /api/v1/observability/insights` returns
>   `{insights: [{date, health_score, anomalies: [{metric, severity, value,
>   baseline, z, note}], top_failures, usage_rollup, trends, narrative_digest}],
>   count}` → the **plain-English anomaly table** uses
>   `root_selector: "insights.anomalies"` and surfaces the analyst's
>   human-readable `note` (e.g. *"cost is 12.9 sigma above the 3-day baseline"*),
>   which is computed **pure (no LLM)**, so it populates **offline + free** right
>   after `mdk demo seed` (the seed runs the analyst under project `default`).
>
> Honest caveat: the **in-flight** tile (`mdk.jobs.in_flight`) is a *live*
> gauge — on a static/replayed demo it reads 0; it animates only while the
> runtime is actively draining work.

> ## Dependency: ADR 047 is not on `main` yet
>
> The insight-fed panels read `GET /api/v1/observability/insights` and
> `GET /api/v1/observability/health`, delivered by **ADR 047** (the
> `observability_insights` store + the `/observability` endpoints). **At the
> time this pack shipped, ADR 047 is not on `main`.** Until it is:
>
> - The narrative banner, health gauges, Top-3 table, score history, trends, and
>   anomaly/deploy annotations render **empty / placeholder** (Grafana shows a
>   datasource error on the insight panels). **This is expected, not a
>   misconfiguration** — each affected panel carries a gap note.
> - The **Prometheus metric charts work standalone today** (they reuse #518's
>   catalog), so the dashboards are still useful before ADR 047 lands.
>
> Wire the JSON/Infinity datasource (below) once ADR 047 is deployed.

## What you need

- **Prometheus datasource** — the same one #518 uses (OTLP → Prometheus stream).
- **A JSON datasource** for the insights API. Either:
  - **Infinity** (`yesoreyeram-infinity-datasource`) — recommended; the
    dashboards reference this plugin id, and it handles JSON arrays, header auth,
    and JSONPath/`root_selector` extraction. Install with
    `grafana-cli plugins install yesoreyeram-infinity-datasource`.
  - or **JSON API** (`marcusolsson-json-datasource` /
    `grafana-json-datasource`) — works too; you'll re-point each insight panel's
    datasource and adjust the field selectors to that plugin's syntax.

## Wiring the JSON/Infinity datasource to the insights API

1. **Install the plugin** (above) and restart Grafana.
2. **Add the datasource** (Connections → Data sources → *Infinity*):
   - **Name:** `Insights API` (the dashboards bind to it via the `DS_INSIGHTS`
     dashboard variable, so the exact name only needs to be selectable at import).
   - **Base URL / Allowed hosts:** your runtime host, e.g.
     `https://<runtime-host>` (Infinity: set this under *URL, Headers & Params* /
     *Allowed hosts* so the relative paths the panels use —
     `/api/v1/observability/insights`, `/api/v1/observability/health` — resolve).
   - **Auth header:** add a custom HTTP header
     `Authorization: Bearer <token>`. The runtime's `/api/v1` is authenticated
     the same way as the rest of the runtime API (ADR 047 reuses the existing
     `/api/v1` auth — see `docs/architecture-principles.md`). **Do not** hardcode
     the token in the dashboard JSON; it lives on the datasource.
3. **Import the dashboards** (`mdk-daily-insights.json`,
   `mdk-project-health.json`). At import, map `DS_PROMETHEUS` → your Prometheus
   datasource and `DS_INSIGHTS` → the `Insights API` datasource.
4. **Template variables** `project` / `tenant` / `environment` populate from
   Prometheus label values (and scope the insight queries via Infinity
   `filters`). Set `environment` to your deployment env.

### Endpoint shapes the panels assume (ADR 047)

These are the response fields the panels read (documented here so ADR 047 and
this pack stay in sync; adjust selectors if ADR 047 finalizes different names):

- `GET /api/v1/observability/insights` →
  `{ narrative_digest: "<markdown>", health: [{project, health_score}],
  anomalies: [{detected_at, metric, severity, explanation, trace_id, run_id,
  project, environment}], top_failures: [{signature, count, share, first_seen,
  last_seen, trace_id, run_id, project}], watchlist: [{severity, project, title,
  explanation, count, trace_id, run_id}], trends: {cost: [{ts, value,
  delta_pct}], latency_p95: [...], quality: [...]}, history: [{ts, project,
  health_score, environment}], deploys: [{deployed_at, version, environment}] }`
- `GET /api/v1/observability/health` →
  `{ projects: [{project, health_score, tenant, environment}] }`

The `watchlist` is the API's pre-merged + pre-sorted "Top 3" (anomalies +
top_failures by severity); the dashboards also keep raw `anomalies` /
`top_failures` for the annotation + failure-cluster panels.

## Health-score thresholds

Composite `health_score` is `0–100` (ADR 047 blends success rate,
latency-vs-baseline, cost-vs-baseline, and eval pass-rate). All gauges/tiles use:

| Band | Range | Color | Meaning |
| --- | --- | --- | --- |
| Healthy | `>= 80` | green | nominal |
| Degraded | `50–79` | amber/yellow | check narrative + Top 3 |
| Unhealthy | `< 50` | red | SLO breach or hard anomaly |

The **quality trend** sparkline inverts these (it's a pass-rate %, where *lower
is worse*): red `< 80%`, amber `80–94%`, green `>= 95%`.

## Anomaly severity → color mapping

Used by the Top-3 table, the failure-cluster table, and the anomaly annotations:

| Severity | Color |
| --- | --- |
| `critical` | dark-red |
| `high` | red |
| `medium` | orange |
| `low` | yellow |

## How anomaly annotations are sourced

Both Grafana dashboards define **dashboard annotation queries** (under
`annotations.list`) bound to the `Insights API` datasource:

- **Insight anomalies** (red markers) — from `anomalies[]` on
  `GET /api/v1/observability/insights`. Each item maps:
  `detected_at` → marker time, `explanation` → hover text, `severity` → tag
  (color), `metric` → title. Hover a marker on any golden-signal chart to read
  the explanation; click through via the panel's Jaeger link or the Top-3 table.
- **CalVer deploys** (blue markers, on `mdk-daily-insights.json`) — from
  `deploys[]` (`deployed_at` → time, `version` like `2026.5.28.1` → text), so you
  can line an anomaly up against the release that introduced it.

Because they're *dashboard* annotations (not per-panel queries), they overlay
**every** time-series panel automatically. Until ADR 047 ships, both queries
return nothing and the charts render with their metric lines only.

### Rendering the live narrative markdown

The core Grafana **Text** panel can't template a datasource field, so the
narrative banner ships as static placeholder text. To render the **live**
`narrative_digest`, swap that panel for a community **Business Text** (formerly
**Dynamic Text**, `marcusolsson-dynamictext-panel`) or **HTML/Markdown** panel
bound to `${DS_INSIGHTS}` with `root_selector: narrative_digest`. This is an
optional plugin — the dashboards work without it (placeholder text), and the
Azure workbook offers a paste-the-digest path that needs no plugin.

## Drill-down links

- **Top 3 / failure clusters** → Jaeger trace (`trace_id`) + the matching run
  (the project-health dashboard / the run detail).
- **Failure clusters** → the **NL-query UI** (`/a/mdk-nl-query`) pre-filled with
  the cluster signature, for conversational follow-up (see `mdk dev` /
  ADR 025 lineage).
- **Golden-signal charts** → Jaeger filtered to slow/failed `agent.execute`
  (same links as #518).

## Anti-drift test

`tests/test_insight_dashboards.py` parses every file in this pack, asserts the
required top-level keys, and checks that any raw-metric `expr` references a name
in the #518 catalog allow-list — while **tolerating** the text/table/gauge
panels that read from the insights JSON datasource (they have no metric `expr`).

## See also

- **#518 raw-metric dashboards:** `dashboards/grafana/mdk-golden-signals.json`,
  `mdk-cost.json`, `mdk-runtime-overview.json`, `mdk-queue-and-pool.json`, and the
  `infra/azure-monitor/workbooks/operator|platform|eval-and-drift|tenant-ops`
  workbooks. **These complement those — open the raw dashboards for the numbers,
  these for the story.**
- `dashboards/README.md` — the dashboards-as-code overview + metric catalog.
- ADR 047 — Observability Intelligence (the `/observability` API these consume).
- ADR 031 — reporting & dashboards (the dashboards-as-code posture).
