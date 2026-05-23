"""Tests for the bench endpoints — BACKLOG #64.

* POST /api/v1/bench/{agent} — enqueue a JobKind.BENCH job
* GET /api/v1/bench/{bench_id} — retrieve a completed comparison
* GET /api/v1/bench?agent=<name> — history list

Mirrors ``tests/test_runtime_evals_v1.py``. Unlike eval, bench has no
synchronous ``wait=true`` path — the POST always enqueues a job and
returns ``{job_id, bench_id, status: "queued"}``. The worker (exercised
in ``tests/test_dispatch_bench.py``) produces the BenchRecord. To test
the GET/list endpoints here we persist a BenchRecord directly through
the storage double, then assert the read path.

Coverage:

* POST enqueues a job → 202 + queued + job_id + bench_id; a JobRecord
  with kind=BENCH lands in storage.
* GET retrieves a tenant-scoped BenchRecord; unknown id → 404;
  other-tenant id → 404; unauthed → 401.
* GET list filters by agent and is tenant-scoped.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import BenchModelResult, BenchRecord, JobKind
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
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="bench-v1-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: bench-demo
version: 0.1.0
description: mock-bench target
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b'Respond with valid JSON: {"output": "<your answer>"}\n\nInput: {{ input.input }}\n'

_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"output": {"type": "string"}}, "required": ["output"]}
).encode()


def _create_agent(
    client: TestClient, auth_header: dict[str, str], *, name: str = "bench-demo"
) -> None:
    agent_yaml = _AGENT_YAML.replace(b"name: bench-demo", f"name: {name}".encode())
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", agent_yaml, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


def _make_record(
    *, tenant_id: str, agent: str = "bench-demo", created_at: datetime | None = None
) -> BenchRecord:
    return BenchRecord(
        bench_id=str(uuid4()),
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        input={"input": "hi"},
        judge_method=None,
        judge_provider=None,
        runs_per_model=1,
        gate_mode="mean",
        models=[
            BenchModelResult(
                provider="openai/gpt-4o-mini-2024-07-18",
                score=None,
                cost_mean_usd=0.0001,
                cost_total_usd=0.0001,
                latency_p50_ms=300,
                latency_p95_ms=300,
                error_count=0,
                sample_output={"output": "hi"},
            ),
        ],
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# POST — enqueue
# ---------------------------------------------------------------------------


def test_bench_kickoff_enqueues_job(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/bench/bench-demo",
        json={
            "models": ["openai/gpt-4o-mini-2024-07-18"],
            "input": {"input": "hi"},
            "mock": True,
        },
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["bench_id"]

    # A BENCH job landed in storage, scoped to the caller's tenant.
    jobs = storage.jobs
    assert len(jobs) == 1
    assert jobs[0].kind == JobKind.BENCH
    assert jobs[0].target == "bench-demo"
    assert jobs[0].tenant_id == tenant_id
    assert jobs[0].job_id == body["job_id"]
    # The pre-generated bench_id is threaded through the job input.
    assert jobs[0].input["bench_id"] == body["bench_id"]
    assert jobs[0].input["models"] == ["openai/gpt-4o-mini-2024-07-18"]


def test_bench_kickoff_unknown_agent_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/bench/never-existed",
        json={"models": ["openai/gpt-4o-mini-2024-07-18"], "input": {"input": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 404


def test_bench_kickoff_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/bench/bench-demo",
        json={"models": ["openai/gpt-4o-mini-2024-07-18"], "input": {"input": "hi"}},
    )
    assert r.status_code == 401


def test_bench_kickoff_requires_models(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/bench/bench-demo",
        json={"models": [], "input": {"input": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 422  # min_length=1 on models


# ---------------------------------------------------------------------------
# GET — retrieve
# ---------------------------------------------------------------------------


async def test_bench_get_returns_result(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    record = _make_record(tenant_id=tenant_id)
    await storage.save_bench(record)

    r = client.get(f"/api/v1/bench/{record.bench_id}", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bench_id"] == record.bench_id
    assert body["agent"] == "bench-demo"
    assert body["agent_version"] == "0.1.0"
    assert body["runs_per_model"] == 1
    assert body["gate_mode"] == "mean"
    assert body["judge_method"] is None
    assert len(body["models"]) == 1
    assert body["models"][0]["provider"] == "openai/gpt-4o-mini-2024-07-18"


def test_bench_get_unknown_id_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get(f"/api/v1/bench/{uuid4()}", headers=auth_header)
    assert r.status_code == 404


async def test_bench_get_other_tenant_returns_404(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """A record owned by another tenant is invisible (404, not 403)."""
    auth_header, _ = auth_setup
    other = _make_record(tenant_id="someone-else")
    await storage.save_bench(other)
    r = client.get(f"/api/v1/bench/{other.bench_id}", headers=auth_header)
    assert r.status_code == 404


def test_bench_get_without_auth_returns_401(client: TestClient) -> None:
    r = client.get(f"/api/v1/bench/{uuid4()}")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET — list
# ---------------------------------------------------------------------------


async def test_bench_list_filters_by_agent(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    foo = _make_record(tenant_id=tenant_id, agent="foo")
    bar = _make_record(tenant_id=tenant_id, agent="bar")
    await storage.save_bench(foo)
    await storage.save_bench(bar)

    r = client.get("/api/v1/bench?agent=foo", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["bench"][0]["bench_id"] == foo.bench_id
    assert body["bench"][0]["agent"] == "foo"


async def test_bench_list_newest_first_and_tenant_scoped(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    older = _make_record(tenant_id=tenant_id, created_at=datetime.now(UTC) - timedelta(seconds=10))
    newer = _make_record(tenant_id=tenant_id, created_at=datetime.now(UTC))
    other = _make_record(tenant_id="someone-else")
    await storage.save_bench(older)
    await storage.save_bench(newer)
    await storage.save_bench(other)

    r = client.get("/api/v1/bench", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    # other-tenant record excluded; newest-first ordering.
    assert body["count"] == 2
    assert [b["bench_id"] for b in body["bench"]] == [newer.bench_id, older.bench_id]


def test_bench_list_without_auth_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/bench")
    assert r.status_code == 401
