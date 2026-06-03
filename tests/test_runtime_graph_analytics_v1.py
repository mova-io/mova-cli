"""Runtime graph-analytics API (ADR 046): centrality / shortest-path /
communities endpoints.

Pins the HTTP contract: response shapes, top-N ordering, the ``?from=``/``?to=``
shortest-path params, community partition, read scope, tenant scoping (no
cross-tenant nodes), node/edge caps, and the ``?project=`` (ADR 046 D1) data
filter.

Hermetic: in-process app over an ``InMemoryStorage`` double seeded the way
``mdk kb ingest --build-graph`` would.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import Entity, Relation
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


def _relation(
    rid: str, src: str, dst: str, *, tenant: str, weight=1.0, type: str = "REL", **kw
) -> Relation:
    return Relation(
        relation_id=rid,
        tenant_id=tenant,
        agent=AGENT,
        src_entity_id=src,
        dst_entity_id=dst,
        type=type,
        weight=weight,
        content_hash=f"h-{tenant}-{rid}",
        **kw,
    )


async def _mint(storage: InMemoryStorage, *, scopes=None) -> tuple[str, str]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="analytics-v1-tests",
        scopes=list(scopes if scopes is not None else ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return tenant_id, f"Bearer {minted.full_key}"


async def _seed_star(storage: InMemoryStorage, tenant: str) -> None:
    """A star: hub connected to a, b, c (hub is the most central node)."""
    await storage.upsert_entity(_entity("hub", "Hub", "Core", tenant=tenant))
    for leaf in ("a", "b", "c"):
        await storage.upsert_entity(_entity(leaf, leaf.upper(), "Leaf", tenant=tenant))
        await storage.upsert_relation(_relation(f"e_{leaf}", "hub", leaf, tenant=tenant))


# ----------------------------------------------------------------------
# Centrality
# ----------------------------------------------------------------------


async def test_centrality_degree_top_node(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_star(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["measure"] == "degree"
    assert set(body.keys()) == {"measure", "scores", "count"}
    # Highest first → hub leads, score 1.0 (connected to every other node).
    assert body["scores"][0]["key"] == "hub"
    assert body["scores"][0]["score"] == pytest.approx(1.0)
    assert body["scores"][0]["label"] == "Hub"
    assert body["scores"][0]["type"] == "Core"
    assert body["count"] == 4


async def test_centrality_betweenness(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_star(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        params={"measure": "betweenness"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["measure"] == "betweenness"
    # Star hub is on every leaf-to-leaf shortest path → max betweenness, leads.
    assert body["scores"][0]["key"] == "hub"


async def test_centrality_top_n(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_star(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        params={"top_n": 2},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert len(resp.json()["scores"]) == 2


async def test_centrality_unknown_measure_defaults_to_degree(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant, auth = await _mint(storage)
    await _seed_star(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        params={"measure": "pagerank"},  # not supported → degrade to degree
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["measure"] == "degree"


async def test_centrality_empty_graph(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, auth = await _mint(storage)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json() == {"measure": "degree", "scores": [], "count": 0}


# ----------------------------------------------------------------------
# Shortest path
# ----------------------------------------------------------------------


async def _seed_path(storage: InMemoryStorage, tenant: str) -> None:
    """A simple path a-b-c-d."""
    for nid in ("a", "b", "c", "d"):
        await storage.upsert_entity(_entity(nid, nid.upper(), "T", tenant=tenant))
    await storage.upsert_relation(_relation("e1", "a", "b", tenant=tenant))
    await storage.upsert_relation(_relation("e2", "b", "c", tenant=tenant))
    await storage.upsert_relation(_relation("e3", "c", "d", tenant=tenant))


async def test_shortest_path_found(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_path(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/path",
        params={"from": "a", "to": "d"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["nodes"] == ["a", "b", "c", "d"]
    assert body["hops"] == 3


async def test_shortest_path_not_found(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_path(storage, tenant)
    # An isolated node — no path to it.
    await storage.upsert_entity(_entity("island", "Island", "T", tenant=tenant))
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/path",
        params={"from": "a", "to": "island"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["nodes"] == []
    assert body["hops"] == 0


async def test_shortest_path_unknown_endpoint(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_path(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/path",
        params={"from": "a", "to": "nope"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["found"] is False


async def test_shortest_path_requires_from_and_to(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant, auth = await _mint(storage)
    await _seed_path(storage, tenant)
    # Missing ?to= → 422 (required query param).
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/path",
        params={"from": "a"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 422


# ----------------------------------------------------------------------
# Communities
# ----------------------------------------------------------------------


async def test_communities_two_islands(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    # Two disjoint pairs → two communities.
    for nid in ("a", "b", "x", "y"):
        await storage.upsert_entity(_entity(nid, nid.upper(), "T", tenant=tenant))
    await storage.upsert_relation(_relation("e1", "a", "b", tenant=tenant))
    await storage.upsert_relation(_relation("e2", "x", "y", tenant=tenant))

    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/communities",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"communities", "count"}
    assert body["count"] == 2
    member_sets = [set(c["members"]) for c in body["communities"]]
    assert {"a", "b"} in member_sets
    assert {"x", "y"} in member_sets
    # Stable small ids, largest-first.
    assert [c["community_id"] for c in body["communities"]] == [0, 1]
    assert all("size" in c for c in body["communities"])


async def test_communities_empty_graph(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, auth = await _mint(storage)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/communities",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json() == {"communities": [], "count": 0}


# ----------------------------------------------------------------------
# Auth: read scope + tenant scoping
# ----------------------------------------------------------------------


async def test_analytics_endpoints_require_auth(
    client: TestClient, storage: InMemoryStorage
) -> None:
    for path in [
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        f"/api/v1/projects/{AGENT}/graph/analytics/path?from=a&to=b",
        f"/api/v1/projects/{AGENT}/graph/analytics/communities",
    ]:
        resp = client.get(path)
        assert resp.status_code == 401, path


async def test_analytics_endpoints_require_read_scope(
    client: TestClient, storage: InMemoryStorage
) -> None:
    _tenant, auth = await _mint(storage, scopes={"run"})  # no "read"
    for path in [
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        f"/api/v1/projects/{AGENT}/graph/analytics/path?from=a&to=b",
        f"/api/v1/projects/{AGENT}/graph/analytics/communities",
    ]:
        resp = client.get(path, headers={"Authorization": auth})
        assert resp.status_code == 403, path


async def test_analytics_tenant_scoping(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_a, auth_a = await _mint(storage)
    await _seed_star(storage, tenant_a)
    # A second tenant's node must never appear in tenant A's centrality.
    await storage.upsert_entity(_entity("secret", "Secret", "T", tenant="other-tenant"))
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        headers={"Authorization": auth_a},
    )
    keys = {s["key"] for s in resp.json()["scores"]}
    assert "secret" not in keys
    assert keys == {"hub", "a", "b", "c"}


# ----------------------------------------------------------------------
# Project scoping (ADR 046 D1) — the ?project= data filter
# ----------------------------------------------------------------------


async def _seed_two_projects(storage: InMemoryStorage, tenant: str) -> None:
    """p1: a-b-c chain; p2: x-y pair; both under the same agent."""
    for nid in ("a", "b", "c"):
        await storage.upsert_entity(_entity(nid, nid.upper(), "T", tenant=tenant, project_id="p1"))
    await storage.upsert_relation(_relation("e1", "a", "b", tenant=tenant, project_id="p1"))
    await storage.upsert_relation(_relation("e2", "b", "c", tenant=tenant, project_id="p1"))
    for nid in ("x", "y"):
        await storage.upsert_entity(_entity(nid, nid.upper(), "T", tenant=tenant, project_id="p2"))
    await storage.upsert_relation(_relation("e3", "x", "y", tenant=tenant, project_id="p2"))


async def test_centrality_project_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_two_projects(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/centrality",
        params={"project": "p1"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    keys = {s["key"] for s in resp.json()["scores"]}
    assert keys == {"a", "b", "c"}  # only p1's nodes


async def test_communities_project_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    tenant, auth = await _mint(storage)
    await _seed_two_projects(storage, tenant)
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/communities",
        params={"project": "p1"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    # p1 is a single connected chain → one community of {a,b,c}; p2 excluded.
    assert body["count"] == 1
    assert set(body["communities"][0]["members"]) == {"a", "b", "c"}


async def test_shortest_path_project_scoped_no_cross_project(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant, auth = await _mint(storage)
    await _seed_two_projects(storage, tenant)
    # Scoped to p1, p2's node x isn't in the window → not found (no leak).
    resp = client.get(
        f"/api/v1/projects/{AGENT}/graph/analytics/path",
        params={"from": "a", "to": "x", "project": "p1"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["found"] is False
