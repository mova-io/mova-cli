"""Graphology-JSON -> NetworkX adapter.

The graph query API serializes a knowledge (sub)graph as **graphology
JSON** — the wire format produced by graphology's ``write`` /
``Graph.export``:

.. code-block:: json

    {
      "attributes": {"name": "kb"},
      "options": {"type": "directed", "multi": false, "allowSelfLoops": true},
      "nodes": [
        {"key": "n1", "attributes": {"label": "Acme", "type": "Org"}}
      ],
      "edges": [
        {"key": "e1", "source": "n1", "target": "n2",
         "attributes": {"weight": 2.0, "type": "EMPLOYS"}}
      ]
    }

This module converts that into a ``networkx`` graph so any networkx-aware
viewer (ipysigma, PyVis, ...) can render it. Node/edge ``attributes`` are
flattened onto the networkx node/edge data dicts verbatim; ``key`` becomes
the node id, ``source``/``target`` the edge endpoints.

``networkx`` is an OPT-IN dependency (the ``graph-notebook`` extra). It is
imported lazily so importing this module never requires it; the import is
only triggered when :func:`graphology_to_networkx` is actually called, and
a friendly install hint is raised if it is missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import networkx as nx

# Friendly, single-source install hint reused by every lazy import in the
# graph-notebook surface.
_INSTALL_HINT = (
    "This feature needs the optional 'graph-notebook' extra.\n"
    "  Install it with:  pip install 'movate-cli[graph-notebook]'\n"
    "  (pulls in networkx + ipysigma — both permissively licensed)."
)


class GraphFormatError(ValueError):
    """Raised when a payload isn't valid graphology JSON.

    Carries a human-readable message pointing at the offending field so
    the notebook/CLI caller can surface *why* the graph couldn't be
    adapted, rather than a bare ``KeyError`` / ``TypeError`` from deep in
    the conversion.
    """


def _require_networkx() -> Any:
    """Lazily import networkx, raising a friendly hint if it's absent."""
    try:
        import networkx as nx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(_INSTALL_HINT) from exc
    return nx


def _add_nodes(graph: nx.Graph, nodes: Any) -> None:
    """Add graphology nodes to ``graph``, validating shape."""
    if not isinstance(nodes, list):
        raise GraphFormatError("'nodes' must be a list")
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise GraphFormatError(f"node at index {index} is not an object")
        if "key" not in node:
            raise GraphFormatError(f"node at index {index} is missing required 'key'")
        node_attrs = node.get("attributes") or {}
        if not isinstance(node_attrs, dict):
            raise GraphFormatError(f"node {node['key']!r} 'attributes' must be an object")
        graph.add_node(node["key"], **node_attrs)


def _add_edges(graph: nx.Graph, edges: Any) -> None:
    """Add graphology edges to ``graph``, validating shape."""
    if not isinstance(edges, list):
        raise GraphFormatError("'edges' must be a list")
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise GraphFormatError(f"edge at index {index} is not an object")
        if "source" not in edge or "target" not in edge:
            raise GraphFormatError(f"edge at index {index} is missing required 'source'/'target'")
        edge_attrs = edge.get("attributes") or {}
        if not isinstance(edge_attrs, dict):
            raise GraphFormatError(f"edge at index {index} 'attributes' must be an object")
        # graphology edges may carry their own "key"; preserve it as an
        # attribute so callers can correlate back to the wire payload.
        if "key" in edge:
            edge_attrs = {"key": edge["key"], **edge_attrs}
        graph.add_edge(edge["source"], edge["target"], **edge_attrs)


def graphology_to_networkx(payload: dict[str, Any]) -> nx.Graph:
    """Adapt a graphology-JSON ``payload`` into a NetworkX graph.

    The ``options.type`` field selects the networkx class:

    * ``"directed"`` -> :class:`networkx.DiGraph`
    * ``"undirected"`` (default) / ``"mixed"`` -> :class:`networkx.Graph`

    (graphology's ``mixed`` graphs — per-edge direction — have no exact
    networkx analogue; we collapse them to an undirected ``Graph`` so the
    topology is preserved for visualization, which is the only consumer.)

    Node ``attributes`` are copied onto the node data dict; the graphology
    ``key`` becomes the networkx node id. Edge ``attributes`` are copied
    onto the edge data dict; ``source``/``target`` are the endpoints.
    Top-level graph ``attributes`` land on ``graph.graph``.

    Raises:
        GraphFormatError: if ``payload`` isn't a dict, ``nodes``/``edges``
            aren't lists, a node lacks a ``key``, or an edge lacks
            ``source``/``target``.
    """
    if not isinstance(payload, dict):
        raise GraphFormatError(f"expected a graphology-JSON object, got {type(payload).__name__}")

    nx = _require_networkx()

    options = payload.get("options") or {}
    graph_type = (
        (options.get("type") or "undirected") if isinstance(options, dict) else "undirected"
    )
    # `nx` is locally typed Any (lazy import), so the inferred type of
    # `graph` is Any — no annotation needed; the public return type
    # (`nx.Graph` from the TYPE_CHECKING import) documents the contract.
    graph = nx.DiGraph() if graph_type == "directed" else nx.Graph()

    # Top-level graph attributes (graphology stores these under "attributes").
    attrs = payload.get("attributes")
    if isinstance(attrs, dict):
        graph.graph.update(attrs)

    _add_nodes(graph, payload.get("nodes", []))
    _add_edges(graph, payload.get("edges", []))

    return graph


__all__ = [
    "GraphFormatError",
    "graphology_to_networkx",
]
