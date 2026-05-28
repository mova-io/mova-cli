"""Graph format adapters — graphology JSON ↔ visualization-library models.

The knowledge-graph query API (ADR 046 D3) serializes every graph-returning
endpoint as **graphology import JSON** — a library-agnostic shape so any
WebGL/Canvas graph library (sigma.js, cytoscape, react-force-graph, PyVis)
is a thin adapter, not a rewrite.

This package holds those thin adapters. Today it ships
:func:`~movate.core.graph.networkx_format.graphology_to_networkx`, which the
PyVis exporter (``mdk graph export``) uses to build a standalone interactive
HTML viewer. The adapters are **pure** (no I/O, no network) so they are
trivially unit-tested.
"""

from __future__ import annotations

from movate.core.graph.networkx_format import graphology_to_networkx

__all__ = ["graphology_to_networkx"]
