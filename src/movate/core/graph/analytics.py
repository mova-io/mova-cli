"""Pure, backend-agnostic graph analytics over a graphology document (ADR 046).

Three read-only analytics, computed **over the in-memory graphology graph
the query layer already builds** (:class:`~movate.core.graph.models.GraphologyDoc`)
— never over a concrete backend. The query layer windows + scopes + caps the
graph (tenant + agent + optional project), and analytics runs on that bounded
result, so the storage/graph boundary (CLAUDE.md rule 6) and the per-request
node/edge budget (ADR 046 "never a melt-the-tab payload") are both inherited
for free.

* **centrality** — per-node importance scores. Two measures, both off the
  same adjacency:
    - **degree** — fraction of other nodes a node is directly connected to.
      O(V + E), trivially cheap; the default.
    - **betweenness** — fraction of all-pairs shortest paths that pass through
      a node (Brandes' algorithm, O(V·E) on the unweighted graph). The classic
      "broker / bottleneck" measure; bounded because the graph is already
      windowed + capped before it gets here.
* **shortest_path** — the shortest path between two entity ids (BFS over the
  undirected projection — every edge connects its endpoints both ways, matching
  ``expand_neighbors``' reachability contract). Returns the node id sequence
  (inclusive of both endpoints) or an empty path when none exists.
  No randomness — a pure function of the edge set.
* **communities** — cluster assignment by **connected components**: each
  maximal set of mutually reachable nodes is one community (a "reachability
  island"). Fully **deterministic** (a pure function of the edge set — no
  randomness, no iteration-order sensitivity), so the same graph always yields
  the same clusters and the viewer's per-community colors stay stable across
  reloads. Connected components is one of the two community approaches the ADR
  names; we pick it over a label-propagation refinement because LPA's
  deterministic variants degenerate (a single bridge floods one label across a
  whole component, collapsing it) while its non-degenerate variants need
  randomized tie-breaking — which would make the viewer's colors flicker on
  every reload. Connected components is the well-defined, stable floor; a
  modularity refinement (Louvain et al.) is a documented follow-up, not v1.

**No new dependency** (CLAUDE.md rule 8): degree is a dict count, betweenness is
Brandes (textbook, ~40 lines), shortest path is BFS, and community detection is
a connected-components sweep — all pure Python over the already-loaded doc. A
heavyweight graph lib (networkx / igraph) would be a new shipped dep for three
small, well-understood algorithms the windowed graph sizes never justify.

Boundary: depends only on the graph view models — no storage, no runtime, no
CLI, no I/O. The runtime layer loads a windowed :class:`GraphologyDoc` via the
query layer and hands it here; analytics is a pure function of that doc.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum

from movate.core.graph.models import GraphologyDoc

__all__ = [
    "CentralityMeasure",
    "CentralityScore",
    "Community",
    "ShortestPath",
    "centrality",
    "communities",
    "shortest_path",
]


class CentralityMeasure(StrEnum):
    """Which centrality measure ``centrality`` computes."""

    DEGREE = "degree"
    """Normalized degree — fraction of other nodes directly connected. O(V+E)."""

    BETWEENNESS = "betweenness"
    """Normalized betweenness — fraction of all-pairs shortest paths through a
    node (Brandes). O(V·E)."""


class CentralityScore:
    """One node's centrality score — ``(key, label, type, score)``.

    Plain value object (not a pydantic model) so the pure layer carries no
    serialization concern; the runtime wraps it in a wire schema. ``score`` is
    normalized to ``[0, 1]`` so degree and betweenness are comparable and the
    viewer can map it onto a size/color ramp without knowing the measure.
    """

    __slots__ = ("key", "label", "score", "type")

    def __init__(self, *, key: str, label: str, type: str, score: float) -> None:
        self.key = key
        self.label = label
        self.type = type
        self.score = score

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"CentralityScore(key={self.key!r}, score={self.score!r})"


class ShortestPath:
    """A shortest path between two entities — the ordered node-id sequence.

    ``nodes`` is inclusive of both endpoints (``[from, ..., to]``); ``hops`` is
    ``len(nodes) - 1`` (0 when ``from == to``). ``found`` is ``False`` (and
    ``nodes`` empty) when the two endpoints are in different components.
    """

    __slots__ = ("found", "nodes")

    def __init__(self, *, nodes: list[str]) -> None:
        self.nodes = nodes
        self.found = bool(nodes)

    @property
    def hops(self) -> int:
        return max(0, len(self.nodes) - 1)


class Community:
    """One detected community — a cluster id and its member node ids.

    ``community_id`` is a small, stable integer (assigned in ascending order of
    each cluster's smallest member id) so the same graph yields the same ids
    across requests — the viewer's per-community color stays stable on reload.
    """

    __slots__ = ("community_id", "members")

    def __init__(self, *, community_id: int, members: list[str]) -> None:
        self.community_id = community_id
        self.members = members

    @property
    def size(self) -> int:
        return len(self.members)


# ----------------------------------------------------------------------
# Adjacency — built once from the doc, shared by all three analytics.
# ----------------------------------------------------------------------


def _adjacency(doc: GraphologyDoc) -> dict[str, set[str]]:
    """Undirected adjacency (id-keyed) from a graphology document.

    Every node is a key (so an isolated node is present with an empty
    neighbor set); each edge connects its endpoints **both ways** — analytics
    treats the graph as undirected for reachability/centrality, matching the
    ``expand_neighbors`` reachability contract (direction is a render concern,
    not a connectivity one). Self-loops and edges that dangle outside the node
    set are dropped (the serializer already drops the latter, but we guard).
    """
    adj: dict[str, set[str]] = {n.key: set() for n in doc.nodes}
    for e in doc.edges:
        if e.source == e.target:
            continue
        if e.source not in adj or e.target not in adj:
            continue
        adj[e.source].add(e.target)
        adj[e.target].add(e.source)
    return adj


def _node_meta(doc: GraphologyDoc) -> dict[str, tuple[str, str]]:
    """Map node key → ``(label, type)`` for decorating scores.

    Falls back to the key for a missing label and ``""`` for a missing type so
    a sparse attribute bag never crashes the analytics output.
    """
    meta: dict[str, tuple[str, str]] = {}
    for n in doc.nodes:
        attrs = n.attributes or {}
        label = attrs.get("label")
        type_ = attrs.get("type")
        meta[n.key] = (
            str(label) if label is not None else n.key,
            str(type_) if type_ is not None else "",
        )
    return meta


# ----------------------------------------------------------------------
# Centrality
# ----------------------------------------------------------------------

# Betweenness is only defined once a node can sit *between* two others — i.e.
# at least 3 nodes. Below this, every node's betweenness is 0.
_BETWEENNESS_MIN_NODES = 3


def centrality(
    doc: GraphologyDoc,
    *,
    measure: CentralityMeasure = CentralityMeasure.DEGREE,
    top_n: int | None = None,
) -> list[CentralityScore]:
    """Per-node centrality, highest score first (ties broken by node id).

    ``measure`` selects degree (default, O(V+E)) or betweenness (Brandes,
    O(V·E)). Scores are normalized to ``[0, 1]``. ``top_n`` (when set) returns
    only the highest-scoring nodes — what the endpoint surfaces as "top-N
    hubs". An empty graph → ``[]``; a single node → score ``0.0`` (it is
    central to nothing).
    """
    adj = _adjacency(doc)
    meta = _node_meta(doc)
    if not adj:
        return []

    raw = _betweenness(adj) if measure is CentralityMeasure.BETWEENNESS else _degree_centrality(adj)

    scores = [
        CentralityScore(key=key, label=meta[key][0], type=meta[key][1], score=raw[key])
        for key in adj
    ]
    # Highest score first; stable tie-break on key so the order is deterministic.
    scores.sort(key=lambda s: (-s.score, s.key))
    if top_n is not None and top_n > 0:
        return scores[:top_n]
    return scores


def _degree_centrality(adj: dict[str, set[str]]) -> dict[str, float]:
    """Degree centrality: degree / (V - 1), in ``[0, 1]``.

    A node connected to every other node scores ``1.0``; an isolated node
    scores ``0.0``. With a single node the denominator is 0 → all ``0.0``.
    """
    n = len(adj)
    if n <= 1:
        return {key: 0.0 for key in adj}
    denom = n - 1
    return {key: len(neighbors) / denom for key, neighbors in adj.items()}


def _betweenness(adj: dict[str, set[str]]) -> dict[str, float]:
    """Brandes' betweenness centrality on an unweighted, undirected graph.

    For every source, a BFS computes shortest-path counts; a reverse
    accumulation tallies each node's share of those paths. The raw sum is
    normalized by ``(n-1)(n-2)`` (the count of ordered endpoint pairs that
    could route through a node, undirected) so the result is in ``[0, 1]`` and
    comparable to degree centrality. O(V·E) — bounded because the graph is
    already windowed + capped before analytics runs.
    """
    betweenness: dict[str, float] = {key: 0.0 for key in adj}
    nodes = list(adj)

    for source in nodes:
        # Single-source shortest-path BFS (Brandes).
        stack: list[str] = []
        predecessors: dict[str, list[str]] = {v: [] for v in adj}
        sigma: dict[str, float] = dict.fromkeys(adj, 0.0)  # number of shortest paths
        sigma[source] = 1.0
        distance: dict[str, int] = {v: -1 for v in adj}
        distance[source] = 0
        queue: deque[str] = deque([source])

        while queue:
            v = queue.popleft()
            stack.append(v)
            # Sort neighbors so the accumulation is deterministic.
            for w in sorted(adj[v]):
                if distance[w] < 0:
                    distance[w] = distance[v] + 1
                    queue.append(w)
                if distance[w] == distance[v] + 1:
                    sigma[w] += sigma[v]
                    predecessors[w].append(v)

        # Reverse accumulation of dependencies.
        delta: dict[str, float] = dict.fromkeys(adj, 0.0)
        while stack:
            w = stack.pop()
            for v in predecessors[w]:
                if sigma[w] != 0.0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != source:
                betweenness[w] += delta[w]

    n = len(nodes)
    # Betweenness needs at least 3 nodes for a node to lie *between* two others;
    # with fewer, no node is on any other pair's path → all zero.
    if n < _BETWEENNESS_MIN_NODES:
        return {key: 0.0 for key in adj}
    # Normalize by the number of orderable endpoint pairs so the max possible
    # score is 1.0 and degree/betweenness are comparable.
    scale = 1.0 / ((n - 1) * (n - 2))
    return {key: betweenness[key] * scale for key in adj}


# ----------------------------------------------------------------------
# Shortest path
# ----------------------------------------------------------------------


def shortest_path(doc: GraphologyDoc, *, source: str, target: str) -> ShortestPath:
    """Shortest path from ``source`` to ``target`` (BFS, undirected).

    Returns the inclusive node-id sequence ``[source, ..., target]``. A missing
    endpoint, or two endpoints in different components, yields an empty
    ``ShortestPath`` (``found == False``) rather than raising — same no-leak
    convention as the query layer (a cross-scope id simply isn't in the doc, so
    it's "not found", never an error). ``source == target`` → a one-node path
    (0 hops) when the node exists.
    """
    adj = _adjacency(doc)
    if source not in adj or target not in adj:
        return ShortestPath(nodes=[])
    if source == target:
        return ShortestPath(nodes=[source])

    # BFS, recording each node's predecessor to reconstruct the path.
    predecessor: dict[str, str] = {source: source}
    queue: deque[str] = deque([source])
    while queue:
        v = queue.popleft()
        if v == target:
            break
        for w in sorted(adj[v]):
            if w not in predecessor:
                predecessor[w] = v
                queue.append(w)

    if target not in predecessor:
        return ShortestPath(nodes=[])

    # Reconstruct from target back to source.
    path: list[str] = [target]
    node = target
    while node != source:
        node = predecessor[node]
        path.append(node)
    path.reverse()
    return ShortestPath(nodes=path)


# ----------------------------------------------------------------------
# Community detection
# ----------------------------------------------------------------------


def communities(doc: GraphologyDoc) -> list[Community]:
    """Detect communities as **connected components** of the undirected graph.

    Each maximal set of mutually reachable nodes is one community. A pure
    function of the edge set — **deterministic** (no randomness, no
    iteration-order sensitivity), so the same graph always yields the same
    clusters and the viewer's per-community colors stay stable across reloads.

    Returns communities sorted by size (largest first), then by smallest member
    id; each ``community_id`` is a small stable integer assigned in that sorted
    order. An empty graph → ``[]``; an isolated node is its own singleton
    community.
    """
    adj = _adjacency(doc)
    if not adj:
        return []

    seen: set[str] = set()
    clusters: list[list[str]] = []
    # Visit nodes in id order so component discovery (and therefore the final
    # ordering / ids) is deterministic.
    for start in sorted(adj):
        if start in seen:
            continue
        # BFS the component reachable from ``start``.
        component: list[str] = []
        queue: deque[str] = deque([start])
        seen.add(start)
        while queue:
            node = queue.popleft()
            component.append(node)
            for nb in sorted(adj[node]):
                if nb not in seen:
                    seen.add(nb)
                    queue.append(nb)
        clusters.append(component)

    # Largest first; tie-break on the smallest member id for stability.
    clusters.sort(key=lambda members: (-len(members), min(members)))
    return [
        Community(community_id=i, members=sorted(members)) for i, members in enumerate(clusters)
    ]
