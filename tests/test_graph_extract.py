"""Unit tests for the GraphRAG extraction pipeline.

The LLM is injected via ``complete_fn`` (prompt → canned JSON) and the
embedding call is monkeypatched, so these run with no network access.
They cover the parts that carry real logic: cross-chunk entity merge +
provenance, relation endpoint resolution, dedup, weight handling, and
fault tolerance for bad model output.
"""

from __future__ import annotations

import json

import pytest

from movate.core.models import KbChunk
from movate.kb import graph_extract
from movate.kb.graph_extract import extract_graph

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    """Deterministic 3-dim embeddings; no API call."""

    async def fake_embed(texts, *, model="", api_key=None, timeout_s=60.0):
        return [[float(len(t) % 5), 1.0, 0.0] for t in texts]

    monkeypatch.setattr(graph_extract, "embed_texts", fake_embed)


def _chunk(chunk_id: str, text: str) -> KbChunk:
    return KbChunk(
        chunk_id=chunk_id,
        tenant_id="t1",
        agent="a1",
        source="doc.md",
        text=text,
        embedding=[0.0, 0.0, 0.0],
        embedding_model="openai/text-embedding-3-small",
        content_hash=f"h-{chunk_id}",
    )


def _responder(mapping: dict[str, dict]):
    """Build a complete_fn that returns canned JSON keyed by a marker
    substring present in the prompt's passage."""

    async def _call(prompt: str) -> str:
        for marker, payload in mapping.items():
            if marker in prompt:
                return json.dumps(payload)
        return json.dumps({"entities": [], "relations": []})

    return _call


async def test_extract_basic_entities_and_relation():
    chunks = [_chunk("c1", "MARKER_A: sso requires the enterprise tier")]
    responder = _responder(
        {
            "MARKER_A": {
                "entities": [
                    {"name": "SAML SSO", "type": "Feature", "description": "Single sign-on."},
                    {"name": "Enterprise Tier", "type": "Tier", "description": "Top plan."},
                ],
                "relations": [
                    {
                        "src": "SAML SSO",
                        "dst": "Enterprise Tier",
                        "type": "REQUIRES",
                        "description": "SSO needs Enterprise.",
                        "weight": 0.9,
                    }
                ],
            }
        }
    )
    entities, relations = await extract_graph(
        chunks, agent="a1", tenant_id="t1", complete_fn=responder
    )
    assert {e.name for e in entities} == {"SAML SSO", "Enterprise Tier"}
    assert all(e.source_chunk_ids == ["c1"] for e in entities)
    assert all(len(e.embedding) == 3 for e in entities)
    assert all(e.embedding_model == "openai/text-embedding-3-small" for e in entities)
    assert len(relations) == 1
    rel = relations[0]
    by_name = {e.entity_id: e.name for e in entities}
    assert by_name[rel.src_entity_id] == "SAML SSO"
    assert by_name[rel.dst_entity_id] == "Enterprise Tier"
    assert rel.type == "REQUIRES"
    assert rel.weight == 0.9
    assert rel.source_chunk_ids == ["c1"]


async def test_entity_merged_across_chunks_unions_provenance():
    chunks = [
        _chunk("c1", "MARKER_A talks about audit log"),
        _chunk("c2", "MARKER_B also mentions audit log"),
    ]
    same_entity = {
        "entities": [{"name": "Audit Log", "type": "Feature", "description": "Records events."}],
        "relations": [],
    }
    responder = _responder({"MARKER_A": same_entity, "MARKER_B": same_entity})
    entities, _ = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    assert len(entities) == 1, "same (name,type) must merge to one node"
    assert set(entities[0].source_chunk_ids) == {"c1", "c2"}


async def test_relation_with_unresolved_endpoint_is_dropped():
    chunks = [_chunk("c1", "MARKER_A")]
    responder = _responder(
        {
            "MARKER_A": {
                "entities": [{"name": "A", "type": "N", "description": ""}],
                "relations": [
                    {"src": "A", "dst": "Ghost", "type": "R", "weight": 0.5},
                ],
            }
        }
    )
    entities, relations = await extract_graph(
        chunks, agent="a1", tenant_id="t1", complete_fn=responder
    )
    assert len(entities) == 1
    assert relations == [], "edge to a non-extracted entity must be dropped"


async def test_self_loop_dropped():
    chunks = [_chunk("c1", "MARKER_A")]
    responder = _responder(
        {
            "MARKER_A": {
                "entities": [{"name": "A", "type": "N", "description": ""}],
                "relations": [{"src": "A", "dst": "A", "type": "R", "weight": 1.0}],
            }
        }
    )
    _, relations = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    assert relations == []


async def test_relation_dedup_takes_max_weight_and_unions_provenance():
    chunks = [_chunk("c1", "MARKER_A"), _chunk("c2", "MARKER_B")]
    payload = lambda w: {  # noqa: E731
        "entities": [
            {"name": "A", "type": "N", "description": ""},
            {"name": "B", "type": "N", "description": ""},
        ],
        "relations": [{"src": "A", "dst": "B", "type": "R", "weight": w}],
    }
    responder = _responder({"MARKER_A": payload(0.3), "MARKER_B": payload(0.8)})
    _, relations = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    assert len(relations) == 1
    assert relations[0].weight == 0.8
    assert set(relations[0].source_chunk_ids) == {"c1", "c2"}


async def test_empty_chunks_returns_empty():
    entities, relations = await extract_graph(
        [], agent="a1", tenant_id="t1", complete_fn=_responder({})
    )
    assert entities == []
    assert relations == []


async def test_bad_output_skipped_without_raising():
    chunks = [_chunk("c1", "GARBAGE"), _chunk("c2", "MARKER_GOOD")]

    async def responder(prompt: str) -> str:
        if "GARBAGE" in prompt:
            return "not json at all {{{"
        return json.dumps(
            {"entities": [{"name": "Good", "type": "N", "description": ""}], "relations": []}
        )

    entities, _ = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    assert {e.name for e in entities} == {"Good"}


async def test_weight_defaulted_when_missing_or_malformed():
    chunks = [_chunk("c1", "MARKER_A")]
    responder = _responder(
        {
            "MARKER_A": {
                "entities": [
                    {"name": "A", "type": "N", "description": ""},
                    {"name": "B", "type": "N", "description": ""},
                ],
                "relations": [{"src": "A", "dst": "B", "type": "R"}],  # no weight
            }
        }
    )
    _, relations = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    assert relations[0].weight == 0.5


async def test_content_hash_is_scoped_and_stable():
    chunks = [_chunk("c1", "MARKER_A")]
    responder = _responder(
        {"MARKER_A": {"entities": [{"name": "X", "type": "Y", "description": ""}], "relations": []}}
    )
    e1, _ = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    e2, _ = await extract_graph(chunks, agent="a1", tenant_id="t2", complete_fn=responder)
    # Same name/type but different tenant → different dedup hash.
    assert e1[0].content_hash != e2[0].content_hash
    # Re-running with the same scope is stable (idempotent upsert key).
    e1b, _ = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    assert e1[0].content_hash == e1b[0].content_hash


async def test_extract_tags_project_id_when_given():
    """ADR 046 D1 — ``project_id`` is stamped on every extracted node/edge
    but is NOT part of the dedup hash (so re-ingest under a project
    backfills in place)."""
    chunks = [_chunk("c1", "MARKER_A: sso requires the enterprise tier")]
    responder = _responder(
        {
            "MARKER_A": {
                "entities": [
                    {"name": "SAML SSO", "type": "Feature", "description": "SSO."},
                    {"name": "Enterprise Tier", "type": "Tier", "description": "Top plan."},
                ],
                "relations": [
                    {"src": "SAML SSO", "dst": "Enterprise Tier", "type": "REQUIRES", "weight": 0.9}
                ],
            }
        }
    )
    entities, relations = await extract_graph(
        chunks, agent="a1", tenant_id="t1", complete_fn=responder, project_id="proj-42"
    )
    assert entities and all(e.project_id == "proj-42" for e in entities)
    assert relations and all(r.project_id == "proj-42" for r in relations)
    # content_hash unchanged vs. the project-less extraction (not in dedup key).
    project_less, _ = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    by_name = {e.name: e for e in project_less}
    for e in entities:
        assert e.content_hash == by_name[e.name].content_hash


async def test_extract_project_id_defaults_none():
    chunks = [_chunk("c1", "MARKER_A")]
    responder = _responder(
        {"MARKER_A": {"entities": [{"name": "X", "type": "Y", "description": ""}], "relations": []}}
    )
    entities, _ = await extract_graph(chunks, agent="a1", tenant_id="t1", complete_fn=responder)
    assert entities[0].project_id is None
