"""Tests for ``DELETE /api/v1/threads/{id}`` (PR-T).

Completes the CRUD surface on /api/v1/threads. Hard-delete with
tenant-scoped 404 semantics on cross-tenant access (matching every
other thread endpoint).

Coverage:
* Happy path — delete returns 204, subsequent GET returns 404
* 404 on missing thread id
* 404 cross-tenant (NEVER 403 — same isolation contract as GET)
* 401 without auth
* Runs that referenced the deleted thread stay in storage (the
  intent of DELETE is "stop showing this conversation", not
  "nuke the historical runs")
* Storage method returns True/False correctly
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import JobStatus, Metrics, RunRecord
from movate.runtime import build_app
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="thread-delete-tests")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def other_tenant_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_delete_thread_returns_204_and_get_then_404s(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]

    r = client.delete(f"/api/v1/threads/{thread_id}", headers=auth_header)
    assert r.status_code == 204, r.text
    # Body empty per the 204 contract.
    assert r.text == ""

    r = client.get(f"/api/v1/threads/{thread_id}", headers=auth_header)
    assert r.status_code == 404


@pytest.mark.integration
def test_delete_thread_removes_from_list(client: TestClient, auth_header: dict[str, str]) -> None:
    """Deleted threads disappear from GET /threads — operator's
    'recent conversations' list cleans up immediately."""
    r1 = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    r2 = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    t1, t2 = r1.json()["thread_id"], r2.json()["thread_id"]
    client.delete(f"/api/v1/threads/{t1}", headers=auth_header)
    rows = client.get("/api/v1/threads", headers=auth_header).json()["threads"]
    ids = {t["thread_id"] for t in rows}
    assert t1 not in ids
    assert t2 in ids


# ---------------------------------------------------------------------------
# Runs survive thread deletion
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_runs_survive_thread_deletion(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Runs that referenced the deleted thread stay in storage —
    operator's intent is 'stop showing this conversation', not
    'nuke the historical runs themselves'."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    tenant_id = storage.conversation_threads[0].tenant_id

    # Seed a run linked to the thread.
    run = RunRecord(
        run_id="r1",
        job_id="j1",
        tenant_id=tenant_id,
        agent="rag-qa",
        agent_version="0.1.0",
        prompt_hash="h",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2024-01-01",
        status=JobStatus.SUCCESS,
        input={"q": "x"},
        output={"a": "y"},
        metrics=Metrics(latency_ms=50, cost_usd=0.001, tokens_in=5, tokens_out=5),
        created_at=datetime.now(UTC),
        thread_id=thread_id,
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(storage.save_run(run))
    finally:
        loop.close()

    # Delete the thread.
    client.delete(f"/api/v1/threads/{thread_id}", headers=auth_header)

    # Run record still exists.
    r = client.get(f"/runs/{run.run_id}", headers=auth_header)
    assert r.status_code == 200
    # And the run's thread_id is preserved (dangling reference is
    # explicitly fine — we don't try to null it out).
    assert r.json()["thread_id"] == thread_id


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_delete_thread_404_on_missing(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.delete("/api/v1/threads/nonexistent", headers=auth_header)
    assert r.status_code == 404


@pytest.mark.integration
def test_delete_thread_404_cross_tenant(
    client: TestClient,
    auth_header: dict[str, str],
    other_tenant_header: dict[str, str],
) -> None:
    """Tenant A's thread, tenant B's auth → 404 (NEVER 403).
    Matches every other thread endpoint's isolation contract."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    # Tenant B tries to delete tenant A's thread.
    r = client.delete(f"/api/v1/threads/{thread_id}", headers=other_tenant_header)
    assert r.status_code == 404
    # And the thread is still there for tenant A.
    r = client.get(f"/api/v1/threads/{thread_id}", headers=auth_header)
    assert r.status_code == 200


@pytest.mark.integration
def test_delete_thread_401_without_auth(client: TestClient) -> None:
    r = client.delete("/api/v1/threads/abc")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Storage-layer return value
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_storage_returns_true_when_row_existed(
    storage: InMemoryStorage,
) -> None:
    from movate.core.models import ConversationThread  # noqa: PLC0415

    t = ConversationThread(thread_id="t_x", tenant_id="ta", agent="rag-qa")
    await storage.save_conversation_thread(t)
    assert await storage.delete_conversation_thread("t_x", tenant_id="ta") is True
    assert await storage.delete_conversation_thread("t_x", tenant_id="ta") is False


@pytest.mark.unit
async def test_delete_storage_returns_false_for_cross_tenant(
    storage: InMemoryStorage,
) -> None:
    """Cross-tenant delete returns False without touching the row.
    The endpoint converts this to a 404."""
    from movate.core.models import ConversationThread  # noqa: PLC0415

    t = ConversationThread(thread_id="t_x", tenant_id="ta", agent="rag-qa")
    await storage.save_conversation_thread(t)
    assert await storage.delete_conversation_thread("t_x", tenant_id="tb") is False
    # Row still in storage for the right tenant.
    got = await storage.get_conversation_thread("t_x", tenant_id="ta")
    assert got is not None
