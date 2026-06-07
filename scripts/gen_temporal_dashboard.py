"""Generate the 'MDK — Temporal (Durable Workflows)' Grafana dashboard JSON.

Companion to ``gen_live_runtime_dashboard.py``. Temporal observability shipped
as an Azure Monitor *workbook* (ADR 082), but there was no intuitive **Grafana**
view of durable-workflow health — this generator fills that gap so the deployed
``movate-dev-grafana-oss`` instance has a first-class Temporal dashboard
alongside the live-runtime one.

Two sections:

  * **Durable workflows** — driven by mdk's own ``mdk.workflow.completed``
    counter (ADR 082: attrs ``workflow`` / ``status`` / ``runtime`` / ``tenant``),
    emitted by ``persist_workflow_result_activity``. Throughput, success rate,
    and a per-workflow breakdown. These are the certain, mdk-owned metrics.

  * **Temporal SDK — worker & task-queue health** — the Temporal Rust-core SDK
    metrics (``temporal_*``) exported via the worker's OTEL metrics runtime
    (``_build_temporal_metrics_runtime``). Names follow the Temporal SDK's OTEL
    convention; the exact spelling in ``AppMetrics`` depends on the SDK export,
    so each is overridable via env and the section populates once a
    ``runtime: temporal`` worker is emitting under real workflow load.

Run:  python scripts/gen_temporal_dashboard.py
Out:  dashboards/grafana/azure/mdk-temporal.json  (import with overwrite=true)

Azure-specific ids are the movate-dev instance's; override via env for another
environment (AZ_SUB / AZ_RG / GRAFANA_AZMON_DS_UID / AZ_LA_WORKSPACE_ID).
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

# Temporal SDK (Rust core) OTEL instrument names. Overridable because the exact
# spelling that lands in AppMetrics depends on the SDK's OTEL exporter config.
SDK_SCHED_TO_START = os.environ.get(
    "TEMPORAL_M_SCHED_TO_START", "temporal_workflow_task_schedule_to_start_latency"
)
SDK_SLOTS_AVAILABLE = os.environ.get(
    "TEMPORAL_M_SLOTS_AVAILABLE", "temporal_worker_task_slots_available"
)
SDK_REQUEST_FAILURE = os.environ.get("TEMPORAL_M_REQUEST_FAILURE", "temporal_request_failure")
SDK_CACHE_HIT = os.environ.get("TEMPORAL_M_CACHE_HIT", "temporal_sticky_cache_hit")
SDK_CACHE_MISS = os.environ.get("TEMPORAL_M_CACHE_MISS", "temporal_sticky_cache_miss")

DS = {"type": "grafana-azure-monitor-datasource", "uid": DS_UID}

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
    if viz == "stat":
        panel["options"] = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
            "colorMode": "value",
        }
    return panel


panels: list[dict] = []
y = 0

# ── Durable workflows — mdk.workflow.completed (ADR 082) ──────────────────────
panels.append(row("Durable workflows — completions, success rate, by workflow (ADR 082)", y))
y += 1
panels += [
    kql_panel(
        "Workflows completed (24h)",
        'AppMetrics\n| where Name == "mdk.workflow.completed"\n'
        "| where TimeGenerated > ago(24h)\n| summarize Total = sum(Sum)",
        x=0,
        y=y,
        w=6,
        h=6,
        viz="stat",
        result="table",
    ),
    kql_panel(
        "Success rate % (24h)",
        'AppMetrics\n| where Name == "mdk.workflow.completed"\n'
        "| where TimeGenerated > ago(24h)\n"
        '| extend status = tostring(Properties["status"])\n'
        '| summarize total = sum(Sum), ok = sumif(Sum, status == "success")\n'
        "| extend SuccessPct = iff(total == 0, 0.0, round(100.0 * ok / total, 1))\n"
        "| project SuccessPct",
        x=6,
        y=y,
        w=6,
        h=6,
        unit="percent",
        viz="stat",
        result="table",
    ),
    kql_panel(
        "Completed / 15m by status",
        'AppMetrics\n| where Name == "mdk.workflow.completed"\n'
        '| extend status = tostring(Properties["status"])\n'
        "| summarize Completed = sum(Sum) by status, bin(TimeGenerated, 15m)\n"
        "| order by TimeGenerated asc",
        x=12,
        y=y,
        w=12,
        h=6,
    ),
]
y += 6
panels += [
    kql_panel(
        "Completed by workflow + status",
        'AppMetrics\n| where Name == "mdk.workflow.completed"\n'
        '| extend workflow = tostring(Properties["workflow"]), '
        'status = tostring(Properties["status"])\n'
        "| summarize Completed = sum(Sum) by workflow, status\n| order by Completed desc",
        x=0,
        y=y,
        w=12,
        viz="table",
        result="table",
    ),
    kql_panel(
        "Completions / 15m by workflow",
        'AppMetrics\n| where Name == "mdk.workflow.completed"\n'
        '| extend workflow = tostring(Properties["workflow"])\n'
        "| summarize Completed = sum(Sum) by workflow, bin(TimeGenerated, 15m)\n"
        "| order by TimeGenerated asc",
        x=12,
        y=y,
        w=12,
    ),
]
y += 8

# ── Temporal SDK — worker & task-queue health (temporal_*) ────────────────────
panels.append(row("Temporal SDK — worker & task-queue health (populates under load)", y))
y += 1
panels += [
    kql_panel(
        "Workflow task schedule-to-start latency (avg ms)",
        f'AppMetrics\n| where Name == "{SDK_SCHED_TO_START}"\n'
        "| summarize Latency = avg(Sum) by bin(TimeGenerated, 5m)\n| order by TimeGenerated asc",
        x=0,
        y=y,
        unit="ms",
    ),
    kql_panel(
        "Worker task slots available",
        f'AppMetrics\n| where Name == "{SDK_SLOTS_AVAILABLE}"\n'
        "| summarize Slots = avg(Sum) by bin(TimeGenerated, 1m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
    ),
]
y += 8
panels += [
    kql_panel(
        "Temporal RPC failures / 5m",
        f'AppMetrics\n| where Name == "{SDK_REQUEST_FAILURE}"\n'
        "| summarize Failures = sum(Sum) by bin(TimeGenerated, 5m)\n| order by TimeGenerated asc",
        x=0,
        y=y,
    ),
    kql_panel(
        "Sticky cache hit vs miss / 5m",
        f'AppMetrics\n| where Name in ("{SDK_CACHE_HIT}", "{SDK_CACHE_MISS}")\n'
        "| summarize Count = sum(Sum) by Name, bin(TimeGenerated, 5m)\n"
        "| order by TimeGenerated asc",
        x=12,
        y=y,
    ),
]
y += 8

dashboard = {
    "uid": "mdk-temporal",
    "title": "MDK — Temporal (Durable Workflows)",
    "schemaVersion": 39,
    "version": 0,
    "editable": True,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "timezone": "browser",
    "tags": ["mdk", "temporal", "workflows", "azure-monitor"],
    "templating": {"list": []},
    "annotations": {"list": []},
    "panels": panels,
}

out = (
    Path(__file__).resolve().parent.parent
    / "dashboards"
    / "grafana"
    / "azure"
    / "mdk-temporal.json"
)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(panels)} panels)")
