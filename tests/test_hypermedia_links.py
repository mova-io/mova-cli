"""Hypermedia ``_links`` + uniform created-resource envelope (ADR 061).

Two layers:

* **Unit** — the pure link builders in :mod:`movate.runtime.hypermedia` produce
  the expected ``{rel: url}`` maps, and every linked path is a *real* route on a
  built app (the "no dead links" guard, ADR 061 D4).
* **Integration** — the core-flow create responses carry ``_links`` and the
  uniform ``id`` / ``created_at`` / ``etag`` envelope on the wire.

Self-contained (own app + auth); creates a real project before the agent so the
agent-create path works regardless of the project-existence check (ADR 061 is
orthogonal to that validation).
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.runtime.hypermedia import agent_links, kb_links, project_links, run_links
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Unit — builders + no-dead-links
# ---------------------------------------------------------------------------


def test_builders_shapes() -> None:
    assert project_links("prj_1")["self"] == "/api/v1/projects/prj_1"
    assert project_links("prj_1")["agents"] == "/api/v1/projects/prj_1/agents"
    a = agent_links("faq-bot")
    assert a["self"] == "/api/v1/agents/faq-bot"
    assert {"validate", "kb", "publish", "run", "versions"} <= set(a)
    assert kb_links("faq-bot")["search"] == "/api/v1/agents/faq-bot/kb/search"
    r = run_links("run-1", "faq-bot")
    assert r["self"] == "/api/v1/runs/run-1"
    assert r["agent"] == "/api/v1/agents/faq-bot"
    # agent rel omitted when the agent name is unknown.
    assert "agent" not in run_links("run-1", None)


def test_no_dead_links() -> None:
    """Every templated link target maps to a registered route (ADR 061 D4)."""
    app = build_app(InMemoryStorage())
    templates = {re_norm(r.path) for r in app.routes if isinstance(r, APIRoute)}

    sample = {
        **project_links("{x}"),
        **agent_links("{x}"),
        **kb_links("{x}"),
        **run_links("{x}", "{x}"),
    }
    for rel, url in sample.items():
        assert re_norm(url) in templates, f"dead link rel={rel!r} url={url!r}"


def re_norm(path: str) -> str:
    """Collapse every ``{param}`` / ``{x}`` segment to ``{}`` so a built link
    (with ids substituted) lines up with the route template."""
    return re.sub(r"\{[^}]*\}", "{}", path)


# ---------------------------------------------------------------------------
# Integration — the wire carries _links + the envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
async def client_and_auth(agents_path: Path):
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="links-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    client = TestClient(build_app(storage, agents_path=agents_path))
    return client, {"Authorization": f"Bearer {minted.full_key}"}


def _spec_body(name: str = "faq-bot") -> dict:
    return {
        "source": "spec",
        "name": name,
        "spec": {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": name,
            "version": "0.1.0",
            "description": "A FAQ bot",
            "model": {"provider": "openai/gpt-4o-mini"},
            "prompt": "./prompt.md",
            "schema": {"input": {"input": "string"}, "output": {"output": "string"}},
        },
        "prompt": "You are a FAQ bot.",
    }


def test_project_create_carries_links_and_id(client_and_auth) -> None:
    client, headers = client_and_auth
    r = client.post("/api/v1/projects", json={"name": "demo"}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    pid = body["project_id"]
    assert body["id"] == pid  # uniform envelope id == typed key
    links = body["_links"]
    assert links["self"] == f"/api/v1/projects/{pid}"
    assert links["agents"] == f"/api/v1/projects/{pid}/agents"


def test_agent_create_carries_links_and_envelope(client_and_auth) -> None:
    client, headers = client_and_auth
    pid = client.post("/api/v1/projects", json={"name": "demo"}, headers=headers).json()[
        "project_id"
    ]
    r = client.post(f"/api/v1/projects/{pid}/agents", json=_spec_body(), headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # Uniform envelope.
    assert body["id"] == "faq-bot"
    assert body["created_at"]  # present
    assert body["etag"]  # content-hash ETag present
    # Hypermedia: the build→ship→run path.
    links = body["_links"]
    assert links["self"] == "/api/v1/agents/faq-bot"
    assert links["kb"] == "/api/v1/agents/faq-bot/kb"
    assert links["run"] == "/api/v1/agents/faq-bot/runs"
