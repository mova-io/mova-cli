"""Tests for ``POST /api/v1/agents/{name}/runs?estimate=true`` — Cost Prediction.

The estimate endpoint returns a pre-flight cost + latency prediction and
executes NOTHING: no LLM call, no job enqueued, no charge. These tests pin:

* the contract — same route + same ``run`` scope as a real run, with the
  ``estimate`` query param documented in the OpenAPI schema;
* estimate mode returns a RunEstimateView (200) and enqueues NO job;
* tokens_in reflects the assembled prompt (longer input → bigger estimate);
* the historical-mean path vs the max_tokens fallback;
* latency present with history, ``unavailable`` without;
* budget_check reflects the agent's configured per-run budget;
* tenant scoping + the ``run`` write scope (read-only key is rejected);
* backward compatibility — a run without ``?estimate`` is unchanged (202).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
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
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="estimate-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


@pytest.fixture
async def read_only_auth(storage: InMemoryStorage):
    """A key with only the ``read`` scope — must be rejected by the
    estimate endpoint (it shares the ``run`` write scope)."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="ro", scopes=["read"]
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: est-demo
version: 0.1.0
description: demo for run estimate
model:
  provider: openai/gpt-4o-mini-2024-07-18
  params:
    max_tokens: 512
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
budget:
  max_cost_usd_per_run: 0.5
"""

_PROMPT = b"You answer questions.\n\nUser: {{ input.text }}\n"

_INPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
).encode()


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


def _save_history(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    output_tokens: int,
    latency_ms: int,
    n: int,
):
    return storage.save_run(
        RunRecord(
            run_id=f"run-{n}",
            job_id=f"job-{n}",
            tenant_id=tenant_id,
            agent="est-demo",
            agent_version="0.1.0",
            prompt_hash="hash",
            provider="openai/gpt-4o-mini-2024-07-18",
            provider_version="",
            pricing_version="test",
            status=JobStatus.SUCCESS,
            input={"text": "x"},
            output={"answer": "y"},
            metrics=Metrics(
                latency_ms=latency_ms,
                tokens=TokenUsage(input=100, output=output_tokens),
                cost_usd=0.001,
            ),
            created_at=datetime.now(UTC),
        )
    )


# ---------------------------------------------------------------------------
# Estimate mode: returns an estimate, executes nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimate_returns_200_estimate_view(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    r = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hello"}},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["estimate"] is True
    assert body["agent_name"] == "est-demo"
    assert body["model"] == "openai/gpt-4o-mini-2024-07-18"
    pred = body["predicted"]
    assert pred["tokens_in"] > 0
    assert pred["tokens_out_max"] == 512
    assert pred["cost_usd_min"] <= pred["cost_usd_expected"] <= pred["cost_usd_max"]
    assert "basis" in body and "budget_check" in body
    assert body["retrieval_embedded"] is False


@pytest.mark.asyncio
async def test_estimate_enqueues_no_job(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """The hard constraint: ?estimate=true NEVER enqueues a job."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)

    r = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hello"}},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text

    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert jobs == []
    runs = await storage.list_runs(tenant_id=tenant_id, limit=100)
    assert runs == []


@pytest.mark.asyncio
async def test_estimate_tokens_in_grows_with_prompt(
    client: TestClient, auth_setup
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    short = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    ).json()
    long = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "word " * 400}},
        headers=auth_header,
    ).json()

    assert long["predicted"]["tokens_in"] > short["predicted"]["tokens_in"]


# ---------------------------------------------------------------------------
# historical-mean vs max_tokens fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimate_max_tokens_fallback_without_history(
    client: TestClient, auth_setup
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    body = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    ).json()

    assert body["basis"]["out_expected_method"] == "max_tokens_fallback"
    assert body["predicted"]["tokens_out_expected"] == 512
    assert body["basis"]["sample_size"] == 0


@pytest.mark.asyncio
async def test_estimate_historical_mean_with_history(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    for i, out in enumerate((100, 200, 300)):
        await _save_history(
            storage, tenant_id=tenant_id, output_tokens=out, latency_ms=500, n=i
        )

    body = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    ).json()

    assert body["basis"]["out_expected_method"] == "historical_mean"
    assert body["predicted"]["tokens_out_expected"] == 200
    assert body["basis"]["sample_size"] == 3


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimate_latency_unavailable_without_history(
    client: TestClient, auth_setup
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    body = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    ).json()
    assert body["basis"]["latency_method"] == "unavailable"
    assert body["predicted"]["latency_ms_p50"] is None
    assert body["predicted"]["latency_ms_p95"] is None


@pytest.mark.asyncio
async def test_estimate_latency_present_with_history(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    for i, lat in enumerate((100, 300, 500, 700, 900)):
        await _save_history(
            storage, tenant_id=tenant_id, output_tokens=50, latency_ms=lat, n=i
        )

    body = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    ).json()

    assert body["basis"]["latency_method"] == "historical_p50p95"
    assert body["predicted"]["latency_ms_p50"] is not None
    assert body["predicted"]["latency_ms_p95"] is not None


# ---------------------------------------------------------------------------
# budget_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimate_budget_check_reflects_agent_budget(
    client: TestClient, auth_setup
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    body = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    ).json()
    bc = body["budget_check"]
    assert bc["per_run_budget_usd"] == 0.5
    assert bc["within_per_run_budget"] is True


# ---------------------------------------------------------------------------
# tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimate_history_is_tenant_scoped(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Another tenant's history must not inform this tenant's estimate."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    # Rich history under a DIFFERENT tenant.
    for i in range(5):
        await _save_history(
            storage, tenant_id="someone-else", output_tokens=42, latency_ms=123, n=i
        )

    body = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    ).json()

    assert body["basis"]["sample_size"] == 0
    assert body["basis"]["out_expected_method"] == "max_tokens_fallback"
    assert body["basis"]["latency_method"] == "unavailable"


# ---------------------------------------------------------------------------
# scope + auth + 404
# ---------------------------------------------------------------------------


def test_estimate_requires_run_scope(
    client: TestClient, read_only_auth
) -> None:
    """A read-only key is rejected — estimate shares the ``run`` write scope."""
    ro_header, _ = read_only_auth
    r = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=ro_header,
    )
    assert r.status_code == 403


def test_estimate_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/est-demo/runs?estimate=true",
        json={"input": {"text": "hi"}},
    )
    assert r.status_code == 401


def test_estimate_unknown_agent_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/agents/never-existed/runs?estimate=true",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# backward compatibility + contract
# ---------------------------------------------------------------------------


def test_run_without_estimate_is_unchanged(client: TestClient, auth_setup) -> None:
    """Backward-compat regression: no ?estimate → today's async path (202)."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/est-demo/runs",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert "estimate" not in body
    assert "predicted" not in body


def test_estimate_false_explicit_is_async(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/est-demo/runs?estimate=false",
        json={"input": {"text": "hi"}},
        headers=auth_header,
    )
    assert r.status_code == 202
    assert r.json()["status"] == "queued"


def test_estimate_param_documented_in_openapi(client: TestClient) -> None:
    """Contract: the ``estimate`` param is part of the documented schema for
    the same route + the route still gates on the ``run`` scope."""
    schema = client.get("/openapi.json").json()
    path = schema["paths"]["/api/v1/agents/{name}/runs"]["post"]
    param_names = {p["name"] for p in path.get("parameters", [])}
    assert "estimate" in param_names
    assert "estimate_retrieval" in param_names
