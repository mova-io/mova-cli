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
    # Golden-signals legend: a TABLE legend surfacing last / max / mean per series
    # so an operator reads the current value AND the recent peak without hovering.
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

    The first step's value must be ``None`` (the base band). Colours
    saturation / error panels green→amber→red so a scan flags a panel that has
    crossed an operational limit (golden-signals: errors + saturation loud)."""
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
        custom["thresholdsStyle"] = {"mode": "dashed"}
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
        description="**Traffic** (golden signal). Durable workflow executions that "
        "reached a terminal state in the last 24h. The throughput headline for the "
        "Temporal backend.",
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
        description="**Errors** (golden signal, inverted). Share of workflows that "
        "ended `success` vs failed/terminated over 24h. Red <90%, amber <99%, "
        "green ≥99% — the reliability headline.",
        thresholds=_thresholds([("red", None), ("yellow", 90), ("green", 99)]),
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
        description="Completion rate per 15m split by terminal status. A `failed` "
        "or `terminated` series appearing is the earliest trend signal that "
        "something regressed.",
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
        description="Per-workflow completion counts broken out by status — the "
        "drill-down that tells you *which* workflow definition is driving "
        "failures, not just that failures exist.",
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
        description="Throughput per 15m split by workflow definition — shows which "
        "workflows carry the load and when each is active.",
    ),
]
y += 8
panels += [
    # ADR 082 follow-on (#737) — workflow latency. mdk.workflow.duration_ms is a
    # histogram; in AppMetrics that lands as Sum + ItemCount per export, so the
    # per-bin mean is sum(Sum)/sum(ItemCount). Excludes HITL wait time? No — it's
    # wall-clock (workflow.info().start_time → workflow.now()), so a HUMAN-paused
    # workflow's duration includes the wait by design; read it per workflow.
    kql_panel(
        "Workflow duration (mean ms by workflow)",
        'AppMetrics\n| where Name == "mdk.workflow.duration_ms"\n'
        '| extend workflow = tostring(Properties["workflow"]), '
        'runtime = tostring(Properties["runtime"])\n'
        '| where runtime == "temporal"\n'
        "| summarize AvgMs = sum(Sum) / sum(ItemCount) by workflow, bin(TimeGenerated, 15m)\n"
        "| order by TimeGenerated asc",
        x=0,
        y=y,
        w=12,
        unit="ms",
        description="**Latency** (golden signal). Mean wall-clock duration per "
        "workflow (histogram sum/count). NOTE: this is true wall-clock — a "
        "HUMAN-paused (HITL) workflow's duration *includes* the time spent waiting "
        "for the human, by design.",
    ),
    kql_panel(
        "Workflow duration (mean ms by status)",
        'AppMetrics\n| where Name == "mdk.workflow.duration_ms"\n'
        '| extend status = tostring(Properties["status"]), '
        'runtime = tostring(Properties["runtime"])\n'
        '| where runtime == "temporal"\n'
        "| summarize AvgMs = sum(Sum) / sum(ItemCount) by status, bin(TimeGenerated, 15m)\n"
        "| order by TimeGenerated asc",
        x=12,
        y=y,
        w=12,
        unit="ms",
        description="**Latency** (golden signal). Mean duration split by terminal "
        "status — failed runs that die fast vs slow tell different stories (fast "
        "fail = validation; slow fail = timeout/retry exhaustion).",
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
        description="**Saturation** (golden signal). Time a workflow task waited in "
        "the queue before a worker picked it up. Rising latency = not enough worker "
        "capacity for the task-queue depth; scale workers.",
    ),
    kql_panel(
        "Worker task slots available",
        f'AppMetrics\n| where Name == "{SDK_SLOTS_AVAILABLE}"\n'
        "| summarize Slots = avg(Sum) by bin(TimeGenerated, 1m)\n| order by TimeGenerated asc",
        x=12,
        y=y,
        description="**Saturation** (golden signal). Free executor slots on the "
        "worker pool. Slots hitting zero while schedule-to-start latency climbs "
        "confirms the worker is the bottleneck.",
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
        description="**Errors** (golden signal). Failed gRPC calls from the worker "
        "to the Temporal frontend. Any sustained non-zero value points at a "
        "connectivity/TLS/namespace problem between worker and server. Amber ≥1, "
        "red ≥10 per 5m.",
        thresholds=_thresholds([("green", None), ("yellow", 1), ("red", 10)]),
    ),
    kql_panel(
        "Sticky cache hit vs miss / 5m",
        f'AppMetrics\n| where Name in ("{SDK_CACHE_HIT}", "{SDK_CACHE_MISS}")\n'
        "| summarize Count = sum(Sum) by Name, bin(TimeGenerated, 5m)\n"
        "| order by TimeGenerated asc",
        x=12,
        y=y,
        description="Sticky-execution cache hits vs misses. A high miss ratio means "
        "workers keep replaying history from scratch (cache evictions / worker "
        "churn) — extra CPU and latency per task.",
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
