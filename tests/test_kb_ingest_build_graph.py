"""The --build-graph ingest hook: ingest_text optionally extracts +
persists a knowledge graph alongside the embedded chunks.

LLM + embeddings are stubbed so this runs offline. Verifies the graph is
gated behind the flag, lands in storage with chunk-level provenance, and
that the summary reports the counts.
"""

from __future__ import annotations

import json

import pytest

from movate.kb import graph_extract, ingest
from movate.kb.ingest import ingest_text
from movate.testing import InMemoryStorage

pytestmark = pytest.mark.unit

_DOC = "SAML SSO is a feature.\n\nThe Enterprise tier unlocks it.\n\nAudit logging is included."


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch):
    async def fake_embed(texts, *, model="", api_key=None, timeout_s=60.0):
        return [[float(len(t) % 5), 1.0, 0.0] for t in texts]

    # Chunk embeddings (ingest namespace) + entity embeddings (extractor).
    monkeypatch.setattr(ingest, "embed_texts", fake_embed)
    monkeypatch.setattr(graph_extract, "embed_texts", fake_embed)


async def _stub_complete(prompt: str) -> str:
    # Same graph for every chunk → entities dedup-merge across chunks,
    # accumulating provenance.
    return json.dumps(
        {
            "entities": [
                {"name": "SAML SSO", "type": "Feature", "description": "SSO."},
                {"name": "Enterprise Tier", "type": "Tier", "description": "Top plan."},
            ],
            "relations": [
                {"src": "SAML SSO", "dst": "Enterprise Tier", "type": "REQUIRES", "weight": 0.9},
            ],
        }
    )


async def test_build_graph_persists_entities_and_relations():
    storage = InMemoryStorage()
    await storage.init()
    summary = await ingest_text(
        storage=storage,
        text=_DOC,
        source="doc.md",
        agent="a1",
        tenant_id="t1",
        build_graph=True,
        complete_fn=_stub_complete,
    )
    assert summary is not None
    assert summary.entities_saved == 2
    assert summary.relations_saved == 1

    entities = await storage.list_entities(agent="a1", tenant_id="t1")
    assert {e.name for e in entities} == {"SAML SSO", "Enterprise Tier"}
    relations = await storage.list_relations(agent="a1", tenant_id="t1")
    assert len(relations) == 1

    # Provenance: each entity cites the chunk ids it was extracted from,
    # and those ids are real persisted chunks.
    chunk_ids = {c.chunk_id for c in await storage.list_kb_chunks(agent="a1", tenant_id="t1")}
    for e in entities:
        assert e.source_chunk_ids, "entity must carry provenance"
        assert set(e.source_chunk_ids) <= chunk_ids


async def test_build_graph_on_by_default():
    """Graph extraction runs by default (build_graph=True since 2026.6).

    The stub complete_fn MUST be called when build_graph defaults to True.
    """
    storage = InMemoryStorage()
    await storage.init()

    summary = await ingest_text(
        storage=storage,
        text=_DOC,
        source="doc.md",
        agent="a1",
        tenant_id="t1",
        complete_fn=_stub_complete,
    )
    assert summary is not None
    assert summary.entities_saved > 0
    assert summary.relations_saved > 0


async def test_build_graph_skipped_when_false():
    """Passing build_graph=False (CLI: --skip-graph) suppresses extraction."""
    storage = InMemoryStorage()
    await storage.init()

    async def _boom(prompt: str) -> str:  # must never be called
        raise AssertionError("extraction ran with build_graph=False")

    summary = await ingest_text(
        storage=storage,
        text=_DOC,
        source="doc.md",
        agent="a1",
        tenant_id="t1",
        build_graph=False,
        complete_fn=_boom,
    )
    assert summary is not None
    assert summary.entities_saved == 0
    assert summary.relations_saved == 0
    assert await storage.list_entities(agent="a1", tenant_id="t1") == []
