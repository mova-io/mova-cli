"""Adapter correctness: graphology JSON → dash-cytoscape elements.

``graphology_to_cytoscape`` is a *pure* function with no viz dependency, so
these tests run without ``dash``/``dash-cytoscape`` installed. They lock the
contract the Dash viewer relies on: node/edge shape, color-by-type +
size-by-degree derivation, provenance passthrough, and the empty/partial
graph failure modes.
"""

from __future__ import annotations

import pytest

from movate.core.graph.cytoscape_format import graphology_to_cytoscape


def _nodes(elements: list[dict]) -> list[dict]:
    return [el["data"] for el in elements if "source" not in el["data"]]


def _edges(elements: list[dict]) -> list[dict]:
    return [el["data"] for el in elements if "source" in el["data"]]


@pytest.mark.unit
def test_empty_graph_returns_empty_list() -> None:
    assert graphology_to_cytoscape({}) == []
    assert graphology_to_cytoscape({"attributes": {}, "nodes": [], "edges": []}) == []
    # Missing keys entirely (not even present) → still empty, no KeyError.
    assert graphology_to_cytoscape({"attributes": {"directed": True}}) == []


@pytest.mark.unit
def test_basic_nodes_and_edges_shape() -> None:
    graphology = {
        "attributes": {},
        "nodes": [
            {"key": "n1", "attributes": {"type": "Feature", "name": "SSO"}},
            {"key": "n2", "attributes": {"type": "Policy", "label": "Access Policy"}},
        ],
        "edges": [
            {
                "key": "e1",
                "source": "n1",
                "target": "n2",
                "attributes": {"type": "GOVERNED_BY", "weight": 0.9},
            }
        ],
    }
    elements = graphology_to_cytoscape(graphology)

    nodes = _nodes(elements)
    edges = _edges(elements)
    assert len(nodes) == 2
    assert len(edges) == 1

    n1 = next(n for n in nodes if n["id"] == "n1")
    assert n1["label"] == "SSO"  # name used as label
    assert n1["type"] == "Feature"

    n2 = next(n for n in nodes if n["id"] == "n2")
    assert n2["label"] == "Access Policy"  # explicit label wins

    edge = edges[0]
    assert edge["source"] == "n1"
    assert edge["target"] == "n2"
    assert edge["label"] == "GOVERNED_BY"  # relation type as edge label
    assert edge["id"] == "e1"
    assert edge["weight"] == 0.9  # passthrough


@pytest.mark.unit
def test_label_fallback_to_key() -> None:
    """A node with neither label nor name falls back to its key."""
    graphology = {"nodes": [{"key": "abc123", "attributes": {"type": "X"}}], "edges": []}
    nodes = _nodes(graphology_to_cytoscape(graphology))
    assert nodes[0]["label"] == "abc123"


@pytest.mark.unit
def test_degree_is_computed_and_attached() -> None:
    """size-by-degree: every node carries a ``degree`` over the edge list."""
    graphology = {
        "nodes": [
            {"key": "hub", "attributes": {"type": "Concept"}},
            {"key": "a", "attributes": {"type": "Concept"}},
            {"key": "b", "attributes": {"type": "Concept"}},
            {"key": "c", "attributes": {"type": "Concept"}},
        ],
        "edges": [
            {"source": "hub", "target": "a", "attributes": {}},
            {"source": "hub", "target": "b", "attributes": {}},
            {"source": "hub", "target": "c", "attributes": {}},
        ],
    }
    nodes = {n["id"]: n for n in _nodes(graphology_to_cytoscape(graphology))}
    assert nodes["hub"]["degree"] == 3
    assert nodes["a"]["degree"] == 1
    assert nodes["b"]["degree"] == 1
    assert nodes["c"]["degree"] == 1


@pytest.mark.unit
def test_isolated_node_has_zero_degree() -> None:
    graphology = {"nodes": [{"key": "lonely", "attributes": {"type": "X"}}], "edges": []}
    nodes = _nodes(graphology_to_cytoscape(graphology))
    assert nodes[0]["degree"] == 0


@pytest.mark.unit
def test_self_loop_counts_twice() -> None:
    """A self-loop touches the node at both endpoints → degree 2.

    Matches Cytoscape's own degree semantics.
    """
    graphology = {
        "nodes": [{"key": "n", "attributes": {"type": "X"}}],
        "edges": [{"source": "n", "target": "n", "attributes": {}}],
    }
    nodes = _nodes(graphology_to_cytoscape(graphology))
    assert nodes[0]["degree"] == 2


@pytest.mark.unit
def test_dangling_edge_is_dropped() -> None:
    """Edge to a node not in ``nodes`` is dropped (can't anchor it)."""
    graphology = {
        "nodes": [{"key": "n1", "attributes": {"type": "X"}}],
        "edges": [
            {"source": "n1", "target": "ghost", "attributes": {"type": "REL"}},
        ],
    }
    elements = graphology_to_cytoscape(graphology)
    assert _edges(elements) == []
    # ...and the dropped edge does NOT inflate the surviving node's degree.
    assert _nodes(elements)[0]["degree"] == 0


@pytest.mark.unit
def test_provenance_and_extra_attrs_pass_through() -> None:
    """Side-panel reads description/confidence/source_chunk off the element."""
    graphology = {
        "nodes": [
            {
                "key": "n1",
                "attributes": {
                    "type": "Feature",
                    "name": "SSO",
                    "description": "Single sign-on",
                    "confidence": 0.82,
                    "source_chunk_ids": ["chunk-7", "chunk-9"],
                },
            }
        ],
        "edges": [],
    }
    node = _nodes(graphology_to_cytoscape(graphology))[0]
    assert node["description"] == "Single sign-on"
    assert node["confidence"] == 0.82
    assert node["source_chunk_ids"] == ["chunk-7", "chunk-9"]


@pytest.mark.unit
def test_passthrough_does_not_clobber_reserved_keys() -> None:
    """A rogue ``id``/``degree`` attribute can't overwrite adapter-owned keys."""
    graphology = {
        "nodes": [
            {
                "key": "real-id",
                "attributes": {"type": "X", "id": "FAKE", "degree": 999},
            }
        ],
        "edges": [],
    }
    node = _nodes(graphology_to_cytoscape(graphology))[0]
    assert node["id"] == "real-id"  # adapter owns id
    assert node["degree"] == 0  # adapter owns degree, not the attr


@pytest.mark.unit
def test_missing_type_yields_empty_string() -> None:
    """A typeless node renders with the default color (empty type string)."""
    graphology = {"nodes": [{"key": "n", "attributes": {"name": "x"}}], "edges": []}
    node = _nodes(graphology_to_cytoscape(graphology))[0]
    assert node["type"] == ""


@pytest.mark.unit
def test_malformed_node_entries_are_skipped() -> None:
    """Nodes without a usable key are skipped, not crash-inducing."""
    graphology = {
        "nodes": [
            {"key": "", "attributes": {"type": "X"}},  # empty key
            {"attributes": {"type": "X"}},  # no key
            {"key": "good", "attributes": {"type": "X"}},
        ],
        "edges": [],
    }
    nodes = _nodes(graphology_to_cytoscape(graphology))
    assert [n["id"] for n in nodes] == ["good"]


@pytest.mark.unit
def test_non_dict_attributes_are_tolerated() -> None:
    """A node/edge with non-dict ``attributes`` doesn't blow up."""
    graphology = {
        "nodes": [{"key": "n1", "attributes": None}, {"key": "n2"}],
        "edges": [{"source": "n1", "target": "n2", "attributes": "oops"}],
    }
    elements = graphology_to_cytoscape(graphology)
    assert len(_nodes(elements)) == 2
    assert len(_edges(elements)) == 1


@pytest.mark.unit
def test_input_is_not_mutated() -> None:
    graphology = {
        "nodes": [{"key": "n1", "attributes": {"type": "X"}}],
        "edges": [],
    }
    before = {"nodes": [{"key": "n1", "attributes": {"type": "X"}}], "edges": []}
    graphology_to_cytoscape(graphology)
    assert graphology == before
