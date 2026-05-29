"""Graphology-native view models for the read-only graph query API (ADR 046).

These models are the **wire contract** the sigma.js client imports with
zero transform. ``GraphologyDoc`` serializes to exactly the shape
``graphology.Graph.import()`` accepts::

    { "attributes": {},
      "nodes": [ {"key": "<id>", "attributes": {...}} ],
      "edges": [ {"key": "<id>", "source": "<id>", "target": "<id>",
                  "attributes": {...}} ] }

Deliberately separate from :mod:`movate.core.models` (the *persisted*
Entity/Relation schema) so the visualization contract evolves
independently of storage — same separation rationale as
``runtime/schemas.py`` vs ``core/models.py``. ``runtime/schemas.py``
re-exports thin ``*View`` wrappers around these for the HTTP layer; the
field shapes are identical so a view is a structural pass-through.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GraphNode(BaseModel):
    """One node in a graphology document.

    ``key`` is the stable node id (the persisted ``Entity.entity_id``).
    ``attributes`` carries the visual + semantic payload sigma renders:
    ``label`` (display text), ``type`` (entity type / category),
    ``size`` (degree-derived, drives node radius), ``color`` (derived
    from ``type`` or ``community``), optional ``community`` (cluster id),
    and optional ``x`` / ``y`` layout coordinates.

    Layout coords are **omitted** when not stored — the client runs
    ForceAtlas2 to lay the graph out. (No layout is persisted today; the
    fields exist so a future stored-layout swap is additive.)
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """One edge in a graphology document.

    ``key`` is the stable edge id; ``source`` / ``target`` are node
    ``key``s. ``attributes`` carries ``label`` (the relation type /
    predicate) and ``weight`` (extraction confidence, drives edge
    thickness). Direction is preserved (source → target) for the client
    to render arrowheads.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    source: str
    target: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphologyDoc(BaseModel):
    """A complete graphology import document — the zero-transform contract.

    ``model_dump(mode="json")`` of this is exactly what a sigma.js client
    feeds to ``graph.import(data)``. ``attributes`` is the graph-level bag
    (empty today; reserved for graph-wide metadata like the layout name).
    """

    model_config = ConfigDict(extra="forbid")

    attributes: dict[str, Any] = Field(default_factory=dict)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class Provenance(BaseModel):
    """Where a node's facts came from — one source chunk's citation.

    Drives the node-detail panel's "sources" list so an operator can
    trace a graph node back to the passage(s) it was extracted from.
    ``url`` is the source document identifier (file path / URL recorded
    at ingest); ``snippet`` is a short preview of the chunk text.
    ``extraction_confidence`` is the strongest edge weight tying this
    chunk to the node (a coarse proxy for "how sure was the extractor").
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    url: str | None = None
    snippet: str | None = None
    extraction_confidence: float | None = None


class NodeNeighbor(BaseModel):
    """One 1-hop connected entity, as the drill-down panel renders it.

    Each neighbor is a connected graph node (a ticket / SOP / doc / feature
    / …) reached by a single relation from the focused node. ``relation``
    is the predicate type of that edge and ``direction`` says whether the
    focused node is the source (``out``) or target (``in``) — so the panel
    can group neighbors by relation type *and* render them as clickable
    links that re-center the graph or open the neighbor's own detail.

    Deliberately lighter than :class:`NodeDetail` (no provenance, no nested
    neighbors): just enough for one row in the "connected entities" list.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    """The neighbor's node id (``Entity.entity_id``) — the click target."""

    label: str
    type: str
    relation: str
    """The relation/predicate type of the edge to this neighbor."""

    direction: str
    """``out`` (focused node → neighbor) or ``in`` (neighbor → focused node)."""


class NodeDetail(BaseModel):
    """Full detail for a single graph node — the expand-on-demand payload.

    Returned by ``GET /api/v1/graph/nodes/{id}``. Carries the node's own
    attributes, its provenance (source chunks + url + confidence), the
    1-hop connected entities (``neighbors`` — the drill-down list, each
    tagged with its relation type + direction), a neighbor count (so the
    UI can show "expand → N more"), the agents that reference it, and a
    ``_links.expand`` hint pointing at the neighbors endpoint (HATEOAS-
    style: the client follows the link rather than constructing the URL).
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    """The node id (``Entity.entity_id``)."""

    label: str
    type: str
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    """Free-form node attributes (e.g. ``community``, source metadata)."""

    provenance: list[Provenance] = Field(default_factory=list)
    neighbors: list[NodeNeighbor] = Field(default_factory=list)
    """The node's 1-hop connected entities, each tagged with the relation
    type + direction — the drill-down panel's "connected entities" list.
    Capped at the node budget; empty when the node has no neighbors."""

    neighbor_count: int = 0
    referenced_by_agents: list[str] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict, alias="_links")
    """HATEOAS links. ``expand`` → the neighbors endpoint for this node.
    Aliased to ``_links`` on the wire (the conventional JSON key)."""


class NodeSearchHit(BaseModel):
    """One matching node in a graph search response (drives fly-to).

    Lighter than ``NodeDetail`` — just enough for the client to render a
    result row and center the camera on the node once imported.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str
