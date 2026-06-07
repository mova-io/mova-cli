"""Generate the 'MDK — Live Runtime (Azure Monitor)' Grafana dashboard JSON.

The live dev dashboard (uid ``mdk-demo`` on movate-dev-grafana-oss) started as 4
bare Container-App infra panels and was created ad-hoc in the instance (NOT
version-controlled, ``provisioned: false`` → lost on redeploy). This generator
makes it a governed artifact and expands it into a real runtime view:

  * Runtime health (infra)  — Azure Monitor platform metrics for api + worker(s)
  * App throughput & latency — App Insights spans/metrics via Log Analytics KQL
  * Cost & usage            — LLM tokens / cost / in-flight jobs
  * Database connection pool — the mdk.db.pool.* gauges (LIVE data today)

Run:  python scripts/gen_live_runtime_dashboard.py
Out:  dashboards/grafana/azure/mdk-live-runtime.json  (import with overwrite=true)

The Azure-specific ids below are the movate-dev instance's; override via env for
another environment (AZ_SUB / AZ_RG / GRAFANA_AZMON_DS_UID / AZ_LA_WORKSPACE_ID).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SUB = os.environ.get("AZ_SUB", "8fab0f8f-b577-45d7-a485-ec32f73b22be")
RG = os.environ.get("AZ_RG", "movate-dev-rg")
DS_UID = os.environ.get("GRAFANA_AZMON_DS_UID", "ffnrfwjnew5xcc")
WS_ID = os.environ.get(
    "AZ_LA_WORKSPACE_ID",
    f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.OperationalInsights/workspaces/movate-dev-logs",
)
APPS = ["movate-dev-api", "movate-dev-worker", "movate-dev-temporal-worker"]

DS = {"type": "grafana-azure-monitor-datasource", "uid": DS_UID}
CA_NS = "microsoft.app/containerapps"

# Mutable counter holder (avoids a module-level `global` statement).
_ID = {"n": 0}


def _next_id() -> int:
    _ID["n"] += 1
    return _ID["n"]


def _opts() -> dict:
    # Golden-signals legend: a TABLE legend that surfaces last / max / mean per
    # series, so an operator reads the current value AND the recent peak without
    # hovering. `tooltip: multi` keeps all series aligned on hover.
    return {
        "legend": {
            "displayMode": "table",
            "placement": "bottom",
            "calcs": ["lastNotNull", "max", "mean"],
        },
        "tooltip": {"mode": "multi"},
    }


def _grid(x: int, y: int, w: int, h: int) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


def row(title: str, y: int) -> dict:
    return {
        "type": "row",
        "id": _next_id(),
        "title": title,
        "collapsed": False,
        "gridPos": _grid(0, y, 24, 1),
        "panels": [],
    }


def _thresholds(steps: list[tuple[str, float | None]]) -> dict:
    """Build a Grafana absolute-threshold config from (color, value) steps.

    The first step's value must be ``None`` (the base band). Used to colour
    saturation / error panels green→amber→red so an at-a-glance scan flags a
    panel that has crossed an operational limit (golden-signals: saturation +
    errors should be visually loud).
    """
    return {
        "mode": "absolute",
        "steps": [{"color": c, "value": v} for c, v in steps],
    }


def _fieldcfg(unit: str | None, thresholds: dict | None = None) -> dict:
    custom: dict = {"drawStyle": "line", "fillOpacity": 10, "showPoints": "never"}
    defaults: dict = {"custom": custom}
    if unit:
        defaults["unit"] = unit
    if thresholds is not None:
        defaults["thresholds"] = thresholds
        defaults["color"] = {"mode": "thresholds"}
        # Render the threshold bands as dashed lines on timeseries panels.
        custom["thresholdsStyle"] = {"mode": "dashed"}
    return {"defaults": defaults, "overrides": []}


def metric_panel(
    title: str,
    metric: str,
    agg: str,
    *,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str | None = None,
    apps: list[str] | None = None,
    description: str = "",
    thresholds: dict | None = None,
) -> dict:
    apps = apps or APPS
    # ONE single-resource target PER app — NOT one multi-resource target.
    # A multi-resource Azure Monitor query uses the subscription-level batch
    # API, which needs Monitoring Reader at SUBSCRIPTION scope; the Grafana MSI
    # only has it at the resource-GROUP scope, so a multi-resource query 403s
    # ("AuthorizationFailed") → "No data". Per-resource queries hit the
    # per-resource metrics API, which the RG-scoped role covers. (Grafana renders
    # one series per target — same visual, working permission.)
    targets = [
        {
            "refId": chr(ord("A") + i),
            "datasource": DS,
            "queryType": "Azure Monitor",
            "subscription": SUB,
            "azureMonitor": {
                "metricNamespace": CA_NS,
                "metricName": metric,
                "aggregation": agg,
                # Grafana's Azure Monitor query model field is `timeGrain`
                # (a string like "auto"/"PT1M"). The earlier `timeGrainUnit` was
                # silently ignored → no interval sent → panels rendered "No data".
                "timeGrain": "auto",
                "resources": [{"resourceGroup": RG, "resourceName": a}],
            },
        }
        for i, a in enumerate(apps)
    ]
    return {
        "type": "timeseries",
        "id": _next_id(),
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": _fieldcfg(unit, thresholds),
        "options": _opts(),
        "targets": targets,
    }


def kql_panel(
    title: str,
    query: str,
    *,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str | None = None,
    viz: str = "timeseries",
    result: str = "time_series",
    description: str = "",
    thresholds: dict | None = None,
) -> dict:
    panel = {
        "type": viz,
        "id": _next_id(),
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": _fieldcfg(unit, thresholds),
        "options": _opts(),
        "targets": [
            {
                "refId": "A",
                "datasource": DS,
                "queryType": "Azure Log Analytics",
                "subscription": SUB,
                "azureLogAnalytics": {"resources": [WS_ID], "query": query, "resultFormat": result},
            }
        ],
    }
    if viz == "table":
        panel["options"] = {"showHeader": True}
    return panel


panels: list[dict] = []
y = 0

# ── Runtime health (infra) — Azure Monitor platform metrics ──────────────────
panels.append(row("Runtime health — Container Apps (api · worker · temporal-worker)", y))
y += 1
panels += [
    metric_panel(
        "Requests / min (api)",
        "Requests",
        "Total",
        x=0,
        y=y,
        apps=["movate-dev-api"],
        description="**Traffic** (golden signal). Inbound HTTP requests to the api "
        "Container App. A flat line at zero usually means no callers — or the app "
        "is down (cross-check Replica count + Restart count).",
    ),
    metric_panel(
        "CPU usage (nanocores)",
        "UsageNanoCores",
        "Average",
        x=12,
        y=y,
        description="**Saturation** (golden signal). Average CPU per app in "
        "nanocores (1e9 = 1 vCPU). Sustained near the per-replica limit drives "
        "autoscaling and latency.",
    ),
]
y += 8
panels += [
    metric_panel(
        "Memory working set",
        "WorkingSetBytes",
        "Average",
        x=0,
        y=y,
        unit="bytes",
        description="**Saturation** (golden signal). Resident memory per app. A "
        "steady climb that never drops is the classic leak signature; watch for it "
        "alongside RestartCount (OOM kills).",
    ),
    metric_panel(
        "Replica count",
        "Replicas",
        "Average",
        x=12,
        y=y,
        description="Active replica count per app (KEDA/HTTP autoscale). Confirms "
        "scale-out under load and scale-to-zero when idle.",
    ),
]
y += 8
panels += [
    metric_panel(
        "Restart count (crash-loop watch)",
        "RestartCount",
        "Maximum",
        x=0,
        y=y,
        description="**Errors** (golden signal). Container restarts — any sustained "
        "non-zero value is a crash loop (bad config, OOM, failing health probe). "
        "Amber ≥1, red ≥3 within the window.",
        thresholds=_thresholds([("green", None), ("yellow", 1), ("red", 3)]),
    ),
    metric_panel(
        "Rx/Tx bytes",
        "RxBytes",
        "Total",
        x=12,
        y=y,
        unit="bytes",
        description="Network ingress bytes per app — a coarse proxy for payload "
        "volume; useful to correlate traffic spikes with bandwidth.",
    ),
]
y += 8

# ── App throughput & latency — App Insights via Log Analytics KQL ────────────
panels.append(row("App throughput & latency — agent/job telemetry (App Insights)", y))
y += 1
panels += [
    kql_panel(
        "Jobs completed / 5m by status",
        'AppMetrics\n| where Name == "mdk.jobs.completed"\n'
        '| extend status = tostring(Properties["status"])\n'
        "| summarize Jobs = sum(Sum) by status, bin(TimeGenerated, 5m)\n"
        "| order by TimeGenerated asc",
        x=0,
        y=y,
        description="**Traffic + Errors** (golden signals). Completed jobs per 5m "
        "split by terminal status — `completed` vs `failed` series side by side is "
        "the quickest health read on the worker.",
    ),
    kql_panel(
        "agent.execute latency p50 / p95 (ms)",
        'AppDependencies\n| where Name == "agent.execute"\n'
        "| summarize p50 = percentile(DurationMs, 50), p95 = percentile(DurationMs, 95) "
        "by bin(TimeGenerated, 5m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
        unit="ms",
        description="**Latency** (golden signal). p50/p95 of the agent execution "
        "span. p95 is the tail your slowest users feel; a widening p95-vs-p50 gap "
        "means variance (cold starts, a slow downstream).",
    ),
]
y += 8
panels += [
    kql_panel(
        "agent.execute error rate %",
        'AppDependencies\n| where Name == "agent.execute"\n'
        "| summarize total = count(), failed = countif(Success == false) "
        "by bin(TimeGenerated, 5m)\n"
        "| extend ErrorPct = iff(total == 0, 0.0, round(100.0 * failed / total, 2))\n"
        "| project TimeGenerated, ErrorPct\n| order by TimeGenerated asc",
        x=0,
        y=y,
        unit="percent",
        description="**Errors** (golden signal). Share of failed agent.execute "
        "spans per 5m. Amber ≥1%, red ≥5% — the SLO-facing error budget burn line.",
        thresholds=_thresholds([("green", None), ("yellow", 1), ("red", 5)]),
    ),
    kql_panel(
        "Recent failed agent.execute (last 50)",
        'AppDependencies\n| where Name == "agent.execute" and Success == false\n'
        "| project TimeGenerated, DurationMs, ResultCode, OperationId, AppRoleInstance\n"
        "| order by TimeGenerated desc\n| take 50",
        x=12,
        y=y,
        viz="table",
        result="table",
        description="The 50 most-recent failed executions with ResultCode + "
        "OperationId — the drill-down companion to the error-rate panel; copy an "
        "OperationId to trace the failing run end-to-end.",
    ),
]
y += 8

# ── Cost & usage ─────────────────────────────────────────────────────────────
panels.append(row("Cost & usage — LLM tokens / spend / in-flight", y))
y += 1
panels += [
    kql_panel(
        "LLM tokens / 5m",
        'AppMetrics\n| where Name == "mdk.run.tokens"\n'
        "| summarize Tokens = sum(Sum) by bin(TimeGenerated, 5m)\n| order by TimeGenerated asc",
        x=0,
        y=y,
        description="Total LLM tokens (prompt + completion) consumed per 5m across "
        "all runs. The volume driver behind the cost panel to its right.",
    ),
    kql_panel(
        "LLM cost (USD) / 5m",
        'AppMetrics\n| where Name == "mdk.run.cost_usd"\n'
        "| summarize CostUSD = sum(Sum) by bin(TimeGenerated, 5m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
        unit="currencyUSD",
        description="Modeled LLM spend per 5m (priced from the model pricing table). "
        "A spend spike with flat traffic points at a pricier model or larger "
        "context.",
    ),
]
y += 8
panels += [
    kql_panel(
        "Jobs in-flight",
        'AppMetrics\n| where Name == "mdk.jobs.in_flight"\n'
        "| summarize InFlight = avg(Sum) by bin(TimeGenerated, 1m)\n| order by TimeGenerated asc",
        x=0,
        y=y,
        w=24,
        description="**Saturation** (golden signal). Jobs currently executing on the "
        "worker. A rising floor that never drains means intake is outpacing "
        "throughput — scale workers or shed load.",
    ),
]
y += 8

# ── Database connection pool — LIVE gauges (ADR 034 D3) ──────────────────────
panels.append(row("Database connection pool — asyncpg saturation (ADR 034 D3)", y))
y += 1
panels += [
    kql_panel(
        "Pool in-use vs max",
        'AppMetrics\n| where Name in ("mdk.db.pool.in_use", "mdk.db.pool.max")\n'
        "| summarize Value = avg(Sum) by Name, bin(TimeGenerated, 1m)\n"
        "| order by TimeGenerated asc",
        x=0,
        y=y,
        description="**Saturation** (golden signal). Active asyncpg connections "
        "(`in_use`) against the configured ceiling (`max`). `in_use` riding `max` "
        "means the pool is the bottleneck — raise the pool or the DB tier.",
    ),
    kql_panel(
        "Pool waiting (blocked acquirers — early warning)",
        'AppMetrics\n| where Name == "mdk.db.pool.waiting"\n'
        "| summarize Waiting = max(Sum) by bin(TimeGenerated, 1m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
        description="**Saturation** (golden signal, leading indicator). Coroutines "
        "blocked waiting for a pool connection. Any sustained non-zero value means "
        "pool exhaustion is already adding latency. Amber ≥1, red ≥5.",
        thresholds=_thresholds([("green", None), ("yellow", 1), ("red", 5)]),
    ),
]
y += 8
panels += [
    kql_panel(
        "Pool idle / size",
        'AppMetrics\n| where Name in ("mdk.db.pool.idle", "mdk.db.pool.size")\n'
        "| summarize Value = avg(Sum) by Name, bin(TimeGenerated, 1m)\n"
        "| order by TimeGenerated asc",
        x=0,
        y=y,
        w=24,
        description="Idle (warm, unused) connections vs total pool `size`. Healthy "
        "headroom keeps a few idle; persistently zero idle while `waiting` climbs "
        "confirms the pool is undersized.",
    ),
]

dashboard = {
    "uid": "mdk-demo",
    "title": "MDK — Live Runtime (Azure Monitor)",
    "schemaVersion": 39,
    "version": 0,
    "editable": True,
    "refresh": "30s",
    "time": {"from": "now-3h", "to": "now"},
    "timezone": "browser",
    "tags": ["mdk", "runtime", "azure-monitor"],
    "templating": {"list": []},
    "annotations": {"list": []},
    "panels": panels,
}

out = (
    Path(__file__).resolve().parent.parent
    / "dashboards"
    / "grafana"
    / "azure"
    / "mdk-live-runtime.json"
)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(panels)} panels)")
