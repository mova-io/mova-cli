"""Drift guard for the Temporal Grafana dashboard (companion to ADR 082).

``dashboards/grafana/azure/mdk-temporal.json`` is a versioned artifact imported
into the deployed Grafana (uid ``mdk-temporal``). It lives under ``grafana/azure/``
so it is NOT picked up by :mod:`tests.test_grafana_dashboards` (that globs only
the top-level ``grafana/*.json``). This test brings it under the same anti-drift
guard the other dashboards get:

* parses as JSON with the Grafana required top-level keys,
* every ``mdk.*``-namespaced token resolves to a real instrument in
  :data:`movate.tracing.metrics.METRIC_NAMES` (so a rename in ``metrics.py``
  can't silently rot the dashboard), and
* it actually references ``mdk.workflow.completed`` (so a truncated/mis-generated
  file can't pass vacuously).

``temporal_*`` SDK metric names are intentionally NOT mdk-namespaced and are not
checked here — their spelling is owned by the Temporal SDK's OTEL export, not
``metrics.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from movate.tracing.metrics import METRIC_NAMES

_DASHBOARD = (
    Path(__file__).resolve().parent.parent
    / "dashboards"
    / "grafana"
    / "azure"
    / "mdk-temporal.json"
)

_MDK_TOKEN = re.compile(r"mdk\.[a-z0-9_.]+")


@pytest.mark.unit
def test_temporal_dashboard_parses_with_required_keys() -> None:
    d = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    assert {"title", "panels", "schemaVersion", "uid"} <= set(d)
    assert d["uid"] == "mdk-temporal"
    assert isinstance(d["panels"], list) and d["panels"], "dashboard has no panels"


@pytest.mark.unit
def test_temporal_dashboard_mdk_metrics_are_known() -> None:
    raw = _DASHBOARD.read_text(encoding="utf-8")
    referenced = set(_MDK_TOKEN.findall(raw))
    unknown = referenced - set(METRIC_NAMES)
    assert not unknown, f"dashboard references unknown mdk metrics: {sorted(unknown)}"
    # Non-vacuous: the headline durable-workflow metric must be present.
    assert "mdk.workflow.completed" in referenced
