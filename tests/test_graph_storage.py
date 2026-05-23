"""Conformance tests for the GraphRAG storage surface.

Every test runs against all three backends via the parametrized
``storage`` fixture in conftest.py (memory / sqlite / postgres — the
last skips unless ``MOVATE_PG_TEST_URL`` is set). The point is that the
``StorageProvider`` graph contract behaves identically regardless of
backend: upsert/dedup, tenant + agent isolation, vector-seed search, and
bounded k-hop expansion.
"""

from __future__ import annotations

import hashlib

import pytest

from movate.core.models import Entity, KbChunk, Relation
from movate.kb.embed import embedding_dim
from movate.storage.base import StorageProvider

pytestmark = pytest.mark.unit


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _pad(vec: list[float]) -> list[float]:
    """Pad a short test vector to the configured embedding dim with zeros.

    The short literals (``[1, 0, 0]``) only fit memory/sqlite; the real
    Postgres backend stores entity embeddings in a fixed ``vector(N)`` column
    (ADR 009 D1), so they must be N-dim. Trailing zeros preserve the cosine
    relationships among the leading components, so search ordering is unchanged.
    """
    dim = embedding_dim()
    return vec if len(vec) >= dim else vec + [0.0] * (dim - len(vec))


def _entity(
    name: str,
    type: str,
    embedding: list[float],
    *,
    agent: str = "a1",
    tenant_id: str = "t1",
    source_chunk_ids: list[str] | None = None,
) -> Entity:
    return Entity(
        tenant_id=tenant_id,
        agent=agent,
        name=name,
        type=type,
        embedding=_pad(embedding),
        embedding_model="test-embed",
        content_hash=_hash(agent, tenant_id, name, type),
        source_chunk_ids=source_chunk_ids or [],
    )


def _relation(
    src: str,
    dst: str,
    type: str,
    weight: float,
    *,
    agent: str = "a1",
    tenant_id: str = "t1",
    source_chunk_ids: list[str] | None = None,
) -> Relation:
    return Relation(
        tenant_id=tenant_id,
        agent=agent,
        src_entity_id=src,
        dst_entity_id=dst,
        type=type,
        weight=weight,
        content_hash=_hash(agent, tenant_id, src, dst, type),
        source_chunk_ids=source_chunk_ids or [],
    )


def _chunk(chunk_id: str, source: str, *, agent: str = "a1", tenant_id: str = "t1") -> KbChunk:
    return KbChunk(
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        agent=agent,
        source=source,
        text=f"text for {chunk_id}",
        embedding=_pad([1.0, 0.0, 0.0]),
        embedding_model="test-embed",
        content_hash=_hash(chunk_id),
    )


# ---------------------------------------------------------------------------
# Upsert / get / dedup
# ---------------------------------------------------------------------------


async def test_upsert_and_get_entity(storage: StorageProvider) -> None:
    e = _entity("SAML SSO", "Feature", [1.0, 0.0, 0.0], source_chunk_ids=["c1"])
    await storage.upsert_entity(e)
    got = await storage.get_entity(e.entity_id, tenant_id="t1")
    assert got is not None
    assert got.name == "SAML SSO"
    assert got.type == "Feature"
    assert got.source_chunk_ids == ["c1"]


async def test_get_entity_tenant_isolation(storage: StorageProvider) -> None:
    e = _entity("SAML SSO", "Feature", [1.0, 0.0, 0.0])
    await storage.upsert_entity(e)
    # Same id, wrong tenant → None (404-not-403; never leaks existence).
    assert await storage.get_entity(e.entity_id, tenant_id="other") is None


async def test_get_missing_entity_returns_none(storage: StorageProvider) -> None:
    assert await storage.get_entity("does-not-exist", tenant_id="t1") is None


async def test_upsert_entity_dedup_merges_provenance(storage: StorageProvider) -> None:
    first = _entity("SAML SSO", "Feature", [1.0, 0.0, 0.0], source_chunk_ids=["c1"])
    await storage.upsert_entity(first)
    # Re-extract the same entity (same content_hash) from a different chunk.
    again = _entity("SAML SSO", "Feature", [0.5, 0.5, 0.0], source_chunk_ids=["c2"])
    await storage.upsert_entity(again)

    rows = await storage.list_entities(agent="a1", tenant_id="t1")
    assert len(rows) == 1, "same content_hash must collapse to one node"
    merged = rows[0]
    # entity_id is preserved from the first insert (cached refs stay valid).
    assert merged.entity_id == first.entity_id
    # Provenance is the UNION; embedding/description refreshed to latest.
    assert set(merged.source_chunk_ids) == {"c1", "c2"}
    assert merged.embedding == _pad([0.5, 0.5, 0.0])


async def test_upsert_relation_dedup_merges_provenance(storage: StorageProvider) -> None:
    r1 = _relation("e1", "e2", "REQUIRES", 0.8, source_chunk_ids=["c1"])
    await storage.upsert_relation(r1)
    r2 = _relation("e1", "e2", "REQUIRES", 0.9, source_chunk_ids=["c2"])
    await storage.upsert_relation(r2)

    rels = await storage.list_relations(agent="a1", tenant_id="t1")
    assert len(rels) == 1
    assert rels[0].relation_id == r1.relation_id
    assert set(rels[0].source_chunk_ids) == {"c1", "c2"}
    assert rels[0].weight == 0.9  # refreshed to latest


# ---------------------------------------------------------------------------
# Vector seed search
# ---------------------------------------------------------------------------


async def test_search_entities_ranks_by_cosine(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("X axis", "Axis", [1.0, 0.0, 0.0]))
    await storage.upsert_entity(_entity("Y axis", "Axis", [0.0, 1.0, 0.0]))
    await storage.upsert_entity(_entity("Z axis", "Axis", [0.0, 0.0, 1.0]))

    hits = await storage.search_entities(
        agent="a1", tenant_id="t1", query_embedding=_pad([0.9, 0.1, 0.0]), limit=2
    )
    assert len(hits) == 2
    assert hits[0].entity.name == "X axis"
    assert hits[0].score >= hits[1].score


async def test_search_entities_empty_graph(storage: StorageProvider) -> None:
    hits = await storage.search_entities(
        agent="a1", tenant_id="t1", query_embedding=_pad([1.0, 0.0, 0.0]), limit=5
    )
    assert hits == []


async def test_search_entities_agent_scoped(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("Shared name", "T", [1.0, 0.0, 0.0], agent="a1"))
    await storage.upsert_entity(_entity("Shared name", "T", [1.0, 0.0, 0.0], agent="a2"))
    hits = await storage.search_entities(
        agent="a1", tenant_id="t1", query_embedding=_pad([1.0, 0.0, 0.0]), limit=10
    )
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# Bounded k-hop expansion
# ---------------------------------------------------------------------------


async def _seed_chain(storage: StorageProvider) -> dict[str, Entity]:
    """A -> B -> C -> D with descending edge weights. Returns name→Entity."""
    a = _entity("A", "N", [1.0, 0.0, 0.0])
    b = _entity("B", "N", [0.0, 1.0, 0.0])
    c = _entity("C", "N", [0.0, 0.0, 1.0])
    d = _entity("D", "N", [1.0, 1.0, 0.0])
    for e in (a, b, c, d):
        await storage.upsert_entity(e)
    await storage.upsert_relation(_relation(a.entity_id, b.entity_id, "R", 0.9))
    await storage.upsert_relation(_relation(b.entity_id, c.entity_id, "R", 0.5))
    await storage.upsert_relation(_relation(c.entity_id, d.entity_id, "R", 0.2))
    return {"A": a, "B": b, "C": c, "D": d}


async def test_expand_one_hop(storage: StorageProvider) -> None:
    nodes = await _seed_chain(storage)
    sg = await storage.expand_neighbors(
        agent="a1", tenant_id="t1", entity_ids=[nodes["A"].entity_id], hops=1
    )
    assert {e.name for e in sg.entities} == {"A", "B"}
    assert len(sg.relations) == 1


async def test_expand_two_hop(storage: StorageProvider) -> None:
    nodes = await _seed_chain(storage)
    sg = await storage.expand_neighbors(
        agent="a1", tenant_id="t1", entity_ids=[nodes["A"].entity_id], hops=2
    )
    assert {e.name for e in sg.entities} == {"A", "B", "C"}
    # Every returned edge's endpoints are present in entities.
    ids = {e.entity_id for e in sg.entities}
    for r in sg.relations:
        assert r.src_entity_id in ids and r.dst_entity_id in ids


async def test_expand_budget_limit_keeps_strongest(storage: StorageProvider) -> None:
    nodes = await _seed_chain(storage)
    sg = await storage.expand_neighbors(
        agent="a1", tenant_id="t1", entity_ids=[nodes["A"].entity_id], hops=3, limit=1
    )
    assert len(sg.relations) == 1
    assert sg.relations[0].weight == 0.9  # strongest edge survives the cap


async def test_expand_empty_seed(storage: StorageProvider) -> None:
    await _seed_chain(storage)
    sg = await storage.expand_neighbors(agent="a1", tenant_id="t1", entity_ids=[], hops=2)
    assert sg.entities == []
    assert sg.relations == []


async def test_expand_unknown_seed(storage: StorageProvider) -> None:
    await _seed_chain(storage)
    sg = await storage.expand_neighbors(agent="a1", tenant_id="t1", entity_ids=["ghost-id"], hops=2)
    # Unknown seed reaches nothing and isn't a stored entity → empty.
    assert sg.entities == []
    assert sg.relations == []


async def test_expand_tenant_isolation(storage: StorageProvider) -> None:
    nodes = await _seed_chain(storage)
    sg = await storage.expand_neighbors(
        agent="a1", tenant_id="other", entity_ids=[nodes["A"].entity_id], hops=2
    )
    assert sg.entities == []
    assert sg.relations == []


# ---------------------------------------------------------------------------
# Listing + deletion
# ---------------------------------------------------------------------------


async def test_list_entities_by_source_chunk(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("From c1", "N", [1.0, 0.0, 0.0], source_chunk_ids=["c1"]))
    await storage.upsert_entity(_entity("From c2", "N", [0.0, 1.0, 0.0], source_chunk_ids=["c2"]))
    only_c1 = await storage.list_entities(agent="a1", tenant_id="t1", source_chunk_id="c1")
    assert {e.name for e in only_c1} == {"From c1"}


async def test_delete_graph_whole(storage: StorageProvider) -> None:
    nodes = await _seed_chain(storage)
    deleted = await storage.delete_graph(agent="a1", tenant_id="t1")
    assert deleted == len(nodes) + 3  # 4 entities + 3 relations
    assert await storage.list_entities(agent="a1", tenant_id="t1") == []
    assert await storage.list_relations(agent="a1", tenant_id="t1") == []


async def test_delete_graph_tenant_and_agent_scoped(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("Keep me", "N", [1.0, 0.0, 0.0], agent="a2"))
    await storage.upsert_entity(_entity("Delete me", "N", [1.0, 0.0, 0.0], agent="a1"))
    await storage.delete_graph(agent="a1", tenant_id="t1")
    assert await storage.list_entities(agent="a1", tenant_id="t1") == []
    survivors = await storage.list_entities(agent="a2", tenant_id="t1")
    assert {e.name for e in survivors} == {"Keep me"}


async def test_delete_graph_by_source_keeps_multi_source(storage: StorageProvider) -> None:
    # Chunks: c1 from doc1, c2 from doc2.
    await storage.save_kb_chunk(_chunk("c1", "doc1.md"))
    await storage.save_kb_chunk(_chunk("c2", "doc2.md"))
    # solely-doc1 entity, solely-doc2 entity, and a spanning entity.
    await storage.upsert_entity(_entity("Only doc1", "N", [1.0, 0.0, 0.0], source_chunk_ids=["c1"]))
    await storage.upsert_entity(_entity("Only doc2", "N", [0.0, 1.0, 0.0], source_chunk_ids=["c2"]))
    await storage.upsert_entity(
        _entity("Spans both", "N", [0.0, 0.0, 1.0], source_chunk_ids=["c1", "c2"])
    )

    await storage.delete_graph(agent="a1", tenant_id="t1", source="doc1.md")

    remaining = {e.name for e in await storage.list_entities(agent="a1", tenant_id="t1")}
    # "Only doc1" is solely from the deleted source → gone.
    # "Spans both" has provenance beyond doc1 → survives.
    assert remaining == {"Only doc2", "Spans both"}
