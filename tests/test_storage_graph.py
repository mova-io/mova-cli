"""Graph query layer over real persistence (ADR 046).

The read-only query API (``core.graph``) reads the ALREADY-PERSISTED
GraphRAG graph through the existing ``StorageProvider`` surface — this PR
adds no new storage methods. These tests therefore exercise the query
layer end-to-end against the *real* backends (InMemory + SQLite, with
Postgres gated on ``MOVATE_PG_TEST_URL``) via the parametrized ``storage``
fixture in conftest.py: persist entities/relations, then assert windowing,
neighbor expansion, and provenance produce the right graphology output on
each backend.

(Backend conformance of the underlying upsert/expand/search primitives is
covered separately by ``test_graph_storage.py``.)
"""

from __future__ import annotations

import hashlib

import pytest

from movate.core.graph import query as gq
from movate.core.models import Entity, KbChunk, Relation
from movate.kb.embed import embedding_dim
from movate.storage.base import StorageProvider

pytestmark = pytest.mark.unit


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _pad(vec: list[float]) -> list[float]:
    dim = embedding_dim()
    return vec if len(vec) >= dim else vec + [0.0] * (dim - len(vec))


def _entity(
    entity_id: str, name: str, type: str = "T", *, source_chunk_ids: list[str] | None = None
) -> Entity:
    return Entity(
        entity_id=entity_id,
        tenant_id="t1",
        agent="a1",
        name=name,
        type=type,
        embedding=_pad([1.0, 0.0, 0.0]),
        embedding_model="test-embed",
        content_hash=_hash("a1", "t1", name, type),
        source_chunk_ids=source_chunk_ids or [],
    )


def _relation(
    rid: str, src: str, dst: str, weight: float = 1.0, *, source_chunk_ids: list[str] | None = None
) -> Relation:
    return Relation(
        relation_id=rid,
        tenant_id="t1",
        agent="a1",
        src_entity_id=src,
        dst_entity_id=dst,
        type="REL",
        weight=weight,
        content_hash=_hash("a1", "t1", src, dst, "REL"),
        source_chunk_ids=source_chunk_ids or [],
    )


def _chunk(chunk_id: str, source: str, text: str) -> KbChunk:
    return KbChunk(
        chunk_id=chunk_id,
        tenant_id="t1",
        agent="a1",
        source=source,
        text=text,
        embedding=_pad([1.0, 0.0, 0.0]),
        embedding_model="test-embed",
        content_hash=_hash(chunk_id),
    )


async def test_window_over_persisted_graph(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("n1", "A"))
    await storage.upsert_entity(_entity("n2", "B"))
    await storage.upsert_relation(_relation("e1", "n1", "n2", weight=0.5))

    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1")
    assert {n.key for n in doc.nodes} == {"n1", "n2"}
    assert {e.key for e in doc.edges} == {"e1"}
    # Edge carries the persisted weight as a graphology attribute.
    assert doc.edges[0].attributes["weight"] == pytest.approx(0.5)


async def test_neighbor_expansion_over_persisted_graph(storage: StorageProvider) -> None:
    for eid, name in (("a", "A"), ("b", "B"), ("c", "C")):
        await storage.upsert_entity(_entity(eid, name))
    await storage.upsert_relation(_relation("e1", "a", "b"))
    await storage.upsert_relation(_relation("e2", "b", "c"))

    doc = await gq.expand_node_neighbors(storage, agent="a1", tenant_id="t1", node_id="a", depth=1)
    assert {n.key for n in doc.nodes} == {"a", "b"}


async def test_node_detail_provenance_over_persisted_graph(storage: StorageProvider) -> None:
    await storage.save_kb_chunk(_chunk("c1", "docs/x.md", "Alpha is a core concept."))
    await storage.upsert_entity(_entity("n1", "Alpha", source_chunk_ids=["c1"]))
    await storage.upsert_entity(_entity("n2", "Beta"))
    await storage.upsert_relation(_relation("e1", "n1", "n2", weight=0.9, source_chunk_ids=["c1"]))

    detail = await gq.node_detail(storage, agent="a1", tenant_id="t1", node_id="n1")
    assert detail is not None
    assert detail.label == "Alpha"
    assert detail.neighbor_count == 1
    assert len(detail.provenance) == 1
    assert detail.provenance[0].url == "docs/x.md"
    assert detail.provenance[0].extraction_confidence == pytest.approx(0.9)


async def test_search_over_persisted_graph(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("n1", "Payment Gateway", "Feature"))
    await storage.upsert_entity(_entity("n2", "User Login", "Feature"))
    hits = await gq.search_nodes(storage, agent="a1", tenant_id="t1", q="payment")
    assert [h.key for h in hits] == ["n1"]


async def test_unknown_node_detail_is_none(storage: StorageProvider) -> None:
    detail = await gq.node_detail(storage, agent="a1", tenant_id="t1", node_id="missing")
    assert detail is None
