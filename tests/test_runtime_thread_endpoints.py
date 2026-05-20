"""Tests for thread management endpoints (PR-O).

POST /api/v1/threads, GET /api/v1/threads, GET /api/v1/threads/{id}.

The messages endpoint (which creates a threaded run with worker
thread_id propagation) is deferred to PR-Q. The Chainlit thread mode
that consumes these endpoints is PR-P.

Coverage:
* Create thread — happy path, 422 on bad body, 401 on no auth
* List threads — happy + agent filter + tenant scope + limit
* Get thread — happy + 404 on missing + 404 on cross-tenant +
  include_runs toggle
* Cross-tenant isolation enforced at the SQL layer (mirrors GET
  /runs/{id} and GET /jobs/{id} 404 semantics)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import (
    ConversationThread,
    JobStatus,
    Metrics,
    RunRecord,
)
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
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="thread-endpoint-tests",
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def other_tenant_header(storage: InMemoryStorage) -> dict[str, str]:
    """Auth for a SECOND tenant — used to confirm cross-tenant
    isolation on GET /threads/{id} (404 instead of leakage)."""
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="other-tenant",
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# POST /api/v1/threads
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_thread_returns_201_with_thread_id(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/threads",
        json={"agent": "rag-qa", "title": "Refund policy questions"},
        headers=auth_header,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["thread_id"]  # non-empty
    assert body["agent"] == "rag-qa"
    assert body["title"] == "Refund policy questions"
    # Bare-create response omits run history (saves storage scan).
    assert body["runs"] is None


@pytest.mark.integration
def test_create_thread_defaults_empty_title(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """``title`` is optional — operators who don't bother get empty
    string. Clients fall back to first-message preview for display."""
    r = client.post(
        "/api/v1/threads",
        json={"agent": "faq"},
        headers=auth_header,
    )
    assert r.status_code == 201
    assert r.json()["title"] == ""


@pytest.mark.integration
def test_create_thread_422_on_missing_agent(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """``agent`` is required; missing → Pydantic 422."""
    r = client.post(
        "/api/v1/threads",
        json={"title": "no agent"},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


@pytest.mark.integration
def test_create_thread_422_on_oversize_title(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Title caps at 256 chars at the wire boundary."""
    r = client.post(
        "/api/v1/threads",
        json={"agent": "rag-qa", "title": "x" * 257},
        headers=auth_header,
    )
    assert r.status_code == 422


@pytest.mark.integration
def test_create_thread_401_without_auth(client: TestClient) -> None:
    r = client.post(
        "/api/v1/threads",
        json={"agent": "rag-qa"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/threads
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_threads_returns_tenant_scoped_rows(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """List returns only the authenticated tenant's threads."""
    client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    client.post("/api/v1/threads", json={"agent": "faq"}, headers=auth_header)
    r = client.get("/api/v1/threads", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert {t["agent"] for t in body["threads"]} == {"rag-qa", "faq"}


@pytest.mark.integration
def test_list_threads_filters_by_agent(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    client.post("/api/v1/threads", json={"agent": "faq"}, headers=auth_header)
    r = client.get("/api/v1/threads?agent=rag-qa", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["threads"][0]["agent"] == "rag-qa"


@pytest.mark.integration
def test_list_threads_respects_limit(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    for _ in range(5):
        client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    r = client.get("/api/v1/threads?limit=2", headers=auth_header)
    assert r.status_code == 200
    assert r.json()["count"] == 2


@pytest.mark.integration
def test_list_threads_tenant_isolated(
    client: TestClient,
    auth_header: dict[str, str],
    other_tenant_header: dict[str, str],
) -> None:
    """Tenant A's threads don't appear in tenant B's list."""
    client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    # Tenant B's list should be empty even though tenant A has a thread.
    r = client.get("/api/v1/threads", headers=other_tenant_header)
    assert r.status_code == 200
    assert r.json()["count"] == 0


@pytest.mark.integration
def test_list_threads_empty_returns_zero_count(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.get("/api/v1/threads", headers=auth_header)
    assert r.status_code == 200
    assert r.json() == {"threads": [], "count": 0}


# ---------------------------------------------------------------------------
# GET /api/v1/threads/{thread_id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_thread_returns_metadata_and_runs(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Default include_runs=true → response carries the chronological
    run history alongside the thread metadata."""
    # Create the thread.
    r = client.post(
        "/api/v1/threads", json={"agent": "rag-qa", "title": "Test"}, headers=auth_header
    )
    thread_id = r.json()["thread_id"]
    tenant_id = r.json()["tenant_id"]

    # Seed two runs in the thread via direct storage write (the messages
    # endpoint that does this via HTTP lands in PR-Q).
    now = datetime.now(UTC)
    for i, ts in enumerate([now, now + timedelta(seconds=10)]):
        run = RunRecord(
            run_id=f"r{i}",
            job_id=f"job_{i}",
            tenant_id=tenant_id,
            agent="rag-qa",
            agent_version="0.1.0",
            prompt_hash="abc",
            provider="openai/gpt-4o-mini-2024-07-18",
            provider_version="1.0",
            pricing_version="2024-01-01",
            status=JobStatus.SUCCESS,
            input={"q": f"turn {i}"},
            output={"a": f"answer {i}"},
            metrics=Metrics(latency_ms=100, cost_usd=0.001, tokens_in=10, tokens_out=10),
            created_at=ts,
            thread_id=thread_id,
        )
        # Run on the event loop synchronously via TestClient context.
        import asyncio  # noqa: PLC0415

        asyncio.get_event_loop().run_until_complete(storage.save_run(run))

    r = client.get(f"/api/v1/threads/{thread_id}", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thread_id"] == thread_id
    assert body["runs"] is not None
    assert len(body["runs"]) == 2
    # ASC by created_at — earliest first.
    assert body["runs"][0]["run_id"] == "r0"
    assert body["runs"][1]["run_id"] == "r1"


@pytest.mark.integration
def test_get_thread_include_runs_false_omits_history(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """include_runs=false skips the history scan — fast path for
    clients that just want the thread metadata."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    r = client.get(
        f"/api/v1/threads/{thread_id}?include_runs=false", headers=auth_header
    )
    assert r.status_code == 200
    assert r.json()["runs"] is None


@pytest.mark.integration
def test_get_thread_404_on_missing(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.get("/api/v1/threads/nonexistent", headers=auth_header)
    assert r.status_code == 404


@pytest.mark.integration
def test_get_thread_404_cross_tenant_does_not_leak_existence(
    client: TestClient,
    auth_header: dict[str, str],
    other_tenant_header: dict[str, str],
) -> None:
    """Tenant A creates a thread; tenant B fetches it → 404 (NOT 403).
    This matches the contract on GET /runs/{id} and GET /jobs/{id} —
    never confirm cross-tenant existence."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    # Tenant B tries to read it.
    r = client.get(f"/api/v1/threads/{thread_id}", headers=other_tenant_header)
    assert r.status_code == 404


@pytest.mark.integration
def test_get_thread_401_without_auth(client: TestClient) -> None:
    r = client.get("/api/v1/threads/abc")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Cross-endpoint round-trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_then_get_then_list_roundtrip(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """End-to-end: create → get returns it → list shows it once."""
    r = client.post(
        "/api/v1/threads", json={"agent": "rag-qa", "title": "Smoke"}, headers=auth_header
    )
    thread_id = r.json()["thread_id"]
    # Get.
    r = client.get(f"/api/v1/threads/{thread_id}", headers=auth_header)
    assert r.status_code == 200
    assert r.json()["title"] == "Smoke"
    # List.
    r = client.get("/api/v1/threads", headers=auth_header)
    ids = [t["thread_id"] for t in r.json()["threads"]]
    assert thread_id in ids
    # Listed once, not twice (no duplicate row from create).
    assert ids.count(thread_id) == 1


# Sanity touch on the unused import so the linter accepts the module.
_ = ConversationThread
