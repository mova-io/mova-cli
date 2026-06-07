"""``POST /api/v1/agents/{name}/finetune`` — the async fine-tune kick-off (ADR 063).

The endpoint mirrors the eval kick-off: it creates a ``JobRecord(kind=FINETUNE)``
and returns 202 + ``job_id``. (The worker handler that *runs* the job — build
dataset → dispatch → register → eval-vs-base — is the ADR 063 rollout follow-up;
these tests pin the kick-off contract: route, 202, job persisted, auth, 404.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import JobKind
from movate.runtime import build_app
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    demo = agents / "demo"
    demo.mkdir(parents=True)
    (demo / "agent.yaml").write_text(
        "api_version: movate/v1\nkind: Agent\nname: demo\nversion: 0.1.0\n"
        "description: Demo\nmodel:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\nschema:\n  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n",
        encoding="utf-8",
    )
    (demo / "prompt.md").write_text("Hi {{ input.text }}\n", encoding="utf-8")
    sd = demo / "schema"
    sd.mkdir()
    (sd / "input.json").write_text('{"type":"object","properties":{"text":{"type":"string"}}}')
    (sd / "output.json").write_text('{"type":"object","properties":{"reply":{"type":"string"}}}')
    return agents


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents=scan_agents(agents_path), agents_path=agents_path))


@pytest.fixture
async def auth(storage: InMemoryStorage) -> tuple[dict[str, str], str]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="ft-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


@pytest.fixture
def auth_header(auth: tuple[dict[str, str], str]) -> dict[str, str]:
    return auth[0]


def test_kickoff_returns_202_and_queues_a_finetune_job(
    client: TestClient, storage: InMemoryStorage, auth: tuple[dict[str, str], str]
) -> None:
    auth_header, tenant_id = auth
    r = client.post(
        "/api/v1/agents/demo/finetune",
        json={"base_model": "openai/gpt-4o-mini", "min_score": 0.7},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]

    # A FINETUNE job was persisted with the config on it.
    job = asyncio.get_event_loop().run_until_complete(
        storage.get_job(body["job_id"], tenant_id=tenant_id)
    )
    assert job is not None
    assert job.kind is JobKind.FINETUNE
    assert job.target == "demo"
    assert job.input["base_model"] == "openai/gpt-4o-mini"
    assert job.input["min_score"] == 0.7
    assert job.input["promote_if_better"] is False  # default — no blind auto-promote


def test_kickoff_unknown_agent_404(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/nope/finetune",
        json={"base_model": "openai/gpt-4o-mini"},
        headers=auth_header,
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "not_found"


def test_kickoff_requires_auth(client: TestClient) -> None:
    r = client.post("/api/v1/agents/demo/finetune", json={"base_model": "openai/gpt-4o-mini"})
    assert r.status_code == 401


def test_kickoff_requires_base_model(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post("/api/v1/agents/demo/finetune", json={}, headers=auth_header)
    assert r.status_code == 422
