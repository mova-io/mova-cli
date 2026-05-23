"""Tests for ``POST /api/v1/agents/{name}/publish`` — item 78.

GitHub publish endpoint. Per ADR 007, the runtime pushes the canonical
bundle to a per-tenant GitHub repo as a single commit on the configured
default branch. This module tests the HTTP surface:

* 503 when the integration is disabled (``MDK_GITHUB_ENABLED`` unset)
* 503 when the runtime has no agents_path (operator misconfig)
* 404 when the agent doesn't exist on disk
* 200 happy path — calls the injected client + returns the right
  AgentPublishedView shape
* 502 when the upstream GitHub call fails (our integration surfaces
  GitHubError(status_code=502))
* 401 unauthed

We inject a fake GitHubClient duck-typed against ``publish_bundle`` so
no live network is touched. The integration layer's own contract tests
live in ``test_integrations_github.py``.
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
from movate.integrations.github import GitHubError, PublishResult
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
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="publish-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: publish-demo
version: 0.1.0
description: demo for publish tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Hi {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
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
    """Duck-typed stand-in for :class:`GitHubClient`.

    Records every ``publish_bundle`` call so tests can assert what was
    sent; configurable return value / raise to cover the happy + sad
    paths without touching httpx."""

    result: PublishResult | None = None
    raise_on_publish: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def publish_bundle(
        self,
        bundle_dir: Path,
        *,
        target_dir: str,
        message: str,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> PublishResult:
        self.calls.append(
            {
                "bundle_dir": str(bundle_dir),
                "target_dir": target_dir,
                "message": message,
                "author_name": author_name,
                "author_email": author_email,
            }
        )
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        if self.result is None:
            # Sensible default — keeps the test concise when the
            # assertion is on the call shape, not the response shape.
            return PublishResult(
                commit_sha="deadbeefcafef00d",
                commit_url=("https://github.com/acme/mova-io-agents-acme/commit/deadbeefcafef00d"),
                branch="main",
                files_changed=[f"{target_dir}/agent.yaml"],
            )
        return self.result


# ---------------------------------------------------------------------------
# 503 — disabled / no agents_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_503_when_integration_disabled(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """Default state — MDK_GITHUB_ENABLED unset, no client injected.
    The endpoint must NOT crash on import; it returns a clear 503 with
    a config-pointer error code."""
    auth_header, _ = auth_setup
    client = TestClient(build_app(storage, agents_path=agents_path))
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/publish-demo/publish",
        json={},
        headers=auth_header,
    )
    assert r.status_code == 503, r.text
    payload = r.json()
    assert payload["detail"]["error"]["code"] == "agent_persistence_unavailable"
    assert "MDK_GITHUB_ENABLED" in payload["detail"]["error"]["message"]


@pytest.mark.asyncio
async def test_publish_503_when_no_agents_path(storage: InMemoryStorage, auth_setup) -> None:
    """Runtime built without agents_path — publish endpoint advertises
    as 'unavailable' (same 503 the create/delete endpoints emit)."""
    auth_header, _ = auth_setup
    client = TestClient(
        build_app(storage, github_client=FakeGitHubClient())  # no agents_path
    )
    r = client.post(
        "/api/v1/agents/anything/publish",
        json={},
        headers=auth_header,
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# 404 — agent not on disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_404_when_agent_missing(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    auth_header, _ = auth_setup
    fake = FakeGitHubClient()
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    r = client.post(
        "/api/v1/agents/ghost/publish",
        json={},
        headers=auth_header,
    )
    assert r.status_code == 404, r.text
    assert fake.calls == []  # never reached the integration


# ---------------------------------------------------------------------------
# 200 happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_happy_path(storage: InMemoryStorage, agents_path: Path, auth_setup) -> None:
    """Successful publish — the endpoint calls the injected client with
    the right (bundle_dir, target_dir, message) tuple and returns the
    AgentPublishedView the Angular client expects."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient()
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.post(
        "/api/v1/agents/publish-demo/publish",
        json={
            "commit_message": "Tighten the system prompt",
            "author_name": "Deva",
            "author_email": "deva@movate.com",
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "publish-demo"
    assert body["commit_sha"] == "deadbeefcafef00d"
    assert body["commit_url"].endswith("/commit/deadbeefcafef00d")
    assert body["branch"] == "main"
    assert "publish-demo/agent.yaml" in body["files_changed"]

    # And the integration was called with the right args.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["target_dir"] == "publish-demo"
    assert call["message"] == "Tighten the system prompt"
    assert call["author_name"] == "Deva"
    assert call["author_email"] == "deva@movate.com"
    # bundle_dir should resolve under agents_path/<name>
    assert call["bundle_dir"].endswith("/agents/publish-demo")


@pytest.mark.asyncio
async def test_publish_default_commit_message(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """When the Angular UI omits ``commit_message``, the endpoint
    backfills ``"Update <agent-name>"`` — matches ADR 007 open
    question 4's v0.7 default."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient()
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.post(
        "/api/v1/agents/publish-demo/publish",
        json={},  # no commit_message
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    assert fake.calls[0]["message"] == "Update publish-demo"


# ---------------------------------------------------------------------------
# 502 — upstream GitHub failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_502_when_github_returns_500(
    storage: InMemoryStorage, agents_path: Path, auth_setup
) -> None:
    """If the integration raises GitHubError(status_code=502), we
    surface it as a 502 with the upstream_unavailable error code so
    the Angular client can show 'GitHub is having a moment, retry'."""
    auth_header, _ = auth_setup
    fake = FakeGitHubClient(
        raise_on_publish=GitHubError(
            "trees POST failed (500)", status_code=502, upstream_status=500
        )
    )
    client = TestClient(build_app(storage, agents_path=agents_path, github_client=fake))
    _create_agent(client, auth_header)

    r = client.post(
        "/api/v1/agents/publish-demo/publish",
        json={},
        headers=auth_header,
    )
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["error"]["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# 401 — auth gating (matches every other v1 endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_requires_auth(storage: InMemoryStorage, agents_path: Path) -> None:
    client = TestClient(
        build_app(storage, agents_path=agents_path, github_client=FakeGitHubClient())
    )
    r = client.post(
        "/api/v1/agents/anything/publish",
        json={},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# OpenAPI surface — even with the integration disabled, the route must
# advertise so the Angular client can generate against it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_route_appears_in_openapi_when_disabled(
    storage: InMemoryStorage, agents_path: Path
) -> None:
    """ADR 007 says: 'the runtime advertises the route in /openapi.json
    regardless [of the env flag] so the Angular client can generate
    against it before the integration goes live.'"""
    client = TestClient(build_app(storage, agents_path=agents_path))
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/v1/agents/{name}/publish" in paths
    assert "post" in paths["/api/v1/agents/{name}/publish"]
