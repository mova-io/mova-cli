"""Anti-drift validator for the insight-fed dashboard pack.

The insight-fed dashboards (``dashboards/grafana/insights/*.json``) and the
companion Azure Monitor workbook
(``infra/azure-monitor/workbooks/insights.workbook.json``) are the intelligence
layer on top of the #518 raw-metric dashboards. Their top rows read from the
ADR 047 Observability Intelligence API via a JSON/Infinity datasource, so most
panels have **no** Prometheus ``expr`` — they're text / table / gauge / stat
panels bound to the insights datasource.

This guard therefore takes the same catalog-first stance as the #518
``tests/test_grafana_dashboards.py`` but is deliberately **tolerant of
non-metric panels**:

* Every Grafana JSON parses and has the required top-level keys
  (``title``, ``panels``, ``schemaVersion``; ``uid`` too for Grafana).
* The Azure workbook JSON parses and has its required keys (``version``,
  ``items``).
* Any ``mdk``-namespaced token that appears in a panel ``targets[*].expr``
  (i.e. the *raw-metric* panels reused from the #518 catalog) must resolve to a
  name in the catalog allow-list. Panels with **no** ``expr`` (the insight-API
  panels) are skipped — that's the explicit tolerance the deliverable asks for.

Allow-list source of truth: :data:`movate.tracing.metrics.METRIC_NAMES`.

Marked ``@pytest.mark.unit`` so it runs in the fast lane.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from movate.tracing.metrics import METRIC_NAMES

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSIGHTS_GRAFANA_DIR = _REPO_ROOT / "dashboards" / "grafana" / "insights"
_INSIGHTS_WORKBOOK = _REPO_ROOT / "infra" / "azure-monitor" / "workbooks" / "insights.workbook.json"

# Prometheus unit/aggregation suffixes the OTLP -> Prometheus convention appends
# (``mdk.jobs.completed`` -> ``mdk_jobs_completed_total``; the ms histogram gets
# ``_milliseconds`` + ``_bucket``/_sum/_count). Peeled iteratively before a
# stripped token is matched against the derived metric bases.
_PROM_SUFFIXES = (
    "_bucket",
    "_count",
    "_sum",
    "_total",
    "_milliseconds",
    "_seconds",
    "_bytes",
)
# Prometheus *base* form of each known instrument, derived from the source of
# truth so it can't drift independently.
_PROM_BASE_TO_DOT: dict[str, str] = {name.replace(".", "_"): name for name in METRIC_NAMES}
# Any token that looks like an mdk metric in either spelling (dot- or
# underscore-form). Anchored on the ``mdk`` namespace.
_METRIC_TOKEN_RE = re.compile(r"\bmdk[._][A-Za-z0-9._]*\b")

# Grafana dashboard top-level keys we require. ``uid`` is included because the
# insight dashboards cross-link each other by uid (and #518 requires it too).
_REQUIRED_GRAFANA_KEYS = ("title", "panels", "schemaVersion", "uid")
# Azure Monitor workbook required top-level keys.
_REQUIRED_WORKBOOK_KEYS = ("version", "items")


def _strip_prometheus_suffixes(name: str) -> str:
    """Peel any known Prometheus unit/aggregation suffix iteratively."""
    changed = True
    while changed:
        changed = False
        for suffix in _PROM_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                changed = True
                break
    return name


def _resolve(token: str) -> str:
    """Resolve a referenced metric token to its canonical dot-name.

    Returns the token verbatim if it can't be resolved, so the membership
    assertion surfaces the exact bad name.
    """
    if token in METRIC_NAMES:
        return token
    if "." in token:
        return token  # dotted but unknown -> surface verbatim
    base = _strip_prometheus_suffixes(token)
    return _PROM_BASE_TO_DOT.get(base, token)


def _iter_insight_dashboards() -> list[Path]:
    """All ``dashboards/grafana/insights/*.json`` files (sorted, stable ids)."""
    return sorted(_INSIGHTS_GRAFANA_DIR.glob("*.json"))


def _collect_exprs(dashboard: dict) -> list[str]:
    """Every ``expr`` string under every panel target (handles row nesting)."""
    exprs: list[str] = []

    def _walk(panels: list[dict]) -> None:
        for panel in panels:
            for target in panel.get("targets", []) or []:
                if isinstance(target, dict):
                    expr = target.get("expr")
                    if isinstance(expr, str):
                        exprs.append(expr)
            nested = panel.get("panels")
            if isinstance(nested, list):
                _walk(nested)

    _walk(dashboard.get("panels", []) or [])
    return exprs


@pytest.mark.unit
def test_insights_dir_has_dashboards() -> None:
    """Fail loud if the insights dir got emptied (avoids a vacuous pass)."""
    assert _INSIGHTS_GRAFANA_DIR.is_dir(), f"missing directory: {_INSIGHTS_GRAFANA_DIR}"
    found = _iter_insight_dashboards()
    assert found, f"no insight dashboards under {_INSIGHTS_GRAFANA_DIR}"


@pytest.mark.unit
@pytest.mark.parametrize("dashboard_path", _iter_insight_dashboards(), ids=lambda p: p.name)
def test_insight_grafana_dashboard_is_valid_and_metrics_are_known(
    dashboard_path: Path,
) -> None:
    """Each insight Grafana dashboard parses, has the required keys, and any
    raw-metric ``expr`` references only catalog metrics.

    Panels with no ``expr`` (the insights-API-fed text/table/gauge/stat panels)
    are tolerated — they simply contribute no metric tokens.
    """
    raw = dashboard_path.read_text(encoding="utf-8")

    try:
        dashboard = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - assertion message
        pytest.fail(f"{dashboard_path.name}: not valid JSON: {exc}")

    missing = [k for k in _REQUIRED_GRAFANA_KEYS if k not in dashboard]
    assert not missing, f"{dashboard_path.name} missing required keys: {missing}"
    assert isinstance(dashboard["panels"], list), f"{dashboard_path.name}: 'panels' must be a list"

    referenced: set[str] = set()
    for expr in _collect_exprs(dashboard):
        for token in _METRIC_TOKEN_RE.findall(expr):
            referenced.add(_resolve(token))

    unknown = referenced - METRIC_NAMES
    assert not unknown, (
        f"{dashboard_path.name} references metric(s) not in the catalog: "
        f"{sorted(unknown)}. The insight panels read the ADR 047 insights API "
        f"(no expr); only raw-metric panels may carry an expr, and those must "
        f"reuse names from movate.tracing.metrics.METRIC_NAMES (the #518 "
        f"catalog). Either the instrument was renamed or the dashboard has a typo."
    )


@pytest.mark.unit
def test_at_least_one_dashboard_has_insight_datasource_panels() -> None:
    """The pack's whole point is insight-fed panels: assert that at least one
    dashboard carries panels with no Prometheus ``expr`` (i.e. they read the
    JSON/Infinity insights datasource). This locks in the tolerance contract —
    if a refactor accidentally turned every panel into a metric panel, the
    "insight-fed" nature would be lost and this fails."""
    saw_non_metric_panel = False
    for path in _iter_insight_dashboards():
        dashboard = json.loads(path.read_text(encoding="utf-8"))

        def _walk(panels: list[dict]) -> None:
            nonlocal saw_non_metric_panel
            for panel in panels:
                ptype = panel.get("type")
                has_expr = any(
                    isinstance(t, dict) and isinstance(t.get("expr"), str)
                    for t in (panel.get("targets") or [])
                )
                if ptype not in ("row", None) and not has_expr:
                    saw_non_metric_panel = True
                nested = panel.get("panels")
                if isinstance(nested, list):
                    _walk(nested)

        _walk(dashboard.get("panels", []) or [])
    assert saw_non_metric_panel, (
        "no insight-fed (non-metric) panels found across the pack — the "
        "dashboards should have text/table/gauge/stat panels bound to the "
        "insights JSON datasource"
    )


@pytest.mark.unit
def test_insight_workbook_is_valid_and_metrics_are_known() -> None:
    """The Azure Monitor insight workbook parses, has the required top-level
    keys, and every mdk metric token in its KQL queries is a real instrument.

    The workbook mixes raw-metric KQL (against ``AppMetrics``) with
    custom-endpoint placeholders for the ADR 047 API; only the former name
    instruments, and those must be in the catalog.
    """
    assert _INSIGHTS_WORKBOOK.is_file(), f"missing workbook: {_INSIGHTS_WORKBOOK}"
    text = _INSIGHTS_WORKBOOK.read_text(encoding="utf-8")

    workbook = json.loads(text)  # raises on invalid JSON
    missing = [k for k in _REQUIRED_WORKBOOK_KEYS if k not in workbook]
    assert not missing, f"insights.workbook.json missing required keys: {missing}"
    assert isinstance(workbook["items"], list), "'items' must be a list"

    # The workbook spells metrics in OTel dot-form inside KQL (Name ==
    # "mdk.jobs.completed"). Any mdk token anywhere in the file must resolve to a
    # real instrument — prose mentioning a metric counts too, same as #518's
    # workbook drift guard.
    referenced = {_resolve(tok) for tok in _METRIC_TOKEN_RE.findall(text)}
    unknown = referenced - METRIC_NAMES
    assert not unknown, (
        f"insights.workbook.json references metric(s) not in the catalog: "
        f"{sorted(unknown)}. Reuse only names from "
        f"movate.tracing.metrics.METRIC_NAMES (the #518 catalog)."
    )
