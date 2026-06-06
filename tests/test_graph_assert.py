"""Unit tests for the deterministic graph-assert builder (ADR 079 D1).

The embedding call is monkeypatched so these run with no network access. They
cover the parts that carry real logic: deterministic/idempotent ids, node
dedup + merge, edge resolution + dropping, assert provenance, and — critically —
the identity contract that keeps asserted and extracted nodes deduping to one
row.
"""

from __future__ import annotations

import pytest

from movate.kb import graph_assert
from movate.kb import graph_extract as ext
from movate.kb.graph_assert import AssertEdge, AssertNode, build_asserted_graph

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    """Deterministic 3-dim embeddings; no API call."""

    async def fake_embed(texts, *, model="", api_key=None, **_):
        return [[float(len(t) % 5), 1.0, 0.0] for t in texts]

    monkeypatch.setattr(graph_assert, "embed_texts", fake_embed)


def _incident_facts():
    nodes = [
        AssertNode(type="Incident", name="INC0042217", description="Frozen POS register"),
        AssertNode(type="Store", name="118"),
        AssertNode(type="Lane", name="5"),
        AssertNode(type="Symptom", name="frozen register"),
    ]
    edges = [
        AssertEdge(src="INC0042217", dst="118", type="AT_STORE"),
        AssertEdge(src="INC0042217", dst="5", type="ON_LANE"),
        AssertEdge(src="INC0042217", dst="frozen register", type="HAS_SYMPTOM"),
    ]
    return nodes, edges


async def _build(nodes, edges=(), **kw):
    return await build_asserted_graph(
        nodes, edges, agent="store-support", tenant_id="devtenant", **kw
    )


@pytest.mark.asyncio
async def test_builds_nodes_and_edges():
    nodes, edges = _incident_facts()
    ents, rels = await _build(nodes, edges)
    assert {e.name for e in ents} == {"INC0042217", "118", "5", "frozen register"}
    assert len(rels) == 3
    # every node embedded
    assert all(len(e.embedding) == 3 for e in ents)
    # assert provenance + confidence on every record
    for rec in [*ents, *rels]:
        assert rec.metadata["source"] == "assert"
        assert rec.metadata["confidence"] == 1.0


@pytest.mark.asyncio
async def test_ids_are_deterministic_and_idempotent():
    """Building the same facts twice yields identical ids — the record-level
    idempotency the upsert layer (content_hash dedup) relies on."""
    nodes, edges = _incident_facts()
    ents1, rels1 = await _build(nodes, edges)
    ents2, rels2 = await _build(nodes, edges)
    assert [e.entity_id for e in ents1] == [e.entity_id for e in ents2]
    assert [e.content_hash for e in ents1] == [e.content_hash for e in ents2]
    assert [r.relation_id for r in rels1] == [r.relation_id for r in rels2]
    # entity_id is derived from content_hash (deterministic, not random uuid)
    assert all(e.entity_id == e.content_hash for e in ents1)


@pytest.mark.asyncio
async def test_relations_reference_entity_ids():
    nodes, edges = _incident_facts()
    ents, rels = await _build(nodes, edges)
    by_name = {e.name: e.entity_id for e in ents}
    inc = by_name["INC0042217"]
    targets = {(r.src_entity_id, r.dst_entity_id) for r in rels}
    assert (inc, by_name["118"]) in targets
    assert (inc, by_name["5"]) in targets
    assert all(r.src_entity_id in by_name.values() for r in rels)
    assert all(r.dst_entity_id in by_name.values() for r in rels)


@pytest.mark.asyncio
async def test_duplicate_nodes_merge():
    nodes = [
        AssertNode(type="Incident", name="INC1", description="short"),
        AssertNode(type="Incident", name="INC1", description="a much longer description"),
        AssertNode(type="Incident", name="inc1"),  # same after normalization
    ]
    ents, _ = await _build(nodes)
    assert len(ents) == 1
    assert ents[0].description == "a much longer description"


@pytest.mark.asyncio
async def test_node_metadata_merged_but_reserved_keys_protected():
    nodes = [
        AssertNode(type="Incident", name="INC1", metadata={"ticket_url": "x", "source": "spoof"}),
    ]
    ents, _ = await _build(nodes)
    assert ents[0].metadata["ticket_url"] == "x"
    # caller cannot override the reserved provenance keys
    assert ents[0].metadata["source"] == "assert"
    assert ents[0].metadata["confidence"] == 1.0


@pytest.mark.asyncio
async def test_dangling_and_selfloop_edges_dropped():
    nodes = [AssertNode(type="Incident", name="INC1"), AssertNode(type="Lane", name="5")]
    edges = [
        AssertEdge(src="INC1", dst="nonexistent", type="ON_LANE"),  # dangling
        AssertEdge(src="ghost", dst="5", type="ON_LANE"),  # dangling
        AssertEdge(src="INC1", dst="INC1", type="SELF"),  # self-loop
        AssertEdge(src="INC1", dst="5", type=""),  # untyped
        AssertEdge(src="INC1", dst="5", type="ON_LANE"),  # the only valid one
    ]
    _, rels = await _build(nodes, edges)
    assert len(rels) == 1
    assert rels[0].type == "ON_LANE"


@pytest.mark.asyncio
async def test_weight_clamped_and_defaulted():
    nodes = [AssertNode(type="Incident", name="INC1"), AssertNode(type="Lane", name="5")]
    edges = [
        AssertEdge(src="INC1", dst="5", type="A", weight=9.0),  # clamps to 1.0
        AssertEdge(src="5", dst="INC1", type="B", weight=-3.0),  # clamps to 0.0
    ]
    _, rels = await _build(nodes, edges)
    by_type = {r.type: r.weight for r in rels}
    assert by_type["A"] == 1.0
    assert by_type["B"] == 0.0


@pytest.mark.asyncio
async def test_empty_nodes_returns_empty_without_embedding(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("embed_texts must not be called for empty input")

    monkeypatch.setattr(graph_assert, "embed_texts", boom)
    assert await _build([]) == ([], [])
    # nodes with only blank name/type also short-circuit before embedding
    assert await _build([AssertNode(type=" ", name=" ")]) == ([], [])


@pytest.mark.asyncio
async def test_project_id_stamped():
    nodes, edges = _incident_facts()
    ents, rels = await _build(nodes, edges, project_id="proj-1")
    assert all(e.project_id == "proj-1" for e in ents)
    assert all(r.project_id == "proj-1" for r in rels)


@pytest.mark.asyncio
async def test_resolve_existing_id_reconciles_endpoints():
    """When the persistence layer reports a node already exists (e.g. extracted
    earlier under a random uuid), the asserted node AND its relations reference
    that existing id — not the deterministic content-hash id."""
    nodes = [AssertNode(type="Incident", name="INC1"), AssertNode(type="Store", name="118")]
    edges = [AssertEdge(src="INC1", dst="118", type="AT_STORE")]

    store_hash = graph_assert._entity_hash("store-support", "devtenant", "118", "Store")

    def resolver(content_hash: str) -> str | None:
        # "118" was extracted earlier under this random uuid
        return "extracted-uuid-118" if content_hash == store_hash else None

    ents, rels = await _build(nodes, edges, resolve_existing_id=resolver)
    by_name = {e.name: e for e in ents}
    assert by_name["118"].entity_id == "extracted-uuid-118"
    # content_hash is unchanged (still the deterministic dedup key)
    assert by_name["118"].content_hash == store_hash
    # the relation's endpoint follows the reconciled id, not the hash
    assert rels[0].dst_entity_id == "extracted-uuid-118"
    # an unreconciled node keeps its deterministic id
    assert by_name["INC1"].entity_id == by_name["INC1"].content_hash


def test_identity_contract_matches_graph_extract():
    """The dedup hashes MUST be byte-identical to graph_extract's, so an
    asserted node and an extracted node for the same (agent, tenant, name, type)
    collapse to one row. This guards against the two builders drifting."""
    args = ("store-support", "devtenant", "INC0042217", "Incident")
    assert graph_assert._entity_hash(*args) == ext._entity_hash(*args)
    assert graph_assert._norm("  Frozen   Register ") == ext._norm("  Frozen   Register ")
    rel_args = ("store-support", "devtenant", "src-id", "dst-id", "ON_LANE")
    assert graph_assert._relation_hash(*rel_args) == ext._relation_hash(*rel_args)
