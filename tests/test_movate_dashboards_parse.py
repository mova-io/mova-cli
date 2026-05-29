"""Movate fleet dashboards (ADR 039) -- parses-only guard.

The dashboards under ``dashboards/grafana/movate/`` are the Movate-side fleet
view introduced by ADR 039 (`docs/adr/039-movate-product-telemetry.md`). They
are an **illustrative, ADR-pending** surface, distinct from the per-customer
dashboards under ``dashboards/grafana/*.json`` that
:mod:`tests.test_dashboards_metric_names` drift-checks against
:data:`movate.tracing.metrics.METRIC_NAMES`.

This sibling test exists because the fleet dashboards intentionally include
**scaffold-with-banner** panels for instruments that do NOT exist on
``origin/main`` today (e.g. ``mdk.eval.pass_rate``, ``mdk.queue.age_ms`` --
see ADR 039 Open Questions + ADR 016 D2). Those scaffold names appear inside
Markdown text panels that the strict drift guard would (correctly) flag as
unknown -- so we keep the fleet dashboards on a *lighter* guard here:

1. **Parses cleanly** -- every file in the subdir is valid JSON.
2. **Every metric token in a KQL/PromQL query position resolves to a real
   instrument** -- with the explicit allow-list ``_KNOWN_SCAFFOLD_METRICS``
   for the names that appear only inside scaffold banners. If a *new* scaffold
   name appears that isn't on the allow-list, the test trips so the operator
   has to either (a) implement the instrument + remove the allow-list entry,
   or (b) explicitly justify it in the allow-list.

This is the looser, evolving-dashboards posture ADR 039's
``dashboards/grafana/movate/README.md`` documents.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from movate.tracing.metrics import METRIC_NAMES

_MOVATE_DIR = Path(__file__).resolve().parent.parent / "dashboards" / "grafana" / "movate"

# Anchored on the ``mdk`` namespace, same shape as the strict drift guard. Matches
# both dot-form (Azure/OTel) and underscore-form (Prometheus) tokens.
_METRIC_TOKEN_RE = re.compile(r"\bmdk[._][A-Za-z0-9._]*\b")

# Explicit allow-list of NOT-YET-EMITTED metric names that appear ONLY inside
# Markdown scaffold panels (with the "needs new instrument" banner). When the
# corresponding instrument lands in ``src/movate/tracing/metrics.py``, the entry
# here is removed. Tied to ADR 039 Open Questions.
_KNOWN_SCAFFOLD_METRICS: dict[str, str] = {
    "mdk.eval.pass_rate": (
        "ADR 016 D2 / ADR 039 Open Q2 -- eval signals are Langfuse scores today."
    ),
    "mdk.eval.drift_score": ("ADR 016 D2 / ADR 039 Open Q2 -- drift via Langfuse today."),
    "mdk.eval": (
        "Prose prefix used in scaffold banners (e.g. 'mdk.eval.*'); the leaf "
        "names are individually allow-listed above."
    ),
    "mdk.queue.age_ms": (
        "src/movate/tracing/metrics.py header -- StorageProvider queue queries "
        "deferred; in_flight is the fleet proxy."
    ),
    "mdk.queue.depth": (
        "src/movate/tracing/metrics.py header -- StorageProvider count query "
        "deferred; in_flight is the fleet proxy ADR 034 D3 endorses."
    ),
    "mdk.db.pool": (
        "Prose prefix for the five pool gauges "
        "(mdk.db.pool.size/.idle/.in_use/.waiting/.max), each individually in "
        "METRIC_NAMES."
    ),
}


def _all_json_files() -> list[Path]:
    return sorted(_MOVATE_DIR.glob("*.json"))


@pytest.mark.parametrize("path", _all_json_files(), ids=lambda p: p.name)
def test_movate_dashboard_parses(path: Path) -> None:
    """Each Movate fleet dashboard JSON parses cleanly."""
    text = path.read_text(encoding="utf-8")
    json.loads(text)  # raises on invalid JSON


@pytest.mark.parametrize("path", _all_json_files(), ids=lambda p: p.name)
def test_movate_dashboard_metric_names_are_known_or_allow_listed(path: Path) -> None:
    """Every ``mdk.*`` token is either a real instrument or an allow-listed
    scaffold name from ADR 039 Open Questions.

    Treats the file body as text (the strict guard does the same) so the check
    covers both query expressions AND any Markdown banner that mentions a
    not-yet-emitted instrument. New scaffold names without an allow-list entry
    trip the test deliberately -- the contract is "either implement it or
    record the gap."
    """
    text = path.read_text(encoding="utf-8")
    referenced = set(_METRIC_TOKEN_RE.findall(text))
    # Tokens are matched verbatim against the canonical dot-form set or the
    # allow-list. Movate fleet dashboards are KQL-only (Azure Monitor data
    # source per ADR 039 D1), so Prometheus underscore-form does not occur --
    # we keep the check on dot-form only and assert no underscore-form sneaks
    # in (which would suggest the dashboard was copy-pasted from PR #518's
    # per-customer Prometheus dashboards by accident).
    underscore_form = {t for t in referenced if "_" in t and "." not in t}
    assert not underscore_form, (
        f"{path.name} references Prometheus underscore-form metric tokens "
        f"{sorted(underscore_form)} -- Movate fleet dashboards target Azure "
        f"Monitor (KQL) per ADR 039 D1; underscore-form belongs on the "
        f"per-customer Prometheus dashboards under dashboards/grafana/*.json."
    )

    unknown = {t for t in referenced if "." in t} - METRIC_NAMES - _KNOWN_SCAFFOLD_METRICS.keys()
    assert not unknown, (
        f"{path.name} references metric(s) not in METRIC_NAMES and not on the "
        f"ADR 039 scaffold allow-list: {sorted(unknown)}. Either the "
        f"instrument was renamed/removed in src/movate/tracing/metrics.py, the "
        f"dashboard has a typo, or a new scaffold-with-banner name needs an "
        f"entry in _KNOWN_SCAFFOLD_METRICS (with an ADR cross-reference)."
    )
