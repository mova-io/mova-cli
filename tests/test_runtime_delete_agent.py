"""Tests for ``DELETE /api/v1/agents/{name}`` (item 117).

Soft-delete: bundle gets renamed to ``.deleted-<name>-<timestamp>/``
sibling for operator-recoverable cleanup. The agent disappears from
``GET /agents`` immediately (registry refresh) but the bytes are
still on disk under the .deleted prefix.

Coverage:

* Happy path: DELETE returns 200 + AgentDeletedView; the agent dir
  becomes ``.deleted-<name>-<ts>/`` sibling.
* Registry refresh: post-DELETE, GET /agents doesn't list it.
* 404 on unknown agent.
* Re-create works after delete (same name; new ``.deleted-...``
  sibling created on second delete to avoid name collision).
* 401 unauthed.
* 503 when runtime has no agents_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
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
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="delete-agent-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: delete-me
version: 0.1.0
description: ephemeral for delete tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"hi {{ input.input }}\n"

_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"output": {"type": "string"}}, "required": ["output"]}
).encode()


def _create(client: TestClient, auth_header: dict[str, str]) -> None:
    """Land the delete-me agent in the registry via the multipart
    POST so we have something to delete."""
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_delete_moves_bundle_to_sibling(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """The bundle dir gets renamed to ``.deleted-<name>-<ts>/`` rather
    than rmtree'd — soft delete for the recovery window."""
    _create(client, auth_header)
    assert (agents_path / "delete-me").is_dir()

    r = client.delete("/api/v1/agents/delete-me", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "delete-me"
    assert body["deleted_dir"].startswith(".deleted-delete-me-")

    # Original dir is gone; sibling exists.
    assert not (agents_path / "delete-me").exists()
    deleted_dir = agents_path / body["deleted_dir"]
    assert deleted_dir.is_dir()
    # Bundle files survive the move (recoverable).
    assert (deleted_dir / "agent.yaml").exists()
    assert (deleted_dir / "prompt.md").exists()


def test_delete_refreshes_registry(client: TestClient, auth_header: dict[str, str]) -> None:
    """Post-DELETE the next GET /agents doesn't list the agent.
    Critical for the Mova iO UX where the user deletes from the
    catalog and expects the list to update immediately."""
    _create(client, auth_header)
    # Pre: agent is in the list
    r1 = client.get("/agents", headers=auth_header)
    assert any(a["name"] == "delete-me" for a in r1.json()["agents"])

    # Delete
    r2 = client.delete("/api/v1/agents/delete-me", headers=auth_header)
    assert r2.status_code == 200

    # Post: agent is gone from the list
    r3 = client.get("/agents", headers=auth_header)
    assert not any(a["name"] == "delete-me" for a in r3.json()["agents"])


def test_delete_then_recreate_same_name(
    client: TestClient, agents_path: Path, auth_header: dict[str, str]
) -> None:
    """Soft-delete + re-create + soft-delete-again under the same name
    works — the second delete gets its own timestamp suffix so the
    .deleted-* siblings don't collide."""
    _create(client, auth_header)
    r1 = client.delete("/api/v1/agents/delete-me", headers=auth_header)
    assert r1.status_code == 200
    first_deleted = r1.json()["deleted_dir"]

    # Re-create with same name → should succeed (the live name is free).
    _create(client, auth_header)
    assert (agents_path / "delete-me").is_dir()

    # Some time has to pass so the timestamp differs. Sleep a bit.
    import time as _time  # noqa: PLC0415

    _time.sleep(1.1)

    r2 = client.delete("/api/v1/agents/delete-me", headers=auth_header)
    assert r2.status_code == 200
    second_deleted = r2.json()["deleted_dir"]
    assert first_deleted != second_deleted
    # Both .deleted-* siblings exist on disk
    assert (agents_path / first_deleted).is_dir()
    assert (agents_path / second_deleted).is_dir()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_delete_unknown_returns_404(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.delete("/api/v1/agents/never-existed", headers=auth_header)
    assert r.status_code == 404


def test_delete_without_auth_returns_401(client: TestClient) -> None:
    r = client.delete("/api/v1/agents/delete-me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_without_agents_path_returns_503() -> None:
    """Defensive: runtime built without agents_path returns 503 with
    a clear error_code, same as the create endpoint."""
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="no-agents-path-test",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    # Build app without agents_path (deliberate test misconfiguration)
    client = TestClient(build_app(storage))

    r = client.delete(
        "/api/v1/agents/whatever",
        headers={"Authorization": f"Bearer {minted.full_key}"},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["code"] == "agent_persistence_unavailable"
