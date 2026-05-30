"""Core graph-analytics behavior (ADR 046): centrality (degree +
betweenness), shortest-path between two entities, and community detection.

Pure over a :class:`GraphologyDoc` (the windowed graph the query layer
builds) — no storage, no runtime. Correctness is checked on hand-verifiable
small graphs (a star, a path, a barbell, two islands) where the right answer
is known by inspection.
"""

from __future__ import annotations

import pytest

from movate.core.graph import analytics as ga
from movate.core.graph.analytics import CentralityMeasure
from movate.core.graph.models import GraphEdge, GraphNode, GraphologyDoc

pytestmark = pytest.mark.unit


def _doc(node_ids: list[str], edges: list[tuple[str, str]]) -> GraphologyDoc:
    """Build a graphology doc from bare node ids + (src, dst) edge pairs."""
    nodes = [GraphNode(key=k, attributes={"label": k.upper(), "type": "T"}) for k in node_ids]
    geds = [
        GraphEdge(key=f"e{i}", source=s, target=t, attributes={"label": "R", "weight": 1.0})
        for i, (s, t) in enumerate(edges)
    ]
    return GraphologyDoc(nodes=nodes, edges=geds)


# ----------------------------------------------------------------------
# Degree centrality
# ----------------------------------------------------------------------


def test_degree_centrality_star() -> None:
    # Star: hub connected to 3 leaves. Hub degree 3 / (4-1) = 1.0; leaves 1/3.
    doc = _doc(["hub", "a", "b", "c"], [("hub", "a"), ("hub", "b"), ("hub", "c")])
    scores = ga.centrality(doc, measure=CentralityMeasure.DEGREE)
    by_key = {s.key: s.score for s in scores}
    assert by_key["hub"] == pytest.approx(1.0)
    assert by_key["a"] == pytest.approx(1 / 3)
    # Highest first → hub leads.
    assert scores[0].key == "hub"
    # Score carries label + type for the viewer.
    assert scores[0].label == "HUB"
    assert scores[0].type == "T"


def test_degree_centrality_isolated_node_is_zero() -> None:
    doc = _doc(["a", "b", "lonely"], [("a", "b")])
    by_key = {s.key: s.score for s in ga.centrality(doc)}
    assert by_key["lonely"] == pytest.approx(0.0)


def test_centrality_top_n() -> None:
    doc = _doc(["hub", "a", "b", "c"], [("hub", "a"), ("hub", "b"), ("hub", "c")])
    top = ga.centrality(doc, measure=CentralityMeasure.DEGREE, top_n=1)
    assert len(top) == 1
    assert top[0].key == "hub"


def test_centrality_empty_graph() -> None:
    assert ga.centrality(GraphologyDoc()) == []


def test_centrality_single_node_zero() -> None:
    doc = _doc(["solo"], [])
    scores = ga.centrality(doc)
    assert len(scores) == 1
    assert scores[0].score == pytest.approx(0.0)


def test_centrality_deterministic_tie_break() -> None:
    # All-equal degrees → ordered by key for stability.
    doc = _doc(["c", "b", "a"], [("a", "b"), ("b", "c"), ("c", "a")])  # triangle
    keys = [s.key for s in ga.centrality(doc)]
    assert keys == ["a", "b", "c"]


# ----------------------------------------------------------------------
# Betweenness centrality (Brandes)
# ----------------------------------------------------------------------


def test_betweenness_path_middle_is_highest() -> None:
    # Path a-b-c-d-e: the middle node c lies on the most shortest paths.
    doc = _doc(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")])
    scores = ga.centrality(doc, measure=CentralityMeasure.BETWEENNESS)
    by_key = {s.key: s.score for s in scores}
    assert scores[0].key == "c"  # the bottleneck
    # Endpoints lie on no shortest path between two OTHER nodes → 0.
    assert by_key["a"] == pytest.approx(0.0)
    assert by_key["e"] == pytest.approx(0.0)
    # Symmetric: b and d equal; both below c.
    assert by_key["b"] == pytest.approx(by_key["d"])
    assert by_key["c"] > by_key["b"]


def test_betweenness_bridge_node() -> None:
    # Barbell: two triangles joined by a single bridge node x. x is on every
    # shortest path between the two halves → maximal betweenness.
    doc = _doc(
        ["a", "b", "c", "x", "d", "e", "f"],
        [
            ("a", "b"),
            ("b", "c"),
            ("c", "a"),
            ("c", "x"),
            ("x", "d"),
            ("d", "e"),
            ("e", "f"),
            ("f", "d"),
        ],
    )
    scores = ga.centrality(doc, measure=CentralityMeasure.BETWEENNESS)
    assert scores[0].key == "x"


def test_betweenness_star_hub() -> None:
    # Star hub is on every leaf-to-leaf shortest path → score 1.0 (max).
    doc = _doc(["hub", "a", "b", "c"], [("hub", "a"), ("hub", "b"), ("hub", "c")])
    by_key = {s.key: s.score for s in ga.centrality(doc, measure=CentralityMeasure.BETWEENNESS)}
    assert by_key["hub"] == pytest.approx(1.0)
    assert by_key["a"] == pytest.approx(0.0)


def test_betweenness_in_unit_range() -> None:
    doc = _doc(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")])
    for s in ga.centrality(doc, measure=CentralityMeasure.BETWEENNESS):
        assert 0.0 <= s.score <= 1.0


# ----------------------------------------------------------------------
# Shortest path
# ----------------------------------------------------------------------


def test_shortest_path_simple() -> None:
    doc = _doc(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    sp = ga.shortest_path(doc, source="a", target="d")
    assert sp.found is True
    assert sp.nodes == ["a", "b", "c", "d"]
    assert sp.hops == 3


def test_shortest_path_picks_shortest_of_two() -> None:
    # a-b-d (2 hops) vs a-c-x-d (3 hops): BFS returns the 2-hop route.
    doc = _doc(
        ["a", "b", "c", "x", "d"],
        [("a", "b"), ("b", "d"), ("a", "c"), ("c", "x"), ("x", "d")],
    )
    sp = ga.shortest_path(doc, source="a", target="d")
    assert sp.nodes == ["a", "b", "d"]
    assert sp.hops == 2


def test_shortest_path_undirected() -> None:
    # Edge stored a->b only; an undirected BFS still reaches a from b.
    doc = _doc(["a", "b"], [("a", "b")])
    sp = ga.shortest_path(doc, source="b", target="a")
    assert sp.nodes == ["b", "a"]


def test_shortest_path_same_node() -> None:
    doc = _doc(["a", "b"], [("a", "b")])
    sp = ga.shortest_path(doc, source="a", target="a")
    assert sp.found is True
    assert sp.nodes == ["a"]
    assert sp.hops == 0


def test_shortest_path_no_path_between_islands() -> None:
    # Two disconnected components → no path.
    doc = _doc(["a", "b", "x", "y"], [("a", "b"), ("x", "y")])
    sp = ga.shortest_path(doc, source="a", target="x")
    assert sp.found is False
    assert sp.nodes == []


def test_shortest_path_unknown_endpoint() -> None:
    doc = _doc(["a", "b"], [("a", "b")])
    assert ga.shortest_path(doc, source="a", target="nope").found is False
    assert ga.shortest_path(doc, source="nope", target="b").found is False


# ----------------------------------------------------------------------
# Community detection
# ----------------------------------------------------------------------


def test_communities_two_islands() -> None:
    # Two disjoint triangles → exactly two communities, never merged.
    doc = _doc(
        ["a", "b", "c", "x", "y", "z"],
        [("a", "b"), ("b", "c"), ("c", "a"), ("x", "y"), ("y", "z"), ("z", "x")],
    )
    comms = ga.communities(doc)
    assert len(comms) == 2
    member_sets = [set(c.members) for c in comms]
    assert {"a", "b", "c"} in member_sets
    assert {"x", "y", "z"} in member_sets


def test_communities_isolated_singleton() -> None:
    doc = _doc(["a", "b", "lonely"], [("a", "b")])
    comms = ga.communities(doc)
    member_sets = [set(c.members) for c in comms]
    assert {"lonely"} in member_sets


def test_communities_ids_stable_and_sorted_by_size() -> None:
    # One 3-node island + one 2-node island → largest first, ids 0,1.
    doc = _doc(["a", "b", "c", "x", "y"], [("a", "b"), ("b", "c"), ("c", "a"), ("x", "y")])
    comms = ga.communities(doc)
    assert [c.community_id for c in comms] == [0, 1]
    assert comms[0].size >= comms[1].size
    # Members are sorted within a community.
    assert comms[0].members == sorted(comms[0].members)


def test_communities_deterministic() -> None:
    doc = _doc(
        ["a", "b", "c", "x", "y", "z"],
        [("a", "b"), ("b", "c"), ("c", "a"), ("x", "y"), ("y", "z"), ("z", "x")],
    )
    first = [(c.community_id, tuple(c.members)) for c in ga.communities(doc)]
    second = [(c.community_id, tuple(c.members)) for c in ga.communities(doc)]
    assert first == second


def test_communities_empty_graph() -> None:
    assert ga.communities(GraphologyDoc()) == []


def test_communities_partition_is_complete_and_disjoint() -> None:
    # Whatever the heuristic decides, the result must be a valid partition:
    # every node assigned to exactly one community, no node lost or duplicated.
    doc = _doc(
        ["a", "b", "c", "d", "e", "f"],
        [
            ("a", "b"),
            ("b", "c"),
            ("c", "a"),
            ("d", "e"),
            ("e", "f"),
            ("f", "d"),
            ("c", "d"),  # a single bridge between the two triangles
        ],
    )
    comms = ga.communities(doc)
    all_members = [m for c in comms for m in c.members]
    assert sorted(all_members) == ["a", "b", "c", "d", "e", "f"]  # complete
    assert len(all_members) == len(set(all_members))  # disjoint


def test_communities_connected_nodes_are_one_community() -> None:
    # Connected-components semantics: any two nodes joined by a path are in the
    # SAME community, even two cliques bridged by a single edge — they form one
    # reachable island. (Splitting a connected component is a modularity-
    # refinement follow-up, not the deterministic v1 contract.)
    doc = _doc(
        ["a", "b", "c", "d", "e", "f", "g", "h"],
        [
            ("a", "b"),
            ("a", "c"),
            ("a", "d"),
            ("b", "c"),
            ("b", "d"),
            ("c", "d"),
            ("e", "f"),
            ("e", "g"),
            ("e", "h"),
            ("f", "g"),
            ("f", "h"),
            ("g", "h"),
            ("d", "e"),  # the single bridge — makes it all one component
        ],
    )
    comms = ga.communities(doc)
    assert len(comms) == 1
    assert set(comms[0].members) == {"a", "b", "c", "d", "e", "f", "g", "h"}
