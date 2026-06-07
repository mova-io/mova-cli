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
    return {
        "legend": {"displayMode": "list", "placement": "bottom"},
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


def _fieldcfg(unit: str | None) -> dict:
    defaults: dict = {"custom": {"drawStyle": "line", "fillOpacity": 10, "showPoints": "never"}}
    if unit:
        defaults["unit"] = unit
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
) -> dict:
    apps = apps or APPS
    return {
        "type": "timeseries",
        "id": _next_id(),
        "title": title,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": _fieldcfg(unit),
        "options": _opts(),
        "targets": [
            {
                "refId": "A",
                "datasource": DS,
                "queryType": "Azure Monitor",
                "subscription": SUB,
                "azureMonitor": {
                    "metricNamespace": CA_NS,
                    "metricName": metric,
                    "aggregation": agg,
                    "timeGrainUnit": "auto",
                    "resources": [{"resourceGroup": RG, "resourceName": a} for a in apps],
                },
            }
        ],
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
) -> dict:
    panel = {
        "type": viz,
        "id": _next_id(),
        "title": title,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": _fieldcfg(unit),
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
    metric_panel("Requests / min (api)", "Requests", "Total", x=0, y=y, apps=["movate-dev-api"]),
    metric_panel("CPU usage (nanocores)", "CpuUsage", "Average", x=12, y=y),
]
y += 8
panels += [
    metric_panel("Memory working set", "MemoryWorkingSetBytes", "Average", x=0, y=y, unit="bytes"),
    metric_panel("Replica count", "Replicas", "Average", x=12, y=y),
]
y += 8
panels += [
    metric_panel("Restart count (crash-loop watch)", "RestartCount", "Maximum", x=0, y=y),
    metric_panel("Rx/Tx bytes", "RxBytes", "Total", x=12, y=y, unit="bytes"),
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
    ),
    kql_panel(
        "agent.execute latency p50 / p95 (ms)",
        'AppDependencies\n| where Name == "agent.execute"\n'
        "| summarize p50 = percentile(DurationMs, 50), p95 = percentile(DurationMs, 95) "
        "by bin(TimeGenerated, 5m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
        unit="ms",
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
    ),
    kql_panel(
        "LLM cost (USD) / 5m",
        'AppMetrics\n| where Name == "mdk.run.cost_usd"\n'
        "| summarize CostUSD = sum(Sum) by bin(TimeGenerated, 5m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
        unit="currencyUSD",
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
    ),
    kql_panel(
        "Pool waiting (blocked acquirers — early warning)",
        'AppMetrics\n| where Name == "mdk.db.pool.waiting"\n'
        "| summarize Waiting = max(Sum) by bin(TimeGenerated, 1m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
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
