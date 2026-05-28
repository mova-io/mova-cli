"""Adapter: graphology JSON → dash-cytoscape elements.

The runtime's graph API (ADR 046, branch ``feat/graph-query-api``) emits
**graphology** JSON::

    {
      "attributes": {...},
      "nodes": [{"key": "n1", "attributes": {"type": "Feature", ...}}, ...],
      "edges": [{"key": "e1", "source": "n1", "target": "n2",
                 "attributes": {"type": "REQUIRES", "weight": 0.8}}, ...]
    }

``dash-cytoscape`` wants a flat list of Cytoscape *elements*, each a
``{"data": {...}}`` dict — nodes carry ``id``/``label``/``type``, edges
carry ``source``/``target``/``label``::

    [
      {"data": {"id": "n1", "label": "SSO", "type": "Feature", "degree": 3}},
      {"data": {"source": "n1", "target": "n2", "label": "REQUIRES",
                "id": "e1", "weight": 0.8}},
    ]

This module is the single, *pure* translation between the two. It has no
viz dependency (no ``dash``/``dash-cytoscape``/``plotly`` import) so it
unit-tests trivially and is shared across viewer options.

Visual encoding decisions made *here* (so they're testable without a
browser):

* **``degree``** is computed per node and attached to ``data`` — the
  Cytoscape stylesheet maps it to node *size* via ``mapData(degree, …)``.
* **``type``** is passed through verbatim — the stylesheet maps it to node
  *color* via selectors. We do not bake colors in here; keeping the raw
  type string keeps the data layer presentation-agnostic.

The adapter is deliberately tolerant of partial/missing attributes: a real
graph API response is trusted, but provenance fields can legitimately be
absent (a node with no source chunk yet), and we must never raise mid-render.
"""

from __future__ import annotations

from typing import Any, TypedDict


class CytoscapeData(TypedDict, total=False):
    """The ``data`` payload of one Cytoscape element.

    ``total=False`` because nodes and edges populate different keys: a node
    has ``id``/``label``/``type``/``degree``; an edge has
    ``id``/``source``/``target``/``label``/``weight``.
    """

    id: str
    label: str
    type: str
    degree: int
    source: str
    target: str
    weight: float


class CytoscapeElement(TypedDict):
    """One dash-cytoscape element: ``{"data": {...}}``.

    A list of these is what ``dash_cytoscape.Cytoscape(elements=...)``
    consumes.
    """

    data: CytoscapeData


# A graphology JSON document, loosely typed — it crosses the HTTP boundary
# as ``dict[str, Any]`` from ``response.json()``, so we don't over-constrain.
GraphologyJSON = dict[str, Any]


def _node_label(key: str, attrs: dict[str, Any]) -> str:
    """Pick the best human-readable label for a node.

    Preference order: explicit ``label`` → ``name`` (the GraphRAG entity
    field) → the node key itself (always present). Never returns empty —
    a blank label renders as an unclickable invisible node.
    """
    for candidate in (attrs.get("label"), attrs.get("name")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return key


def _edge_label(attrs: dict[str, Any]) -> str:
    """Edge label from its relation ``type`` (e.g. ``REQUIRES``).

    Falls back to ``label`` then empty string — an unlabeled edge is fine
    (the line still renders); an empty type just means no caption.
    """
    for candidate in (attrs.get("label"), attrs.get("type")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


def graphology_to_cytoscape(graphology_json: GraphologyJSON) -> list[CytoscapeElement]:
    """Convert one graphology JSON document to dash-cytoscape elements.

    Pure function. Given the ``{attributes, nodes, edges}`` shape the graph
    API emits, returns the flat ``[{"data": {...}}, ...]`` list
    ``dash_cytoscape.Cytoscape(elements=…)`` expects.

    Behavior:

    * **Empty / missing** ``nodes``/``edges`` → ``[]`` (empty graph renders
      as a blank canvas, never an error).
    * **Degree** is computed over the edge list and attached to each node's
      ``data`` so the stylesheet can size nodes by connectivity. Both
      endpoints of every edge increment, including self-loops (counted
      twice, matching Cytoscape's own degree semantics).
    * **Edges to unknown nodes** are dropped — a dangling ``source``/
      ``target`` that isn't in ``nodes`` would render as a floating line
      Cytoscape can't anchor. (Defensive: a well-formed windowed subgraph
      from the API shouldn't contain these, but a partial-retrieval failure
      mode could.)
    * Arbitrary extra node/edge attributes (``description``, ``confidence``,
      …) are copied through into ``data`` so callbacks/side-panels can read
      them without a second fetch. Reserved keys (``id``/``source``/
      ``target``/``label``/``degree``) are owned by the adapter and not
      overwritten by passthrough.

    The function never mutates its input.
    """
    nodes_in = graphology_json.get("nodes") or []
    edges_in = graphology_json.get("edges") or []

    # First pass over nodes: collect keys (so we can drop dangling edges)
    # and seed degree at 0 for every real node.
    node_keys: set[str] = set()
    degree: dict[str, int] = {}
    for node in nodes_in:
        key = node.get("key")
        if not isinstance(key, str) or not key:
            continue
        node_keys.add(key)
        degree[key] = 0

    # Pass over edges: count degree, but only for edges whose BOTH endpoints
    # are real nodes (so degree matches the edges we actually emit).
    valid_edges: list[dict[str, Any]] = []
    for edge in edges_in:
        source = edge.get("source")
        target = edge.get("target")
        if source not in node_keys or target not in node_keys:
            continue
        valid_edges.append(edge)
        degree[source] += 1
        degree[target] += 1

    elements: list[CytoscapeElement] = []

    # Node elements.
    for node in nodes_in:
        key = node.get("key")
        if not isinstance(key, str) or not key:
            continue
        attrs = node.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        data: CytoscapeData = {
            "id": key,
            "label": _node_label(key, attrs),
            "type": str(attrs.get("type", "")),
            "degree": degree[key],
        }
        _copy_passthrough(attrs, data, reserved={"id", "label", "type", "degree"})
        elements.append({"data": data})

    # Edge elements (only the validated ones).
    for edge in valid_edges:
        attrs = edge.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        data = {
            "source": str(edge["source"]),
            "target": str(edge["target"]),
            "label": _edge_label(attrs),
        }
        edge_key = edge.get("key")
        if isinstance(edge_key, str) and edge_key:
            data["id"] = edge_key
        _copy_passthrough(attrs, data, reserved={"id", "source", "target", "label"})
        elements.append({"data": data})

    return elements


def _copy_passthrough(attrs: dict[str, Any], data: CytoscapeData, *, reserved: set[str]) -> None:
    """Copy non-reserved scalar/collection attrs from ``attrs`` into ``data``.

    Lets side-panel callbacks read ``description``/``confidence``/
    ``source_chunk_ids`` straight off the element without a second fetch.
    Reserved keys (owned by the adapter) are skipped so passthrough can't
    clobber the structural fields. JSON-incompatible values are skipped —
    Dash serializes ``data`` to the browser, so only JSON-safe types belong.
    """
    for attr_key, value in attrs.items():
        if attr_key in reserved:
            continue
        if isinstance(value, (str, int, float, bool, list, dict)) or value is None:
            data[attr_key] = value  # type: ignore[literal-required]  # extra keys allowed
