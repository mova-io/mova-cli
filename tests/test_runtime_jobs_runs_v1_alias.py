"""``/api/v1`` aliases for the unversioned job-poll + run-fetch routes.

A caller that submits via ``POST /api/v1/agents/{name}/runs`` gets back a
``job_id`` and naturally polls the *versioned* path
``GET /api/v1/jobs/{job_id}``, then fetches the run at
``GET /api/v1/runs/{run_id}``. Those routes previously only existed
UNVERSIONED (``/jobs/{id}``, ``/runs/{id}``), so the obvious v1 path 404'd.

These tests pin the additive aliases:

* A submitted job is fetchable at BOTH ``/jobs/{id}`` AND
  ``/api/v1/jobs/{id}`` → identical 200 + payload.
* ``GET /api/v1/jobs`` (list) works like ``GET /jobs``.
* ``GET /api/v1/runs/{id}`` works like ``GET /runs/{id}``.
* Scope + tenant behaviour preserved on the v1 aliases: missing ``read`` →
  403; another tenant's id → 404 (never 403).
* The unversioned routes still work unchanged (back-compat).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, mint_api_key
from movate.core.models import (
    ApiKeyEnv,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
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
async def minted_key(storage: InMemoryStorage):
    """A persisted API key (all scopes) + its bearer header value."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="v1-alias-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return minted, f"Bearer {minted.full_key}"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": token}


def _make_run(
    *,
    tenant_id: str,
    job_id: str = "job-1",
    run_id: str = "run-1",
    output: dict | None = None,
) -> RunRecord:
    """Construct a minimal RunRecord for endpoint round-trip tests."""
    return RunRecord(
        run_id=run_id,
        job_id=job_id,
        tenant_id=tenant_id,
        agent="demo",
        agent_version="0.1.0",
        prompt_hash="sha256:deadbeef",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2024-09",
        status=JobStatus.SUCCESS,
        input={"q": "hi"},
        output=output or {"answer": "hello"},
        metrics=Metrics(
            latency_ms=120,
            tokens=TokenUsage(input=5, output=3),
            cost_usd=0.0001,
            provider="openai/gpt-4o-mini",
            pricing_version="2024-09",
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{id} — alias of GET /jobs/{id}
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_job_both_paths_identical(client: TestClient, minted_key) -> None:
    """A submitted job is fetchable at BOTH the unversioned and versioned
    path, returning the SAME 200 + payload."""
    _, bearer = minted_key
    submit = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {"x": 1}},
        headers=_auth_headers(bearer),
    )
    job_id = submit.json()["job_id"]

    unversioned = client.get(f"/jobs/{job_id}", headers=_auth_headers(bearer))
    versioned = client.get(f"/api/v1/jobs/{job_id}", headers=_auth_headers(bearer))

    assert unversioned.status_code == 200
    assert versioned.status_code == 200
    # Byte-for-byte identical body — same handler, same view model.
    assert versioned.json() == unversioned.json()
    body = versioned.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"
    assert body["target"] == "demo"


@pytest.mark.unit
def test_v1_get_job_404_for_unknown_id(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.get("/api/v1/jobs/no-such-id", headers=_auth_headers(bearer))
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


@pytest.mark.unit
async def test_v1_get_job_404_when_cross_tenant(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Tenant scoping preserved on the v1 alias: another tenant's key must
    not see the job — 404, never 403 (which would leak the id's existence)."""
    _, bearer = minted_key
    submit = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers=_auth_headers(bearer),
    )
    job_id = submit.json()["job_id"]

    other = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=list(ALL_SCOPES))
    await storage.save_api_key(other.record)

    r = client.get(
        f"/api/v1/jobs/{job_id}",
        headers={"Authorization": f"Bearer {other.full_key}"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


@pytest.mark.unit
async def test_v1_get_job_missing_read_scope_403(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """A key WITHOUT the ``read`` scope is rejected with 403 on the v1
    alias — same scope gate as the unversioned route."""
    # First mint an all-scopes key to submit a job we can target.
    owner = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=list(ALL_SCOPES))
    await storage.save_api_key(owner.record)
    submit = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers={"Authorization": f"Bearer {owner.full_key}"},
    )
    job_id = submit.json()["job_id"]

    # A key for the SAME tenant but without "read" — scope check fires
    # before tenant lookup, so this is 403 not 404.
    no_read = mint_api_key(tenant_id=owner.record.tenant_id, env=ApiKeyEnv.LIVE, scopes=["run"])
    await storage.save_api_key(no_read.record)

    r = client.get(
        f"/api/v1/jobs/{job_id}",
        headers={"Authorization": f"Bearer {no_read.full_key}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.unit
def test_v1_get_job_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/jobs/any-id")  # no auth header
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/jobs (list) — behaves like GET /jobs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_v1_list_jobs_matches_unversioned(client: TestClient, minted_key) -> None:
    """The versioned list returns the same jobs as the unversioned one."""
    _, bearer = minted_key
    submitted = []
    for i in range(3):
        r = client.post(
            "/run",
            json={"kind": "agent", "target": f"demo-{i}", "input": {"i": i}},
            headers=_auth_headers(bearer),
        )
        submitted.append(r.json()["job_id"])

    unversioned = client.get("/jobs", headers=_auth_headers(bearer))
    versioned = client.get("/api/v1/jobs", headers=_auth_headers(bearer))

    assert versioned.status_code == 200
    assert versioned.json()["count"] == 3
    assert {j["job_id"] for j in versioned.json()["jobs"]} == set(submitted)
    # Same id set as the unversioned list.
    assert {j["job_id"] for j in versioned.json()["jobs"]} == {
        j["job_id"] for j in unversioned.json()["jobs"]
    }


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{id} — alias of GET /runs/{id}
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_run_both_paths_identical(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """A persisted run is reachable at BOTH the unversioned and versioned
    path, carrying the SAME 200 + output payload."""
    minted, bearer = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    unversioned = client.get(f"/runs/{run.run_id}", headers=_auth_headers(bearer))
    versioned = client.get(f"/api/v1/runs/{run.run_id}", headers=_auth_headers(bearer))

    assert unversioned.status_code == 200
    assert versioned.status_code == 200
    assert versioned.json() == unversioned.json()
    body = versioned.json()
    assert body["run_id"] == run.run_id
    assert body["output"] == {"answer": "hello"}
    # tenant_id is audit-only — must NOT leak over the wire.
    assert "tenant_id" not in body


@pytest.mark.unit
def test_v1_get_run_404_for_unknown_id(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.get("/api/v1/runs/no-such-run", headers=_auth_headers(bearer))
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


@pytest.mark.unit
async def test_v1_get_run_404_when_cross_tenant(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Tenant scoping preserved on the v1 run alias: 404, never 403."""
    minted, _ = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    other = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=list(ALL_SCOPES))
    await storage.save_api_key(other.record)

    r = client.get(
        f"/api/v1/runs/{run.run_id}",
        headers={"Authorization": f"Bearer {other.full_key}"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


@pytest.mark.unit
async def test_v1_get_run_missing_read_scope_403(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """A key WITHOUT the ``read`` scope is rejected with 403 on the v1 run
    alias — scope gate fires before the tenant lookup."""
    owner = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=list(ALL_SCOPES))
    await storage.save_api_key(owner.record)
    run = _make_run(tenant_id=owner.record.tenant_id)
    await storage.save_run(run)

    no_read = mint_api_key(tenant_id=owner.record.tenant_id, env=ApiKeyEnv.LIVE, scopes=["run"])
    await storage.save_api_key(no_read.record)

    r = client.get(
        f"/api/v1/runs/{run.run_id}",
        headers={"Authorization": f"Bearer {no_read.full_key}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.unit
def test_v1_get_run_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/runs/any-id")  # no auth header
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Back-compat — unversioned routes still work unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_unversioned_routes_unchanged(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """The unversioned routes are untouched by the additive v1 aliases."""
    minted, bearer = minted_key
    submit = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {"x": 1}},
        headers=_auth_headers(bearer),
    )
    job_id = submit.json()["job_id"]
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    assert client.get(f"/jobs/{job_id}", headers=_auth_headers(bearer)).status_code == 200
    assert client.get("/jobs", headers=_auth_headers(bearer)).status_code == 200
    assert client.get(f"/runs/{run.run_id}", headers=_auth_headers(bearer)).status_code == 200
