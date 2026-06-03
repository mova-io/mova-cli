"""Tests for the dead-letter management API (additive ``/api/v1``).

Operate jobs that exhausted their retry budget and landed in
``JobStatus.DEAD_LETTER`` (distinct from a one-off ``ERROR``):

* ``GET  /api/v1/jobs/dead-letter``        — list (``read`` scope)
* ``POST /api/v1/jobs/{job_id}/requeue``   — recover one (``run`` scope)
* ``POST /api/v1/jobs/dead-letter/purge``  — delete (``admin`` scope)

Coverage: the happy paths, scope gating (403), tenant scoping (404 not
403 on cross-tenant), the requeue-of-a-non-dead-letter clean error, and
the route-ordering invariant (``/jobs/dead-letter`` is not captured as a
``{job_id}``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import ErrorInfo, JobKind, JobRecord, JobStatus
from movate.runtime import build_app
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    """A full-scope key (read+run+admin) so each verb's happy path works."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="dl-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


async def _seed_dead_letter(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    target: str = "faq-bot",
    completed_at: datetime | None = None,
) -> JobRecord:
    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target=target,
        status=JobStatus.DEAD_LETTER,
        input={"text": "hi"},
        attempt_count=3,
        error=ErrorInfo(type="provider_error", message="exhausted", retryable=True),
        completed_at=completed_at or datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    await storage.save_job(job)
    return job


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/dead-letter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_only_dead_letters(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    header, tenant_id = auth_setup
    dl = await _seed_dead_letter(storage, tenant_id=tenant_id)
    # A non-dead-letter job must not appear.
    live = JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="faq-bot",
        status=JobStatus.QUEUED,
        input={"text": "hi"},
        created_at=datetime.now(UTC),
    )
    await storage.save_job(live)

    r = client.get("/api/v1/jobs/dead-letter", headers=header)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == dl.job_id
    assert body["jobs"][0]["status"] == "dead_letter"


@pytest.mark.asyncio
async def test_list_agent_filter(client: TestClient, storage: InMemoryStorage, auth_setup) -> None:
    header, tenant_id = auth_setup
    a = await _seed_dead_letter(storage, tenant_id=tenant_id, target="alpha")
    await _seed_dead_letter(storage, tenant_id=tenant_id, target="beta")
    r = client.get("/api/v1/jobs/dead-letter?agent=alpha", headers=header)
    body = r.json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == a.job_id


@pytest.mark.asyncio
async def test_list_tenant_scoped(client: TestClient, storage: InMemoryStorage, auth_setup) -> None:
    header, tenant_id = auth_setup
    await _seed_dead_letter(storage, tenant_id=tenant_id)
    await _seed_dead_letter(storage, tenant_id=uuid4().hex)  # another tenant
    r = client.get("/api/v1/jobs/dead-letter", headers=header)
    assert r.json()["count"] == 1


@pytest.mark.asyncio
async def test_list_route_not_captured_as_job_id(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Route-ordering invariant: ``/jobs/dead-letter`` resolves to the
    list endpoint (200 + envelope), NOT the ``/jobs/{job_id}`` poll (which
    would 404 on the literal id 'dead-letter')."""
    header, _ = auth_setup
    r = client.get("/api/v1/jobs/dead-letter", headers=header)
    assert r.status_code == 200
    assert "jobs" in r.json()


def test_list_without_auth_401(client: TestClient) -> None:
    assert client.get("/api/v1/jobs/dead-letter").status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{job_id}/requeue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_resets_to_queued(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    header, tenant_id = auth_setup
    dl = await _seed_dead_letter(storage, tenant_id=tenant_id)
    r = client.post(f"/api/v1/jobs/{dl.job_id}/requeue", headers=header)
    assert r.status_code == 200
    assert r.json()["status"] == "queued"

    got = await storage.get_job(dl.job_id, tenant_id=tenant_id)
    assert got is not None
    assert got.status == JobStatus.QUEUED
    assert got.attempt_count == 0
    assert got.next_retry_at is None
    assert got.error is None


@pytest.mark.asyncio
async def test_requeue_non_dead_letter_404(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Requeue of a job that isn't dead-lettered is a clean 404 — never
    mutate a live job."""
    header, tenant_id = auth_setup
    live = JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="faq-bot",
        status=JobStatus.QUEUED,
        input={"text": "hi"},
        created_at=datetime.now(UTC),
    )
    await storage.save_job(live)
    r = client.post(f"/api/v1/jobs/{live.job_id}/requeue", headers=header)
    assert r.status_code == 404
    got = await storage.get_job(live.job_id, tenant_id=tenant_id)
    assert got is not None and got.status == JobStatus.QUEUED  # untouched


@pytest.mark.asyncio
async def test_requeue_cross_tenant_404(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    header, _ = auth_setup
    other = await _seed_dead_letter(storage, tenant_id=uuid4().hex)
    r = client.post(f"/api/v1/jobs/{other.job_id}/requeue", headers=header)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_requeue_requires_run_scope(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    _, tenant_id = auth_setup
    dl = await _seed_dead_letter(storage, tenant_id=tenant_id)
    read_only = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, scopes=["read"])
    await storage.save_api_key(read_only.record)
    r = client.post(
        f"/api/v1/jobs/{dl.job_id}/requeue",
        headers={"Authorization": f"Bearer {read_only.full_key}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/dead-letter/purge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_deletes_tenant_dead_letters(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    header, tenant_id = auth_setup
    dl = await _seed_dead_letter(storage, tenant_id=tenant_id)
    other = await _seed_dead_letter(storage, tenant_id=uuid4().hex)
    r = client.post("/api/v1/jobs/dead-letter/purge", headers=header)
    assert r.status_code == 200
    assert r.json()["purged"] == 1
    assert await storage.get_job(dl.job_id, tenant_id=tenant_id) is None
    # Another tenant's dead-letter is untouched.
    assert await storage.get_job(other.job_id, tenant_id=other.tenant_id) is not None


@pytest.mark.asyncio
async def test_purge_before_cutoff(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    header, tenant_id = auth_setup
    old = await _seed_dead_letter(
        storage, tenant_id=tenant_id, completed_at=datetime.now(UTC) - timedelta(days=2)
    )
    recent = await _seed_dead_letter(storage, tenant_id=tenant_id, completed_at=datetime.now(UTC))
    cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    # Pass via params so httpx URL-encodes the ``+00:00`` offset (a raw
    # ``+`` in the query string would decode to a space and 422).
    r = client.post("/api/v1/jobs/dead-letter/purge", params={"before": cutoff}, headers=header)
    assert r.status_code == 200, r.text
    assert r.json()["purged"] == 1
    assert await storage.get_job(old.job_id, tenant_id=tenant_id) is None
    assert await storage.get_job(recent.job_id, tenant_id=tenant_id) is not None


@pytest.mark.asyncio
async def test_purge_requires_admin_scope(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    _, tenant_id = auth_setup
    await _seed_dead_letter(storage, tenant_id=tenant_id)
    run_only = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, scopes=["run"])
    await storage.save_api_key(run_only.record)
    r = client.post(
        "/api/v1/jobs/dead-letter/purge",
        headers={"Authorization": f"Bearer {run_only.full_key}"},
    )
    assert r.status_code == 403


def test_purge_without_auth_401(client: TestClient) -> None:
    assert client.post("/api/v1/jobs/dead-letter/purge").status_code == 401
