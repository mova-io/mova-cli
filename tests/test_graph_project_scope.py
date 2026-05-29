"""Project-scope (ADR 046 D1) conformance for the GraphRAG store + query API.

Nodes/edges carry an additive, nullable ``project_id`` so the graph viewer /
query API can window a subgraph at the PROJECT grain (a project's graph across
an agent's KBs), not only per agent. These tests assert, across all backends
(memory / sqlite / postgres — the last skips unless ``MOVATE_PG_TEST_URL``):

* the column round-trips through upsert → get/list,
* the optional ``project_id`` filter on list/expand/search narrows results,
* ``None`` (the default) keeps the historical per-agent scope — project-less
  rows AND a mixed graph both come back unfiltered (backward-compatible),
* re-ingesting under a project backfills the tag in place (COALESCE preserve),
* the pure ``core.graph`` query API honors the filter end-to-end.
"""

from __future__ import annotations

import hashlib

import pytest

from movate.core.graph import query as gq
from movate.core.models import Entity, Relation
from movate.kb.embed import embedding_dim
from movate.storage.base import StorageProvider

pytestmark = pytest.mark.unit


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _pad(vec: list[float]) -> list[float]:
    dim = embedding_dim()
    return vec if len(vec) >= dim else vec + [0.0] * (dim - len(vec))


def _entity(
    name: str,
    *,
    type: str = "T",
    project_id: str | None = None,
    embedding: list[float] | None = None,
    agent: str = "a1",
    tenant_id: str = "t1",
) -> Entity:
    return Entity(
        tenant_id=tenant_id,
        agent=agent,
        project_id=project_id,
        name=name,
        type=type,
        embedding=_pad(embedding or [1.0, 0.0, 0.0]),
        embedding_model="test-embed",
        content_hash=_hash(agent, tenant_id, name, type),
    )


def _relation(
    src: str,
    dst: str,
    *,
    type: str = "REL",
    weight: float = 1.0,
    project_id: str | None = None,
    agent: str = "a1",
    tenant_id: str = "t1",
) -> Relation:
    return Relation(
        tenant_id=tenant_id,
        agent=agent,
        project_id=project_id,
        src_entity_id=src,
        dst_entity_id=dst,
        type=type,
        weight=weight,
        content_hash=_hash(agent, tenant_id, src, dst, type),
    )


# ---------------------------------------------------------------------------
# Column round-trip
# ---------------------------------------------------------------------------


async def test_project_id_round_trips_on_entity(storage: StorageProvider) -> None:
    e = _entity("Alpha", project_id="p1")
    await storage.upsert_entity(e)
    got = await storage.get_entity(e.entity_id, tenant_id="t1")
    assert got is not None
    assert got.project_id == "p1"


async def test_project_id_round_trips_on_relation(storage: StorageProvider) -> None:
    a = _entity("Alpha", project_id="p1")
    b = _entity("Beta", project_id="p1")
    await storage.upsert_entity(a)
    await storage.upsert_entity(b)
    await storage.upsert_relation(_relation(a.entity_id, b.entity_id, project_id="p1"))
    rels = await storage.list_relations(agent="a1", tenant_id="t1")
    assert len(rels) == 1
    assert rels[0].project_id == "p1"


async def test_project_id_defaults_null_when_unset(storage: StorageProvider) -> None:
    """A project-less ingest (the dominant path) carries NULL — backward-compat."""
    await storage.upsert_entity(_entity("Alpha"))
    rows = await storage.list_entities(agent="a1", tenant_id="t1")
    assert len(rows) == 1
    assert rows[0].project_id is None


# ---------------------------------------------------------------------------
# Optional filter on list / search / expand
# ---------------------------------------------------------------------------


async def test_list_entities_filters_by_project(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("InP1", project_id="p1"))
    await storage.upsert_entity(_entity("InP2", project_id="p2"))
    await storage.upsert_entity(_entity("NoProj"))

    only_p1 = await storage.list_entities(agent="a1", tenant_id="t1", project_id="p1")
    assert {e.name for e in only_p1} == {"InP1"}

    # None = no filter → every node (the historical per-agent view).
    everything = await storage.list_entities(agent="a1", tenant_id="t1")
    assert {e.name for e in everything} == {"InP1", "InP2", "NoProj"}


async def test_list_relations_filters_by_project(storage: StorageProvider) -> None:
    a = _entity("A", project_id="p1")
    b = _entity("B", project_id="p1")
    c = _entity("C", project_id="p2")
    for e in (a, b, c):
        await storage.upsert_entity(e)
    await storage.upsert_relation(_relation(a.entity_id, b.entity_id, type="R1", project_id="p1"))
    await storage.upsert_relation(_relation(a.entity_id, c.entity_id, type="R2", project_id="p2"))

    p1 = await storage.list_relations(agent="a1", tenant_id="t1", project_id="p1")
    assert {r.type for r in p1} == {"R1"}
    assert len(await storage.list_relations(agent="a1", tenant_id="t1")) == 2


async def test_search_entities_filters_by_project(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("InP1", project_id="p1", embedding=[1.0, 0.0, 0.0]))
    await storage.upsert_entity(_entity("InP2", project_id="p2", embedding=[1.0, 0.0, 0.0]))

    hits = await storage.search_entities(
        agent="a1", tenant_id="t1", query_embedding=_pad([1.0, 0.0, 0.0]), project_id="p1"
    )
    assert {h.entity.name for h in hits} == {"InP1"}


async def test_expand_neighbors_stays_inside_project(storage: StorageProvider) -> None:
    # p1: A—B ; a cross-project edge A—C (tagged p2) must NOT be followed
    # when the expansion is scoped to p1.
    a = _entity("A", project_id="p1")
    b = _entity("B", project_id="p1")
    c = _entity("C", project_id="p2")
    for e in (a, b, c):
        await storage.upsert_entity(e)
    await storage.upsert_relation(
        _relation(a.entity_id, b.entity_id, type="IN_P1", project_id="p1")
    )
    await storage.upsert_relation(
        _relation(a.entity_id, c.entity_id, type="IN_P2", project_id="p2")
    )

    sub = await storage.expand_neighbors(
        agent="a1", tenant_id="t1", entity_ids=[a.entity_id], hops=2, project_id="p1"
    )
    assert {e.name for e in sub.entities} == {"A", "B"}
    assert {r.type for r in sub.relations} == {"IN_P1"}

    # Unfiltered: both edges are reachable.
    sub_all = await storage.expand_neighbors(
        agent="a1", tenant_id="t1", entity_ids=[a.entity_id], hops=2
    )
    assert {e.name for e in sub_all.entities} == {"A", "B", "C"}


# ---------------------------------------------------------------------------
# Backfill: re-ingest under a project tags rows in place (COALESCE-preserve)
# ---------------------------------------------------------------------------


async def test_reingest_backfills_project_id(storage: StorageProvider) -> None:
    # First ingest: no project.
    await storage.upsert_entity(_entity("Alpha"))
    rows = await storage.list_entities(agent="a1", tenant_id="t1")
    assert rows[0].project_id is None
    # Re-ingest the SAME entity (same content_hash) now under a project.
    await storage.upsert_entity(_entity("Alpha", project_id="p1"))
    rows = await storage.list_entities(agent="a1", tenant_id="t1")
    assert len(rows) == 1, "same content_hash collapses to one node"
    assert rows[0].project_id == "p1", "project_id backfilled in place"


async def test_reingest_without_project_preserves_existing_tag(
    storage: StorageProvider,
) -> None:
    # Tagged once, then re-ingested project-less → the tag survives (COALESCE).
    await storage.upsert_entity(_entity("Alpha", project_id="p1"))
    await storage.upsert_entity(_entity("Alpha"))  # project-less re-ingest
    rows = await storage.list_entities(agent="a1", tenant_id="t1")
    assert len(rows) == 1
    assert rows[0].project_id == "p1"


# ---------------------------------------------------------------------------
# Query API (core.graph) honors the filter end-to-end
# ---------------------------------------------------------------------------


async def test_windowed_subgraph_scopes_to_project(storage: StorageProvider) -> None:
    a = _entity("A", project_id="p1")
    b = _entity("B", project_id="p1")
    c = _entity("C", project_id="p2")
    for e in (a, b, c):
        await storage.upsert_entity(e)
    await storage.upsert_relation(_relation(a.entity_id, b.entity_id, project_id="p1"))

    doc = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1", project_id="p1")
    assert {n.attributes["label"] for n in doc.nodes} == {"A", "B"}

    # No filter → all three nodes (the default per-agent view, unchanged).
    doc_all = await gq.windowed_subgraph(storage, agent="a1", tenant_id="t1")
    assert {n.attributes["label"] for n in doc_all.nodes} == {"A", "B", "C"}


async def test_windowed_subgraph_rooted_rejects_cross_project_root(
    storage: StorageProvider,
) -> None:
    c = _entity("C", project_id="p2")
    await storage.upsert_entity(c)
    # Root belongs to p2 but the window asks for p1 → empty doc (no leak).
    doc = await gq.windowed_subgraph(
        storage, agent="a1", tenant_id="t1", root=c.entity_id, project_id="p1"
    )
    assert doc.nodes == []


async def test_search_nodes_scopes_to_project(storage: StorageProvider) -> None:
    await storage.upsert_entity(_entity("Widget", project_id="p1"))
    await storage.upsert_entity(_entity("Widget", type="Other", project_id="p2"))

    hits = await gq.search_nodes(storage, agent="a1", tenant_id="t1", q="widget", project_id="p1")
    assert {h.type for h in hits} == {"T"}
    assert len(await gq.search_nodes(storage, agent="a1", tenant_id="t1", q="widget")) == 2
