"""Runtime ``/api/v1/observability/*`` endpoints (ADR 047) — hermetic.

TestClient + InMemoryStorage seeded directly (no LLM, no DB, no server). The
ask/troubleshoot routes pass ``mock: true`` so the MockProvider is used — no
real spend, deterministic. Covers:

* all five routes exist + return the documented shapes.
* tenant scoping (another tenant's insights are invisible).
* read vs admin scope (analyze requires admin; the reads require read).
* citations: ask/troubleshoot answers carry evidence[].
* budget cap is plumbed through (cost_usd surfaced).
* analyze enqueues a JobKind.OBSERVABILITY_ANALYZE job; the worker dispatch
  runs it end-to-end and appends an insight.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.executor import Executor
from movate.core.models import (
    JobKind,
    JobRecord,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.core.observability.models import ObservabilityInsight
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.runtime import build_app
from movate.runtime.dispatch import WorkerDispatch
from movate.testing import InMemoryStorage, NullTracer


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def read_auth(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="obs-read", scopes=["read"]
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


@pytest.fixture
async def admin_auth(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="obs-admin", scopes=["read", "admin"]
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


def _insight(
    tenant_id: str, *, project_id: str = "default", day: date | None = None
) -> ObservabilityInsight:
    return ObservabilityInsight(
        tenant_id=tenant_id,
        project_id=project_id,
        date=day or datetime.now(UTC).date(),
        health_score=72.0,
        anomalies=[
            {
                "metric": "cost",
                "severity": "warning",
                "note": "cost up",
                "value": 1.0,
                "baseline": 0.3,
                "z": 3.1,
            }
        ],
        top_failures=[
            {"signature": "Timeout", "count": 2, "sample_message": "boom", "agent": "triage"}
        ],
        usage_rollup={"runs": 5, "errors": 1, "error_rate": 0.2, "cost_usd": 0.09},
        trends={},
        narrative_digest="Yesterday: 5 runs. Watch: a timeout.",
    )


def _run(tenant_id: str, *, run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id=tenant_id,
        agent="triage",
        agent_version="0.1.0",
        prompt_hash="h",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2026-05",
        status=JobStatus.SUCCESS,
        input={"q": "x"},
        output={"a": "y"},
        metrics=Metrics(cost_usd=0.01, latency_ms=100, tokens=TokenUsage(input=10, output=5)),
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# insights + health (read)
# ---------------------------------------------------------------------------


async def test_insights_lists_seeded(client, storage, read_auth) -> None:
    header, tenant_id = read_auth
    await storage.save_insight(_insight(tenant_id))
    r = client.get("/api/v1/observability/insights", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    row = body["insights"][0]
    assert row["health_score"] == 72.0
    assert row["narrative_digest"].startswith("Yesterday")
    assert row["usage_rollup"]["runs"] == 5


async def test_insights_tenant_scoped(client, storage, read_auth) -> None:
    header, tenant_id = read_auth
    await storage.save_insight(_insight(tenant_id))
    await storage.save_insight(_insight(uuid4().hex))  # other tenant
    r = client.get("/api/v1/observability/insights", headers=header)
    assert r.json()["count"] == 1  # only this tenant's row


async def test_health_returns_latest(client, storage, read_auth) -> None:
    header, tenant_id = read_auth
    await storage.save_insight(_insight(tenant_id))
    r = client.get("/api/v1/observability/health", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_insight"] is True
    assert body["health_score"] == 72.0
    assert body["anomaly_count"] == 1


async def test_health_cold_start_is_200_not_404(client, read_auth) -> None:
    header, _ = read_auth
    r = client.get("/api/v1/observability/health?project_id=new", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_insight"] is False
    assert body["health_score"] is None


# ---------------------------------------------------------------------------
# ask + troubleshoot (read, citations mandatory)
# ---------------------------------------------------------------------------


async def test_ask_returns_grounded_answer_with_evidence(client, storage, read_auth) -> None:
    header, tenant_id = read_auth
    await storage.save_insight(_insight(tenant_id))
    await storage.save_run(_run(tenant_id, run_id="r1"))
    r = client.post(
        "/api/v1/observability/ask",
        headers=header,
        json={"question": "how is the fleet?", "project_id": "default", "mock": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"]
    assert body["evidence"], "ask must carry evidence[] when data exists"
    assert any(e["kind"] == "insight" for e in body["evidence"])
    assert "confidence" in body
    assert "cost_usd" in body  # budget plumbing surfaces spend


async def test_troubleshoot_returns_evidence(client, storage, read_auth) -> None:
    header, tenant_id = read_auth
    await storage.save_insight(_insight(tenant_id))
    r = client.post(
        "/api/v1/observability/troubleshoot",
        headers=header,
        json={"symptom": "timeouts", "time_window_days": 7, "mock": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["evidence"]
    assert any(e["kind"] == "failure" for e in body["evidence"])


# ---------------------------------------------------------------------------
# scopes + auth
# ---------------------------------------------------------------------------


def test_reads_require_auth(client) -> None:
    assert client.get("/api/v1/observability/insights").status_code == 401
    assert client.get("/api/v1/observability/health").status_code == 401
    assert client.post("/api/v1/observability/ask", json={"question": "x"}).status_code == 401


async def test_analyze_requires_admin_scope(client, read_auth) -> None:
    header, _ = read_auth  # read-only key
    r = client.post("/api/v1/observability/analyze", headers=header, json={"project_id": "default"})
    assert r.status_code == 403  # read scope can't trigger the analyst


async def test_analyze_enqueues_job(client, storage, admin_auth) -> None:
    header, tenant_id = admin_auth
    r = client.post(
        "/api/v1/observability/analyze",
        headers=header,
        json={"project_id": "default", "budget_usd": 0.1},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["kind"] == JobKind.OBSERVABILITY_ANALYZE.value
    assert body["project_id"] == "default"
    # A QUEUED job landed for this tenant.
    jobs = await storage.list_jobs(tenant_id=tenant_id)
    assert len(jobs) == 1
    assert jobs[0].kind == JobKind.OBSERVABILITY_ANALYZE
    assert jobs[0].input["project_id"] == "default"


# ---------------------------------------------------------------------------
# end-to-end: the worker dispatch runs the analyst
# ---------------------------------------------------------------------------


async def test_analyze_job_runs_via_dispatch(storage) -> None:
    """The OBSERVABILITY_ANALYZE job dispatch produces an append-only insight."""
    tenant_id = uuid4().hex
    await storage.save_run(_run(tenant_id, run_id="r1"))

    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id=tenant_id,
    )
    dispatch = WorkerDispatch(storage=storage, executor=executor, use_mock_for_eval=True)

    yesterday = (datetime.now(UTC).date()).isoformat()
    job = JobRecord(
        job_id=uuid4().hex,
        tenant_id=tenant_id,
        kind=JobKind.OBSERVABILITY_ANALYZE,
        target="observability:default",
        status=JobStatus.QUEUED,
        input={"project_id": "default", "date": yesterday, "mock": True},
    )
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS
    assert outcome.result_run_id  # carries the insight id
    rows = await storage.list_insights(tenant_id, project_id="default")
    assert len(rows) == 1
    assert rows[0].id == outcome.result_run_id


async def test_analyze_job_missing_project_id_errors(storage) -> None:
    tenant_id = uuid4().hex
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id=tenant_id,
    )
    dispatch = WorkerDispatch(storage=storage, executor=executor, use_mock_for_eval=True)
    job = JobRecord(
        job_id=uuid4().hex,
        tenant_id=tenant_id,
        kind=JobKind.OBSERVABILITY_ANALYZE,
        target="observability:",
        status=JobStatus.QUEUED,
        input={},  # no project_id
    )
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "observability_config"
