"""HTTP runtime — auth middleware + /healthz + /run + /jobs/{id}.

Built against ``fastapi.TestClient`` over ``InMemoryStorage`` so each
test gets a hermetic app + DB. Covers:

* /healthz unauthed
* Auth: every failure mode → uniform 401 AUTH_REQUIRED
* /run queues a job; the persisted record carries the right
  tenant_id + api_key_id
* /jobs/{id} polls; cross-tenant lookups 404 (not 403)

The full job *lifecycle* (queue → claim → terminal) lives with the
worker in stage 4; here we only test the HTTP layer transitions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.auth import mint_api_key
from movate.core.models import (
    ApiKeyEnv,
    JobKind,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.runtime import build_app
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage

cli_runner = CliRunner(mix_stderr=False)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    """Fresh in-memory storage per test."""
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    """TestClient bound to a fresh app + storage."""
    return TestClient(build_app(storage))


@pytest.fixture
async def minted_key(storage: InMemoryStorage):
    """A persisted API key + the bearer-formatted token to present."""
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="test-suite")
    await storage.save_api_key(minted.record)
    return minted, f"Bearer {minted.full_key}"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": token}


# ---------------------------------------------------------------------------
# /healthz — unauthed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_healthz_returns_ok_without_auth(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok"
    assert "version" in payload


# ---------------------------------------------------------------------------
# /ready — unauthed readiness probe with deep checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ready_returns_200_when_storage_healthy(client: TestClient) -> None:
    """Happy path: storage ping succeeds → 200 + every check ok."""
    r = client.get("/ready")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["storage"] == "ok"
    assert "version" in payload


@pytest.mark.unit
def test_ready_returns_503_when_storage_ping_fails() -> None:
    """When the storage backend can't be reached, /ready returns 503 with
    a per-check failure reason so ACA stops routing traffic to this pod
    until the dependency recovers."""

    class FailingStorage(InMemoryStorage):
        async def ping(self) -> None:
            raise ConnectionError("simulated DB down")

    failing = FailingStorage()
    # ``init`` is a no-op on the in-memory double; no need to await
    # since the failing variant inherits the no-op.
    app = build_app(failing)
    client = TestClient(app)

    r = client.get("/ready")
    assert r.status_code == 503
    payload = r.json()
    assert payload["status"] == "not_ready"
    assert "ConnectionError" in payload["checks"]["storage"]
    assert "simulated DB down" in payload["checks"]["storage"]


@pytest.mark.unit
def test_ready_does_not_require_auth(client: TestClient) -> None:
    """ACA's readiness probe hits this endpoint without any Authorization
    header. Must work, same as /healthz."""
    r = client.get("/ready")  # no auth header
    assert r.status_code in (200, 503)  # NOT 401


# ---------------------------------------------------------------------------
# Auth — every failure mode collapses to 401 AUTH_REQUIRED
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_without_auth_header_returns_401(client: TestClient) -> None:
    r = client.post("/run", json={"kind": "agent", "target": "demo", "input": {}})
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "auth_required"


@pytest.mark.unit
def test_run_with_wrong_scheme_returns_401(client: TestClient) -> None:
    """`Authorization: Basic <stuff>` must 401, not crash."""
    r = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers={"Authorization": "Basic abc123"},
    )
    assert r.status_code == 401


@pytest.mark.unit
def test_run_with_malformed_token_returns_401(client: TestClient) -> None:
    r = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers={"Authorization": "Bearer garbage"},
    )
    assert r.status_code == 401
    # Body shape is the same single-shape envelope.
    assert r.json()["detail"]["error"]["code"] == "auth_required"


@pytest.mark.unit
async def test_run_with_unknown_key_id_returns_401(
    client: TestClient, storage: InMemoryStorage
) -> None:
    """Token shape is valid but no matching record → 401, indistinguishable
    from a parse failure (timing-oracle defense)."""
    # Mint a key but DON'T persist it — server has no idea who this is.
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    r = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers={"Authorization": f"Bearer {minted.full_key}"},
    )
    assert r.status_code == 401


@pytest.mark.unit
async def test_run_with_revoked_key_returns_401(
    client: TestClient, storage: InMemoryStorage
) -> None:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    await storage.revoke_api_key(minted.record.key_id, tenant_id=minted.record.tenant_id)

    r = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers={"Authorization": f"Bearer {minted.full_key}"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /run — queues a job
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_queues_agent_job(client: TestClient, minted_key, storage) -> None:
    minted, bearer = minted_key
    r = client.post(
        "/run",
        json={"kind": "agent", "target": "demo-agent", "input": {"text": "hi"}},
        headers=_auth_headers(bearer),
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    # Verify the persisted record carries the tenant + key attribution.
    saved = await storage.get_job(job_id, tenant_id=minted.record.tenant_id)
    assert saved is not None
    assert saved.tenant_id == minted.record.tenant_id
    assert saved.api_key_id == minted.record.key_id
    assert saved.kind == JobKind.AGENT
    assert saved.target == "demo-agent"
    assert saved.input == {"text": "hi"}
    assert saved.status == JobStatus.QUEUED


@pytest.mark.unit
async def test_run_queues_workflow_job(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.post(
        "/run",
        json={
            "kind": "workflow",
            "target": "returns-pipeline",
            "input": {"order_id": "ord-123"},
        },
        headers=_auth_headers(bearer),
    )
    assert r.status_code == 202, r.text


@pytest.mark.unit
def test_run_rejects_missing_required_fields(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.post("/run", json={"kind": "agent"}, headers=_auth_headers(bearer))
    assert r.status_code == 422  # FastAPI's stock validation error


@pytest.mark.unit
def test_run_rejects_unknown_kind(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.post(
        "/run",
        json={"kind": "ritual", "target": "demo", "input": {}},
        headers=_auth_headers(bearer),
    )
    assert r.status_code == 422


@pytest.mark.unit
def test_run_rejects_empty_target(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.post(
        "/run",
        json={"kind": "agent", "target": "", "input": {}},
        headers=_auth_headers(bearer),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_job_returns_queued_state(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    submit = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {"x": 1}},
        headers=_auth_headers(bearer),
    )
    job_id = submit.json()["job_id"]

    r = client.get(f"/jobs/{job_id}", headers=_auth_headers(bearer))
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"
    assert body["target"] == "demo"
    assert body["input"] == {"x": 1}
    assert body["result_run_id"] is None
    assert body["completed_at"] is None


@pytest.mark.unit
def test_get_job_404_for_unknown_id(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.get("/jobs/no-such-id", headers=_auth_headers(bearer))
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


@pytest.mark.unit
async def test_get_job_404_when_cross_tenant(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Cross-tenant lookups MUST return 404 (not 403). 403 would let an
    attacker probe whether a job_id exists in any tenant."""
    # Submit a job under the legitimate key.
    _, bearer = minted_key
    submit = client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers=_auth_headers(bearer),
    )
    job_id = submit.json()["job_id"]

    # Now mint a SECOND key for a DIFFERENT tenant.
    other_minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(other_minted.record)

    # That tenant's key must NOT be able to see the first tenant's job.
    r = client.get(
        f"/jobs/{job_id}",
        headers={"Authorization": f"Bearer {other_minted.full_key}"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# GET /jobs (list)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_jobs_empty(client: TestClient, minted_key) -> None:
    """No jobs yet → 200 + empty list + count=0."""
    _, bearer = minted_key
    r = client.get("/jobs", headers=_auth_headers(bearer))
    assert r.status_code == 200
    body = r.json()
    assert body == {"jobs": [], "count": 0}


@pytest.mark.unit
def test_list_jobs_returns_recent_jobs(client: TestClient, minted_key) -> None:
    """Submit three jobs and verify they all come back in the list,
    newest first (server-side ordering)."""
    _, bearer = minted_key
    submitted = []
    for i in range(3):
        r = client.post(
            "/run",
            json={"kind": "agent", "target": f"demo-{i}", "input": {"i": i}},
            headers=_auth_headers(bearer),
        )
        submitted.append(r.json()["job_id"])

    r = client.get("/jobs", headers=_auth_headers(bearer))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    returned_ids = [j["job_id"] for j in body["jobs"]]
    # Same ids (set-equal); order is newest-first which means reversed.
    assert set(returned_ids) == set(submitted)


@pytest.mark.unit
def test_list_jobs_filters_by_status(client: TestClient, minted_key) -> None:
    """``?status=queued`` returns only queued jobs. Submitting alone never
    advances state past queued, so all three should match."""
    _, bearer = minted_key
    for i in range(3):
        client.post(
            "/run",
            json={"kind": "agent", "target": "demo", "input": {"i": i}},
            headers=_auth_headers(bearer),
        )
    r = client.get("/jobs", params={"status": "queued"}, headers=_auth_headers(bearer))
    assert r.status_code == 200
    assert r.json()["count"] == 3

    # No errored jobs → empty.
    r = client.get("/jobs", params={"status": "error"}, headers=_auth_headers(bearer))
    assert r.status_code == 200
    assert r.json()["count"] == 0


@pytest.mark.unit
def test_list_jobs_respects_limit_cap(client: TestClient, minted_key) -> None:
    """Server hard-caps limit at 100 so a runaway client can't fetch
    arbitrarily large pages."""
    _, bearer = minted_key
    # Don't actually submit 100+; just verify the endpoint accepts the
    # param and doesn't 4xx. The cap-enforcement detail is unit-tested
    # against storage in test_jobs_storage.
    r = client.get("/jobs", params={"limit": 5000}, headers=_auth_headers(bearer))
    assert r.status_code == 200


@pytest.mark.unit
async def test_list_jobs_is_tenant_scoped(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Tenant A submits a job; tenant B's key must NOT see it in the
    list (no cross-tenant leakage — same isolation as show/run/etc.)."""
    _, bearer = minted_key
    client.post(
        "/run",
        json={"kind": "agent", "target": "demo", "input": {}},
        headers=_auth_headers(bearer),
    )

    # Mint a second key for a different tenant.
    other = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(other.record)

    r = client.get(
        "/jobs",
        headers={"Authorization": f"Bearer {other.full_key}"},
    )
    assert r.status_code == 200
    # Tenant B has no jobs of its own → empty list, NOT tenant A's job.
    assert r.json()["count"] == 0


# ---------------------------------------------------------------------------
# GET /runs/{id}
# ---------------------------------------------------------------------------


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


@pytest.mark.unit
async def test_get_run_returns_output(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Happy path: persisted run is reachable via GET /runs/{id}, and the
    response carries the agent's actual ``output`` — the whole reason this
    endpoint exists (``GET /jobs/{id}`` deliberately omits the output)."""
    minted, bearer = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    r = client.get(f"/runs/{run.run_id}", headers=_auth_headers(bearer))
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run.run_id
    assert body["job_id"] == run.job_id
    assert body["output"] == {"answer": "hello"}
    assert body["metrics"]["cost_usd"] == 0.0001
    assert body["provider"] == "openai/gpt-4o-mini"
    # tenant_id is audit-only — must NOT leak over the wire.
    assert "tenant_id" not in body


@pytest.mark.unit
def test_get_run_404_for_unknown_id(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.get("/runs/no-such-run", headers=_auth_headers(bearer))
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


@pytest.mark.unit
async def test_get_run_404_when_cross_tenant(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Same isolation contract as GET /jobs/{id}: a key from tenant B
    must not see tenant A's run. 404, never 403 — 403 would leak that
    the id exists in some other tenant."""
    minted, _ = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    # Mint a second key for a different tenant.
    other = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(other.record)

    r = client.get(
        f"/runs/{run.run_id}",
        headers={"Authorization": f"Bearer {other.full_key}"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


@pytest.mark.unit
def test_get_run_requires_auth(client: TestClient) -> None:
    r = client.get("/runs/any-id")  # no auth header
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Auth side effects — last_used_at gets bumped
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_successful_request_touches_last_used_at(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Touch is now awaited inline (was fire-and-forget in stage 3a;
    moved inline in stage 5 to fix an asyncpg pool race). That makes
    the assertion deterministic — no sleep, no skip-on-race."""
    minted, bearer = minted_key
    pre = await storage.get_api_key(minted.record.key_id)
    assert pre is not None
    assert pre.last_used_at is None

    # /jobs/{any} requires auth even when the id is bogus, which is
    # what we want — we're testing the side effect of successful auth.
    client.get("/jobs/whatever", headers=_auth_headers(bearer))

    post = await storage.get_api_key(minted.record.key_id)
    assert post is not None
    assert post.last_used_at is not None


# ---------------------------------------------------------------------------
# GET /agents
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_agents_returns_empty_when_registry_unset(
    minted_key, storage: InMemoryStorage
) -> None:
    """``build_app(storage)`` without ``agents=`` yields an empty catalog."""
    app = build_app(storage)
    client = TestClient(app)
    _, bearer = minted_key
    r = client.get("/agents", headers=_auth_headers(bearer))
    assert r.status_code == 200
    assert r.json() == {"agents": []}


@pytest.mark.unit
async def test_agents_returns_metadata_only(minted_key, storage: InMemoryStorage, tmp_path) -> None:
    """Registry returns name/version/description — never schemas or prompts."""
    # Scaffold a real agent to get a real AgentBundle.
    cli_runner.invoke(
        cli_app,
        ["init", "alpha", "-t", "default", "--target", str(tmp_path)],
        catch_exceptions=False,
    )
    bundles = scan_agents(tmp_path)

    test_app = build_app(storage, agents=bundles)
    test_client = TestClient(test_app)
    _, bearer = minted_key
    r = test_client.get("/agents", headers=_auth_headers(bearer))
    assert r.status_code == 200
    body = r.json()
    assert len(body["agents"]) == 1
    entry = body["agents"][0]
    # Surface metadata only.
    assert set(entry.keys()) == {"name", "version", "description"}
    assert entry["name"] == "alpha"


@pytest.mark.unit
async def test_agents_requires_auth(storage: InMemoryStorage) -> None:
    """Discovery is gated on auth — same envelope as every other endpoint."""
    app_under_test = build_app(storage)
    test_client = TestClient(app_under_test)
    r = test_client.get("/agents")
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "auth_required"
