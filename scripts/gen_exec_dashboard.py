"""Generate the 'MDK — Executive (Business KPIs)' Grafana dashboard JSON.

The business-outcome view for CIOs/COOs/sponsors — NOT token counts. Page-1
answers "is the AI platform delivering value today?" in six hero tiles, then a
trend section tells the story (adoption ↑, automation ↑, cost stable, value ↑).
Engineers keep the live-runtime / temporal technical dashboards separately.

Data: Azure Monitor Log-Analytics KQL over AppMetrics (the working datasource).
Wired from metrics that EXIST today:
  * mdk.workflow.completed {workflow,status,runtime} → volume, success rate,
    outcome mix, automation-by-workflow
  * mdk.workflow.duration_ms {workflow,status}       → resolution time
  * mdk.run.cost_usd                                 → spend → cost/workflow
  * mdk.jobs.completed {status=safety_blocked}       → policy actions blocked

Hours-saved + business-value are BUSINESS ASSUMPTIONS, not metrics — set via
env so each customer can tune them:
  MINUTES_SAVED_PER_WORKFLOW (default 7)   HOURLY_RATE_USD (default 75)

Run:  python scripts/gen_exec_dashboard.py
Out:  dashboards/grafana/azure/mdk-exec.json  (import with overwrite=true)
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
    f"/subscriptions/{SUB}/resourceGroups/{RG}"
    "/providers/Microsoft.OperationalInsights/workspaces/movate-dev-logs",
)
# Business assumptions (per-customer tunable) for the value tiles.
MIN_SAVED = float(os.environ.get("MINUTES_SAVED_PER_WORKFLOW", "7"))
RATE = float(os.environ.get("HOURLY_RATE_USD", "75"))

DS = {"type": "grafana-azure-monitor-datasource", "uid": DS_UID}
_ID = {"n": 0}


def _next_id() -> int:
    _ID["n"] += 1
    return _ID["n"]


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


def _target(query: str, result: str) -> list[dict]:
    return [
        {
            "refId": "A",
            "datasource": DS,
            "queryType": "Azure Log Analytics",
            "subscription": SUB,
            "azureLogAnalytics": {"resources": [WS_ID], "query": query, "resultFormat": result},
        }
    ]


def stat(
    title: str,
    query: str,
    *,
    x: int,
    y: int,
    w: int = 4,
    h: int = 5,
    unit: str | None = None,
    thresholds: list[tuple[float | None, str]] | None = None,
    description: str = "",
) -> dict:
    """A big-number KPI tile with optional green/yellow/red thresholds."""
    field: dict = {"defaults": {}, "overrides": []}
    if unit:
        field["defaults"]["unit"] = unit
    if thresholds:
        field["defaults"]["thresholds"] = {
            "mode": "absolute",
            "steps": [{"value": v, "color": c} for v, c in thresholds],
        }
        field["defaults"]["color"] = {"mode": "thresholds"}
    return {
        "type": "stat",
        "id": _next_id(),
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": field,
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value",
            "colorMode": "value" if thresholds else "none",
            "graphMode": "none",
        },
        "targets": _target(query, "table"),
    }


def timeseries(
    title: str, query: str, *, x: int, y: int, w: int = 12, h: int = 8, unit: str | None = None
) -> dict:
    defaults: dict = {"custom": {"drawStyle": "line", "fillOpacity": 20, "showPoints": "never"}}
    if unit:
        defaults["unit"] = unit
    return {
        "type": "timeseries",
        "id": _next_id(),
        "title": title,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": _target(query, "time_series"),
    }


def viz(title: str, query: str, kind: str, *, x: int, y: int, w: int = 12, h: int = 8) -> dict:
    return {
        "type": kind,
        "id": _next_id(),
        "title": title,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "right"}}
        if kind in ("piechart",)
        else {"showHeader": True},
        "targets": _target(query, "table"),
    }


panels: list[dict] = []
y = 0

# ── Page 1: the six hero tiles (last 30 days) ────────────────────────────────
panels.append(row("Executive summary — last 30 days", y))
y += 1
panels += [
    stat(
        "Automation Success Rate",
        'AppMetrics\n| where Name == "mdk.workflow.completed" | where TimeGenerated > ago(30d)\n'
        '| extend status = tostring(Properties["status"])\n'
        '| summarize total = sum(Sum), ok = sumif(Sum, status == "success")\n'
        "| project Pct = iff(total == 0, 0.0, round(100.0 * ok / total, 1))",
        x=0,
        y=y,
        unit="percent",
        thresholds=[(None, "red"), (75, "orange"), (90, "green")],
        description="Workflows completing successfully — the headline AI-value signal.",
    ),
    stat(
        "Workflows Completed",
        'AppMetrics\n| where Name == "mdk.workflow.completed" | where TimeGenerated > ago(30d)\n'
        "| summarize Total = sum(Sum)",
        x=4,
        y=y,
        description="Adoption — total durable workflows run this month.",
    ),
    stat(
        "Cost per Workflow",
        "AppMetrics\n| where TimeGenerated > ago(30d)\n"
        '| summarize cost = sumif(Sum, Name == "mdk.run.cost_usd"), '
        'wf = sumif(Sum, Name == "mdk.workflow.completed")\n'
        "| project CostPer = iff(wf == 0, 0.0, round(cost / wf, 4))",
        x=8,
        y=y,
        unit="currencyUSD",
        thresholds=[(None, "green"), (0.25, "orange"), (1.0, "red")],
        description="True LLM cost to complete one workflow (total spend / workflows).",
    ),
    stat(
        "Hours Saved",
        'AppMetrics\n| where Name == "mdk.workflow.completed" | where TimeGenerated > ago(30d)\n'
        f"| summarize wf = sum(Sum) | project Hours = round(wf * {MIN_SAVED} / 60.0, 0)",
        x=12,
        y=y,
        unit="h",
        description=f"Labor hours saved = workflows x {MIN_SAVED:g} min each (tunable).",
    ),
    stat(
        "Business Value",
        'AppMetrics\n| where Name == "mdk.workflow.completed" | where TimeGenerated > ago(30d)\n'
        f"| summarize wf = sum(Sum) | project USD = round(wf * {MIN_SAVED} / 60.0 * {RATE}, 0)",
        x=16,
        y=y,
        unit="currencyUSD",
        description=f"Estimated value = hours saved x ${RATE:g}/hr (tunable).",
    ),
    stat(
        "Policy Actions Blocked",
        'AppMetrics\n| where Name == "mdk.jobs.completed" | where TimeGenerated > ago(30d)\n'
        '| extend status = tostring(Properties["status"])\n'
        '| summarize Blocked = sumif(Sum, status == "safety_blocked")',
        x=20,
        y=y,
        description="Governance value — unsafe actions the platform refused to take.",
    ),
]
y += 5

# ── Trends — the story over time ─────────────────────────────────────────────
panels.append(row("Trends", y))
y += 1
panels += [
    viz(
        "Workflow volume / day",
        'AppMetrics\n| where Name == "mdk.workflow.completed"\n'
        "| summarize Workflows = sum(Sum) by bin(TimeGenerated, 1d)\n| order by TimeGenerated asc",
        "timeseries",
        x=0,
        y=y,
    ),
    timeseries(
        "Automation success rate / day (%)",
        'AppMetrics\n| where Name == "mdk.workflow.completed"\n'
        '| extend status = tostring(Properties["status"])\n'
        '| summarize total = sum(Sum), ok = sumif(Sum, status == "success") '
        "by bin(TimeGenerated, 1d)\n"
        "| project TimeGenerated, SuccessPct = iff(total == 0, 0.0, round(100.0 * ok / total, 1))\n"
        "| order by TimeGenerated asc",
        x=12,
        y=y,
        unit="percent",
    ),
]
y += 8
panels += [
    # The killer exec slide: spend vs value on one chart (two series via union).
    timeseries(
        "Cost vs Value / day (USD)",
        "union\n"
        '  (AppMetrics | where Name == "mdk.run.cost_usd"\n'
        '   | summarize V = sum(Sum) by bin(TimeGenerated, 1d) | extend Series = "LLM Cost"),\n'
        '  (AppMetrics | where Name == "mdk.workflow.completed"\n'
        f"   | summarize V = sum(Sum) * {MIN_SAVED} / 60.0 * {RATE} by bin(TimeGenerated, 1d)\n"
        '   | extend Series = "Business Value")\n'
        "| project TimeGenerated, Series, V\n| order by TimeGenerated asc",
        x=0,
        y=y,
        unit="currencyUSD",
    ),
    viz(
        "Workflow outcome mix",
        'AppMetrics\n| where Name == "mdk.workflow.completed" | where TimeGenerated > ago(30d)\n'
        '| extend status = tostring(Properties["status"])\n'
        "| summarize Count = sum(Sum) by status",
        "piechart",
        x=12,
        y=y,
    ),
]
y += 8

# ── Operations — where to focus ──────────────────────────────────────────────
panels.append(row("Operations", y))
y += 1
panels += [
    viz(
        "Automation rate by workflow",
        'AppMetrics\n| where Name == "mdk.workflow.completed" | where TimeGenerated > ago(30d)\n'
        '| extend workflow = tostring(Properties["workflow"]), '
        'status = tostring(Properties["status"])\n'
        '| summarize total = sum(Sum), ok = sumif(Sum, status == "success") by workflow\n'
        "| extend AutomationPct = iff(total == 0, 0.0, round(100.0 * ok / total, 1))\n"
        "| project workflow, AutomationPct, total\n| order by AutomationPct desc",
        "table",
        x=0,
        y=y,
    ),
    timeseries(
        "Resolution time (mean ms by workflow)",
        'AppMetrics\n| where Name == "mdk.workflow.duration_ms"\n'
        '| extend workflow = tostring(Properties["workflow"])\n'
        "| summarize AvgMs = sum(Sum) / sum(ItemCount) by workflow, bin(TimeGenerated, 1h)\n"
        "| order by TimeGenerated asc",
        x=12,
        y=y,
        unit="ms",
    ),
]
y += 8

dashboard = {
    "uid": "mdk-exec",
    "title": "MDK — Executive (Business KPIs)",
    "schemaVersion": 39,
    "version": 0,
    "editable": True,
    "refresh": "5m",
    "time": {"from": "now-30d", "to": "now"},
    "timezone": "browser",
    "tags": ["mdk", "executive", "business", "kpi"],
    "templating": {"list": []},
    "annotations": {"list": []},
    "panels": panels,
}

out = Path(__file__).resolve().parent.parent / "dashboards" / "grafana" / "azure" / "mdk-exec.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(panels)} panels)")
