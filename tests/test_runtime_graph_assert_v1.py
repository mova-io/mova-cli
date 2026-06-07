"""Runtime graph-assert write API — POST /projects/{project}/graph/assert (ADR 079 D2).

Pins the write contract: deterministic node/edge write + dedup-on-reassert,
assert provenance (source / confidence), skip-report for dangling edges,
cross-source reconciliation onto an extracted node's id, kb:write scope, and
tenant scoping. Hermetic over InMemoryStorage with embeddings stubbed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import Entity
from movate.kb import graph_assert
from movate.runtime import build_app
from movate.testing import InMemoryStorage

pytestmark = pytest.mark.unit

AGENT = "store-support"


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    async def fake_embed(texts, *, model="", api_key=None, **_):
        return [[0.0, 1.0, 0.0] for _ in texts]

    monkeypatch.setattr(graph_assert, "embed_texts", fake_embed)


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


async def _mint(storage: InMemoryStorage, *, scopes=None) -> tuple[str, str]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="assert-tests",
        scopes=list(scopes if scopes is not None else ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return tenant_id, f"Bearer {minted.full_key}"


def _incident_body() -> dict:
    return {
        "nodes": [
            {"type": "Incident", "name": "INC0042217", "description": "Frozen register"},
            {"type": "Store", "name": "118"},
            {"type": "Lane", "name": "5"},
        ],
        "edges": [
            {"src": "INC0042217", "dst": "118", "type": "AT_STORE"},
            {"src": "INC0042217", "dst": "5", "type": "ON_LANE"},
        ],
    }


def _post(client, auth, body):
    return client.post(
        f"/api/v1/projects/{AGENT}/graph/assert", json=body, headers={"Authorization": auth}
    )


async def test_assert_writes_nodes_and_edges(client, storage):
    tenant, auth = await _mint(storage)
    resp = _post(client, auth, _incident_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied_nodes"] == 3
    assert body["applied_edges"] == 2
    assert body["skipped_edges"] == []

    # persisted + carries assert provenance that survives the confidence floor
    ents = await storage.list_entities(agent=AGENT, tenant_id=tenant)
    inc = next(e for e in ents if e.name == "INC0042217")
    assert inc.metadata["source"] == "assert"
    assert inc.metadata["confidence"] == 1.0

    # appears in the read graph
    g = client.get(f"/api/v1/projects/{AGENT}/graph", headers={"Authorization": auth}).json()
    assert "INC0042217" in {n["attributes"]["label"] for n in g["nodes"]}


async def test_reassert_is_idempotent(client, storage):
    tenant, auth = await _mint(storage)
    for _ in range(2):
        assert _post(client, auth, _incident_body()).status_code == 200
    ents = await storage.list_entities(agent=AGENT, tenant_id=tenant)
    # two asserts, still exactly three nodes (content_hash dedup)
    assert len([e for e in ents if e.name in {"INC0042217", "118", "5"}]) == 3


async def test_dangling_edge_skipped_and_reported(client, storage):
    _tenant, auth = await _mint(storage)
    body = {
        "nodes": [{"type": "Incident", "name": "INC1"}],
        "edges": [{"src": "INC1", "dst": "ghost", "type": "AT_STORE"}],
    }
    resp = _post(client, auth, body)
    assert resp.status_code == 200
    out = resp.json()
    assert out["applied_nodes"] == 1
    assert out["applied_edges"] == 0
    assert out["skipped_edges"] == [
        {"src": "INC1", "dst": "ghost", "type": "AT_STORE", "reason": "unresolved endpoint"}
    ]


async def test_reconciles_onto_extracted_node_id(client, storage):
    """A Store extracted earlier under a random uuid: an asserted edge to it must
    link to that existing id (reconciliation), not a fresh content-hash id."""
    tenant, auth = await _mint(storage)
    store_hash = graph_assert._entity_hash(AGENT, tenant, "118", "Store")
    await storage.upsert_entity(
        Entity(
            entity_id="extracted-uuid",
            tenant_id=tenant,
            agent=AGENT,
            name="118",
            type="Store",
            embedding=[0.0, 1.0, 0.0],
            embedding_model="test",
            content_hash=store_hash,
        )
    )
    body = {
        "nodes": [{"type": "Incident", "name": "INC1"}, {"type": "Store", "name": "118"}],
        "edges": [{"src": "INC1", "dst": "118", "type": "AT_STORE"}],
    }
    assert _post(client, auth, body).status_code == 200

    rels = await storage.list_relations(agent=AGENT, tenant_id=tenant)
    edge = next(r for r in rels if r.type == "AT_STORE")
    assert edge.dst_entity_id == "extracted-uuid"  # reconciled, not the hash id
    ents = await storage.list_entities(agent=AGENT, tenant_id=tenant)
    stores = [e for e in ents if e.name == "118"]
    assert len(stores) == 1  # merged, not duplicated


async def test_requires_kb_write_scope(client, storage):
    _tenant, auth = await _mint(storage, scopes=["read"])
    resp = _post(client, auth, _incident_body())
    assert resp.status_code == 403


async def test_tenant_scoped(client, storage):
    _t1, auth1 = await _mint(storage)
    assert _post(client, auth1, _incident_body()).status_code == 200
    t2, _auth2 = await _mint(storage)
    # a different tenant's graph is untouched
    assert await storage.list_entities(agent=AGENT, tenant_id=t2) == []
