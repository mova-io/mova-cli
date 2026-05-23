"""HTTP runtime — ``POST /api/v1/agents/{name}/dataset/harvest`` (ADR 016 D1).

Covers:

* Harvest returns proposed cases from this tenant's runs by signal, with
  provenance (``source_run_id`` + the feedback signal + the prod output).
* thumbs-up → suggested ``expected``; thumbs-down → needs-review, no expected.
* **Proposed-not-applied:** the harvest endpoint does NOT create / modify the
  stored ``evals/dataset.jsonl`` (the human-review gate).
* Tenant isolation — another tenant's runs are never harvested.
* Scope gating — needs ``eval``; a key lacking it gets 403.
* 401 unauthenticated, 404 unknown agent, 400 unknown source.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import (
    FeedbackRecord,
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
    (p / "rag-qa").mkdir(parents=True)
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
def client_no_agents_path(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage, agents_path=None))


async def _mint(storage: InMemoryStorage, *, scopes: list[str]) -> tuple[str, str]:
    """Mint a key with the given scopes; return (tenant_id, bearer)."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="harvest-tests", scopes=scopes
    )
    await storage.save_api_key(minted.record)
    return tenant_id, f"Bearer {minted.full_key}"


def _make_run(*, tenant_id: str, agent: str = "rag-qa", question: str = "hi") -> RunRecord:
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
        status=JobStatus.SUCCESS,
        input={"question": question},
        output={"answer": "an answer"},
        metrics=Metrics(
            cost_usd=0.0001,
            latency_ms=100,
            tokens=TokenUsage(input=10, output=5),
            pricing_version="2024-09",
        ),
    )


def _feedback(*, run: RunRecord, score: int) -> FeedbackRecord:
    return FeedbackRecord(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        agent=run.agent,
        user_id="u1",
        score=score,
    )


_URL = "/api/v1/agents/rag-qa/dataset/harvest"


@pytest.mark.unit
async def test_harvest_returns_proposed_cases(client: TestClient, storage: InMemoryStorage) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["eval"])
    down = _make_run(tenant_id=tenant_id, question="bad")
    await storage.save_run(down)
    await storage.save_feedback(_feedback(run=down, score=-1))

    r = client.post(_URL, params={"source": "thumbs-down"}, headers={"Authorization": bearer})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_name"] == "rag-qa"
    assert body["source"] == "thumbs-down"
    assert body["proposed_count"] == 1
    assert body["needs_review_count"] == 1
    assert body["applied"] is False
    case = body["cases"][0]
    assert case["input"] == down.input
    assert case["expected"] is None  # thumbs-down asserts no expected
    assert case["needs_review"] is True
    assert case["provenance"]["source_run_id"] == down.run_id
    assert case["provenance"]["feedback_score"] == -1


@pytest.mark.unit
async def test_harvest_thumbs_up_suggests_expected(
    client: TestClient, storage: InMemoryStorage
) -> None:
    tenant_id, bearer = await _mint(storage, scopes=["eval"])
    up = _make_run(tenant_id=tenant_id, question="good")
    await storage.save_run(up)
    await storage.save_feedback(_feedback(run=up, score=1))

    r = client.post(_URL, params={"source": "thumbs-up"}, headers={"Authorization": bearer})
    assert r.status_code == 200, r.text
    case = r.json()["cases"][0]
    assert case["expected"] == up.output  # golden case
    assert case["needs_review"] is False


@pytest.mark.unit
async def test_harvest_does_not_modify_dataset(
    client: TestClient, storage: InMemoryStorage, agents_path: Path
) -> None:
    """Proposed-not-applied: harvesting must NOT write evals/dataset.jsonl."""
    tenant_id, bearer = await _mint(storage, scopes=["eval"])
    down = _make_run(tenant_id=tenant_id)
    await storage.save_run(down)
    await storage.save_feedback(_feedback(run=down, score=-1))

    dataset_path = agents_path / "rag-qa" / "evals" / "dataset.jsonl"
    assert not dataset_path.exists()

    r = client.post(_URL, params={"source": "thumbs-down"}, headers={"Authorization": bearer})
    assert r.status_code == 200, r.text
    # The harvest endpoint is read-only — the live dataset stays untouched.
    assert not dataset_path.exists()


@pytest.mark.unit
async def test_harvest_is_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant_id, bearer = await _mint(storage, scopes=["eval"])
    # A run belonging to a DIFFERENT tenant, same agent name.
    other = _make_run(tenant_id=uuid4().hex, question="theirs")
    await storage.save_run(other)
    await storage.save_feedback(_feedback(run=other, score=-1))

    r = client.post(_URL, params={"source": "thumbs-down"}, headers={"Authorization": bearer})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposed_count"] == 0  # never harvest another tenant's run


@pytest.mark.unit
async def test_harvest_requires_auth(client: TestClient) -> None:
    r = client.post(_URL, params={"source": "thumbs-down"})
    assert r.status_code == 401


@pytest.mark.unit
async def test_harvest_requires_eval_scope(client: TestClient, storage: InMemoryStorage) -> None:
    # A key with only ``read`` — lacks ``eval``.
    _, bearer = await _mint(storage, scopes=["read"])
    r = client.post(_URL, params={"source": "thumbs-down"}, headers={"Authorization": bearer})
    assert r.status_code == 403, r.text
    assert "eval" in r.json()["detail"]["error"]["message"]


@pytest.mark.unit
async def test_harvest_unknown_agent_404(client: TestClient, storage: InMemoryStorage) -> None:
    _, bearer = await _mint(storage, scopes=["eval"])
    r = client.post(
        "/api/v1/agents/nope/dataset/harvest",
        params={"source": "thumbs-down"},
        headers={"Authorization": bearer},
    )
    assert r.status_code == 404


@pytest.mark.unit
async def test_harvest_unknown_source_400(client: TestClient, storage: InMemoryStorage) -> None:
    _, bearer = await _mint(storage, scopes=["eval"])
    r = client.post(_URL, params={"source": "bogus"}, headers={"Authorization": bearer})
    assert r.status_code == 400


@pytest.mark.unit
async def test_harvest_no_agents_path_503(
    client_no_agents_path: TestClient, storage: InMemoryStorage
) -> None:
    _, bearer = await _mint(storage, scopes=["eval"])
    r = client_no_agents_path.post(
        _URL, params={"source": "thumbs-down"}, headers={"Authorization": bearer}
    )
    assert r.status_code == 503
