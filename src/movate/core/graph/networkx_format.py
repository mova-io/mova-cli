"""graphology JSON → NetworkX adapter for the PyVis exporter.

The knowledge-graph query API (ADR 046 D5) returns subgraphs in **graphology
import JSON** (ADR 046 D3)::

    {
      "attributes": {"name": "project-42 knowledge graph"},
      "nodes": [
        {"key": "n_saml_sso",
         "attributes": {"label": "SAML SSO", "type": "entity",
                        "size": 6, "color": "#4f86c6",
                        "x": 0.42, "y": -1.13, "community": 3,
                        "properties": {"confidence": 0.9},
                        "source_provenance": {"url": "https://..."}}}
      ],
      "edges": [
        {"key": "e_1", "source": "n_saml_sso", "target": "n_sec_policy_v3",
         "attributes": {"label": "governed-by", "weight": 0.8}}
      ]
    }

:func:`graphology_to_networkx` turns that into a :class:`networkx.Graph` whose
node/edge attributes are already shaped for PyVis: a stable ``color`` per node
``type``, a ``size`` scaled by degree (so hubs read as bigger), and an HTML
``title`` (the hover tooltip) carrying label + type + a few key properties +
the provenance source URL. PyVis renders a NetworkX graph directly via
``Network.from_nx`` / ``add_node``.

This module is **pure**: no network, no file I/O, no PyVis import — only
NetworkX (a transitive dependency of PyVis, imported lazily so importing
``movate.core.graph`` never hard-fails when the ``graph-pyvis`` extra is
absent). That keeps it trivially unit-testable and keeps the adapter seam
honest — the CLI command owns I/O; the adapter owns the shape mapping.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import networkx as nx

# A small, stable, colour-blind-friendly palette. Node ``type`` strings are
# mapped onto it deterministically (sorted-order hashing below) so the same
# graph always renders the same colours and the legend is reproducible.
_PALETTE: tuple[str, ...] = (
    "#4f86c6",  # blue
    "#e0823d",  # orange
    "#5aa469",  # green
    "#c0504d",  # red
    "#8064a2",  # purple
    "#4bacc6",  # teal
    "#d9b43c",  # gold
    "#9b6b4a",  # brown
    "#d16ba5",  # pink
    "#7f8c8d",  # grey
)

_DEFAULT_COLOR = "#7f8c8d"  # grey — untyped / unknown nodes

# Degree → node size mapping. Base size for an isolated node, plus a per-degree
# bump, capped so a mega-hub doesn't swallow the canvas.
_BASE_SIZE = 10.0
_SIZE_PER_DEGREE = 3.0
_MAX_SIZE = 60.0

# How many of a node's ``properties`` to surface in the hover tooltip. Keeps
# the tooltip readable on property-heavy nodes.
_MAX_TOOLTIP_PROPS = 6


def build_type_color_map(types: list[str]) -> dict[str, str]:
    """Return a deterministic ``node type → hex colour`` mapping.

    Types are assigned palette colours in **sorted** order so the mapping is
    stable across exports (a reproducible legend). When there are more types
    than palette entries, colours wrap around — distinctness degrades
    gracefully rather than erroring.

    Args:
        types: The distinct node ``type`` strings present in the graph.

    Returns:
        ``{type: "#rrggbb"}`` for every input type. Empty input → ``{}``.
    """
    ordered = sorted({t for t in types if t})
    return {t: _PALETTE[i % len(_PALETTE)] for i, t in enumerate(ordered)}


def graphology_to_networkx(graphology_json: dict[str, Any]) -> nx.Graph:
    """Convert a graphology-import-JSON document into a PyVis-ready NetworkX graph.

    Node attributes set on the returned graph:

    * ``label`` — display label (falls back to the node key).
    * ``type`` — node class, drives ``color`` + the legend.
    * ``color`` — stable hex colour per ``type`` (see :func:`build_type_color_map`).
    * ``size`` — scaled by degree so hub nodes are visually larger.
    * ``title`` — **HTML** hover tooltip: label, type, top properties, and the
      provenance source URL when present.
    * ``x`` / ``y`` — copied through when the API shipped persisted layout
      coordinates (ADR 046 D4), so PyVis can honour the server's layout.

    Edge attributes: ``label``/``title`` (the relation type), ``weight``, and
    ``value`` (PyVis renders edge thickness from ``value``).

    The conversion is tolerant: malformed nodes/edges (missing key, endpoints
    that reference an undeclared node) are skipped rather than raising — a
    partial graph is more useful than a hard failure, and mirrors the
    extractor's best-effort posture (``kb.graph_extract``). An empty or
    structurally-empty document yields an empty graph.

    Args:
        graphology_json: A parsed graphology import-JSON object
            (``{"attributes"?, "nodes": [...], "edges": [...]}``).

    Returns:
        A :class:`networkx.Graph` ready to hand to ``pyvis.network.Network``.

    Raises:
        TypeError: If ``graphology_json`` is not a mapping.
    """
    import networkx as nx  # noqa: PLC0415 — lazy: NetworkX rides in the graph-pyvis extra

    if not isinstance(graphology_json, dict):
        raise TypeError(
            f"graphology_to_networkx expects a dict, got {type(graphology_json).__name__}"
        )

    graph: nx.Graph = nx.Graph()

    raw_nodes = graphology_json.get("nodes") or []
    raw_edges = graphology_json.get("edges") or []

    # First pass: collect declared keys + the distinct node types so we can
    # build a stable colour map before we know degrees.
    declared: list[tuple[str, dict[str, Any]]] = []
    seen_types: list[str] = []
    for node in raw_nodes:
        if not isinstance(node, dict):
            continue
        key = node.get("key")
        if key is None or key == "":
            continue
        attrs = _attrs_of(node)
        declared.append((str(key), attrs))
        type_ = str(attrs.get("type", "")).strip()
        if type_:
            seen_types.append(type_)

    color_map = build_type_color_map(seen_types)

    valid_keys: set[str] = set()
    for key, attrs in declared:
        type_ = str(attrs.get("type", "")).strip()
        graph.add_node(
            key,
            label=str(attrs.get("label", key)),
            type=type_,
            color=color_map.get(type_, _DEFAULT_COLOR),
            # `size`/`title` are finalised in a second pass once degrees are
            # known; placeholders here keep the attribute set consistent.
            size=_BASE_SIZE,
            **_passthrough_layout(attrs),
        )
        valid_keys.add(key)

    # Edges: skip any whose endpoints weren't declared as nodes (graphology
    # would reject those on import; we drop them silently).
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        src = edge.get("source")
        dst = edge.get("target")
        if src not in valid_keys or dst not in valid_keys:
            continue
        attrs = _attrs_of(edge)
        label = str(attrs.get("label", "")).strip()
        weight = _coerce_float(attrs.get("weight"))
        graph.add_edge(
            src,
            dst,
            label=label,
            title=label,
            weight=weight,
            # PyVis maps edge thickness off `value`; fall back to 1.0 so all
            # edges are visible even when the API omits weights.
            value=weight if weight is not None else 1.0,
        )

    # Second pass: now degrees are known — size by degree + build the tooltip.
    declared_attrs = {key: attrs for key, attrs in declared}
    for key in graph.nodes:
        degree = graph.degree[key]
        graph.nodes[key]["size"] = _size_for_degree(degree)
        graph.nodes[key]["title"] = _node_tooltip(
            label=graph.nodes[key]["label"],
            type_=graph.nodes[key]["type"],
            degree=degree,
            attrs=declared_attrs.get(key, {}),
        )

    return graph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attrs_of(element: dict[str, Any]) -> dict[str, Any]:
    """The ``attributes`` mapping of a graphology node/edge, or ``{}``.

    graphology nests display/data attributes under ``"attributes"``; tolerate a
    missing or non-dict value by returning an empty mapping.
    """
    attrs = element.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _passthrough_layout(attrs: dict[str, Any]) -> dict[str, float]:
    """Copy persisted layout coordinates (ADR 046 D4) through when present.

    The graph API may ship ``x``/``y`` (a server-computed ForceAtlas2 layout)
    so the client never has to lay the whole graph out on load. When present
    and numeric we pass them to PyVis; otherwise PyVis runs its own physics
    layout.
    """
    out: dict[str, float] = {}
    for axis in ("x", "y"):
        value = _coerce_float(attrs.get(axis))
        if value is not None:
            out[axis] = value
    return out


def _size_for_degree(degree: int) -> float:
    """Node size scaled by degree, capped at :data:`_MAX_SIZE`."""
    return min(_BASE_SIZE + _SIZE_PER_DEGREE * float(degree), _MAX_SIZE)


def _coerce_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on non-numeric input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _node_tooltip(*, label: str, type_: str, degree: int, attrs: dict[str, Any]) -> str:
    """Build the HTML hover tooltip for a node.

    Shows the label + type + degree, a handful of key ``properties``, and the
    provenance source URL when the API supplied one. Everything is
    HTML-escaped — node labels/properties come from extracted document content
    and must not be able to inject markup into the tooltip.
    """
    lines: list[str] = [f"<b>{html.escape(label)}</b>"]
    if type_:
        lines.append(f"type: {html.escape(type_)}")
    lines.append(f"connections: {degree}")

    props = attrs.get("properties")
    if isinstance(props, dict) and props:
        for shown, (prop_key, prop_val) in enumerate(props.items()):
            if shown >= _MAX_TOOLTIP_PROPS:
                break
            lines.append(f"{html.escape(str(prop_key))}: {html.escape(str(prop_val))}")

    source_url = _provenance_url(attrs)
    if source_url:
        lines.append(f"source: {html.escape(source_url)}")

    return "<br>".join(lines)


def _provenance_url(attrs: dict[str, Any]) -> str | None:
    """Pull a source URL out of the node's provenance, if any.

    The API exposes provenance (ADR 046 D5) as ``source_provenance``; tolerate
    either a dict carrying a ``url``/``source_url`` or a plain string.
    """
    prov = attrs.get("source_provenance")
    if isinstance(prov, dict):
        for candidate in ("url", "source_url", "source"):
            value = prov.get(candidate)
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(prov, str) and prov.strip():
        return prov.strip()
    return None
