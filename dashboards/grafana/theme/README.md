# Movate theme — palette + reusable panels + kiosk variant

The shared **Movate brand palette** for MDK Grafana dashboards, two reusable
panel snippets (cost forecast, SLO error budget), and a **TV / kiosk** variant
of the executive dashboard for an ops wall or a demo screen.

This directory is **additive**. It is applied to the new exec dashboard
(`../mdk-exec-summary.json`) and documented here for retrofitting onto the
existing #518 / #555 dashboards — those are **not** rewritten (out of scope).

## The palette

Machine-readable values live in [`palette.json`](palette.json). The named
colors:

| Token | Hex | Use |
| --- | --- | --- |
| `primary` | `#2D6CDF` | Movate blue — headline stats, info series |
| `primary_dark` | `#1B3A8C` | dark blue — backgrounds, accents |
| `accent` | `#5BC0EB` | sky — secondary series |
| `success` / `ok` | `#2BB673` | green — healthy, in-SLO |
| `warning` / `degraded` / `forecast` | `#F2A93B` | amber — degraded, projection line |
| `danger` / `breached` | `#D64550` | red — breach, budget line |
| `danger_dark` | `#7A1620` | deep red — critical severity cell |
| `neutral_bg` | `#11161D` | dashboard background (use Grafana `dark` style) |
| `text` | `#E6EAF0` | primary text |
| `muted` | `#8A94A6` | secondary text |

The **semantic** mapping (ok / degraded / breached / info / forecast) is the
one to reach for — it keeps "green = healthy, amber = watch, red = bad"
consistent across every panel regardless of the underlying metric.

`palette.json` also ships ready-made `thresholds[]` step arrays for the three
recurring scales (`health_score`, `success_rate`, `error_budget_remaining`) so
you can paste them straight into a panel's `fieldConfig.defaults.thresholds`.

## How to apply it (Grafana has no global theme variable)

Grafana has no dashboard-level "palette variable" primitive — color is set
**per panel**. There are two anchors:

1. **Single-color panels** (stat, single-series timeseries): set
   `fieldConfig.defaults.color = {"mode": "fixed", "fixedColor": "#2D6CDF"}`.
2. **Thresholded panels** (gauge, stat-with-thresholds, burndown): set
   `fieldConfig.defaults.color = {"mode": "thresholds"}` and use the matching
   `thresholds.steps[]` from `palette.json` (green/amber/red on the brand
   hexes, not Grafana's defaults).

For a **dashed forecast / projection series**, add a per-series override
matching the series name to `lineStyle.dash = [10, 10]` + `fixedColor`
`#F2A93B` (see the cost-forecast snippet).

Always set the dashboard `"style": "dark"` so the neutral background reads as
the Movate dark theme.

## Reusable panel snippets

Both are **panel objects**, not full dashboards — drop them into a dashboard's
`panels[]` array and fix up the panel `id` (must be unique in the host
dashboard) and `gridPos`.

| File | What | Metrics (real #518 catalog) |
| --- | --- | --- |
| [`cost-forecast-panel.json`](cost-forecast-panel.json) | Cumulative spend + linear projection to period end vs a budget line | `mdk.run.cost_usd` |
| [`slo-error-budget-panel.json`](slo-error-budget-panel.json) | Google-SRE error-budget burndown + a "remaining" gauge | `mdk.jobs.completed` |

Both expect the host dashboard to define the variables they read:

- cost forecast → `monthly_budget` (custom variable, the budget in `$`).
- SLO budget → `slo_target` (custom variable, the success-rate SLO, default
  `0.99`).

The exec dashboard (`../mdk-exec-summary.json`) already defines both; the
snippets are byte-identical to its panels `21` (cost) and `30`/`31` (SLO) so
the standalone copies can't drift from the live ones.

### Forecast methodology

`predict_linear(mdk_run_cost_usd_total[7d], 86400 * 30)` fits a least-squares
line over the last 7 days of the cumulative cost counter and projects it 30
days forward. Where the dashed projection crosses the budget line is the
**projected breach date**. It is deliberately simple (linear, no seasonality) —
the point is an executive early-warning, not a forecast model. Cost is
*observed, not paged* in MDK (ADR 017 — there is no cost SLO alert), so this
panel is the leadership signal, not an alerting rule.

### Error-budget methodology

SLO = `$slo_target` success (default 99%), so the budget is `1 - 0.99 = 1%` of
runs may fail this period. **Consumed** = observed non-success runs ÷ allowed
non-success runs; `1.0` means the budget is gone. **Remaining** = `1 -
consumed`, floored at 0. Retune by changing `$slo_target`.

## TV / kiosk variant

[`mdk-exec-summary-kiosk.json`](mdk-exec-summary-kiosk.json) is the ops-wall /
demo-screen cut of the exec dashboard: 30s auto-refresh, big single-stat
panels (80px value text), no per-panel triage prose, Movate palette, dark
style. It drops the ADR-047-fed panels (fleet health, wins, risks) because
those need the insights API and small text a wall can't read; it keeps the
live OTel-metric panels (success rate, spend, runs, error budget, forecast).

**Open it in kiosk mode** so the wall shows just the panels:

- Append `?kiosk=tv` to the dashboard URL — hides the toolbar, keeps the
  variable/time row.
- Append `?kiosk` — hides all chrome (full wall mode).
- Or click the **monitor / TV icon** in Grafana's top bar.

Pair it with `mdk demo seed` (see below) so the wall tells a believable story
during a demo.

## Lighting it up for a demo

```
mdk demo seed --agents 6 --tenants 3 --days 30   # synthetic, demo-tagged fleet
# ... point Grafana at the Prometheus that scrapes the OTLP stream ...
mdk demo clear                                    # purge when done
```

Every seeded row is tagged (`tenant=demo-*` + `input.__mdk_demo__=true`) and
fully purgeable — it never co-mingles with real telemetry. See
`src/movate/cli/demo_cmd.py` and `src/movate/core/demo/`.

## How to retrofit the palette onto the existing #518 / #555 dashboards

Those dashboards (`../mdk-*.json`, `../insights/*.json`, `../movate/*.json`)
are intentionally **left as-is** in this change. To bring them onto the Movate
palette later, per panel:

1. Replace Grafana's default `palette-classic` / default threshold colors with
   the brand hexes from `palette.json` (use the semantic mapping).
2. For the threshold scales, paste the matching `thresholds.steps[]` array.
3. Set the dashboard `"style": "dark"`.

This is a mechanical, panel-by-panel edit; do it in a **separate, additive PR**
per the one-PR-one-responsibility rule (CLAUDE.md). Nothing about the metric
queries changes — only `fieldConfig` colors — so the anti-drift guards
(`tests/test_grafana_dashboards.py`, `tests/test_dashboards_metric_names.py`)
stay green.

## Cross-reference

- Exec dashboard — `../mdk-exec-summary.json` (applies this palette).
- Catalog / metric source of truth — `docs/observability.md`,
  `src/movate/tracing/metrics.py:METRIC_NAMES`.
- Existing dashboards (not modified) — `../mdk-golden-signals.json` (#518),
  `../insights/` (#555), `../movate/` (ADR 039 fleet view).
