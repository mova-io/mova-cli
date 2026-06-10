"""Anti-drift validator for every Grafana dashboard under ``dashboards/grafana/``.

The companion to :mod:`tests.test_dashboards_metric_names`, which curates per-
file *expected* metric sets for the original golden-signals dashboard + the
Prometheus rules + the Azure workbook. This test takes a complementary,
catalog-first stance:

* It iterates **every** ``dashboards/grafana/*.json`` (so a newly-added
  dashboard is auto-picked up without having to update a per-file case).
* It asserts each file parses as JSON and has the Grafana required top-level
  keys (``title``, ``panels``, ``schemaVersion``, ``uid``).
* It walks every panel's ``targets[*].expr`` and asserts every ``mdk``-namespaced
  token in those PromQL expressions resolves to a metric in a frozen
  ``_ALLOWED_METRICS`` allow-list pinned from
  :data:`movate.tracing.metrics.METRIC_NAMES`. A future rename in
  ``src/movate/tracing/metrics.py`` is caught here (the allow-list stops
  matching) AND on the source-of-truth subset assertion below.

Marked ``@pytest.mark.unit`` so it runs in the fast lane.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from movate.tracing.metrics import METRIC_NAMES

# Repo-root-relative path. tests/ is one level under the root.
_GRAFANA_DIR = Path(__file__).resolve().parent.parent / "dashboards" / "grafana"

# Inline allow-list pinned from the catalog ("populate from your catalog" per
# the deliverable spec). Keeping it inline (rather than importing METRIC_NAMES
# verbatim) means a rename in metrics.py that *also* lands here still trips the
# subset assertion further down -- the two have to agree, and the allow-list
# encodes the human-curated catalog at the point this test was written.
_ALLOWED_METRICS: frozenset[str] = frozenset(
    {
        # Golden-signal core (recorded at the runtime/worker edges).
        "mdk.jobs.completed",
        "mdk.job.duration_ms",
        "mdk.jobs.in_flight",
        "mdk.run.tokens",
        "mdk.run.cost_usd",
        # ADR 034 D3 -- per-pod asyncpg pool saturation gauges.
        "mdk.db.pool.size",
        "mdk.db.pool.idle",
        "mdk.db.pool.in_use",
        "mdk.db.pool.waiting",
        "mdk.db.pool.max",
        # ADR 082 -- durable-workflow completion + latency (Temporal terminal
        # activity), referenced by the certification dashboard's supporting panels.
        "mdk.workflow.completed",
        "mdk.workflow.duration_ms",
        # Certification matrix -- harness-emitted pass/fail per (scenario,
        # capability), the metric behind dashboards/grafana/mdk-certification.json.
        "mdk.certification.scenario",
        # ADR 093 — governance gate decisions (kind/effect/mode/tenant), the
        # warn→enforce rollout dashboard.
        "mdk.governance.decisions",
    }
)

# Grafana panel ``expr`` strings carry Prometheus-spelt names. The OTLP ->
# Prometheus convention is dots->underscores plus a unit/aggregation suffix
# (``_total`` for monotonic counters, ``_milliseconds`` + ``_bucket``/_sum/_count
# for the ms histogram, no suffix for gauges/up-down counters).
_PROM_SUFFIXES = (
    "_bucket",
    "_count",
    "_sum",
    "_total",
    "_milliseconds",
    "_seconds",
    "_bytes",
)
# Prom *base* form of each allowed instrument (``mdk.jobs.completed`` ->
# ``mdk_jobs_completed``). A stripped Prom token is "known" iff it equals one
# of these. Derived from the inline allow-list so it can't drift independently.
_PROM_BASE_TO_DOT: dict[str, str] = {name.replace(".", "_"): name for name in _ALLOWED_METRICS}
# Matches any token that looks like an mdk metric in either spelling (dot-form
# or underscore-form). Anchored on the ``mdk`` namespace so unrelated identifiers
# in panel descriptions don't get scooped up.
_METRIC_TOKEN_RE = re.compile(r"\bmdk[._][A-Za-z0-9._]*\b")

# Grafana dashboard JSON top-level keys we require (the standard schema; an
# import-able dashboard always has these).
_REQUIRED_TOP_LEVEL_KEYS = ("title", "panels", "schemaVersion", "uid")


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

    Returns the token itself if it can't be resolved -- the caller asserts on
    membership in :data:`_ALLOWED_METRICS`, so an unresolved token (e.g. a typo
    in PromQL or a brand-new metric not in the catalog) fails loudly with the
    exact bad name.
    """
    if token in _ALLOWED_METRICS:
        return token
    if "." in token:
        return token  # dotted but unknown -> surface verbatim
    base = _strip_prometheus_suffixes(token)
    return _PROM_BASE_TO_DOT.get(base, token)


def _iter_grafana_dashboards() -> list[Path]:
    """All ``dashboards/grafana/*.json`` files (sorted for stable pytest ids)."""
    return sorted(_GRAFANA_DIR.glob("*.json"))


def _collect_exprs(dashboard: dict) -> list[str]:
    """Every ``expr`` string under every panel target. Handles row panels too."""
    exprs: list[str] = []

    def _walk_panels(panels: list[dict]) -> None:
        for panel in panels:
            for target in panel.get("targets", []) or []:
                expr = target.get("expr")
                if isinstance(expr, str):
                    exprs.append(expr)
            # Row panels nest child panels under ``panels``.
            nested = panel.get("panels")
            if isinstance(nested, list):
                _walk_panels(nested)

    _walk_panels(dashboard.get("panels", []) or [])
    return exprs


@pytest.mark.unit
def test_grafana_dir_has_dashboards() -> None:
    """Fail loud if the directory got emptied -- this test would otherwise
    pass vacuously with zero parametrize ids."""
    assert _GRAFANA_DIR.is_dir(), f"missing directory: {_GRAFANA_DIR}"
    found = _iter_grafana_dashboards()
    assert found, f"no Grafana dashboards under {_GRAFANA_DIR}"


@pytest.mark.unit
@pytest.mark.parametrize("dashboard_path", _iter_grafana_dashboards(), ids=lambda p: p.name)
def test_grafana_dashboard_is_valid_and_only_references_known_metrics(
    dashboard_path: Path,
) -> None:
    """For each Grafana dashboard JSON file:

    1. it parses as JSON,
    2. it has the required top-level Grafana schema keys, and
    3. every ``mdk``-namespaced token in every panel ``expr`` resolves to a
       metric in the inline allow-list.
    """
    raw = dashboard_path.read_text(encoding="utf-8")

    # (1) Parses cleanly as JSON.
    try:
        dashboard = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{dashboard_path.name}: not valid JSON: {exc}")

    # (2) Required top-level keys.
    missing_keys = [k for k in _REQUIRED_TOP_LEVEL_KEYS if k not in dashboard]
    assert not missing_keys, f"{dashboard_path.name} missing required Grafana keys: {missing_keys}"
    assert isinstance(dashboard["panels"], list), f"{dashboard_path.name}: 'panels' must be a list"

    # (3) Every metric token in every panel expr is in the allow-list. We
    # extract from the expr strings (not the raw text) so prose descriptions
    # documenting deferred metrics don't trip the check.
    exprs = _collect_exprs(dashboard)
    referenced: set[str] = set()
    for expr in exprs:
        for token in _METRIC_TOKEN_RE.findall(expr):
            referenced.add(_resolve(token))

    unknown = referenced - _ALLOWED_METRICS
    assert not unknown, (
        f"{dashboard_path.name} references metric(s) not in the catalog "
        f"allow-list: {sorted(unknown)}. Either the instrument was renamed in "
        f"src/movate/tracing/metrics.py (update _ALLOWED_METRICS here) or the "
        f"dashboard has a typo / references a metric mdk doesn't emit."
    )


@pytest.mark.unit
def test_allow_list_is_subset_of_source_of_truth() -> None:
    """The inline allow-list must be a subset of :data:`METRIC_NAMES`.

    If an instrument is renamed/removed in ``src/movate/tracing/metrics.py``,
    this fires first -- before the per-dashboard check above -- pointing
    straight at the stale allow-list entry.
    """
    stale = _ALLOWED_METRICS - METRIC_NAMES
    assert not stale, (
        f"_ALLOWED_METRICS contains names absent from "
        f"movate.tracing.metrics.METRIC_NAMES: {sorted(stale)}. "
        f"The catalog (metrics.py) is the source of truth; update the "
        f"allow-list (and the dashboards under dashboards/grafana/) to match."
    )
