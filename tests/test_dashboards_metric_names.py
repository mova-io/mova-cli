"""Dashboards-as-code drift guard (ADR 031 D2).

The in-repo dashboards under ``dashboards/`` are versioned artifacts customers
import (Grafana JSON + Prometheus rules YAML + an Azure Monitor workbook JSON).
They render the OTel metrics mdk emits from ``src/movate/tracing/metrics.py``.

This test is the anti-drift guard ADR 031 calls for. It:

1. Parses **every** dashboard/rules file (asserts JSON/YAML load cleanly), and
2. Cross-checks that **every mdk metric the files reference actually exists** in
   :data:`movate.tracing.metrics.METRIC_NAMES` — the single source of truth.

So if someone renames an instrument in ``metrics.py`` (or fat-fingers a metric
in a dashboard) the dashboards can't silently go stale: this fails loudly.

Backends spell the same instrument differently, so the extractor resolves each
referenced token back to a canonical instrument name:

* **Azure / OTel dot-form** — ``mdk.jobs.completed`` (verbatim in ``AppMetrics``);
  matched directly against :data:`METRIC_NAMES`.
* **Prometheus form** — ``mdk_jobs_completed_total``: dots→underscores, plus a
  unit/aggregation suffix (``_total`` for monotonic counters,
  ``_milliseconds`` + ``_bucket`` / ``_sum`` / ``_count`` for the ms histogram).
  Because OTel instrument names themselves contain underscores
  (``duration_ms``, ``cost_usd``, ``in_flight``), a blind underscore→dot reversal
  is ambiguous — so instead we derive the Prometheus *base* of each known metric
  (``mdk.jobs.completed`` → ``mdk_jobs_completed``) and check a stripped token
  against that set. No guessing.

It also asserts each surface references the metrics we expect, so a truncated or
mis-parsed file can't pass vacuously.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from movate.tracing.metrics import METRIC_NAMES

# Repo-root-relative dashboards directory (tests/ is one level under the root).
_DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "dashboards"
_GRAFANA = _DASHBOARDS_DIR / "grafana" / "mdk-golden-signals.json"
_PROM_RULES = _DASHBOARDS_DIR / "prometheus" / "mdk-rules.yaml"
_AZURE_WORKBOOK = _DASHBOARDS_DIR / "azure" / "mdk-golden-signals.workbook.json"

# Prometheus unit / aggregation suffixes the OTLP -> Prometheus convention
# appends. Stripped (one layer per pass) before matching a Prometheus token
# against the derived metric bases below. Histogram suffixes like ``_bucket``
# sit on top of the ``_milliseconds`` unit suffix, so peel iteratively.
_PROM_SUFFIXES = (
    "_bucket",
    "_count",
    "_sum",
    "_total",
    "_milliseconds",
    "_seconds",
    "_bytes",
)

# Prometheus *base* form of each known instrument: dots -> underscores, no
# suffix (``mdk.jobs.completed`` -> ``mdk_jobs_completed``). A stripped
# Prometheus token is "known" iff it equals one of these. Built from the source
# of truth so it can't drift.
_PROM_BASE_TO_DOT: dict[str, str] = {name.replace(".", "_"): name for name in METRIC_NAMES}

# Tokens that look like an mdk metric in either spelling: dot-form (Azure/OTel)
# or underscore-form (Prometheus). We extract every such token from the file
# text, then resolve each to a real instrument. Anchored on the ``mdk``
# namespace so we don't pick up unrelated identifiers. The dot-form alternative
# is tried first (longest match) so a dotted name isn't truncated.
_METRIC_TOKEN_RE = re.compile(r"\bmdk[._][A-Za-z0-9._]*\b")


def _strip_prometheus_suffixes(name: str) -> str:
    """Peel known Prometheus unit/aggregation suffixes off an underscore name."""
    changed = True
    while changed:
        changed = False
        for suffix in _PROM_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                changed = True
                # Restart so stacked suffixes (``_milliseconds`` under
                # ``_bucket``) are all removed.
                break
    return name


def _resolve(token: str) -> str | None:
    """Resolve a referenced metric token to its canonical dot-name, or None.

    Dot-form is matched directly. Prometheus underscore-form is matched by
    stripping suffixes and looking up the derived base map — never by blind
    underscore->dot reversal (which is ambiguous because instrument names
    themselves contain underscores). Returns None for a token that resolves to
    nothing known, so the caller can flag it as drift.
    """
    if token in METRIC_NAMES:
        return token
    if "." in token:
        # A dotted token that isn't a known instrument -> report it verbatim so
        # the drift assertion surfaces the exact bad name.
        return token
    base = _strip_prometheus_suffixes(token)
    return _PROM_BASE_TO_DOT.get(base, token)


def _referenced_metrics(text: str) -> set[str]:
    """All distinct canonical mdk metric names referenced in a file's text."""
    return {_resolve(tok) for tok in _METRIC_TOKEN_RE.findall(text)}


# All five golden-signal instruments mdk emits today (canonical dot-form).
# Verified by hand against src/movate/tracing/metrics.py — do NOT trust memory.
# Each surface references all five (some in a query/expr, some documented in the
# file's header/prose for the operator); the drift guard treats any mention as a
# reference and requires it to be a real instrument.
_ALL_FIVE = {
    "mdk.jobs.completed",
    "mdk.job.duration_ms",
    "mdk.jobs.in_flight",
    "mdk.run.tokens",
    "mdk.run.cost_usd",
}

# ADR 034 D3 — DB connection-pool observable gauges. Currently surfaced on the
# Grafana dashboard only (the Prometheus rules + Azure workbook stay on the five
# golden signals). When a pool panel/rule is added to those surfaces, extend
# their expected sets here too.
_POOL_METRICS = {
    "mdk.db.pool.size",
    "mdk.db.pool.idle",
    "mdk.db.pool.in_use",
    "mdk.db.pool.waiting",
    "mdk.db.pool.max",
}

_CASES = [
    pytest.param(_GRAFANA, "json", _ALL_FIVE | _POOL_METRICS, id="grafana"),
    pytest.param(_PROM_RULES, "yaml", _ALL_FIVE, id="prometheus-rules"),
    pytest.param(_AZURE_WORKBOOK, "json", _ALL_FIVE, id="azure-workbook"),
]


@pytest.mark.parametrize(("path", "fmt", "expected"), _CASES)
def test_dashboard_parses_and_references_only_real_metrics(
    path: Path, fmt: str, expected: set[str]
) -> None:
    """Each dashboard loads cleanly AND every mdk metric it names is real."""
    assert path.is_file(), f"missing dashboard artifact: {path}"
    text = path.read_text(encoding="utf-8")

    # 1) Parses cleanly (the file is valid JSON / YAML, not just any text).
    if fmt == "json":
        json.loads(text)
    else:
        yaml.safe_load(text)

    referenced = _referenced_metrics(text)

    # 2) Anti-drift core: no referenced metric may be absent from the source of
    # truth. This FAILS LOUDLY if a metric is renamed in metrics.py (or a
    # dashboard references one mdk never emits) — the whole point of the guard.
    unknown = referenced - METRIC_NAMES
    assert not unknown, (
        f"{path.name} references metric(s) not in "
        f"movate.tracing.metrics.METRIC_NAMES: {sorted(unknown)}. "
        f"Either the instrument was renamed/removed in "
        f"src/movate/tracing/metrics.py or the dashboard has a typo."
    )

    # 3) The surface references exactly the metrics we curated for it — guards
    # against a truncated/garbled file passing vacuously, and against silently
    # dropping a golden signal.
    assert referenced == expected, (
        f"{path.name} references {sorted(referenced)} but expected "
        f"{sorted(expected)} (update this test + dashboards/README.md together "
        f"when intentionally changing a dashboard's metric coverage)."
    )


def test_curated_expectations_are_subset_of_source_of_truth() -> None:
    """Sanity: every metric the cases expect actually exists in METRIC_NAMES.

    Keeps the per-surface ``expected`` sets honest even before reading a file —
    if an instrument is removed from metrics.py, this trips immediately.
    """
    all_expected: set[str] = set()
    for case in _CASES:
        all_expected |= case.values[2]
    missing = all_expected - METRIC_NAMES
    assert not missing, (
        f"test expectations reference metrics not in METRIC_NAMES: "
        f"{sorted(missing)} — update the cases to match "
        f"src/movate/tracing/metrics.py"
    )


def test_every_emitted_metric_appears_on_some_dashboard() -> None:
    """Every instrument mdk emits should surface on at least one dashboard.

    Not strictly required by ADR 031, but it catches the *other* drift
    direction: a NEW metric added to metrics.py with no dashboard coverage. If a
    metric is intentionally not dashboarded, add it to the allow-list below with
    a reason.
    """
    # Metrics deliberately not (yet) on a dashboard, with justification.
    _not_dashboarded: dict[str, str] = {
        # ADR 035 D3 — SSE event-stream subscriber gauge. Powers the
        # internal "is a runaway client owning the pool?" operator
        # check + pairs with the advisory per-tenant cap; not yet on a
        # customer-facing dashboard because D3 just shipped and the
        # alerting story for SSE saturation is the same item-#27 work
        # the rest of the data-plane gauges are waiting on. Add a panel
        # alongside the DB-pool gauges when item #27 lands.
        "mdk.sse.connections_active": "ADR 035 D3, dashboard panel deferred to item #27",
    }

    covered: set[str] = set()
    for case in _CASES:
        covered |= case.values[2]

    uncovered = METRIC_NAMES - covered - set(_not_dashboarded)
    assert not uncovered, (
        f"emitted metric(s) {sorted(uncovered)} are not referenced by any "
        f"dashboard under dashboards/. Add a panel/rule (and update the test "
        f"cases) or record them in _not_dashboarded with a reason."
    )
