"""Pure, backend-agnostic graph query operations (ADR 046).

Windowing, neighbor expansion, search, node detail (with provenance),
and bounded traversal — all composed over the ``StorageProvider``
Protocol's *existing* read surface (``list_entities`` / ``list_relations``
/ ``get_entity`` / ``expand_neighbors`` / ``list_kb_chunks``). No new
storage methods, no writes, no extraction.

Every operation is **hard-capped** (node + edge budgets) so a browser can
never receive a melt-the-tab payload. ``traverse`` is additionally
depth-and-breadth bounded.

Tenant + agent (== "project") scoping is enforced at the storage layer:
every call threads ``tenant_id`` through, and the single-record getters
return ``None`` for a cross-tenant id (404-not-403). A windowed subgraph
therefore can never contain a cross-tenant node or edge.

``mode``: ``knowledge`` is the GraphRAG entity/relation graph that ADR
010 extraction persists (the only graph today). ``topology`` is reserved
for a future agent/skill wiring graph and currently returns empty — the
endpoint stays stable for the client when it lands.
"""

from __future__ import annotations

from enum import StrEnum

from movate.core.graph.models import (
    GraphologyDoc,
    NodeDetail,
    NodeSearchHit,
    Provenance,
)
from movate.core.graph.serialize import to_graphology
from movate.core.models import Entity, Relation
from movate.storage.base import StorageProvider

# Hard payload caps. ``DEFAULT_CAP`` is the per-request node/edge budget
# when the caller doesn't ask; ``MAX_CAP`` is the ceiling a caller can
# raise to. Both guard the browser from an unbounded graph (ADR 046:
# "the browser never gets a melt-the-tab payload").
DEFAULT_CAP = 500
MAX_CAP = 5000

# Traversal bounds for ``POST /graph/query`` — depth (hops) and breadth
# (relations followed) are both capped so a hub can't blow up a traverse.
MAX_DEPTH = 6
DEFAULT_DEPTH = 1
# Snippet length for provenance previews — enough to recognize the
# passage, short enough to keep the detail payload small.
_SNIPPET_CHARS = 240
# How many candidate entities we scan before windowing. Bounded so a
# pathological graph can't make a single request load unbounded rows;
# generous enough that the windowing cap is the real limiter.
_SCAN_LIMIT = 100_000


class GraphMode(StrEnum):
    """Which graph a query targets."""

    KNOWLEDGE = "knowledge"
    """The GraphRAG entity/relation graph (ADR 010 extraction)."""

    TOPOLOGY = "topology"
    """Reserved: agent/skill wiring graph. Empty until implemented."""


def clamp_cap(value: int | None) -> int:
    """Clamp a caller-supplied node/edge cap into ``[1, MAX_CAP]``.

    ``None`` / non-positive → ``DEFAULT_CAP``. Above ``MAX_CAP`` → capped.
    Single source of truth so every endpoint enforces the same ceiling.
    """
    if value is None or value <= 0:
        return DEFAULT_CAP
    return min(int(value), MAX_CAP)


def _clamp_depth(value: int | None) -> int:
    if value is None or value <= 0:
        return DEFAULT_DEPTH
    return min(int(value), MAX_DEPTH)


async def windowed_subgraph(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    mode: GraphMode = GraphMode.KNOWLEDGE,
    type: str | None = None,
    root: str | None = None,
    depth: int | None = None,
    limit: int | None = None,
) -> GraphologyDoc:
    """A capped window onto the graph, as a graphology document.

    Two windowing strategies:

    * **rooted** (``root`` set) — a bounded k-hop expansion from one node
      via the storage layer's ``expand_neighbors`` (depth = ``depth``,
      relations budget = ``limit``). The natural "show me around this
      node" view.
    * **unrooted** (no ``root``) — the first ``limit`` entities for the
      ``(agent, tenant)`` graph (optionally filtered to ``type``) plus
      every relation among them. A whole-graph overview, capped.

    ``mode=topology`` returns an empty document (not yet implemented).
    Every path is hard-capped at ``min(limit, MAX_CAP)`` nodes/edges.
    """
    cap = clamp_cap(limit)
    if mode is not GraphMode.KNOWLEDGE:
        # Topology graph not persisted yet — stable empty response.
        return to_graphology([], [])

    if root is not None:
        return await _rooted_window(
            storage,
            agent=agent,
            tenant_id=tenant_id,
            root=root,
            depth=_clamp_depth(depth),
            cap=cap,
            type_filter=type,
        )

    return await _unrooted_window(
        storage,
        agent=agent,
        tenant_id=tenant_id,
        cap=cap,
        type_filter=type,
    )


async def _rooted_window(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    root: str,
    depth: int,
    cap: int,
    type_filter: str | None,
) -> GraphologyDoc:
    # Reject a cross-tenant / unknown root up front (no leak): get_entity
    # returns None for a foreign tenant, so an empty doc comes back rather
    # than another tenant's neighborhood.
    seed = await storage.get_entity(root, tenant_id=tenant_id)
    if seed is None or seed.agent != agent:
        return to_graphology([], [])
    sub = await storage.expand_neighbors(
        agent=agent,
        tenant_id=tenant_id,
        entity_ids=[root],
        hops=depth,
        limit=cap,
    )
    entities = _apply_type_filter(sub.entities, type_filter, always_keep={root})
    entities = entities[:cap]
    relations = _cap_relations(sub.relations, entities, cap)
    return to_graphology(entities, relations)


async def _unrooted_window(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    cap: int,
    type_filter: str | None,
) -> GraphologyDoc:
    entities = await storage.list_entities(agent=agent, tenant_id=tenant_id, limit=_SCAN_LIMIT)
    entities = _apply_type_filter(entities, type_filter)
    entities = entities[:cap]
    if not entities:
        return to_graphology([], [])
    relations = await storage.list_relations(agent=agent, tenant_id=tenant_id, limit=_SCAN_LIMIT)
    relations = _cap_relations(relations, entities, cap)
    return to_graphology(entities, relations)


async def expand_node_neighbors(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    node_id: str,
    depth: int | None = None,
    limit: int | None = None,
) -> GraphologyDoc:
    """Expand-on-demand: the neighborhood of one node, graphology JSON.

    Used by the client when an operator clicks a node to grow the graph.
    Bounded by ``depth`` (hops, capped at ``MAX_DEPTH``) and ``limit``
    (node/edge cap). An unknown / cross-tenant node → empty document.
    """
    cap = clamp_cap(limit)
    seed = await storage.get_entity(node_id, tenant_id=tenant_id)
    if seed is None or seed.agent != agent:
        return to_graphology([], [])
    sub = await storage.expand_neighbors(
        agent=agent,
        tenant_id=tenant_id,
        entity_ids=[node_id],
        hops=_clamp_depth(depth),
        limit=cap,
    )
    entities = sub.entities[:cap]
    relations = _cap_relations(sub.relations, entities, cap)
    return to_graphology(entities, relations)


async def node_detail(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    node_id: str,
) -> NodeDetail | None:
    """Full detail for one node: properties + provenance + neighbor count.

    Returns ``None`` for an unknown or cross-tenant node (the caller maps
    that to a 404 — never a 403, so a tenant can't probe for foreign ids).

    Provenance resolves each of the node's ``source_chunk_ids`` to its
    source URL + a text snippet by indexing the agent's chunks once.
    ``neighbor_count`` and ``referenced_by_agents`` come from a 1-hop
    expansion (capped) — enough for the UI's "expand → N more" affordance.
    """
    entity = await storage.get_entity(node_id, tenant_id=tenant_id)
    if entity is None or entity.agent != agent:
        return None

    provenance = await _build_provenance(storage, agent=agent, tenant_id=tenant_id, entity=entity)

    # 1-hop expansion (capped) gives both the neighbor count and a cheap
    # answer to "which agents reference this node" — for the per-agent
    # graph that's just this agent, but the shape is forward-compatible
    # with a future shared/cross-agent graph.
    sub = await storage.expand_neighbors(
        agent=agent,
        tenant_id=tenant_id,
        entity_ids=[node_id],
        hops=1,
        limit=MAX_CAP,
    )
    neighbor_count = max(0, len({e.entity_id for e in sub.entities} - {node_id}))
    referenced_by = sorted({e.agent for e in sub.entities})

    properties: dict[str, object] = {}
    if entity.metadata:
        properties.update(entity.metadata)

    return NodeDetail(
        key=entity.entity_id,
        label=entity.name,
        type=entity.type,
        description=entity.description,
        properties=properties,
        provenance=provenance,
        neighbor_count=neighbor_count,
        referenced_by_agents=referenced_by,
        _links={"expand": f"/api/v1/graph/nodes/{entity.entity_id}/neighbors"},
    )


async def search_nodes(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    q: str,
    type: str | None = None,
    limit: int | None = None,
) -> list[NodeSearchHit]:
    """Substring (case-insensitive) label search for fly-to.

    A lexical match on ``name`` — no embedding, no model call (the
    vector-seed ``search_entities`` is a *retrieval* primitive; this is a
    UI fly-to box). Optionally filtered to ``type``. Capped at the node
    budget. Empty ``q`` → ``[]``.
    """
    needle = q.strip().lower()
    if not needle:
        return []
    cap = clamp_cap(limit)
    entities = await storage.list_entities(agent=agent, tenant_id=tenant_id, limit=_SCAN_LIMIT)
    hits: list[NodeSearchHit] = []
    for e in entities:
        if type is not None and e.type != type:
            continue
        if needle in e.name.lower():
            hits.append(NodeSearchHit(key=e.entity_id, label=e.name, type=e.type))
            if len(hits) >= cap:
                break
    return hits


async def traverse(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    root: str,
    depth: int | None = None,
    limit: int | None = None,
    type: str | None = None,
) -> GraphologyDoc:
    """Bounded traverse/subgraph from a root node — ``POST /graph/query``.

    Depth- and breadth-bounded (``depth`` ≤ ``MAX_DEPTH`` hops, ``limit``
    ≤ ``MAX_CAP`` relations/nodes). Same no-leak contract as the rooted
    window: an unknown / cross-tenant ``root`` yields an empty document.
    Reuses the rooted-window path so traversal mechanics live in one
    place.
    """
    cap = clamp_cap(limit)
    return await _rooted_window(
        storage,
        agent=agent,
        tenant_id=tenant_id,
        root=root,
        depth=_clamp_depth(depth),
        cap=cap,
        type_filter=type,
    )


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _apply_type_filter(
    entities: list[Entity],
    type_filter: str | None,
    *,
    always_keep: set[str] | None = None,
) -> list[Entity]:
    """Filter entities to ``type_filter`` (no-op when ``None``).

    ``always_keep`` ids survive the filter regardless of type — used so a
    rooted window never drops its own seed node even if the operator
    filtered to a different type.
    """
    if type_filter is None:
        return entities
    keep = always_keep or set()
    return [e for e in entities if e.type == type_filter or e.entity_id in keep]


def _cap_relations(
    relations: list[Relation],
    entities: list[Entity],
    cap: int,
) -> list[Relation]:
    """Keep edges fully inside the windowed node set, strongest first, capped.

    Mirrors the serializer's drop-dangling rule but applied *before*
    serialization so the edge budget is enforced on the strongest edges
    (descending weight) — a truncated window keeps the most important
    relationships.
    """
    node_ids = {e.entity_id for e in entities}
    internal = [r for r in relations if r.src_entity_id in node_ids and r.dst_entity_id in node_ids]
    internal.sort(key=lambda r: r.weight, reverse=True)
    return internal[:cap]


async def _build_provenance(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    entity: Entity,
) -> list[Provenance]:
    """Resolve a node's source chunk ids to citation provenance.

    Indexes the agent's chunks once (bounded scan) and joins on the
    node's ``source_chunk_ids``. ``extraction_confidence`` is the strongest
    edge weight that touches this node *and* cites the same chunk — a
    coarse but cheap confidence proxy without storing per-node scores.
    A chunk id that no longer resolves (re-ingest churn) is still listed
    with a null url/snippet so the count stays honest.
    """
    chunk_ids = list(entity.source_chunk_ids)
    if not chunk_ids:
        return []

    chunks = await storage.list_kb_chunks(agent=agent, tenant_id=tenant_id, limit=_SCAN_LIMIT)
    by_id = {c.chunk_id: c for c in chunks}

    # Strongest edge weight per chunk that touches this node — confidence.
    relations = await storage.list_relations(agent=agent, tenant_id=tenant_id, limit=_SCAN_LIMIT)
    conf_by_chunk: dict[str, float] = {}
    for r in relations:
        if entity.entity_id not in (r.src_entity_id, r.dst_entity_id):
            continue
        for cid in r.source_chunk_ids:
            prev = conf_by_chunk.get(cid)
            if prev is None or r.weight > prev:
                conf_by_chunk[cid] = r.weight

    provenance: list[Provenance] = []
    for cid in chunk_ids:
        chunk = by_id.get(cid)
        snippet: str | None = None
        url: str | None = None
        if chunk is not None:
            url = chunk.source
            text = chunk.text.strip()
            snippet = text if len(text) <= _SNIPPET_CHARS else text[:_SNIPPET_CHARS].rstrip() + "…"
        provenance.append(
            Provenance(
                chunk_id=cid,
                url=url,
                snippet=snippet,
                extraction_confidence=conf_by_chunk.get(cid),
            )
        )
    return provenance
