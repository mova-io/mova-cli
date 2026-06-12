#!/usr/bin/env python3
"""Render the landing page's Pattern Catalog fragment from the registry.

Statically generates the ``#catalog`` in-page view of the movate-dev landing
page (the landing Container App serves a single HTML doc from ``HTML_B64`` —
see ``README.md`` — so the catalog lives inside ``index.html.tmpl`` behind a
hash route, exactly like the Agent Control Plane view, not as a separate
``catalog.html``).

Invoked by ``deploy-landing.sh`` at deploy time; writes an HTML fragment (one
card per pattern: name, kind, topology, description, and the exact
``mdk init`` scaffold snippet) to stdout, which the deploy script substitutes
for the ``__PATTERN_CATALOG__`` placeholder.

The data comes straight from the registry (:data:`movate.templates.PATTERN_TEMPLATES`,
the same source ``mdk patterns list --json`` reads — record shape mirrored
from ``src/movate/cli/patterns_cmd.py``), so the page can never drift from
what ``mdk init --pattern`` actually accepts. Run from the repo so the
``movate`` package resolves, e.g.::

    uv run python infra/azure/landing/render_catalog.py
"""

from __future__ import annotations

import html
import sys

from movate.templates import PATTERN_TEMPLATES, list_patterns


def _card(name: str) -> str:
    """One catalog card: name + kind pill, topology line, description, snippet."""
    _dir, is_workflow, description, topology = PATTERN_TEMPLATES[name]
    kind = "workflow" if is_workflow else "agent"
    init_command = f"mdk init <target-dir> --pattern {name}"
    return (
        '<div class="pat">'
        f'<div class="pat-head"><h2>{html.escape(name)}</h2>'
        f'<span class="pat-kind {kind}">{kind}</span></div>'
        f'<p class="pat-topo">{html.escape(topology)}</p>'
        f'<p class="pat-desc">{html.escape(description)}</p>'
        f'<code class="pat-init">{html.escape(init_command)}</code>'
        "</div>"
    )


def render() -> str:
    """The full catalog fragment (intro line + the card grid)."""
    names = list_patterns()
    workflows = sum(1 for n in names if PATTERN_TEMPLATES[n][1])
    agents = len(names) - workflows
    intro = (
        f'<p class="sub" style="margin-bottom:18px">All {len(names)} governed patterns '
        f"({workflows} workflow &middot; {agents} agent) from the mdk registry &mdash; "
        "each bakes in bounds (budgets, fan-out caps, turn caps), eval-gates and full "
        "tracing. Scaffold any of them with the <code>mdk init</code> snippet on its card. "
        "Generated at deploy time from <code>movate.templates</code> "
        "(the registry behind <code>mdk patterns list</code>).</p>"
    )
    cards = "\n".join(_card(name) for name in names)
    return f'{intro}\n<div class="pat-grid">\n{cards}\n</div>'


if __name__ == "__main__":
    sys.stdout.write(render() + "\n")
