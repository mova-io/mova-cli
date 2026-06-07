"""Drift guard for the Executive (Business KPIs) Grafana dashboard.

``dashboards/grafana/azure/mdk-exec.json`` is the business-outcome dashboard
(uid ``mdk-exec``). It lives under ``grafana/azure/`` so it's not covered by
:mod:`tests.test_grafana_dashboards` (top-level glob). Same anti-drift guard as
the other generated dashboards: valid JSON + required Grafana keys, and every
``mdk.*`` token resolves to :data:`movate.tracing.metrics.METRIC_NAMES` (so a
rename in metrics.py can't silently rot it).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from movate.tracing.metrics import METRIC_NAMES

_DASHBOARD = (
    Path(__file__).resolve().parent.parent / "dashboards" / "grafana" / "azure" / "mdk-exec.json"
)
_MDK_TOKEN = re.compile(r"mdk\.[a-z0-9_.]+")


@pytest.mark.unit
def test_exec_dashboard_parses_with_required_keys() -> None:
    d = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    assert {"title", "panels", "schemaVersion", "uid"} <= set(d)
    assert d["uid"] == "mdk-exec"
    assert isinstance(d["panels"], list) and d["panels"]


@pytest.mark.unit
def test_exec_dashboard_mdk_metrics_are_known() -> None:
    raw = _DASHBOARD.read_text(encoding="utf-8")
    referenced = set(_MDK_TOKEN.findall(raw))
    unknown = referenced - set(METRIC_NAMES)
    assert not unknown, f"exec dashboard references unknown mdk metrics: {sorted(unknown)}"
    # Non-vacuous: the business KPIs are built on the workflow completion signal.
    assert "mdk.workflow.completed" in referenced
