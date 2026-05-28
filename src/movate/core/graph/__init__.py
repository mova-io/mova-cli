"""Knowledge-graph helpers shared across CLI viewers.

This package holds *pure*, dependency-light helpers that operate on the
graphology JSON the runtime's graph API emits
(``GET /api/v1/projects/{id}/graph`` and friends). Keeping these here —
rather than inside any one viewer module — lets every viewer option in the
viz bake-off (sigma, Dash Cytoscape, …) share one format contract.

Nothing in this package imports a heavy viz dependency (``dash``,
``dash-cytoscape``, ``plotly``). Those live behind the opt-in
``graph-dash`` extra and are imported only by the Dash app module.
"""

from __future__ import annotations

from movate.core.graph.cytoscape_format import (
    CytoscapeElement,
    GraphologyJSON,
    graphology_to_cytoscape,
)

__all__ = [
    "CytoscapeElement",
    "GraphologyJSON",
    "graphology_to_cytoscape",
]
