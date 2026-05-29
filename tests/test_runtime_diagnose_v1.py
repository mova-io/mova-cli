"""HTTP runtime — ``POST /api/v1/agents/{name}/diagnose`` + GET (ADR 043 D1).

Covers:

* The POST returns 202 with ``status="running"`` and a ``status_url``
  pointing to the GET endpoint.
* Empty-failures case ⇒ zero clusters, ``status="completed"``, zero cost.
* ``max_clusters`` is enforced on the persisted result.
* Tenant scoping — another tenant's diagnosis is NEVER returned (404,
  never 403).
* Scope gating — POST needs ``eval``; GET needs ``read``.
* The endpoint is READ-ONLY w.r.t. agent state — no agent bundle row is
  touched.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.providers.base import CompletionRequest, CompletionResponse
from movate.runtime import build_app
from movate.testing import InMemoryStorage


# A stub provider the in-process tests use instead of a real LiteLLM call.
# Injected into the runtime's diagnose path by monkey-patching the lazy
# ``LiteLLMProvider`` import the endpoint resolves at task-spawn time.
class _StubProvider:
    name = "stub"
    version = "0.0.1"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(text=self._reply, tokens=TokenUsage(input=10, output=20))

    def stream(self, request: CompletionRequest) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError


def _diagnose_reply(*, clusters: list[dict[str, Any]] | None = None) -> str:
    return json.dumps({"clusters": clusters or []})


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    (p / "rag-qa").mkdir(parents=True)
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


async def _mint(storage: InMemoryStorage, *, scopes: list[str]) -> tuple[str, str]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="diag-tests", scopes=scopes
    )
    await storage.save_api_key(minted.record)
    return tenant_id, f"Bearer {minted.full_key}"


def _make_failed_run(*, tenant_id: str, agent: str = "rag-qa") -> RunRecord:
    return RunRecord(
        run_id=f"run-{uuid4().hex[:12]}",
        job_id=f"job-{uuid4().hex[:12]}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="1.0.0",
        prompt_hash="deadbeef",
        provider="openai/gpt-4o-mini",
        provider_version="2024-09",
        pricing_version="2024-09",
        status=JobStatus.ERROR,
        input={"question": "refund?"},
        output=None,
        metrics=Metrics(
            cost_usd=0.0001,
            latency_ms=100,
            tokens=TokenUsage(input=10, output=5),
            pricing_version="2024-09",
        ),
        created_at=datetime.now(UTC),
    )


async def _wait_for_terminal(
    client: TestClient, bearer: str, diagnosis_id: str, *, attempts: int = 50
) -> dict[str, Any]:
    """Poll the GET endpoint until ``status != running`` (or attempts run out)."""
    for _ in range(attempts):
        r = client.get(f"/api/v1/diagnoses/{diagnosis_id}", headers={"Authorization": bearer})
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.02)
    pytest.fail("diagnose job never reached a terminal status")


_POST_URL = "/api/v1/agents/rag-qa/diagnose"


@pytest.mark.unit
async def test_post_returns_202_with_status_url(
    client: TestClient, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST returns 202 + a job_id + a status_url pointing at the GET endpoint."""
    _tenant_id, bearer = await _mint(storage, scopes=["eval", "read"])

    # No failures stored → the diagnoser sees an empty list → no LLM
    # call is made, so the stub doesn't matter for this assertion.
    stub = _StubProvider(reply=_diagnose_reply())
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", lambda *a, **kw: stub)

    r = client.post(_POST_URL, json={}, headers={"Authorization": bearer})

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["job_id"]
    assert body["status_url"] == f"/api/v1/diagnoses/{body['job_id']}"


@pytest.mark.unit
async def test_empty_failures_zero_clusters(
    client: TestClient, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent with no failures yields a completed diagnosis with 0 clusters."""
    _tenant_id, bearer = await _mint(storage, scopes=["eval", "read"])
    stub = _StubProvider(reply=_diagnose_reply())
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", lambda *a, **kw: stub)

    r = client.post(_POST_URL, json={"window_days": 7}, headers={"Authorization": bearer})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    body = await _wait_for_terminal(client, bearer, job_id)
    assert body["status"] == "completed"
    assert body["result"]["clusters"] == []
    assert body["result"]["input_summary"]["total_failures_examined"] == 0
    assert body["result"]["input_summary"]["clusters_identified"] == 0
    assert body["tokens_used"] == 0
    assert body["cost_usd"] == 0.0
    # Read-only contract: no LLM call when there's nothing to analyze.
    assert stub.calls == []


@pytest.mark.unit
async def test_diagnose_returns_typed_clusters(
    client: TestClient, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With failures + a canned LLM reply, the GET returns typed clusters."""
    tenant_id, bearer = await _mint(storage, scopes=["eval", "read"])
    for _ in range(3):
        await storage.save_run(_make_failed_run(tenant_id=tenant_id))

    reply = _diagnose_reply(
        clusters=[
            {
                "id": "cl1",
                "summary": "Agent rejects valid refund requests",
                "example_count": 3,
                "example_ids": [r.run_id for r in storage.runs][:2],
                "confidence": "high",
                "proposed_fix": {
                    "kind": "prompt_edit",
                    "payload": {
                        "before": "all refunds need approval",
                        "after": "refunds under $50 are auto-approved",
                        "patch_text": (
                            "@@\n- all refunds need approval\n+ refunds under $50 are auto-approved"
                        ),
                    },
                    "rationale": "policy mismatch with prompt",
                    "expected_improvement": {
                        "metric": "eval_pass_rate",
                        "delta": 0.04,
                        "based_on": "2 of 3 failed cases would pass",
                    },
                },
            }
        ]
    )
    stub = _StubProvider(reply=reply)
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", lambda *a, **kw: stub)

    r = client.post(
        _POST_URL,
        json={"min_failure_count": 1, "max_clusters": 5},
        headers={"Authorization": bearer},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    body = await _wait_for_terminal(client, bearer, job_id)
    assert body["status"] == "completed"
    assert body["kind"] == "diagnose"
    assert body["agent_name"] == "rag-qa"
    assert len(body["result"]["clusters"]) == 1
    cluster = body["result"]["clusters"][0]
    assert cluster["proposed_fix"]["kind"] == "prompt_edit"
    assert "diff" in cluster["proposed_fix"]
    assert cluster["proposed_fix"]["diff"]["after"].startswith("refunds under $50")
    # The LLM was actually called this time.
    assert len(stub.calls) == 1


@pytest.mark.unit
async def test_max_clusters_enforced(
    client: TestClient, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LLM that returns more clusters than ``max_clusters`` is capped."""
    tenant_id, bearer = await _mint(storage, scopes=["eval", "read"])
    await storage.save_run(_make_failed_run(tenant_id=tenant_id))

    reply = _diagnose_reply(
        clusters=[
            {
                "id": f"cl{i}",
                "summary": f"cluster {i}",
                "example_count": 1,
                "example_ids": [storage.runs[0].run_id],
                "confidence": "medium",
                "proposed_fix": {
                    "kind": "prompt_edit",
                    "payload": {"before": "x", "after": "y", "patch_text": "z"},
                    "rationale": "r",
                },
            }
            for i in range(6)
        ]
    )
    stub = _StubProvider(reply=reply)
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", lambda *a, **kw: stub)

    r = client.post(
        _POST_URL,
        json={"max_clusters": 2, "min_failure_count": 1},
        headers={"Authorization": bearer},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    body = await _wait_for_terminal(client, bearer, job_id)
    assert len(body["result"]["clusters"]) == 2


@pytest.mark.unit
async def test_get_is_tenant_scoped(
    client: TestClient, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second tenant cannot read the first tenant's diagnosis (404, not 403)."""
    _tenant_a, bearer_a = await _mint(storage, scopes=["eval", "read"])
    _tenant_b, bearer_b = await _mint(storage, scopes=["eval", "read"])
    stub = _StubProvider(reply=_diagnose_reply())
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", lambda *a, **kw: stub)

    r = client.post(_POST_URL, json={}, headers={"Authorization": bearer_a})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Tenant A can fetch it.
    r = client.get(f"/api/v1/diagnoses/{job_id}", headers={"Authorization": bearer_a})
    assert r.status_code == 200, r.text

    # Tenant B sees a 404 — no existence leak across tenants.
    r = client.get(f"/api/v1/diagnoses/{job_id}", headers={"Authorization": bearer_b})
    assert r.status_code == 404


@pytest.mark.unit
async def test_post_requires_eval_scope(client: TestClient, storage: InMemoryStorage) -> None:
    _, bearer = await _mint(storage, scopes=["read"])
    r = client.post(_POST_URL, json={}, headers={"Authorization": bearer})
    assert r.status_code == 403, r.text


@pytest.mark.unit
async def test_get_requires_read_scope(
    client: TestClient, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tenant_id, bearer = await _mint(storage, scopes=["eval", "read"])
    stub = _StubProvider(reply=_diagnose_reply())
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", lambda *a, **kw: stub)
    r = client.post(_POST_URL, json={}, headers={"Authorization": bearer})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # A key with neither read nor eval gets 403 on the GET.
    _, no_read_bearer = await _mint(storage, scopes=["kb:write"])
    r = client.get(f"/api/v1/diagnoses/{job_id}", headers={"Authorization": no_read_bearer})
    assert r.status_code in (403, 404)  # 403 on scope; 404 if tenant-scoped first


@pytest.mark.unit
async def test_post_requires_auth(client: TestClient) -> None:
    r = client.post(_POST_URL, json={})
    assert r.status_code == 401


@pytest.mark.unit
async def test_unknown_agent_404(client: TestClient, storage: InMemoryStorage) -> None:
    _, bearer = await _mint(storage, scopes=["eval"])
    r = client.post("/api/v1/agents/nope/diagnose", json={}, headers={"Authorization": bearer})
    assert r.status_code == 404


@pytest.mark.unit
async def test_diagnose_does_not_modify_agent(
    client: TestClient,
    storage: InMemoryStorage,
    agents_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only contract: a diagnose never writes prompt.md or any bundle row."""
    tenant_id, bearer = await _mint(storage, scopes=["eval", "read"])
    await storage.save_run(_make_failed_run(tenant_id=tenant_id))

    prompt_path = agents_path / "rag-qa" / "prompt.md"
    # Ensure it doesn't exist beforehand.
    assert not prompt_path.exists()
    # Ensure no agent_bundle row exists for this tenant.
    assert (await storage.get_agent_bundle("rag-qa", tenant_id=tenant_id)) is None

    stub = _StubProvider(
        reply=_diagnose_reply(
            clusters=[
                {
                    "id": "cl1",
                    "summary": "Looks fixable with a prompt edit",
                    "example_count": 1,
                    "example_ids": [storage.runs[0].run_id],
                    "confidence": "high",
                    "proposed_fix": {
                        "kind": "prompt_edit",
                        "payload": {
                            "before": "old",
                            "after": "new",
                            "patch_text": "diff",
                        },
                        "rationale": "tighten the prompt",
                    },
                }
            ]
        )
    )
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", lambda *a, **kw: stub)

    r = client.post(_POST_URL, json={"min_failure_count": 1}, headers={"Authorization": bearer})
    job_id = r.json()["job_id"]
    body = await _wait_for_terminal(client, bearer, job_id)

    assert body["status"] == "completed"
    # The agent's prompt.md was NEVER written — diagnose is read-only.
    assert not prompt_path.exists()
    # The agent registry row was NEVER written either.
    assert (await storage.get_agent_bundle("rag-qa", tenant_id=tenant_id)) is None
