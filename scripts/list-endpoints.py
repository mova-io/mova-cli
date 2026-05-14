"""Print the live MDK endpoint surface grouped by pillar.

Fetches `/api/v1/openapi.json` from a runtime and classifies every
path into one of the four pillars from
``docs/deva-pillar-walkthrough.md`` plus a "Coming soon /
lifecycle" bucket for feature-flagged + recently-added endpoints.

Pure stdlib — no `uv run --with` needed. Output is GitHub-flavored
markdown; pipe to a file or paste into Teams.

Usage
-----

::

    # Default — hits the personal-sandbox runtime URL stored in env
    python scripts/list-endpoints.py

    # Against a specific runtime
    python scripts/list-endpoints.py \\
        https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io

    # Same thing via env var
    MDK_BASE=https://... python scripts/list-endpoints.py

    # Machine-readable JSON instead of markdown
    python scripts/list-endpoints.py --json | jq .

Designed so you can pipe to a file + share:

::

    python scripts/list-endpoints.py > ~/.movate/live-endpoints.md

Why this exists
---------------

The OpenAPI spec at ``/api/v1/openapi.json`` is the canonical source
of truth for what the runtime advertises; what changes is which
pillar each endpoint belongs to, and that classification lives in
the walkthrough doc. When the surface grows (e.g. after a deploy
ships items 80/81 or flips the GitHub feature flag), running this
script regenerates the doc-aligned table without manual editing.

Classification rules live in :func:`_classify`. To re-label an
endpoint, update the function — it's the single source of truth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any

# The four pillars from docs/deva-pillar-walkthrough.md, plus the
# two "off-grid" buckets for endpoints that don't fit cleanly.
PILLAR_ORDER: tuple[str, ...] = (
    "Pillar 1 — Agent creation",
    "Pillar 2 — Evals",
    "Pillar 3 — Observability",
    "Pillar 4 — Auth + status",
    "Coming soon (feature-flagged) / lifecycle",
    "Other / meta",
)

# How long the summary column can grow before we truncate with an
# ellipsis. Tuned so the markdown table renders on a typical laptop
# screen without horizontal scroll.
_SUMMARY_MAX_CHARS = 70


def _classify(method: str, path: str) -> str:
    """Map ``(method, path)`` to a pillar label.

    Rules are ordered most-specific to least-specific. The first
    match wins; an endpoint that fits multiple buckets always lands
    in the most-specific one. Updating this function is the only
    place to touch when a new endpoint ships — every other label in
    the output flows from here.
    """
    # Pillar 4 — narrowest matches (health probes, single-job poll,
    # the legacy unversioned run-submit endpoint).
    if path in {"/healthz", "/ready"}:
        return "Pillar 4 — Auth + status"
    if path in {"/jobs/{job_id}", "/run"}:
        return "Pillar 4 — Auth + status"

    # Pillar 3 — cross-run observability. Listing jobs (versioned +
    # legacy) and the trace endpoint live here; single-job
    # state-polling stays in Pillar 4 because it's transactional,
    # not a browsing surface.
    if path in {"/jobs", "/api/v1/jobs"}:
        return "Pillar 3 — Observability"
    if path.endswith("/trace") or path.startswith("/runs/"):
        return "Pillar 3 — Observability"

    # Feature-flagged or lifecycle — GitHub publish/history are
    # advertised but return 503 today; DELETE is the soft-delete.
    if path.endswith("/publish") or path.endswith("/history"):
        return "Coming soon (feature-flagged) / lifecycle"
    if method == "DELETE" and path.startswith("/api/v1/agents/"):
        return "Coming soon (feature-flagged) / lifecycle"

    # Pillar 2 — the eval trio (kickoff, scorecard, history).
    if "/evals" in path:
        return "Pillar 2 — Evals"

    # Pillar 1 — agent CRUD + run. Everything left under /agents
    # (versioned and unversioned) lands here.
    if path in ("/agents", "/api/v1/agents") or path.endswith("/from-wizard"):
        return "Pillar 1 — Agent creation"
    if path.endswith("/validate") or path.endswith("/runs"):
        return "Pillar 1 — Agent creation"
    if path.startswith("/api/v1/agents/{") and method == "GET":
        return "Pillar 1 — Agent creation"

    # Catch-all — anything new the runtime adds without us updating
    # the classifier lands here. Shows up in the output so the
    # operator knows to re-label it next time.
    return "Other / meta"


def _fetch_spec(url: str) -> dict[str, Any]:
    """Fetch + parse the OpenAPI spec from ``<url>/api/v1/openapi.json``.

    Five-second timeout — the endpoint is unauthenticated and trivially
    cheap; a slow response means the runtime is unreachable, not that
    we should wait longer.
    """
    spec_url = f"{url.rstrip('/')}/api/v1/openapi.json"
    try:
        with urllib.request.urlopen(spec_url, timeout=5) as resp:
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except urllib.error.URLError as exc:  # pragma: no cover - network
        print(f"error: could not fetch {spec_url}: {exc}", file=sys.stderr)
        sys.exit(2)


def _collect_endpoints(spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Walk the OpenAPI paths and return ``[(METHOD, path, summary), ...]``.

    OpenAPI paths nest methods as dict keys; the ``"parameters"`` key
    (when present at the path level) is a sibling — exclude it so we
    don't emit a phantom row.
    """
    endpoints: list[tuple[str, str, str]] = []
    for path, methods in sorted(spec["paths"].items()):
        for method, op in methods.items():
            if method == "parameters":
                continue
            summary = (op.get("summary") or "").strip()
            endpoints.append((method.upper(), path, summary))
    return endpoints


def _bucket(
    endpoints: list[tuple[str, str, str]],
) -> dict[str, list[tuple[str, str, str]]]:
    """Apply :func:`_classify` to every endpoint and return a
    pillar-ordered dict."""
    buckets: dict[str, list[tuple[str, str, str]]] = {pillar: [] for pillar in PILLAR_ORDER}
    for method, path, summary in endpoints:
        buckets[_classify(method, path)].append((method, path, summary))
    return buckets


def _render_markdown(
    spec: dict[str, Any],
    runtime_url: str,
    buckets: dict[str, list[tuple[str, str, str]]],
    *,
    total: int,
) -> str:
    """Build the human-readable markdown report."""
    lines: list[str] = []
    lines.append("# MDK live endpoint inventory")
    lines.append("")
    lines.append(f"**Runtime:** `{runtime_url}`")
    lines.append(f"**Spec version:** {spec['info'].get('version', '?')}")
    lines.append(f"**Total endpoints advertised in OpenAPI:** {total}")
    lines.append("")
    for pillar in PILLAR_ORDER:
        items = buckets.get(pillar) or []
        if not items:
            continue
        lines.append(f"## {pillar}  _({len(items)})_")
        lines.append("")
        lines.append("| Method | Path | Summary |")
        lines.append("|---|---|---|")
        for method, path, summary in items:
            short = summary[:_SUMMARY_MAX_CHARS] + (
                "…" if len(summary) > _SUMMARY_MAX_CHARS else ""
            )
            lines.append(f"| `{method}` | `{path}` | {short} |")
        lines.append("")
    return "\n".join(lines)


def _render_json(
    spec: dict[str, Any],
    runtime_url: str,
    buckets: dict[str, list[tuple[str, str, str]]],
    *,
    total: int,
) -> str:
    """Build the machine-readable JSON variant. Useful for piping
    into ``jq`` or feeding a UI."""
    payload = {
        "runtime": runtime_url,
        "spec_version": spec["info"].get("version", "?"),
        "total": total,
        "pillars": [
            {
                "name": pillar,
                "count": len(items),
                "endpoints": [{"method": m, "path": p, "summary": s} for m, p, s in items],
            }
            for pillar, items in buckets.items()
            if items
        ],
    }
    return json.dumps(payload, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List the MDK endpoint surface grouped by pillar.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage", 1)[1] if "Usage" in (__doc__ or "") else "",
    )
    parser.add_argument(
        "runtime_url",
        nargs="?",
        default=os.environ.get("MDK_BASE"),
        help=(
            "Runtime URL (e.g. https://movate-dev-api.<hash>.eastus2.azurecontainerapps.io). "
            "Falls back to $MDK_BASE if not given."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of markdown (for jq / programmatic use).",
    )
    args = parser.parse_args()

    if not args.runtime_url:
        parser.error("runtime URL is required — pass as a positional arg or set MDK_BASE")

    spec = _fetch_spec(args.runtime_url)
    endpoints = _collect_endpoints(spec)
    buckets = _bucket(endpoints)

    if args.json:
        out = _render_json(spec, args.runtime_url, buckets, total=len(endpoints))
    else:
        out = _render_markdown(spec, args.runtime_url, buckets, total=len(endpoints))
    print(out)


if __name__ == "__main__":
    main()
