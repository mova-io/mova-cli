"""Tests for the read-only run-explain endpoint (BACKLOG #66).

* GET /api/v1/runs/{run_id}/explain — decision chain for a stored run.

Mirrors ``mdk explain <run_id> --json`` over HTTP. Tenant-scoped at the
storage layer: a cross-tenant id returns 404 (never 403) so the existence
of another tenant's run never leaks.

Coverage:

* Happy path: seed a run via storage → GET returns the chain (run id,
  agent, status, input, llm_call, output) scoped to the caller's tenant.
* ``?steps=true`` embeds the full skill_calls list; default returns a
  one-line skill_calls_hint instead.
* Unknown id → 404; another tenant's run → 404.
* 401 without a bearer token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import (
    ErrorInfo,
    JobStatus,
    Metrics,
    RunRecord,
    SkillCallRecord,
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
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="explain-v1-tests")
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


def _make_run(
    *,
    run_id: str,
    tenant_id: str,
    status: str = JobStatus.SUCCESS,
    output: dict | None = None,
    error: ErrorInfo | None = None,
    skill_calls: list[SkillCallRecord] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id="job-1",
        tenant_id=tenant_id,
        agent="faq-agent",
        agent_version="0.1.0",
        prompt_hash="abc123",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2026",
        status=status,
        input={"question": "What is the return policy?"},
        output=output,
        metrics=Metrics(
            latency_ms=42,
            cost_usd=0.000019,
            tokens=TokenUsage(input=312, output=87, cached_input=0),
            provider="openai/gpt-4o-mini-2024-07-18",
        ),
        error=error,
        created_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
        skill_calls=skill_calls or [],
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_explain_returns_chain(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await storage.save_run(
        _make_run(
            run_id="run-abc",
            tenant_id=tenant_id,
            output={"answer": "30 days"},
        )
    )

    r = client.get("/api/v1/runs/run-abc/explain", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == "run-abc"
    assert body["agent"] == "faq-agent"
    assert body["agent_version"] == "0.1.0"
    assert body["status"] == JobStatus.SUCCESS
    assert body["input"] == {"question": "What is the return policy?"}
    assert body["output"] == {"answer": "30 days"}
    assert body["error"] is None
    # LLM-call summary mirrors mdk explain --json.
    assert body["llm_call"]["model"] == "openai/gpt-4o-mini-2024-07-18"
    assert body["llm_call"]["tokens_in"] == 312
    assert body["llm_call"]["tokens_out"] == 87
    assert body["llm_call"]["latency_ms"] == 42
    # Default (no ?steps) → hint, no full list.
    assert body["skill_calls"] is None
    assert "no skill calls" in body["skill_calls_hint"].lower()


async def test_explain_error_run_includes_error(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await storage.save_run(
        _make_run(
            run_id="run-err",
            tenant_id=tenant_id,
            status=JobStatus.ERROR,
            output=None,
            error=ErrorInfo(type="timeout", message="call timed out", retryable=True),
        )
    )

    body = client.get("/api/v1/runs/run-err/explain", headers=auth_header).json()
    assert body["output"] is None
    assert body["error"]["type"] == "timeout"
    assert body["error"]["message"] == "call timed out"


async def test_explain_steps_true_includes_skill_calls(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await storage.save_run(
        _make_run(
            run_id="run-steps",
            tenant_id=tenant_id,
            output={"answer": "30 days"},
            skill_calls=[
                SkillCallRecord(
                    step=1,
                    skill="kb-vector-lookup",
                    input={"query": "return policy"},
                    output={"results": ["30 days"]},
                    latency_ms=123.4,
                )
            ],
        )
    )

    body = client.get("/api/v1/runs/run-steps/explain?steps=true", headers=auth_header).json()
    assert body["skill_calls_hint"] is None
    assert isinstance(body["skill_calls"], list)
    assert len(body["skill_calls"]) == 1
    assert body["skill_calls"][0]["skill"] == "kb-vector-lookup"

    # Same run, default (no steps) → hint instead of the list.
    default_body = client.get("/api/v1/runs/run-steps/explain", headers=auth_header).json()
    assert default_body["skill_calls"] is None
    assert "1 skill call" in default_body["skill_calls_hint"]


# ---------------------------------------------------------------------------
# Errors / tenant scoping
# ---------------------------------------------------------------------------


def test_explain_unknown_run_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get(f"/api/v1/runs/{uuid4().hex}/explain", headers=auth_header)
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"]["code"] == "not_found"


async def test_explain_other_tenant_run_returns_404(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """A run owned by a different tenant must 404 (never leak existence)."""
    auth_header, _ = auth_setup
    await storage.save_run(
        _make_run(
            run_id="run-other",
            tenant_id="some-other-tenant",
            output={"answer": "secret"},
        )
    )

    r = client.get("/api/v1/runs/run-other/explain", headers=auth_header)
    assert r.status_code == 404, r.text


def test_explain_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/runs/run-abc/explain").status_code == 401
