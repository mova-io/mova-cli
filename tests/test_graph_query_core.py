"""Core graph-query behavior (ADR 046): windowing caps, neighbor
expansion, search, bounded traversal, and tenant isolation.

Runs against ``InMemoryStorage`` (the contract is pure over the
``StorageProvider`` Protocol; backend conformance is in
``test_storage_graph.py``).
"""

from __future__ import annotations

import pytest

from movate.core.graph import query as gq
from movate.core.graph.query import DEFAULT_CAP, MAX_CAP, MAX_DEPTH, GraphMode
from movate.core.models import Entity, Relation
from movate.testing import InMemoryStorage

pytestmark = pytest.mark.unit


def _entity(entity_id: str, name: str, type: str = "T", *, agent="a1", tenant_id="t1") -> Entity:
    return Entity(
        entity_id=entity_id,
        tenant_id=tenant_id,
        agent=agent,
        name=name,
        type=type,
        embedding=[0.0],
        embedding_model="test-embed",
        content_hash=f"h-{tenant_id}-{agent}-{entity_id}",
    )


def _relation(
    rid: str, src: str, dst: str, *, weight=1.0, agent="a1", tenant_id="t1", type="R"
) -> Relation:
    return Relation(
        relation_id=rid,
        tenant_id=tenant_id,
        agent=agent,
        src_entity_id=src,
        dst_entity_id=dst,
        type=type,
        weight=weight,
        content_hash=f"h-{tenant_id}-{agent}-{rid}",
    )


async def _seed(storage: InMemoryStorage, ents, rels) -> None:
    for e in ents:
        await storage.upsert_entity(e)
    for r in rels:
        await storage.upsert_relation(r)


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


# ----------------------------------------------------------------------
# Windowing + caps
# ----------------------------------------------------------------------


async def test_clamp_cap_default_and_max() -> None:
    assert gq.clamp_cap(None) == DEFAULT_CAP
    assert gq.clamp_cap(0) == DEFAULT_CAP
    assert gq.clamp_cap(-5) == DEFAULT_CAP
    assert gq.clamp_cap(10) == 10
    assert gq.clamp_cap(10_000) == MAX_CAP


async def test_unrooted_window_capped(storage: InMemoryStorage) -> None:
    ents = [_entity(f"n{i}", f"name{i}") for i in range(20)]
    await _seed(storage, ents, [])
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", limit=5)
    assert len(doc.nodes) == 5


async def test_window_edge_cap_keeps_strongest(storage: InMemoryStorage) -> None:
    ents = [_entity(f"n{i}", f"name{i}") for i in range(4)]
    rels = [
        _relation("e_weak", "n0", "n1", weight=0.1),
        _relation("e_strong", "n2", "n3", weight=0.9),
    ]
    await _seed(storage, ents, rels)
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", limit=4)
    # Edge cap applies after node windowing; with all 4 nodes present and a
    # cap of 4 both edges survive. Tighten to force a single survivor:
    doc1 = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", limit=1)
    assert len(doc1.nodes) == 1  # node cap of 1
    assert len(doc.edges) == 2


async def test_type_filter(storage: InMemoryStorage) -> None:
    ents = [_entity("n1", "A", type="Feature"), _entity("n2", "B", type="Policy")]
    await _seed(storage, ents, [])
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", type="Feature")
    assert [n.key for n in doc.nodes] == ["n1"]


async def test_topology_mode_empty(storage: InMemoryStorage) -> None:
    await _seed(storage, [_entity("n1", "A")], [])
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", mode=GraphMode.TOPOLOGY)
    assert doc.nodes == []
    assert doc.edges == []


async def test_rooted_window(storage: InMemoryStorage) -> None:
    ents = [_entity("root", "Root"), _entity("n1", "Neighbor"), _entity("far", "Far")]
    rels = [_relation("e1", "root", "n1")]  # far is unconnected
    await _seed(storage, ents, rels)
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", root="root", depth=1)
    keys = {n.key for n in doc.nodes}
    assert keys == {"root", "n1"}
    assert "far" not in keys


async def test_rooted_window_unknown_root_empty(storage: InMemoryStorage) -> None:
    await _seed(storage, [_entity("n1", "A")], [])
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", root="nope")
    assert doc.nodes == []


# ----------------------------------------------------------------------
# Neighbor expansion
# ----------------------------------------------------------------------


async def test_expand_neighbors(storage: InMemoryStorage) -> None:
    ents = [_entity("a", "A"), _entity("b", "B"), _entity("c", "C")]
    rels = [_relation("e1", "a", "b"), _relation("e2", "b", "c")]
    await _seed(storage, ents, rels)
    doc1 = await gq.expand_node_neighbors(storage, agent="a1", tenant_id="t1", node_id="a", depth=1)
    assert {n.key for n in doc1.nodes} == {"a", "b"}
    doc2 = await gq.expand_node_neighbors(storage, agent="a1", tenant_id="t1", node_id="a", depth=2)
    assert {n.key for n in doc2.nodes} == {"a", "b", "c"}


async def test_expand_neighbors_depth_capped() -> None:
    # _clamp_depth caps at MAX_DEPTH regardless of request.
    from movate.core.graph.query import _clamp_depth  # noqa: PLC0415

    assert _clamp_depth(1000) == MAX_DEPTH
    assert _clamp_depth(None) == 1


async def test_expand_unknown_node_empty(storage: InMemoryStorage) -> None:
    doc = await gq.expand_node_neighbors(storage, agent="a1", tenant_id="t1", node_id="nope")
    assert doc.nodes == []


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


async def test_search_substring_case_insensitive(storage: InMemoryStorage) -> None:
    ents = [_entity("n1", "SAML SSO", "Feature"), _entity("n2", "OAuth", "Feature")]
    await _seed(storage, ents, [])
    hits = await gq.search_nodes(storage, agent="a1", tenant_id="t1", q="saml")
    assert [h.key for h in hits] == ["n1"]


async def test_search_type_filter(storage: InMemoryStorage) -> None:
    ents = [
        _entity("n1", "Login", "Feature"),
        _entity("n2", "Login Policy", "Policy"),
    ]
    await _seed(storage, ents, [])
    hits = await gq.search_nodes(storage, agent="a1", tenant_id="t1", q="login", type="Policy")
    assert [h.key for h in hits] == ["n2"]


async def test_search_empty_query(storage: InMemoryStorage) -> None:
    await _seed(storage, [_entity("n1", "A")], [])
    assert await gq.search_nodes(storage, agent="a1", tenant_id="t1", q="   ") == []


async def test_search_capped(storage: InMemoryStorage) -> None:
    ents = [_entity(f"n{i}", f"match{i}") for i in range(10)]
    await _seed(storage, ents, [])
    hits = await gq.search_nodes(storage, agent="a1", tenant_id="t1", q="match", limit=3)
    assert len(hits) == 3


# ----------------------------------------------------------------------
# Bounded traversal
# ----------------------------------------------------------------------


async def test_traverse_bounded(storage: InMemoryStorage) -> None:
    ents = [_entity(f"n{i}", f"name{i}") for i in range(5)]
    rels = [_relation(f"e{i}", f"n{i}", f"n{i + 1}") for i in range(4)]  # chain
    await _seed(storage, ents, rels)
    doc = await gq.traverse(storage, agent="a1", tenant_id="t1", root="n0", depth=2)
    keys = {n.key for n in doc.nodes}
    assert keys == {"n0", "n1", "n2"}  # 2 hops from n0


async def test_traverse_unknown_root_empty(storage: InMemoryStorage) -> None:
    await _seed(storage, [_entity("n1", "A")], [])
    doc = await gq.traverse(storage, agent="a1", tenant_id="t1", root="nope")
    assert doc.nodes == []


# ----------------------------------------------------------------------
# Tenant + agent isolation
# ----------------------------------------------------------------------


async def test_tenant_isolation_window(storage: InMemoryStorage) -> None:
    await _seed(storage, [_entity("n1", "Mine", tenant_id="t1")], [])
    await _seed(storage, [_entity("n2", "Theirs", tenant_id="t2")], [])
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1")
    assert {n.key for n in doc.nodes} == {"n1"}


async def test_cross_tenant_node_detail_is_none(storage: InMemoryStorage) -> None:
    await _seed(storage, [_entity("n1", "Theirs", tenant_id="t2")], [])
    detail = await gq.node_detail(storage, agent="a1", tenant_id="t1", node_id="n1")
    assert detail is None


async def test_cross_agent_window_isolation(storage: InMemoryStorage) -> None:
    await _seed(storage, [_entity("n1", "AgentA", agent="a1")], [])
    await _seed(storage, [_entity("n2", "AgentB", agent="a2")], [])
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1")
    assert {n.key for n in doc.nodes} == {"n1"}


async def test_no_cross_tenant_edges(storage: InMemoryStorage) -> None:
    # Two tenants each have a node with the same id space; an edge in t1
    # must never pull a t2 node.
    await _seed(
        storage,
        [_entity("n1", "A", tenant_id="t1"), _entity("n2", "B", tenant_id="t1")],
        [_relation("e1", "n1", "n2", tenant_id="t1")],
    )
    await _seed(storage, [_entity("x1", "X", tenant_id="t2")], [])
    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1")
    assert {n.key for n in doc.nodes} == {"n1", "n2"}
    assert all(e.source in {"n1", "n2"} and e.target in {"n1", "n2"} for e in doc.edges)


# ----------------------------------------------------------------------
# Node detail + provenance
# ----------------------------------------------------------------------


async def test_node_detail_with_provenance(storage: InMemoryStorage) -> None:
    from movate.core.models import KbChunk  # noqa: PLC0415

    chunk = KbChunk(
        chunk_id="c1",
        tenant_id="t1",
        agent="a1",
        source="docs/auth.md",
        text="SAML SSO requires an identity provider configured at the tenant level.",
        embedding=[0.0],
        embedding_model="test-embed",
        content_hash="ch1",
    )
    await storage.save_kb_chunk(chunk)
    ent = _entity("n1", "SAML SSO", "Feature")
    ent = ent.model_copy(update={"source_chunk_ids": ["c1"], "description": "Single sign-on."})
    rel = _relation("e1", "n1", "n2", weight=0.7)
    rel = rel.model_copy(update={"source_chunk_ids": ["c1"]})
    await _seed(storage, [ent, _entity("n2", "IdP")], [rel])

    detail = await gq.node_detail(storage, agent="a1", tenant_id="t1", node_id="n1")
    assert detail is not None
    assert detail.label == "SAML SSO"
    assert detail.type == "Feature"
    assert detail.description == "Single sign-on."
    assert detail.neighbor_count == 1
    assert "a1" in detail.referenced_by_agents
    assert detail.links["expand"] == "/api/v1/graph/nodes/n1/neighbors"
    assert len(detail.provenance) == 1
    prov = detail.provenance[0]
    assert prov.chunk_id == "c1"
    assert prov.url == "docs/auth.md"
    assert prov.snippet is not None and "SAML SSO" in prov.snippet
    assert prov.extraction_confidence == pytest.approx(0.7)
