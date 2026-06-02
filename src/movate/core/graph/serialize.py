"""Entity/Relation → graphology-document serialization (ADR 046).

The single source of truth for the **graphology-native** wire shape. A
sigma.js client imports ``to_graphology(...).model_dump(mode="json")``
with zero transform.

Derivations (so the client doesn't have to compute them):

* **size** ← node degree. A hub node is bigger. Degree is computed from
  the edges in the *returned window* (not the whole graph) so a windowed
  subgraph still gets sensible relative sizes; ``size`` is clamped to a
  small pixel-ish range so one mega-hub doesn't dwarf everything.
* **color** ← ``community`` when present, else ``type``. Stable hash →
  palette index so the same category always gets the same color across
  requests (the client can override, but a sane default ships).
* **x / y** ← stored layout coords *if present* in the entity metadata,
  else **omitted** (the client runs ForceAtlas2).
* **confidence** ← ``metadata["confidence"]`` *if present* (ADR 046 D2's
  ``properties.confidence``), clamped to ``[0, 1]``, else **omitted** so the
  viewer treats the node as full-confidence and the wire shape is unchanged
  for graphs that never recorded a score.

Pure + backend-agnostic: takes already-loaded ``Entity`` / ``Relation``
lists, returns a ``GraphologyDoc``. No storage, no I/O.
"""

from __future__ import annotations

import hashlib

from movate.core.graph.models import GraphEdge, GraphNode, GraphologyDoc
from movate.core.models import Entity, Relation

# A small, colorblind-friendly categorical palette. The client may
# override per-type colors; this is the zero-config default so a freshly
# imported graph is legible immediately.
_PALETTE: tuple[str, ...] = (
    "#4e79a7",
    "#f28e2b",
    "#e15759",
    "#76b7b2",
    "#59a14f",
    "#edc948",
    "#b07aa1",
    "#ff9da7",
    "#9c755f",
    "#bab0ac",
)

# Node size bounds (sigma renders ``size`` as a radius-ish scalar). A
# degree-0 node still shows; a mega-hub is capped so the layout stays
# readable.
_MIN_SIZE = 4.0
_MAX_SIZE = 24.0
# Degree at which a node reaches _MAX_SIZE. Beyond this the size plateaus.
_SIZE_SATURATION_DEGREE = 12


def color_for(*, type: str | None, community: int | str | None) -> str:
    """Pick a stable palette color for a node.

    ``community`` wins when present (cluster coloring is more useful than
    type coloring once communities are computed); otherwise falls back to
    ``type``. An empty/None key hashes to a deterministic default slot, so
    every node always gets *a* color.
    """
    key = ""
    if community is not None and str(community) != "":
        key = f"c:{community}"
    elif type:
        key = f"t:{type}"
    digest = hashlib.sha256(key.encode()).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


def size_from_degree(degree: int) -> float:
    """Map a node's degree to a bounded display size.

    Linear from ``_MIN_SIZE`` (degree 0) up to ``_MAX_SIZE`` at
    ``_SIZE_SATURATION_DEGREE``, then flat. Keeps a hub from melting the
    layout while still making relative importance visible.
    """
    if degree <= 0:
        return _MIN_SIZE
    frac = min(degree, _SIZE_SATURATION_DEGREE) / _SIZE_SATURATION_DEGREE
    return round(_MIN_SIZE + frac * (_MAX_SIZE - _MIN_SIZE), 2)


def _confidence_of(entity: Entity) -> float | None:
    """Read a stored extraction-confidence score from an entity's metadata.

    ADR 046 D2 specifies that extraction confidence, where present, rides on
    the node as ``properties.confidence`` so the viewer can dim/filter
    low-confidence nodes. The extractor stores it under
    ``metadata["confidence"]`` (a ``[0, 1]``-ish score). Absent / non-numeric
    → ``None`` (the node is treated as full-confidence by the viewer + the
    ``min_confidence`` filter, so a graph that never recorded confidence is
    byte-for-byte unchanged). ``bool`` is rejected explicitly (it's an ``int``
    subclass) and the value is clamped into ``[0, 1]`` so a stray out-of-range
    score can't break the viewer's dim ramp.
    """
    if entity.metadata is None:
        return None
    raw = entity.metadata.get("confidence")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0.0, min(1.0, float(raw)))


def _community_of(entity: Entity) -> int | str | None:
    """Read a stored community/cluster id from an entity's metadata.

    No community detection ships yet (ADR 046 leaves it to a later pass),
    so this is ``None`` for today's graphs — but if a future ingest writes
    ``metadata={"community": ...}`` the serializer picks it up additively.
    """
    if entity.metadata is None:
        return None
    community = entity.metadata.get("community")
    if isinstance(community, (int, str)):
        return community
    return None


def _layout_coords(entity: Entity) -> tuple[float, float] | None:
    """Read stored ``(x, y)`` layout coords from metadata, if any.

    Omitted from the output when absent so the client knows to run its own
    layout (FA2) rather than stacking every node at the origin.
    """
    if entity.metadata is None:
        return None
    x = entity.metadata.get("x")
    y = entity.metadata.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return float(x), float(y)
    return None


def to_graphology(
    entities: list[Entity],
    relations: list[Relation],
) -> GraphologyDoc:
    """Serialize ``entities`` + ``relations`` into a graphology document.

    Only edges whose **both** endpoints are present in ``entities`` are
    emitted (a dangling edge has nothing to attach to in the rendered
    graph — same drop-on-join contract as the storage layer's
    ``expand_neighbors``). Degree (for ``size``) is computed from the
    surviving edges, so a windowed view sizes nodes by their *visible*
    connectivity.

    The result is graphology-import-ready: ``model_dump(mode="json")``
    feeds straight into ``graph.import(...)``.
    """
    node_ids = {e.entity_id for e in entities}

    # Keep only edges fully inside the window; compute degree over them.
    kept_relations = [
        r for r in relations if r.src_entity_id in node_ids and r.dst_entity_id in node_ids
    ]
    degree: dict[str, int] = {nid: 0 for nid in node_ids}
    for r in kept_relations:
        degree[r.src_entity_id] += 1
        degree[r.dst_entity_id] += 1

    nodes: list[GraphNode] = []
    for e in entities:
        community = _community_of(e)
        attributes: dict[str, object] = {
            "label": e.name,
            "type": e.type,
            "size": size_from_degree(degree.get(e.entity_id, 0)),
            "color": color_for(type=e.type, community=community),
        }
        if community is not None:
            attributes["community"] = community
        confidence = _confidence_of(e)
        if confidence is not None:
            # ADR 046 D2: extraction confidence rides on the node so the viewer
            # can dim/filter low-confidence nodes. Omitted when unrecorded (the
            # viewer treats an absent score as full confidence), keeping a
            # confidence-less graph's wire shape byte-for-byte unchanged.
            attributes["confidence"] = confidence
        coords = _layout_coords(e)
        if coords is not None:
            attributes["x"], attributes["y"] = coords
        nodes.append(GraphNode(key=e.entity_id, attributes=attributes))

    edges: list[GraphEdge] = []
    for r in kept_relations:
        edges.append(
            GraphEdge(
                key=r.relation_id,
                source=r.src_entity_id,
                target=r.dst_entity_id,
                attributes={"label": r.type, "weight": r.weight},
            )
        )

    return GraphologyDoc(attributes={}, nodes=nodes, edges=edges)
