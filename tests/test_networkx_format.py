"""Unit tests for the graphology JSON → NetworkX adapter.

Covers the pure shape mapping the PyVis exporter relies on: node/edge counts,
attribute derivation (color-by-type, size-by-degree, HTML tooltip with
provenance), layout pass-through, tolerance of malformed input, and the empty
graph. No PyVis, no network — just the adapter.

NetworkX rides in the opt-in ``graph-pyvis`` extra, so the whole module is
skipped when it isn't installed (matching the project's convention for
optional-extra-backed code).
"""

from __future__ import annotations

import pytest

pytest.importorskip("networkx", reason="networkx ships in the graph-pyvis extra")

from movate.core.graph.networkx_format import (
    build_type_color_map,
    graphology_to_networkx,
)


def _sample_graphology() -> dict:
    """A small two-type graph with provenance + properties on one node."""
    return {
        "attributes": {"name": "demo knowledge graph"},
        "nodes": [
            {
                "key": "n_saml",
                "attributes": {
                    "label": "SAML SSO",
                    "type": "Feature",
                    "properties": {"confidence": 0.9, "owner": "security"},
                    "source_provenance": {"url": "https://docs.example.com/saml"},
                    "x": 0.42,
                    "y": -1.13,
                },
            },
            {
                "key": "n_policy",
                "attributes": {"label": "Security Policy v3", "type": "Policy"},
            },
            {
                "key": "n_tier",
                "attributes": {"label": "Enterprise Tier", "type": "Tier"},
            },
        ],
        "edges": [
            {
                "key": "e1",
                "source": "n_saml",
                "target": "n_policy",
                "attributes": {"label": "governed-by", "weight": 0.8},
            },
            {
                "key": "e2",
                "source": "n_saml",
                "target": "n_tier",
                "attributes": {"label": "available-in"},
            },
        ],
    }


def test_node_and_edge_counts_match():
    graph = graphology_to_networkx(_sample_graphology())
    assert graph.number_of_nodes() == 3
    assert graph.number_of_edges() == 2
    assert set(graph.nodes) == {"n_saml", "n_policy", "n_tier"}


def test_labels_and_types_carried_through():
    graph = graphology_to_networkx(_sample_graphology())
    assert graph.nodes["n_saml"]["label"] == "SAML SSO"
    assert graph.nodes["n_saml"]["type"] == "Feature"
    assert graph.nodes["n_policy"]["type"] == "Policy"


def test_label_falls_back_to_key_when_absent():
    graph = graphology_to_networkx({"nodes": [{"key": "bare"}], "edges": []})
    assert graph.nodes["bare"]["label"] == "bare"


def test_color_is_stable_per_type():
    graph = graphology_to_networkx(_sample_graphology())
    # Two distinct types → two distinct colours; same type → same colour.
    colors = {data["type"]: data["color"] for _, data in graph.nodes(data=True)}
    assert len({colors["Feature"], colors["Policy"], colors["Tier"]}) == 3
    # Deterministic: the standalone color map agrees with the graph's colours.
    color_map = build_type_color_map(["Feature", "Policy", "Tier"])
    assert graph.nodes["n_saml"]["color"] == color_map["Feature"]


def test_size_scales_with_degree():
    graph = graphology_to_networkx(_sample_graphology())
    # n_saml has degree 2; the leaves have degree 1 → the hub is larger.
    assert graph.nodes["n_saml"]["size"] > graph.nodes["n_policy"]["size"]


def test_tooltip_includes_label_type_properties_and_provenance():
    graph = graphology_to_networkx(_sample_graphology())
    title = graph.nodes["n_saml"]["title"]
    assert "SAML SSO" in title
    assert "Feature" in title
    assert "confidence" in title  # a surfaced property
    assert "https://docs.example.com/saml" in title  # provenance source URL


def test_tooltip_html_escapes_node_content():
    """Node content comes from extracted documents — it must not inject markup."""
    payload = {
        "nodes": [{"key": "x", "attributes": {"label": "<script>alert(1)</script>", "type": "T"}}],
        "edges": [],
    }
    graph = graphology_to_networkx(payload)
    title = graph.nodes["x"]["title"]
    assert "<script>" not in title
    assert "&lt;script&gt;" in title


def test_layout_coordinates_passed_through_when_present():
    graph = graphology_to_networkx(_sample_graphology())
    assert graph.nodes["n_saml"]["x"] == pytest.approx(0.42)
    assert graph.nodes["n_saml"]["y"] == pytest.approx(-1.13)
    # A node with no coordinates doesn't get bogus ones.
    assert "x" not in graph.nodes["n_policy"]


def test_edge_attributes_label_weight_value():
    graph = graphology_to_networkx(_sample_graphology())
    edge = graph.edges["n_saml", "n_policy"]
    assert edge["label"] == "governed-by"
    assert edge["weight"] == pytest.approx(0.8)
    # Edge with no weight falls back to a visible thickness.
    fallback = graph.edges["n_saml", "n_tier"]
    assert fallback["value"] == pytest.approx(1.0)


def test_empty_graph_yields_empty_networkx():
    graph = graphology_to_networkx({"nodes": [], "edges": []})
    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0


def test_missing_nodes_and_edges_keys_tolerated():
    graph = graphology_to_networkx({})
    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0


def test_malformed_nodes_and_dangling_edges_are_skipped():
    payload = {
        "nodes": [
            {"key": "ok", "attributes": {"label": "Fine", "type": "T"}},
            {"attributes": {"label": "no key"}},  # missing key → skipped
            "not-a-dict",  # skipped
            {"key": ""},  # empty key → skipped
        ],
        "edges": [
            {"source": "ok", "target": "ghost"},  # dangling endpoint → skipped
            {"source": "ok", "target": "ok"},  # references only declared nodes
            "not-a-dict",  # skipped
        ],
    }
    graph = graphology_to_networkx(payload)
    assert set(graph.nodes) == {"ok"}
    # Only the self-edge survives (both endpoints declared); the dangling one drops.
    assert graph.number_of_edges() == 1


def test_non_dict_input_raises_typeerror():
    with pytest.raises(TypeError):
        graphology_to_networkx(["not", "a", "dict"])  # type: ignore[arg-type]


def test_build_type_color_map_is_deterministic_and_empty_safe():
    assert build_type_color_map([]) == {}
    first = build_type_color_map(["B", "A", "A", ""])
    second = build_type_color_map(["A", "B"])
    # Order-independent + ignores empty type strings.
    assert first == second
    assert "" not in first
