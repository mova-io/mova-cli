"""Runtime knowledge-graph query API (ADR 046).

Pins the HTTP contract the sigma.js front end depends on:

* graphology JSON shape (zero-transform import) on subgraph + neighbors,
* node-detail provenance + ``_links.expand``,
* SSE growth event shape (``node.added`` / ``edge.added`` / ``done``),
* read scope on every endpoint,
* tenant scoping (no cross-tenant nodes/edges),
* node/edge caps enforced.

Hermetic: in-process app over an ``InMemoryStorage`` double, seeded with
entities/relations the same way ``mdk kb ingest --build-graph`` would.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import Entity, KbChunk, Relation
from movate.runtime import build_app
from movate.testing import InMemoryStorage

pytestmark = pytest.mark.unit

AGENT = "faq-bot"


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


def _entity(eid: str, name: str, type: str, *, tenant: str, **kw) -> Entity:
    return Entity(
        entity_id=eid,
        tenant_id=tenant,
        agent=AGENT,
        name=name,
        type=type,
        embedding=[0.0],
        embedding_model="test-embed",
        content_hash=f"h-{tenant}-{eid}",
        **kw,
    )


def _relation(rid: str, src: str, dst: str, *, tenant: str, weight=1.0, **kw) -> Relation:
    return Relation(
        relation_id=rid,
        tenant_id=tenant,
        agent=AGENT,
        src_entity_id=src,
        dst_entity_id=dst,
        type="PART_OF",
        weight=weight,
        content_hash=f"h-{tenant}-{rid}",
        **kw,
    )


async def _mint(storage: InMemoryStorage, *, scopes=None) -> tuple[str, str]:
    """Return ``(tenant_id, bearer_header_value)`` for a fresh key."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="graph-v1-tests",
        scopes=list(scopes if scopes is not None else ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return tenant_id, f"Bearer {minted.full_key}"


async def _seed_basic(storage: InMemoryStorage, tenant: str) -> None:
    await storage.upsert_entity(_entity("n1", "SAML SSO", "Feature", tenant=tenant))
    await storage.upsert_entity(_entity("n2", "Auth System", "System", tenant=tenant))
    await storage.upsert_relation(_relation("e1", "n1", "n2", tenant=tenant, weight=0.8))


# ----------------------------------------------------------------------
# Windowed subgraph — graphology JSON shape pinned
# ----------------------------------------------------------------------


async def test_subgraph_graphology_shape(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)

    resp = client.get(f"/api/v1/projects/{AGENT}/graph", headers={"Authorization": auth})
    assert resp.status_code == 200
    body = resp.json()

    # Exact top-level contract — zero-transform graphology import.
    assert set(body.keys()) == {"attributes", "nodes", "edges"}
    assert body["attributes"] == {}

    node = next(n for n in body["nodes"] if n["key"] == "n1")
    assert set(node.keys()) == {"key", "attributes"}
    attrs = node["attributes"]
    assert attrs["label"] == "SAML SSO"
    assert attrs["type"] == "Feature"
    assert "size" in attrs and "color" in attrs

    edge = body["edges"][0]
    assert set(edge.keys()) == {"key", "source", "target", "attributes"}
    assert edge["source"] == "n1"
    assert edge["target"] == "n2"
    assert edge["attributes"]["label"] == "PART_OF"
    assert edge["attributes"]["weight"] == 0.8


async def test_subgraph_rooted(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)
    await storage.upsert_entity(_entity("far", "Unrelated", "Other", tenant=tenant))

    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph",
        params={"root": "n1", "depth": 1},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    keys = {n["key"] for n in resp.json()["nodes"]}
    assert keys == {"n1", "n2"}


async def test_subgraph_node_cap_enforced(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    for i in range(30):
        await storage.upsert_entity(_entity(f"n{i}", f"name{i}", "T", tenant=tenant))

    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph",
        params={"limit": 5},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert len(resp.json()["nodes"]) == 5


async def test_subgraph_limit_clamped_to_max(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)
    # Asking for a huge limit must not error; server clamps to MAX_CAP.
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph",
        params={"limit": 10_000_000},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert len(resp.json()["nodes"]) == 2  # only what exists


async def test_subgraph_topology_mode_empty(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph",
        params={"mode": "topology"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["nodes"] == []


# ----------------------------------------------------------------------
# Node detail + provenance
# ----------------------------------------------------------------------


async def test_node_detail_provenance(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await storage.save_kb_chunk(
        KbChunk(
            chunk_id="c1",
            tenant_id=tenant,
            agent=AGENT,
            source="https://docs.example.com/auth",
            text="SAML SSO requires an external identity provider.",
            embedding=[0.0],
            embedding_model="test-embed",
            content_hash="ch1",
        )
    )
    await storage.upsert_entity(
        _entity(
            "n1",
            "SAML SSO",
            "Feature",
            tenant=tenant,
            source_chunk_ids=["c1"],
            description="Single sign-on via SAML.",
        )
    )
    await storage.upsert_entity(_entity("n2", "IdP", "System", tenant=tenant))
    await storage.upsert_relation(
        _relation("e1", "n1", "n2", tenant=tenant, weight=0.75, source_chunk_ids=["c1"])
    )

    resp = client.get("/api/v1/graph/nodes/n1", headers={"Authorization": auth})
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "n1"
    assert body["label"] == "SAML SSO"
    assert body["type"] == "Feature"
    assert body["description"] == "Single sign-on via SAML."
    assert body["neighbor_count"] == 1
    assert AGENT in body["referenced_by_agents"]
    # _links.expand points at the neighbors endpoint (HATEOAS).
    assert body["_links"]["expand"] == "/api/v1/graph/nodes/n1/neighbors"
    # Provenance: source chunk + url + confidence.
    prov = body["provenance"]
    assert len(prov) == 1
    assert prov[0]["chunk_id"] == "c1"
    assert prov[0]["url"] == "https://docs.example.com/auth"
    assert "SAML SSO" in prov[0]["snippet"]
    assert prov[0]["extraction_confidence"] == 0.75


async def test_node_detail_unknown_404(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, auth = await _mint(storage)
    resp = client.get("/api/v1/graph/nodes/nope", headers={"Authorization": auth})
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# Neighbors (expand-on-demand) — graphology JSON
# ----------------------------------------------------------------------


async def test_neighbors_graphology(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)
    resp = client.get(
        "/api/v1/graph/nodes/n1/neighbors",
        params={"depth": 1},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"attributes", "nodes", "edges"}
    assert {n["key"] for n in body["nodes"]} == {"n1", "n2"}


async def test_neighbors_unknown_node_empty(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, auth = await _mint(storage)
    resp = client.get("/api/v1/graph/nodes/nope/neighbors", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.json()["nodes"] == []


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


async def test_search(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)
    resp = client.get(
        "/api/v1/graph/search",
        params={"q": "saml", "project": AGENT},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "saml"
    assert body["count"] == 1
    assert body["results"][0]["key"] == "n1"


# ----------------------------------------------------------------------
# POST /graph/query — bounded traversal
# ----------------------------------------------------------------------


async def test_graph_query_traverse(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)
    resp = client.post(
        "/api/v1/graph/query",
        json={"project": AGENT, "root": "n1", "depth": 1},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    keys = {n["key"] for n in resp.json()["nodes"]}
    assert keys == {"n1", "n2"}


async def test_graph_query_depth_clamped(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)
    # depth far above MAX_DEPTH must not error (server clamps).
    resp = client.post(
        "/api/v1/graph/query",
        json={"project": AGENT, "root": "n1", "depth": 999},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200


# ----------------------------------------------------------------------
# SSE growth stream
# ----------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE byte stream into ``[(event, data_dict), ...]``."""
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        events.append((event, json.loads(data) if data else {}))
    return events


async def test_sse_growth_stream_shape(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_basic(storage, tenant)

    resp = client.get(f"/api/v1/projects/{AGENT}/graph/stream", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert "node.added" in kinds
    assert "edge.added" in kinds
    assert kinds[-1] == "done"

    # Each node.added payload is itself a graphology-importable doc with one node.
    node_event = next(d for e, d in events if e == "node.added")
    assert set(node_event.keys()) == {"attributes", "nodes", "edges"}
    assert len(node_event["nodes"]) == 1
    assert node_event["edges"] == []

    edge_event = next(d for e, d in events if e == "edge.added")
    assert len(edge_event["edges"]) == 1
    assert edge_event["nodes"] == []
    assert {"key", "source", "target", "attributes"} == set(edge_event["edges"][0].keys())

    done = next(d for e, d in events if e == "done")
    assert done == {"nodes": 2, "edges": 1}


# ----------------------------------------------------------------------
# Auth: read scope + tenant scoping
# ----------------------------------------------------------------------


async def test_endpoints_require_auth(client: TestClient, storage: InMemoryStorage) -> None:
    for method, path in [
        ("GET", f"/api/v1/projects/{AGENT}/graph"),
        ("GET", "/api/v1/graph/nodes/n1"),
        ("GET", "/api/v1/graph/nodes/n1/neighbors"),
        ("GET", "/api/v1/graph/search"),
        ("GET", f"/api/v1/projects/{AGENT}/graph/stream"),
    ]:
        resp = client.request(method, path)
        assert resp.status_code == 401, f"{method} {path}"
    resp = client.post("/api/v1/graph/query", json={"project": AGENT, "root": "n1"})
    assert resp.status_code == 401


async def test_endpoints_require_read_scope(client: TestClient, storage: InMemoryStorage) -> None:
    # A key with only "run" (not "read") is forbidden on the graph reads.
    _tenant, auth = await _mint(storage, scopes={"run"})
    resp = client.get(f"/api/v1/projects/{AGENT}/graph", headers={"Authorization": auth})
    assert resp.status_code == 403
    resp = client.post(
        "/api/v1/graph/query",
        json={"project": AGENT, "root": "n1"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 403


async def test_tenant_scoping_no_cross_tenant_nodes(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant_a, auth_a = await _mint(storage)
    await _seed_basic(storage, tenant_a)
    # Seed a second tenant's graph; tenant A must never see it.
    await storage.upsert_entity(_entity("x1", "Secret", "Other", tenant="other-tenant"))

    resp = client.get(f"/api/v1/projects/{AGENT}/graph", headers={"Authorization": auth_a})
    keys = {n["key"] for n in resp.json()["nodes"]}
    assert keys == {"n1", "n2"}
    assert "x1" not in keys


async def test_cross_tenant_node_detail_404(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant_a, auth_a = await _mint(storage)
    await storage.upsert_entity(_entity("x1", "Secret", "Other", tenant="other-tenant"))
    resp = client.get("/api/v1/graph/nodes/x1", headers={"Authorization": auth_a})
    assert resp.status_code == 404
