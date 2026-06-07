"""POST /api/v1/runs/{run_id}/replay — run replay / time-travel (ADR 045 D13).

Re-executes a historical run's recorded input against a chosen agent version and
returns the original + replayed runs side-by-side. The agent stage is driven by
``MockProvider`` via ``?mock=true`` (the UNCHANGED Executor runs offline). The
original run is seeded directly into storage; the replay is a brand-new run.
Coverage: a full replay (original immutable, replayed persisted, ``changed``
flag), version selection (``?against=version:X`` 404 on a missing version),
unknown run → 404, and ``run`` scope enforcement.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.core.models import JobStatus, Metrics, RunRecord
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
def app(storage: InMemoryStorage, agents_path: Path):
    return build_app(storage, agents_path=agents_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="replay", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return minted.full_key, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: replay-demo
version: 0.1.0
description: demo agent for run replay
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""
_PROMPT = b"Reply to {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()
_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"message": {"type": "string"}}}
).encode()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_agent(client: TestClient, token: str) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text


async def _seed_run(storage: InMemoryStorage, tenant_id: str, *, output: dict) -> str:
    run_id = f"run-{uuid4().hex[:8]}"
    await storage.save_run(
        RunRecord(
            run_id=run_id,
            job_id=f"j-{run_id}",
            tenant_id=tenant_id,
            agent="replay-demo",
            agent_version="0.1.0",
            prompt_hash="h",
            provider="openai/gpt-4o-mini",
            provider_version="v1",
            pricing_version="2026-05",
            status=JobStatus.SUCCESS,
            input={"text": "turn the lights on"},
            output=output,
            metrics=Metrics(),
        )
    )
    return run_id


async def test_replay_full_turn(client, storage, auth_setup) -> None:
    """A replay re-runs the original input, leaves the original immutable, and
    returns both runs side-by-side with a ``changed`` flag."""
    token, tenant_id = auth_setup
    _create_agent(client, token)
    run_id = await _seed_run(storage, tenant_id, output={"message": "OLD answer"})

    r = client.post(
        f"/api/v1/runs/{run_id}/replay",
        params={"mock": "true"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Original is the seeded run, unchanged; replayed is a NEW run.
    assert body["original"]["run_id"] == run_id
    assert body["original"]["output"] == {"message": "OLD answer"}
    assert body["replayed"]["run_id"] != run_id
    assert body["replayed"]["input"] == {"text": "turn the lights on"}  # same input
    assert body["against"] == "published"
    assert isinstance(body["changed"], bool)
    # Economics delta surfaced (replayed minus original); present + numeric.
    assert isinstance(body["cost_delta_usd"], (int, float))
    assert isinstance(body["latency_delta_ms"], (int, float))

    # The original run is immutable — still present and unchanged in storage.
    again = await storage.get_run(run_id, tenant_id=tenant_id)
    assert again is not None
    assert again.output == {"message": "OLD answer"}


async def test_replay_unknown_run_is_404(client, auth_setup) -> None:
    token, _ = auth_setup
    _create_agent(client, token)
    r = client.post("/api/v1/runs/nope/replay", params={"mock": "true"}, headers=_auth(token))
    assert r.status_code == 404, r.text


async def test_replay_missing_version_is_404(client, storage, auth_setup) -> None:
    """``?against=version:X`` for a version that doesn't exist → 404 (no silent
    fall back to latest)."""
    token, tenant_id = auth_setup
    _create_agent(client, token)
    run_id = await _seed_run(storage, tenant_id, output={"message": "x"})
    r = client.post(
        f"/api/v1/runs/{run_id}/replay",
        params={"mock": "true", "against": "version:9.9.9"},
        headers=_auth(token),
    )
    assert r.status_code == 404, r.text


async def test_replay_requires_run_scope(client, storage, auth_setup) -> None:
    token, tenant_id = auth_setup
    _create_agent(client, token)
    run_id = await _seed_run(storage, tenant_id, output={"message": "x"})
    ro = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    r = client.post(
        f"/api/v1/runs/{run_id}/replay", params={"mock": "true"}, headers=_auth(ro.full_key)
    )
    assert r.status_code == 403, r.text
