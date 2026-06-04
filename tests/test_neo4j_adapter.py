"""Neo4j graph adapter tests — unit (mock) + integration (live Neo4j).

Unit tests verify Cypher generation and parameter building against a mocked
Neo4j driver. They always run (no external dependency).

Integration tests are gated behind ``@pytest.mark.neo4j`` and require a
running Neo4j instance (``NEO4J_URI`` + ``NEO4J_USER`` + ``NEO4J_PASSWORD``
env vars). They are **skipped by default** in CI; run them with::

    pytest -m neo4j --neo4j-uri bolt://localhost:7687

The mark is registered in ``conftest.py`` (or ``pyproject.toml``).
"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from movate.core.models import Entity, Relation

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _entity(
    entity_id: str = "e1",
    name: str = "TestEntity",
    type: str = "Feature",
    *,
    agent: str = "a1",
    tenant_id: str = "t1",
    project_id: str | None = None,
) -> Entity:
    return Entity(
        entity_id=entity_id,
        tenant_id=tenant_id,
        agent=agent,
        name=name,
        type=type,
        embedding=[1.0, 0.0, 0.0],
        embedding_model="test",
        content_hash=_hash(agent, tenant_id, name, type),
        source_chunk_ids=[],
        project_id=project_id,
    )


def _relation(
    relation_id: str = "r1",
    src: str = "e1",
    dst: str = "e2",
    weight: float = 1.0,
    *,
    agent: str = "a1",
    tenant_id: str = "t1",
    project_id: str | None = None,
) -> Relation:
    return Relation(
        relation_id=relation_id,
        tenant_id=tenant_id,
        agent=agent,
        src_entity_id=src,
        dst_entity_id=dst,
        type="RELATES_TO",
        weight=weight,
        content_hash=_hash(agent, tenant_id, src, dst),
        source_chunk_ids=[],
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# Unit: parameter builders
# ---------------------------------------------------------------------------


def test_entity_to_params() -> None:
    """_entity_to_params produces the expected Cypher param dict."""
    from movate.storage.neo4j import _entity_to_params  # noqa: PLC0415

    entity = _entity()
    params = _entity_to_params(entity)
    assert params["entity_id"] == "e1"
    assert params["name"] == "TestEntity"
    assert params["agent"] == "a1"
    assert params["tenant_id"] == "t1"
    assert params["content_hash"] == entity.content_hash
    assert params["embedding"] == json.dumps([1.0, 0.0, 0.0])
    assert params["source_chunk_ids"] == []
    assert params["project_id"] is None


def test_relation_to_params() -> None:
    """_relation_to_params produces the expected Cypher param dict."""
    from movate.storage.neo4j import _relation_to_params  # noqa: PLC0415

    relation = _relation()
    params = _relation_to_params(relation)
    assert params["relation_id"] == "r1"
    assert params["src_entity_id"] == "e1"
    assert params["dst_entity_id"] == "e2"
    assert params["weight"] == 1.0
    assert params["type"] == "RELATES_TO"


# ---------------------------------------------------------------------------
# Unit: record → model converters
# ---------------------------------------------------------------------------


def test_record_to_entity() -> None:
    """_record_to_entity reconstructs an Entity from a Neo4j node dict."""
    from movate.storage.neo4j import _record_to_entity  # noqa: PLC0415

    node = {
        "entity_id": "e1",
        "tenant_id": "t1",
        "agent": "a1",
        "name": "Alpha",
        "type": "Feature",
        "description": "A feature",
        "embedding": json.dumps([0.1, 0.2]),
        "embedding_model": "test",
        "content_hash": "abc",
        "source_chunk_ids": ["c1", "c2"],
        "metadata": json.dumps({"key": "val"}),
        "project_id": "p1",
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    entity = _record_to_entity(node)
    assert entity.entity_id == "e1"
    assert entity.name == "Alpha"
    assert entity.embedding == [0.1, 0.2]
    assert entity.metadata == {"key": "val"}
    assert entity.project_id == "p1"


def test_record_to_relation() -> None:
    """_record_to_relation reconstructs a Relation from a Neo4j rel dict."""
    from movate.storage.neo4j import _record_to_relation  # noqa: PLC0415

    rel = {
        "relation_id": "r1",
        "tenant_id": "t1",
        "agent": "a1",
        "type": "RELATES_TO",
        "description": None,
        "weight": 0.5,
        "content_hash": "def",
        "source_chunk_ids": [],
        "metadata": None,
        "project_id": None,
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    relation = _record_to_relation(rel, "e1", "e2")
    assert relation.relation_id == "r1"
    assert relation.src_entity_id == "e1"
    assert relation.dst_entity_id == "e2"
    assert relation.weight == 0.5


# ---------------------------------------------------------------------------
# Unit: Cypher templates contain the expected patterns
# ---------------------------------------------------------------------------


def test_upsert_entity_cypher_contains_merge() -> None:
    """The entity upsert Cypher uses MERGE for dedup."""
    from movate.storage.neo4j import _UPSERT_ENTITY_CYPHER  # noqa: PLC0415

    assert "MERGE" in _UPSERT_ENTITY_CYPHER
    assert "ON CREATE SET" in _UPSERT_ENTITY_CYPHER
    assert "ON MATCH SET" in _UPSERT_ENTITY_CYPHER
    assert "$entity_id" in _UPSERT_ENTITY_CYPHER
    assert "content_hash" in _UPSERT_ENTITY_CYPHER


def test_upsert_relation_cypher_contains_match_merge() -> None:
    """The relation upsert Cypher MATCHes endpoints then MERGEs the edge."""
    from movate.storage.neo4j import _UPSERT_RELATION_CYPHER  # noqa: PLC0415

    assert "MATCH" in _UPSERT_RELATION_CYPHER
    assert "MERGE" in _UPSERT_RELATION_CYPHER
    assert "$src_entity_id" in _UPSERT_RELATION_CYPHER
    assert "$dst_entity_id" in _UPSERT_RELATION_CYPHER
    assert "RELATION" in _UPSERT_RELATION_CYPHER


def test_delete_graph_cypher_pattern() -> None:
    """The delete_graph Cypher uses DETACH DELETE scoped to agent + tenant."""
    from movate.storage.neo4j import _DELETE_GRAPH_ENTITIES_CYPHER  # noqa: PLC0415

    assert "DETACH DELETE" in _DELETE_GRAPH_ENTITIES_CYPHER
    assert "$agent" in _DELETE_GRAPH_ENTITIES_CYPHER
    assert "$tenant_id" in _DELETE_GRAPH_ENTITIES_CYPHER


async def test_expand_neighbors_empty_ids() -> None:
    """expand_neighbors with empty entity_ids returns empty Subgraph."""
    from movate.storage.neo4j import Neo4jStorageProvider  # noqa: PLC0415

    provider = Neo4jStorageProvider(uri="bolt://fake", user="u", password="p")
    provider._driver = AsyncMock()  # won't be called

    result = await provider.expand_neighbors(agent="a1", tenant_id="t1", entity_ids=[], hops=1)
    assert result.entities == []
    assert result.relations == []


# ---------------------------------------------------------------------------
# Unit: build_graph_storage
# ---------------------------------------------------------------------------


def test_build_graph_storage_returns_none_when_not_configured() -> None:
    """build_graph_storage returns None when MOVATE_GRAPH_BACKEND is unset."""
    from movate.storage import build_graph_storage  # noqa: PLC0415

    os.environ.pop("MOVATE_GRAPH_BACKEND", None)
    result = build_graph_storage()
    assert result is None


def test_build_graph_storage_returns_none_when_uri_missing() -> None:
    """build_graph_storage returns None when NEO4J_URI is missing."""
    from movate.storage import build_graph_storage  # noqa: PLC0415

    with patch.dict("os.environ", {"MOVATE_GRAPH_BACKEND": "neo4j"}, clear=False):
        os.environ.pop("NEO4J_URI", None)
        result = build_graph_storage()
    assert result is None


def test_build_graph_storage_creates_provider_when_configured() -> None:
    """build_graph_storage returns a Neo4jStorageProvider when configured."""
    from movate.storage import build_graph_storage  # noqa: PLC0415

    env = {
        "MOVATE_GRAPH_BACKEND": "neo4j",
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "secret",
    }
    with patch.dict("os.environ", env, clear=False):
        result = build_graph_storage()
    assert result is not None
    assert result.name == "neo4j"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Integration: round-trip (gated behind @pytest.mark.neo4j)
# ---------------------------------------------------------------------------

neo4j_mark = pytest.mark.neo4j


@neo4j_mark
async def test_neo4j_entity_roundtrip() -> None:
    """Upsert an entity, query it back, delete it -- round-trip on live Neo4j."""
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "password")

    from movate.storage.neo4j import Neo4jStorageProvider  # noqa: PLC0415

    provider = Neo4jStorageProvider(uri=uri, user=user, password=password)
    await provider.init()

    try:
        # Clean slate
        await provider.delete_graph(agent="test-agent", tenant_id="test-tenant")

        entity = Entity(
            entity_id="int-e1",
            tenant_id="test-tenant",
            agent="test-agent",
            name="Integration Test Entity",
            type="Test",
            embedding=[1.0, 0.0, 0.0],
            embedding_model="test",
            content_hash=_hash("test-agent", "test-tenant", "int-e1"),
            source_chunk_ids=["c1"],
        )
        await provider.upsert_entity(entity)

        # Query back
        got = await provider.get_entity("int-e1", tenant_id="test-tenant")
        assert got is not None
        assert got.name == "Integration Test Entity"
        assert got.source_chunk_ids == ["c1"]

        # List
        entities = await provider.list_entities(agent="test-agent", tenant_id="test-tenant")
        assert any(e.entity_id == "int-e1" for e in entities)

        # Upsert relation
        entity2 = Entity(
            entity_id="int-e2",
            tenant_id="test-tenant",
            agent="test-agent",
            name="Second Entity",
            type="Test",
            embedding=[0.0, 1.0, 0.0],
            embedding_model="test",
            content_hash=_hash("test-agent", "test-tenant", "int-e2"),
            source_chunk_ids=[],
        )
        await provider.upsert_entity(entity2)

        relation = Relation(
            relation_id="int-r1",
            tenant_id="test-tenant",
            agent="test-agent",
            src_entity_id="int-e1",
            dst_entity_id="int-e2",
            type="RELATES_TO",
            weight=0.8,
            content_hash=_hash("test-agent", "test-tenant", "int-e1", "int-e2"),
            source_chunk_ids=[],
        )
        await provider.upsert_relation(relation)

        # Expand neighbors
        sub = await provider.expand_neighbors(
            agent="test-agent",
            tenant_id="test-tenant",
            entity_ids=["int-e1"],
            hops=1,
        )
        assert len(sub.entities) >= 2
        assert len(sub.relations) >= 1

        # Delete
        deleted = await provider.delete_graph(agent="test-agent", tenant_id="test-tenant")
        assert deleted >= 2

        # Verify deletion
        got_after = await provider.get_entity("int-e1", tenant_id="test-tenant")
        assert got_after is None

    finally:
        await provider.close()
