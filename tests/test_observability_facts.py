"""observability_facts — the unified reporting surface (ADR 096).

Covers the four layers of the feature:

* **Model** — :class:`ObservabilityFact` roundtrip + defaults.
* **Storage** — save + list across the shared ``storage`` fixture
  (InMemory + sqlite + postgres-behind-skip-guard): upsert idempotency on
  ``fact_id`` (same id twice → ONE row, updated values), tenant scoping,
  and every list filter (kind / workflow / agent / status / since / limit).
* **Builders** — :func:`fact_from_run_record` /
  :func:`fact_from_workflow_run` mapping correctness, incl. metrics
  flattening, attribute projection, route extraction, and the
  missing-metrics / runtime-None defaults.
* **Edges** — the dispatch agent path writes a fact next to
  ``record_run_usage`` and is FAIL-SOFT (a raising
  ``save_observability_fact`` never fails the job); the API route
  (``GET /api/v1/observability/facts``) is tenant-scoped, filtered, and
  read-scope gated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.executor import Executor
from movate.core.models import (
    ErrorInfo,
    JobKind,
    JobRecord,
    JobStatus,
    Metrics,
    ObservabilityFact,
    RunRecord,
    TokenUsage,
    TurnRecord,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.runtime import build_app
from movate.runtime.dispatch import WorkerDispatch
from movate.runtime.facts import (
    fact_from_run_record,
    fact_from_workflow_run,
    write_fact_failsoft,
)
from movate.testing import InMemoryStorage, NullTracer

cli_runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_fact(
    *,
    fact_id: str | None = None,
    kind: str = "run",
    tenant_id: str = "tenant-a",
    status: str = "success",
    cost_usd: float = 0.0123,
    created_at: datetime | None = None,
    **overrides: object,
) -> ObservabilityFact:
    source_id = (fact_id or str(uuid4())).split(":", 1)[-1]
    return ObservabilityFact(
        fact_id=fact_id or f"{kind}:{source_id}",
        kind=kind,  # type: ignore[arg-type]
        source_id=source_id,
        trace_id="trace-123",
        tenant_id=tenant_id,
        agent="demo-agent" if kind == "run" else None,
        workflow="demo-flow" if kind == "workflow_run" else None,
        status=status,
        runtime="native",
        cost_usd=cost_usd,
        tokens_in=10,
        tokens_out=20,
        latency_ms=350,
        created_at=created_at or datetime.now(UTC),
        attributes={"provider": "mock", "pricing_version": "v1"},
        **overrides,  # type: ignore[arg-type]
    )


def _make_run_record(
    *,
    run_id: str = "run-1",
    tenant_id: str = "tenant-a",
    metrics: Metrics | None = None,
    turns: list[TurnRecord] | None = None,
    status: JobStatus = JobStatus.SUCCESS,
    error: ErrorInfo | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id="job-1",
        tenant_id=tenant_id,
        agent="demo-agent",
        agent_version="0.1.0",
        prompt_hash="abc123",
        provider="mock",
        provider_version="1.0",
        pricing_version="2026-01",
        status=status,
        input={"text": "hi"},
        output={"message": "hello"},
        metrics=metrics if metrics is not None else Metrics(),
        error=error,
        node_id="step1",
        turns=turns or [],
    )


def _make_workflow_run(
    *,
    workflow_run_id: str = "wf-1",
    tenant_id: str = "tenant-a",
    status: WorkflowStatus = WorkflowStatus.SUCCESS,
    final_state: dict | None = None,
    paused_state: dict | None = None,
    runtime: str | None = None,
    error: ErrorInfo | None = None,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=workflow_run_id,
        tenant_id=tenant_id,
        workflow="triage-flow",
        workflow_version="0.1.0",
        status=status,
        initial_state={"text": "seed"},
        final_state=final_state,
        paused_state=paused_state,
        runtime=runtime,
        error=error,
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fact_model_roundtrip() -> None:
    fact = _make_fact(fact_id="run:r-1")
    assert ObservabilityFact.model_validate(fact.model_dump()) == fact
    assert ObservabilityFact.model_validate_json(fact.model_dump_json()) == fact


@pytest.mark.unit
def test_fact_model_defaults() -> None:
    """The flattened scalars default to zero / empty — a fact built from a
    record with no metrics is valid, never an error (fail-soft posture)."""
    fact = ObservabilityFact(
        fact_id="run:r-min",
        kind="run",
        source_id="r-min",
        tenant_id="t",
        status="success",
        runtime="native",
    )
    assert fact.trace_id == ""
    assert fact.cost_usd == 0.0
    assert fact.tokens_in == 0
    assert fact.tokens_out == 0
    assert fact.latency_ms == 0
    assert fact.governance_effect is None
    assert fact.attributes == {}


# ---------------------------------------------------------------------------
# Storage — parametrized over InMemory + sqlite (+ postgres when configured)
# ---------------------------------------------------------------------------


async def test_save_and_list_roundtrip(storage) -> None:
    fact = _make_fact(fact_id="run:r-1")
    await storage.save_observability_fact(fact)
    rows = await storage.list_observability_facts(tenant_id="tenant-a")
    assert len(rows) == 1
    got = rows[0]
    assert got.fact_id == "run:r-1"
    assert got.trace_id == "trace-123"
    assert got.cost_usd == pytest.approx(0.0123)
    assert got.tokens_in == 10
    assert got.tokens_out == 20
    assert got.latency_ms == 350
    assert got.attributes == {"provider": "mock", "pricing_version": "v1"}


async def test_upsert_same_fact_id_is_one_row_with_updated_values(storage) -> None:
    """Same fact_id twice → ONE row carrying the second write's values
    (the pause→terminal transition and any backfill re-run depend on this)."""
    await storage.save_observability_fact(
        _make_fact(fact_id="workflow_run:wf-1", kind="workflow_run", status="paused")
    )
    await storage.save_observability_fact(
        _make_fact(
            fact_id="workflow_run:wf-1",
            kind="workflow_run",
            status="success",
            cost_usd=0.5,
        )
    )
    rows = await storage.list_observability_facts(tenant_id="tenant-a")
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].cost_usd == pytest.approx(0.5)


async def test_list_is_tenant_scoped(storage) -> None:
    await storage.save_observability_fact(_make_fact(fact_id="run:a", tenant_id="tenant-a"))
    await storage.save_observability_fact(_make_fact(fact_id="run:b", tenant_id="tenant-b"))
    rows = await storage.list_observability_facts(tenant_id="tenant-a")
    assert [f.fact_id for f in rows] == ["run:a"]


async def test_list_filters_and_limit(storage) -> None:
    now = datetime.now(UTC)
    await storage.save_observability_fact(
        _make_fact(fact_id="run:old", status="error", created_at=now - timedelta(days=2))
    )
    await storage.save_observability_fact(
        _make_fact(fact_id="run:new", created_at=now - timedelta(minutes=1))
    )
    await storage.save_observability_fact(
        _make_fact(fact_id="workflow_run:wf", kind="workflow_run", created_at=now)
    )

    by_kind = await storage.list_observability_facts(tenant_id="tenant-a", kind="workflow_run")
    assert [f.fact_id for f in by_kind] == ["workflow_run:wf"]

    by_workflow = await storage.list_observability_facts(tenant_id="tenant-a", workflow="demo-flow")
    assert [f.fact_id for f in by_workflow] == ["workflow_run:wf"]

    by_agent = await storage.list_observability_facts(tenant_id="tenant-a", agent="demo-agent")
    assert {f.fact_id for f in by_agent} == {"run:old", "run:new"}

    by_status = await storage.list_observability_facts(tenant_id="tenant-a", status="error")
    assert [f.fact_id for f in by_status] == ["run:old"]

    by_since = await storage.list_observability_facts(
        tenant_id="tenant-a", since=now - timedelta(hours=1)
    )
    assert {f.fact_id for f in by_since} == {"run:new", "workflow_run:wf"}

    # Newest-first ordering + limit.
    limited = await storage.list_observability_facts(tenant_id="tenant-a", limit=2)
    assert [f.fact_id for f in limited] == ["workflow_run:wf", "run:new"]


# ---------------------------------------------------------------------------
# Builders — mapping correctness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fact_from_run_record_flattens_metrics() -> None:
    record = _make_run_record(
        metrics=Metrics(
            latency_ms=420,
            tokens=TokenUsage(input=100, output=25),
            cost_usd=0.0042,
            provider="anthropic",
            pricing_version="2026-01",
            trace_id="tr-99",
        ),
        turns=[TurnRecord(index=1, model="claude-x")],
    )
    fact = fact_from_run_record(record)
    assert fact.fact_id == "run:run-1"
    assert fact.kind == "run"
    assert fact.source_id == "run-1"
    assert fact.trace_id == "tr-99"
    assert fact.tenant_id == "tenant-a"
    assert fact.workflow is None
    assert fact.agent == "demo-agent"
    assert fact.node_id == "step1"
    assert fact.status == "success"
    assert fact.runtime == "native"
    assert fact.cost_usd == pytest.approx(0.0042)
    assert fact.tokens_in == 100
    assert fact.tokens_out == 25
    assert fact.latency_ms == 420
    assert fact.governance_effect is None  # ADR 096 follow-up: projector unset
    assert fact.error_type is None
    assert fact.created_at == record.created_at
    assert fact.attributes == {
        "provider": "mock",
        "pricing_version": "2026-01",
        "model": "claude-x",
    }


@pytest.mark.unit
def test_fact_from_run_record_default_metrics_is_fail_soft() -> None:
    """A record with bare default Metrics() (tracing off, nothing computed)
    still maps cleanly: zeros + empty trace_id, never an exception."""
    record = _make_run_record(
        metrics=Metrics(),
        status=JobStatus.ERROR,
        error=ErrorInfo(type="provider_timeout", message="boom"),
    )
    fact = fact_from_run_record(record)
    assert fact.trace_id == ""
    assert fact.cost_usd == 0.0
    assert fact.tokens_in == 0
    assert fact.tokens_out == 0
    assert fact.latency_ms == 0
    assert fact.status == "error"
    assert fact.error_type == "provider_timeout"
    assert "model" not in fact.attributes  # no turns → no model claim


@pytest.mark.unit
def test_fact_from_workflow_run_maps_route_and_runtime() -> None:
    record = _make_workflow_run(
        final_state={"text": "x", "tier": "gold"},
        runtime="temporal",
    )
    fact = fact_from_workflow_run(record)
    assert fact.fact_id == "workflow_run:wf-1"
    assert fact.kind == "workflow_run"
    assert fact.source_id == "wf-1"
    assert fact.workflow == "triage-flow"
    assert fact.agent is None
    assert fact.status == "success"
    assert fact.runtime == "temporal"
    assert fact.route == "gold"
    assert fact.created_at == record.created_at


@pytest.mark.unit
def test_fact_from_workflow_run_route_fallback_and_defaults() -> None:
    # "route" key honored when "tier" is absent; runtime None ⇒ native.
    routed = fact_from_workflow_run(_make_workflow_run(final_state={"route": "fast-lane"}))
    assert routed.route == "fast-lane"
    assert routed.runtime == "native"

    # No route key anywhere ⇒ None; paused records read paused_state.
    paused = fact_from_workflow_run(
        _make_workflow_run(
            status=WorkflowStatus.PAUSED,
            paused_state={"text": "x", "tier": "silver"},
        )
    )
    assert paused.status == "paused"
    assert paused.route == "silver"

    plain = fact_from_workflow_run(_make_workflow_run(final_state={"text": "x"}))
    assert plain.route is None
    assert plain.error_type is None

    errored = fact_from_workflow_run(
        _make_workflow_run(
            status=WorkflowStatus.ERROR,
            error=ErrorInfo(type="node_failed", message="boom"),
        )
    )
    assert errored.error_type == "node_failed"


@pytest.mark.unit
async def test_write_fact_failsoft_swallows_storage_errors() -> None:
    class _ExplodingStorage(InMemoryStorage):
        async def save_observability_fact(self, fact: ObservabilityFact) -> None:
            raise RuntimeError("facts table missing")

    storage = _ExplodingStorage()
    # Must not raise — the ADR 096 D3 contract.
    await write_fact_failsoft(storage, _make_fact())


# ---------------------------------------------------------------------------
# Dispatch edge — fact written next to record_run_usage; fail-soft
# ---------------------------------------------------------------------------


@pytest.fixture
def scaffolded_agent(tmp_path: Path) -> Path:
    """Real on-disk agent named ``alpha`` so the registry can load it
    (mirrors tests/test_runtime_worker.py)."""
    cli_runner.invoke(
        cli_app,
        ["init", "--bare", "alpha", "-t", "default", "--target", str(tmp_path)],
        catch_exceptions=False,
    )
    return tmp_path


def _make_dispatch(storage: InMemoryStorage, agents_dir: Path) -> WorkerDispatch:
    from movate.runtime.registry import scan_agents  # noqa: PLC0415

    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )
    return WorkerDispatch(storage=storage, executor=executor, agents=scan_agents(agents_dir))


def _make_job(*, tenant_id: str = "tenant-a") -> JobRecord:
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="alpha",
        status=JobStatus.QUEUED,
        input={"text": "hi"},
    )


@pytest.mark.unit
async def test_dispatch_agent_writes_run_fact(
    scaffolded_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "hi"}')
    storage = InMemoryStorage()
    await storage.init()
    dispatch = _make_dispatch(storage, scaffolded_agent)
    job = _make_job()
    await storage.save_job(job)

    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS

    facts = await storage.list_observability_facts(tenant_id="tenant-a")
    assert len(facts) == 1
    fact = facts[0]
    assert fact.fact_id == f"run:{outcome.result_run_id}"
    assert fact.kind == "run"
    assert fact.agent == "alpha"
    assert fact.tenant_id == "tenant-a"
    assert fact.status == "success"
    # The flattened columns mirror the persisted record's metrics blob.
    record = next(r for r in storage.runs if r.run_id == outcome.result_run_id)
    assert fact.cost_usd == pytest.approx(record.metrics.cost_usd)
    assert fact.tokens_in == record.metrics.tokens.input
    assert fact.tokens_out == record.metrics.tokens.output


@pytest.mark.unit
async def test_dispatch_survives_fact_writer_failure(
    scaffolded_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 096 D3 — a raising save_observability_fact NEVER fails dispatch:
    the job still completes SUCCESS and the RunRecord is untouched."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "hi"}')
    storage = InMemoryStorage()
    await storage.init()

    async def _explode(fact: ObservabilityFact) -> None:
        raise RuntimeError("facts table missing")

    monkeypatch.setattr(storage, "save_observability_fact", _explode)

    dispatch = _make_dispatch(storage, scaffolded_agent)
    job = _make_job()
    await storage.save_job(job)

    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS
    assert outcome.error is None
    assert any(r.run_id == outcome.result_run_id for r in storage.runs)


# ---------------------------------------------------------------------------
# API — GET /api/v1/observability/facts
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(api_storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(api_storage))


@pytest.fixture
async def auth_setup(api_storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="facts-tests", scopes=list(ALL_SCOPES)
    )
    await api_storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


def test_facts_list_empty(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    r = client.get("/api/v1/observability/facts", headers=header)
    assert r.status_code == 200, r.text
    assert r.json() == {"facts": [], "count": 0}


def test_facts_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/observability/facts")
    assert r.status_code == 401


async def test_facts_list_is_tenant_scoped_and_filtered(
    client: TestClient, auth_setup, api_storage: InMemoryStorage
) -> None:
    header, tenant_id = auth_setup
    await api_storage.save_observability_fact(_make_fact(fact_id="run:mine", tenant_id=tenant_id))
    await api_storage.save_observability_fact(
        _make_fact(
            fact_id="workflow_run:mine-wf",
            kind="workflow_run",
            tenant_id=tenant_id,
            status="paused",
        )
    )
    await api_storage.save_observability_fact(
        _make_fact(fact_id="run:other", tenant_id="someone-else")
    )

    r = client.get("/api/v1/observability/facts", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    ids = {f["fact_id"] for f in body["facts"]}
    assert ids == {"run:mine", "workflow_run:mine-wf"}
    # tenant_id never crosses the wire (view drops it, like WorkflowRunView).
    assert all("tenant_id" not in f for f in body["facts"])

    r = client.get("/api/v1/observability/facts?kind=workflow_run", headers=header)
    assert [f["fact_id"] for f in r.json()["facts"]] == ["workflow_run:mine-wf"]

    r = client.get("/api/v1/observability/facts?status=paused", headers=header)
    assert [f["fact_id"] for f in r.json()["facts"]] == ["workflow_run:mine-wf"]

    r = client.get("/api/v1/observability/facts?agent=demo-agent", headers=header)
    assert [f["fact_id"] for f in r.json()["facts"]] == ["run:mine"]

    r = client.get("/api/v1/observability/facts?workflow=demo-flow", headers=header)
    assert [f["fact_id"] for f in r.json()["facts"]] == ["workflow_run:mine-wf"]

    since = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r = client.get("/api/v1/observability/facts", params={"since": since}, headers=header)
    assert r.json() == {"facts": [], "count": 0}


def test_facts_limit_is_capped_at_500(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    r = client.get("/api/v1/observability/facts?limit=501", headers=header)
    assert r.status_code == 422
    r = client.get("/api/v1/observability/facts?limit=500", headers=header)
    assert r.status_code == 200
