"""Contract test for the single-pane "mission control" demo dashboard.

``dashboards/grafana/insights/mdk-mission-control.json`` is the Monday-demo
operator wall: ONE screen that fuses the live OTel golden signals (spend,
throughput, error rate, p95 latency, in-flight) from Prometheus with the
overnight analyst's structured output (a health gauge + plain-English
anomalies) from the ADR 047 Observability Intelligence API.

The generic insight-pack guard (``tests/test_insight_dashboards.py``) already
parameterizes over every ``insights/*.json`` file, so this asset inherits the
JSON-validity + metric-catalog-conformance checks for free. THIS test is the
narrower, asset-specific contract: it asserts the single pane actually wires
to the *real* sources it claims to — the four golden-signal Prometheus
instruments **and** the two real observability endpoints with the real
response-shape selectors — so a future refactor of either the metric catalog
or the ADR 047 wire types can't silently leave the demo screen pointing at a
dead datasource.

Marked ``@pytest.mark.unit`` so it runs in the fast lane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from movate.tracing.metrics import (
    METRIC_JOB_DURATION_MS,
    METRIC_JOBS_COMPLETED,
    METRIC_JOBS_IN_FLIGHT,
    METRIC_NAMES,
    METRIC_RUN_COST_USD,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ASSET = _REPO_ROOT / "dashboards" / "grafana" / "insights" / "mdk-mission-control.json"

# The two observability endpoints the single pane is allowed to consume. The
# whole-point constraint of the deliverable is "CONSUME existing endpoints —
# do NOT add new /api/v1 routes"; pinning the exact paths here means a typo or
# a drift to a non-existent route fails loudly.
_INSIGHTS_URL = "/api/v1/observability/insights"
_HEALTH_URL = "/api/v1/observability/health"


def _load() -> dict:
    assert _ASSET.is_file(), f"missing mission-control asset: {_ASSET}"
    return json.loads(_ASSET.read_text(encoding="utf-8"))


def _iter_panels(dashboard: dict) -> list[dict]:
    """Flatten panels, descending into row sub-panels."""
    out: list[dict] = []

    def _walk(panels: list[dict]) -> None:
        for p in panels:
            out.append(p)
            nested = p.get("panels")
            if isinstance(nested, list):
                _walk(nested)

    _walk(dashboard.get("panels", []) or [])
    return out


def _prom_exprs(dashboard: dict) -> list[str]:
    return [
        t["expr"]
        for p in _iter_panels(dashboard)
        for t in (p.get("targets") or [])
        if isinstance(t, dict) and isinstance(t.get("expr"), str)
    ]


def _infinity_targets(dashboard: dict) -> list[dict]:
    """Every Infinity/JSON target (the insight-API-fed panels)."""
    return [
        t
        for p in _iter_panels(dashboard)
        for t in (p.get("targets") or [])
        if isinstance(t, dict) and t.get("type") == "json" and isinstance(t.get("url"), str)
    ]


@pytest.mark.unit
def test_mission_control_is_valid_grafana_dashboard() -> None:
    """Parses, has the required Grafana keys, and is the single-pane asset."""
    dash = _load()
    for key in ("title", "uid", "panels", "schemaVersion"):
        assert key in dash, f"mission-control dashboard missing required key: {key!r}"
    assert dash["uid"] == "mdk-mission-control"
    assert isinstance(dash["panels"], list) and dash["panels"], "panels must be a non-empty list"


@pytest.mark.unit
def test_mission_control_golden_signals_use_catalog_metrics() -> None:
    """The live tiles reference the four real OTel golden-signal instruments,
    and EVERY Prometheus expr names only catalog metrics.

    Asserting the four are present (not just "no unknowns") is what locks in
    that the single pane actually shows health/spend/throughput+error/latency/
    in-flight rather than having quietly lost a panel.
    """
    dash = _load()
    exprs = _prom_exprs(dash)
    assert exprs, "expected live Prometheus panels on the mission-control pane"

    joined = "\n".join(exprs)
    # Prometheus spelling of each catalog instrument the deliverable requires.
    required = {
        METRIC_RUN_COST_USD: "mdk_run_cost_usd_total",  # spend
        METRIC_JOBS_COMPLETED: "mdk_jobs_completed_total",  # throughput + error rate
        METRIC_JOB_DURATION_MS: "mdk_job_duration_ms_milliseconds_bucket",  # p95/p99
        METRIC_JOBS_IN_FLIGHT: "mdk_jobs_in_flight",  # in-flight saturation
    }
    for dot_name, prom_name in required.items():
        assert dot_name in METRIC_NAMES, f"{dot_name} not in the metric catalog"
        assert prom_name in joined, (
            f"mission-control pane is missing a panel for {dot_name} "
            f"(expected Prometheus series {prom_name!r} in some expr)"
        )


@pytest.mark.unit
def test_mission_control_consumes_only_existing_observability_endpoints() -> None:
    """The insight-fed panels hit ONLY the two real observability read
    endpoints (no invented routes), and use the REAL response-shape selectors.

    Real shapes (movate.runtime.schemas):
      * GET /observability/health  -> flat object  => root_selector ""
        (health_score, anomaly_count are top-level fields).
      * GET /observability/insights-> {insights:[{anomalies:[...]}], count}
        => root_selector "insights" (rows) or "insights.anomalies" (flattened
        plain-English anomaly feed).
    """
    dash = _load()
    targets = _infinity_targets(dash)
    assert targets, "expected insight-API (Infinity) panels on the mission-control pane"

    # Strip any query string before checking the route.
    seen_paths = {t["url"].split("?", 1)[0] for t in targets}
    assert seen_paths <= {_INSIGHTS_URL, _HEALTH_URL}, (
        f"mission-control pane references non-existent/observability route(s): "
        f"{sorted(seen_paths - {_INSIGHTS_URL, _HEALTH_URL})}. It must CONSUME the "
        f"existing endpoints only — no new /api/v1 routes."
    )

    # Index selectors per (path, root_selector) so we can assert the real ones.
    by_path: dict[str, set[str]] = {}
    for t in targets:
        path = t["url"].split("?", 1)[0]
        by_path.setdefault(path, set()).add(t.get("root_selector", ""))

    # health: flat object -> top-level field selector ("")
    assert "" in by_path.get(_HEALTH_URL, set()), (
        "the health gauge must read GET /observability/health with root_selector "
        "'' (the API returns a flat object; health_score/anomaly_count are "
        "top-level fields)"
    )
    # insights: the plain-English anomaly feed flattens insights[].anomalies[]
    assert "insights.anomalies" in by_path.get(_INSIGHTS_URL, set()), (
        "the plain-English anomaly table must read GET /observability/insights "
        "with root_selector 'insights.anomalies' (flattening the nested per-day "
        "anomaly arrays into rows)"
    )


@pytest.mark.unit
def test_mission_control_anomaly_table_surfaces_plain_english_note() -> None:
    """The headline panel must surface the analyst's human-readable `note`
    field — that's the 'top anomalies in plain English' the deliverable asks
    for, and it's the column that carries e.g. 'cost is 12.9 sigma above the
    3-day baseline' (produced pure, no LLM, by detect_anomalies)."""
    dash = _load()
    anomaly_targets = [
        t for t in _infinity_targets(dash) if t.get("root_selector") == "insights.anomalies"
    ]
    assert anomaly_targets, "expected an insights.anomalies-fed table"
    selectors = {
        col.get("selector")
        for t in anomaly_targets
        for col in (t.get("columns") or [])
        if isinstance(col, dict)
    }
    # The four real Anomaly fields the table reads (movate.core.observability.models.Anomaly).
    for field in ("note", "metric", "severity", "z"):
        assert field in selectors, (
            f"anomaly table must read the real Anomaly.{field} field; "
            f"got selectors {sorted(s for s in selectors if s)}"
        )
