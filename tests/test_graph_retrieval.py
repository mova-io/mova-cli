"""Tests for GraphRAG retrieval: seed → expand → assemble, plus the
budget guards and the prompt rendering. Embeddings are stubbed so the
seed step is deterministic and offline.
"""

from __future__ import annotations

import hashlib

import pytest

from movate.core.models import Entity, Relation
from movate.kb import graph_retrieval
from movate.kb.graph_retrieval import GraphContext, graph_retrieve, render_graph_context
from movate.testing import InMemoryStorage

pytestmark = pytest.mark.unit


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _entity(name: str, embedding: list[float], *, type: str = "N", description: str = "") -> Entity:
    return Entity(
        tenant_id="t1",
        agent="a1",
        name=name,
        type=type,
        description=description or None,
        embedding=embedding,
        embedding_model="test-embed",
        content_hash=_hash("a1", "t1", name, type),
    )


def _relation(src: str, dst: str, type: str, weight: float = 1.0) -> Relation:
    return Relation(
        tenant_id="t1",
        agent="a1",
        src_entity_id=src,
        dst_entity_id=dst,
        type=type,
        weight=weight,
        content_hash=_hash("a1", "t1", src, dst, type),
    )


@pytest.fixture
def _query_hits_alpha(monkeypatch):
    """Stub embed_texts so the query vector points at the 'Alpha' seed."""

    async def fake_embed(texts, *, model="", api_key=None, timeout_s=60.0):
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(graph_retrieval, "embed_texts", fake_embed)


async def _seed_graph(storage: InMemoryStorage) -> dict[str, Entity]:
    a = _entity("Alpha", [1.0, 0.0, 0.0], type="Feature", description="The A feature.")
    b = _entity("Beta", [0.0, 1.0, 0.0], type="Tier")
    c = _entity("Gamma", [0.0, 0.0, 1.0], type="Policy")
    for e in (a, b, c):
        await storage.upsert_entity(e)
    await storage.upsert_relation(_relation(a.entity_id, b.entity_id, "REQUIRES", 0.9))
    await storage.upsert_relation(_relation(b.entity_id, c.entity_id, "GOVERNS", 0.5))
    return {"A": a, "B": b, "C": c}


async def test_retrieve_seeds_and_one_hop(_query_hits_alpha):
    storage = InMemoryStorage()
    await storage.init()
    await _seed_graph(storage)

    ctx = await graph_retrieve(
        storage=storage,
        agent="a1",
        tenant_id="t1",
        query="tell me about alpha",
        seed_limit=1,
        hops=1,
    )
    assert isinstance(ctx, GraphContext)
    assert {e.name for e in ctx.entities} == {"Alpha", "Beta"}
    assert len(ctx.relations) == 1
    assert "Entities:" in ctx.text
    assert "Alpha (Feature): The A feature." in ctx.text
    assert "Alpha —REQUIRES→ Beta" in ctx.text


async def test_retrieve_two_hop_reaches_further(_query_hits_alpha):
    storage = InMemoryStorage()
    await storage.init()
    await _seed_graph(storage)

    ctx = await graph_retrieve(
        storage=storage, agent="a1", tenant_id="t1", query="alpha", seed_limit=1, hops=2
    )
    assert {e.name for e in ctx.entities} == {"Alpha", "Beta", "Gamma"}


async def test_hops_clamped_to_max(_query_hits_alpha):
    storage = InMemoryStorage()
    await storage.init()
    await _seed_graph(storage)

    # hops=99 is clamped to MAX_HOPS; on this 3-node chain that still
    # reaches everything but must not error.
    ctx = await graph_retrieve(
        storage=storage, agent="a1", tenant_id="t1", query="alpha", seed_limit=1, hops=99
    )
    assert {e.name for e in ctx.entities} == {"Alpha", "Beta", "Gamma"}


async def test_max_relations_caps_result(_query_hits_alpha):
    storage = InMemoryStorage()
    await storage.init()
    await _seed_graph(storage)

    ctx = await graph_retrieve(
        storage=storage,
        agent="a1",
        tenant_id="t1",
        query="alpha",
        seed_limit=1,
        hops=2,
        max_relations=1,
    )
    assert len(ctx.relations) == 1  # capped, strongest edge kept


async def test_empty_query_returns_empty(_query_hits_alpha):
    storage = InMemoryStorage()
    await storage.init()
    await _seed_graph(storage)
    ctx = await graph_retrieve(storage=storage, agent="a1", tenant_id="t1", query="   ")
    assert ctx.is_empty
    assert ctx.text == ""


async def test_empty_graph_returns_empty(_query_hits_alpha):
    storage = InMemoryStorage()
    await storage.init()
    ctx = await graph_retrieve(storage=storage, agent="a1", tenant_id="t1", query="anything")
    assert ctx.is_empty
    assert ctx.text == ""


def test_render_graph_context_format():
    a = _entity("Alpha", [1.0, 0.0, 0.0], type="Feature", description="A.")
    b = _entity("Beta", [0.0, 1.0, 0.0], type="Tier")
    rel = _relation(a.entity_id, b.entity_id, "REQUIRES")
    rel.description = "Needs it."
    text = render_graph_context([a, b], [rel])
    assert text == (
        "Entities:\n"
        "- Alpha (Feature): A.\n"
        "- Beta (Tier)\n"
        "\n"
        "Relationships:\n"
        "- Alpha —REQUIRES→ Beta: Needs it."
    )


def test_render_empty_is_empty_string():
    assert render_graph_context([], []) == ""
