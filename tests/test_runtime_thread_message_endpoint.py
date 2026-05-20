"""Tests for ``POST /api/v1/threads/{id}/messages`` (PR-Q).

The endpoint queues a JobRecord with ``thread_id`` set, refreshes
the thread's ``updated_at`` so it floats to the top of the list,
and returns the standard ``RunAccepted`` envelope (job_id + status).

The worker's thread_id propagation onto the spawned RunRecord
(``dispatch.py``) is exercised by the integration test that
seeds a fake worker outcome — we verify the JobRecord carries
thread_id end-to-end through the storage layer.

Coverage:
* Create thread → POST messages → returns 202 with job_id
* JobRecord persists thread_id on the saved job
* Thread's updated_at gets refreshed on each submission
* 404 on missing / cross-tenant thread (never leaks existence)
* 401 without auth; 422 on bad body
* RunRecord with thread_id appears in GET /threads/{id} run history
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import JobStatus, Metrics, RunRecord, TokenUsage
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="thread-message-tests")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def other_tenant_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _create_thread(client: TestClient, auth_header: dict[str, str]) -> str:
    """Convenience: create a thread + return its id."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_message_returns_202_with_job_id(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Messages endpoint queues a job and returns the standard
    RunAccepted envelope (same shape as POST /run)."""
    thread_id = _create_thread(client, auth_header)
    r = client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"question": "and what about prorated refunds?"}},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job_id"]  # non-empty
    assert body["status"] == JobStatus.QUEUED.value


@pytest.mark.integration
def test_post_message_persists_thread_id_on_job(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """The queued JobRecord carries thread_id so the worker can
    propagate it onto the spawned RunRecord."""
    thread_id = _create_thread(client, auth_header)
    r = client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "x"}},
        headers=auth_header,
    )
    job_id = r.json()["job_id"]
    # Look up the job in storage and check it has thread_id set.
    loop = asyncio.new_event_loop()
    try:
        job = loop.run_until_complete(storage.get_job(job_id, tenant_id=storage.jobs[0].tenant_id))
    finally:
        loop.close()
    assert job is not None
    assert job.thread_id == thread_id
    # Also: kind=agent, target=thread's agent.
    assert job.target == "rag-qa"


@pytest.mark.integration
def test_post_message_refreshes_thread_updated_at(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Each new message bumps the thread's updated_at so it floats
    to the top of the operator's recent-activity list."""
    thread_id = _create_thread(client, auth_header)
    # Read original updated_at via direct storage.
    loop = asyncio.new_event_loop()
    try:
        original_thread = loop.run_until_complete(
            storage.get_conversation_thread(
                thread_id, tenant_id=storage.conversation_threads[0].tenant_id
            )
        )
    finally:
        loop.close()
    assert original_thread is not None
    original_updated = original_thread.updated_at

    # Sleep a moment so the wall-clock difference is detectable.
    import time  # noqa: PLC0415

    time.sleep(0.01)

    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "x"}},
        headers=auth_header,
    )

    loop = asyncio.new_event_loop()
    try:
        refreshed = loop.run_until_complete(
            storage.get_conversation_thread(
                thread_id, tenant_id=storage.conversation_threads[0].tenant_id
            )
        )
    finally:
        loop.close()
    assert refreshed is not None
    assert refreshed.updated_at > original_updated
    # created_at + title preserved.
    assert refreshed.created_at == original_thread.created_at
    assert refreshed.title == original_thread.title


# ---------------------------------------------------------------------------
# RunRecord with thread_id appears in GET /threads/{id} history
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_threaded_run_appears_in_get_thread_history(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """End-to-end: submit a message → worker (simulated) creates a
    RunRecord with thread_id → GET /threads/{id} shows it in runs."""
    thread_id = _create_thread(client, auth_header)
    tenant_id = storage.conversation_threads[0].tenant_id

    # Simulate the worker spawning a successful run with the thread
    # linkage (the actual worker propagation is tested at a lower
    # tier — this asserts the read endpoint stitches it back together).
    run = RunRecord(
        run_id="r_threaded",
        job_id="j_x",
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
        metrics=Metrics(latency_ms=50, cost_usd=0.001, tokens=TokenUsage(input=5, output=5)),
        created_at=datetime.now(UTC),
        thread_id=thread_id,
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(storage.save_run(run))
    finally:
        loop.close()

    r = client.get(f"/api/v1/threads/{thread_id}", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["runs"] is not None
    assert len(body["runs"]) == 1
    assert body["runs"][0]["run_id"] == "r_threaded"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_message_404_on_missing_thread(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/threads/nonexistent/messages",
        json={"input": {"q": "x"}},
        headers=auth_header,
    )
    assert r.status_code == 404


@pytest.mark.integration
def test_post_message_404_cross_tenant(
    client: TestClient,
    auth_header: dict[str, str],
    other_tenant_header: dict[str, str],
) -> None:
    """Tenant A's thread, tenant B's API key → 404 (never leak existence)."""
    thread_id = _create_thread(client, auth_header)
    r = client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "x"}},
        headers=other_tenant_header,
    )
    assert r.status_code == 404


@pytest.mark.integration
def test_post_message_401_without_auth(client: TestClient) -> None:
    r = client.post(
        "/api/v1/threads/abc/messages",
        json={"input": {"q": "x"}},
    )
    assert r.status_code == 401


@pytest.mark.integration
def test_post_message_422_missing_input(client: TestClient, auth_header: dict[str, str]) -> None:
    thread_id = _create_thread(client, auth_header)
    r = client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={},  # missing 'input'
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Thread sort order after multiple messages
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_thread_floats_to_top_after_message(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Create A, create B, message A → list shows A first
    (most recently active)."""
    a = _create_thread(client, auth_header)
    # Sleep a tick so create timestamps are distinguishable.
    import time  # noqa: PLC0415

    time.sleep(0.01)
    _create_thread(client, auth_header)  # B
    time.sleep(0.01)
    # Message A → bumps its updated_at.
    client.post(
        f"/api/v1/threads/{a}/messages",
        json={"input": {"q": "x"}},
        headers=auth_header,
    )
    rows = client.get("/api/v1/threads", headers=auth_header).json()["threads"]
    assert rows[0]["thread_id"] == a


# silence: timedelta used in import block for shape parity with the
# sibling test file
_ = timedelta
