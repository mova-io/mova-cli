"""ADR 032 D2 — aggregate monitor endpoints.

* ``GET /api/v1/report`` — cross-agent, tenant-scoped rollup (scope ``read``)
* ``GET /api/v1/agents/{name}/metrics`` — per-agent slice (scope ``read``)

Both expose the SAME aggregation ``mdk report`` computes
(:mod:`movate.core.reporting`) over the local store — the in-product monitor
feed the Mova iO front end renders. The runtime never imports ``cli``; the
shared rollup lives in ``core`` (``cli ⊥ runtime``).

Hermetic: TestClient + an ``InMemoryStorage`` seeded with ``RunRecord`` /
``EvalRecord`` rows directly (no LLM, no DB, no server).

Coverage:

* Aggregates match the seeded records (totals, per-agent rollup, latency
  percentiles, top failing cases, pass-rate).
* Tenant scoping — another tenant's data is invisible.
* Per-agent endpoint scopes to one agent; an unknown agent → zeroed rollup
  (200), not a 404 / 500.
* Empty store → zeroed report (200), not a 500.
* The ``window`` param narrows to the last N days.
* Auth: 401 unauthed; ``read`` scope is the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import (
    ErrorInfo,
    EvalRecord,
    JobStatus,
    JudgeMethod,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Record builders (mirror tests/test_report_cmd.py)
# ---------------------------------------------------------------------------


def _run(
    *,
    tenant_id: str,
    run_id: str = "r1",
    agent: str = "triage",
    status: JobStatus = JobStatus.SUCCESS,
    cost: float = 0.001,
    latency_ms: int = 100,
    inp: dict | None = None,
    error: ErrorInfo | None = None,
    when: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="hash",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2026-05",
        status=status,
        input=inp if inp is not None else {"q": "x"},
        output={"a": "y"} if status == JobStatus.SUCCESS else None,
        error=error,
        metrics=Metrics(
            cost_usd=cost,
            latency_ms=latency_ms,
            tokens=TokenUsage(input=10, output=5),
            provider="openai/gpt-4o-mini",
        ),
        created_at=when or datetime.now(UTC),
    )


def _eval(
    *,
    tenant_id: str,
    eval_id: str = "e1",
    agent: str = "triage",
    pass_rate: float = 1.0,
    mean_score: float = 0.9,
    cost: float = 0.01,
    when: datetime | None = None,
) -> EvalRecord:
    return EvalRecord(
        eval_id=eval_id,
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        dataset_hash="dh",
        judge_method=JudgeMethod.LLM_JUDGE,
        judge_provider="openai/gpt-4o",
        runs_per_case=1,
        gate_mode="mean",
        threshold=0.7,
        mean_score=mean_score,
        pass_rate=pass_rate,
        sample_count=10,
        total_cost_usd=cost,
        created_at=when or datetime.now(UTC),
    )


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
async def auth_setup(storage: InMemoryStorage):
    """A ``read``-scoped key + its tenant_id. Report endpoints gate on ``read``."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="report-v1-tests", scopes=["read"]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


async def _seed_two_agents(storage: InMemoryStorage, tenant_id: str) -> None:
    """triage: 2 success + 1 failure (with cost+latency); summary: 1 success.
    Two eval summaries. Mirrors the CLI test's populated DB."""
    await storage.save_run(
        _run(tenant_id=tenant_id, run_id="t1", agent="triage", cost=0.05, latency_ms=120)
    )
    await storage.save_run(
        _run(tenant_id=tenant_id, run_id="t2", agent="triage", cost=0.06, latency_ms=300)
    )
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="t3",
            agent="triage",
            status=JobStatus.ERROR,
            inp={"q": "boom"},
            latency_ms=80,
            error=ErrorInfo(type="Timeout", message="timed out"),
        )
    )
    await storage.save_run(
        _run(tenant_id=tenant_id, run_id="s1", agent="summary", cost=0.02, latency_ms=200)
    )
    await storage.save_eval(
        _eval(tenant_id=tenant_id, eval_id="e-triage", agent="triage", pass_rate=0.75)
    )
    await storage.save_eval(
        _eval(tenant_id=tenant_id, eval_id="e-summary", agent="summary", pass_rate=1.0)
    )


# ---------------------------------------------------------------------------
# GET /api/v1/report — cross-agent
# ---------------------------------------------------------------------------


async def test_report_aggregates_seeded_records(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_two_agents(storage, tenant_id)

    r = client.get("/api/v1/report", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()

    # totals
    assert body["totals"]["runs"] == 4
    assert body["totals"]["failed_runs"] == 1
    assert body["totals"]["eval_runs"] == 2
    assert body["totals"]["latency_ms"]["p50"] is not None
    assert body["totals"]["latency_ms"]["count"] == 4

    # per-agent rollup
    names = {a["name"] for a in body["agents"]}
    assert names == {"triage", "summary"}
    triage = next(a for a in body["agents"] if a["name"] == "triage")
    assert triage["runs"] == 3
    assert triage["failed_runs"] == 1
    assert triage["latest_pass_rate"] == 0.75
    # 0.05 + 0.06 + 0.001 (the failing t3 run still carries the default cost)
    assert abs(triage["total_cost_usd"] - 0.111) < 1e-9

    # top failing cases — the failing triage input clusters with its error
    assert len(body["top_failing_cases"]) >= 1
    top = body["top_failing_cases"][0]
    assert top["failures"] == 1
    assert top["last_error"] == "timed out"

    # cross-agent feed has no agent_filter
    assert body["agent_filter"] is None
    assert body["window_days"] == 0


async def test_report_is_tenant_scoped(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Another tenant's runs/evals must not appear in this tenant's report."""
    auth_header, tenant_id = auth_setup
    await _seed_two_agents(storage, tenant_id)
    # A second tenant's data that must stay invisible.
    other = uuid4().hex
    await storage.save_run(
        _run(tenant_id=other, run_id="x1", agent="other-agent", cost=99.0, latency_ms=999)
    )
    await storage.save_eval(_eval(tenant_id=other, eval_id="ex", agent="other-agent"))

    r = client.get("/api/v1/report", headers=auth_header)
    body = r.json()
    names = {a["name"] for a in body["agents"]}
    assert "other-agent" not in names
    assert body["totals"]["runs"] == 4  # only this tenant's 4 runs
    assert body["totals"]["cost_usd"] < 1.0  # the $99 other-tenant run excluded


async def test_report_empty_store_is_zeroed_not_500(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get("/api/v1/report", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["runs"] == 0
    assert body["totals"]["eval_runs"] == 0
    assert body["totals"]["latest_pass_rate"] is None
    assert body["totals"]["latency_ms"]["p50"] is None
    assert body["agents"] == []
    assert body["top_failing_cases"] == []


async def test_report_window_narrows_to_recent(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="old",
            agent="ancient",
            when=datetime.now(UTC) - timedelta(days=30),
        )
    )
    await storage.save_run(
        _run(
            tenant_id=tenant_id,
            run_id="new",
            agent="fresh",
            when=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    r = client.get("/api/v1/report?window=7", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {a["name"] for a in body["agents"]}
    assert "fresh" in names
    assert "ancient" not in names
    assert body["window_days"] == 7


async def test_report_rejects_negative_window(client: TestClient, auth_setup) -> None:
    """The ``window`` param is bounded (ge=0) at the API layer."""
    auth_header, _ = auth_setup
    r = client.get("/api/v1/report?window=-1", headers=auth_header)
    assert r.status_code == 422


def test_report_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/report").status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/agents/{name}/metrics — per-agent
# ---------------------------------------------------------------------------


async def test_agent_metrics_scopes_to_one_agent(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_two_agents(storage, tenant_id)

    r = client.get("/api/v1/agents/triage/metrics", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "triage"
    # totals are scoped to triage only (3 runs, not the cross-agent 4)
    assert body["totals"]["runs"] == 3
    assert body["rollup"]["name"] == "triage"
    assert body["rollup"]["runs"] == 3
    assert body["rollup"]["failed_runs"] == 1
    assert body["rollup"]["latest_pass_rate"] == 0.75
    # summary should not leak into a triage-scoped metrics call
    assert "summary" not in {c for tc in body["top_failing_cases"] for c in tc["agents"]}


async def test_agent_metrics_unknown_agent_is_zeroed_not_404(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """A metrics view for an agent with no data → zeroed rollup (200),
    not a 404 — the front end renders an empty panel."""
    auth_header, tenant_id = auth_setup
    await _seed_two_agents(storage, tenant_id)
    r = client.get("/api/v1/agents/never-existed/metrics", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "never-existed"
    assert body["totals"]["runs"] == 0
    assert body["rollup"]["name"] == "never-existed"
    assert body["rollup"]["runs"] == 0
    assert body["rollup"]["failure_rate"] == 0.0
    assert body["rollup"]["mean_cost_usd"] == 0.0
    assert body["rollup"]["latest_pass_rate"] is None
    assert body["rollup"]["latency_ms"]["p50"] is None


async def test_agent_metrics_is_tenant_scoped(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, _tenant_id = auth_setup
    other = uuid4().hex
    # Same agent name owned by a different tenant — must stay invisible.
    await storage.save_run(
        _run(tenant_id=other, run_id="x1", agent="triage", cost=42.0, latency_ms=500)
    )
    r = client.get("/api/v1/agents/triage/metrics", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    # No runs for THIS tenant → zeroed, the other tenant's data excluded.
    assert body["totals"]["runs"] == 0
    assert body["rollup"]["runs"] == 0


def test_agent_metrics_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/agents/triage/metrics").status_code == 401
