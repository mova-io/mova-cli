"""Tests for ``GET /api/v1/agents/{name}/history`` — item 79.

GitHub commit-history endpoint. Per ADR 007, the runtime reads the
per-tenant repo's commit log filtered to one agent's directory. This
module tests the HTTP surface:

* 503 when the integration is disabled (``MDK_GITHUB_ENABLED`` unset)
* 503 when the runtime has no agents_path
* 404 when the agent doesn't exist on disk (check happens BEFORE
  calling GitHub, so a typo doesn't burn API budget)
* 200 happy path — calls the injected client + returns the right
  AgentHistoryView shape, including ``has_more`` heuristic
* 200 empty — newly-created agents return an empty commit list, not 404
* 502 when the upstream GitHub call fails
* 401 unauthed
* Query params propagate: ``?limit=N&page=N``

We inject a fake GitHubClient duck-typed against ``list_history`` so
no live network is touched. The integration layer's own contract
tests live in ``test_integrations_github.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.integrations.github import CommitInfo, GitHubError
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures + fake client
# ---------------------------------------------------------------------------


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
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="history-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: history-demo
version: 0.1.0
description: demo for history tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Hi {{ input.text }}\n"
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


@dataclass
class FakeGitHubClient:
    """Duck-typed stand-in exposing ``list_history``. Same pattern as
    the publish endpoint's test fixture."""

    rows: list[CommitInfo] = field(default_factory=list)
    raise_on_call: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def list_history(
        self,
        *,
        target_dir: str,
        limit: int = 50,
        page: int = 1,
    ) -> list[CommitInfo]:
        self.calls.append({"target_dir": target_dir, "limit": limit, "page": page})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.rows)


def _two_commits() -> list[CommitInfo]:
    return [
        CommitInfo(
            sha="abc" + "0" * 37,
            message="Tighten the system prompt",
            author_name="Deva",
            author_email="deva@movate.com",
            timestamp="2026-05-14T09:00:00Z",
            html_url=("https://github.com/acme/mova-io-agents-acme/commit/abc" + "0" * 37),
        ),
        CommitInfo(
            sha="def" + "0" * 37,
            message="Initial publish",
            author_name="Mova iO",
            author_email="noreply@mova-io.movate.com",
            timestamp="2026-05-13T18:00:00Z",
            html_url=("https://github.com/acme/mova-io-agents-acme/commit/def" + "0" * 37),
        ),
    ]


# ---------------------------------------------------------------------------
# 503 — disabled / no agents_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_503_when_integration_disabled(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """Default state — MDK_GITHUB_ENABLED unset, no client injected.
    503 with the config-pointer error code, same as publish."""
    auth_header, _ = auth_setup
    client = TestClient(build_app(storage, agents_path=agents_path))
    _create_agent(client, auth_header)
    r = client.get("/api/v1/agents/history-demo/history", headers=auth_header)
    assert r.status_code == 503, r.text
    payload = r.json()
    assert payload["detail"]["error"]["code"] == "agent_persistence_unavailable"
    assert "MDK_GITHUB_ENABLED" in payload["detail"]["error"]["message"]


@pytest.mark.asyncio
async def test_history_503_when_no_agents_path(storage: InMemoryStorage, auth_setup) -> None:
    auth_header, _ = auth_setup
    client = TestClient(
        build_app(storage, github_client=FakeGitHubClient())  # no agents_path
    )
    r = client.get("/api/v1/agents/anything/history", headers=auth_header)
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# 404 — agent not on disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_404_when_agent_missing(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """We check on-disk before calling GitHub so a typo doesn't burn
    API budget. ``list_history`` should never be invoked here."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient()
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    r = client.get("/api/v1/agents/ghost/history", headers=auth_header)
    assert r.status_code == 404
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 200 — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_happy_path(storage: InMemoryStorage, agents_path: Path, auth_setup) -> None:
    """Two commits → two-row AgentHistoryView with the right field
    shape + correct pagination metadata."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient(rows=_two_commits())
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.get("/api/v1/agents/history-demo/history", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "history-demo"
    assert body["page"] == 1
    assert body["limit"] == 50
    assert body["has_more"] is False  # 2 < 50, definitely no more pages
    assert len(body["commits"]) == 2

    first = body["commits"][0]
    assert first["sha"].startswith("abc")
    assert first["message"] == "Tighten the system prompt"
    assert first["author_name"] == "Deva"
    assert first["timestamp"] == "2026-05-14T09:00:00Z"
    assert first["html_url"].endswith(first["sha"])

    # The integration was called with the right target_dir.
    assert len(fake.calls) == 1
    assert fake.calls[0]["target_dir"] == "history-demo"
    assert fake.calls[0]["limit"] == 50
    assert fake.calls[0]["page"] == 1


@pytest.mark.asyncio
async def test_history_empty_when_never_published(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """A newly-created agent has no commits yet. Endpoint returns 200
    with empty commits list, NOT 404 — 'no history' is a valid state."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient(rows=[])  # never published
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.get("/api/v1/agents/history-demo/history", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["commits"] == []
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_history_query_params_propagate(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """?limit=N&page=N reach the integration call so the UI can page
    through long histories."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient(rows=_two_commits())
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.get(
        "/api/v1/agents/history-demo/history?limit=10&page=3",
        headers=auth_header,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["page"] == 3
    assert body["limit"] == 10
    assert fake.calls[0]["limit"] == 10
    assert fake.calls[0]["page"] == 3


@pytest.mark.asyncio
async def test_history_has_more_when_page_is_full(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """has_more=true when len(commits) == limit. The UI uses this to
    show a 'Load more' button. Heuristic, not a guarantee."""
    auth_header, _ = auth_setup
    # Build a 5-row commit list + ask for limit=5 → has_more should be true
    rows = [
        CommitInfo(
            sha=f"{i:040x}",
            message=f"commit {i}",
            author_name="x",
            author_email="x@x",
            timestamp="2026-05-14T00:00:00Z",
            html_url="https://github.com/x/x/commit/" + f"{i:040x}",
        )
        for i in range(5)
    ]
    fake = FakeGitHubClient(rows=rows)
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.get("/api/v1/agents/history-demo/history?limit=5", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["has_more"] is True
    assert len(body["commits"]) == 5


# ---------------------------------------------------------------------------
# 502 — upstream GitHub failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_502_when_github_returns_500(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """Integration raises GitHubError(status_code=502) → we surface as
    502 with the upstream_unavailable error code."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient(
        raise_on_call=GitHubError(
            "commits call failed (500)",
            status_code=502,
            upstream_status=500,
        )
    )
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.get("/api/v1/agents/history-demo/history", headers=auth_header)
    assert r.status_code == 502
    assert r.json()["detail"]["error"]["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# 401 — auth gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_requires_auth(storage: InMemoryStorage, agents_path: Path) -> None:
    client = TestClient(
        build_app(storage, agents_path=agents_path, github_client=FakeGitHubClient())
    )
    r = client.get("/api/v1/agents/anything/history")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# OpenAPI surface — same contract as publish: route appears in spec
# even when the integration is disabled, so client-gen tooling sees
# the typed signature before ops registers the GitHub App.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_route_appears_in_openapi_when_disabled(
    storage: InMemoryStorage, agents_path: Path
) -> None:
    client = TestClient(build_app(storage, agents_path=agents_path))
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/v1/agents/{name}/history" in paths
    assert "get" in paths["/api/v1/agents/{name}/history"]
