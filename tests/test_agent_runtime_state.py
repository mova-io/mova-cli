"""Agent lifecycle state + control-plane API (ADR 090).

Covers the operational-state storage surface and the three control-plane
behaviours layered on it:

* ``PATCH /api/v1/agents/{name}/status`` sets active/deprecated/disabled
  (422 on a bad value, 404 on an unknown agent).
* ``GET /api/v1/agents`` overlays ``status`` on every item and filters on
  ``?status=`` — the default (no filter) still returns everything.
* ``GET /api/v1/agents/{name}/health`` reports derived health.
* A ``disabled`` agent rejects runs with 409 (active/deprecated run normally).

The storage round-trip is asserted against the in-memory double (the same
conformance contract the sqlite/postgres backends implement).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import AgentRuntimeState, AgentStatus
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures
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
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="adr090-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: demo
version: 0.1.0
description: ADR 090 control-plane test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""
_PROMPT = b"Echo: {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
).encode()
_OUTPUT_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
).encode()


def _create_demo_agent(client: TestClient, auth_header: dict[str, str]) -> None:
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
# Storage round-trip (in-memory double — the conformance contract)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_storage_state_roundtrip_and_isolation(storage: InMemoryStorage) -> None:
    # Absent ⇒ None (caller treats as ACTIVE).
    assert await storage.get_agent_state("faq", tenant_id="t1") is None
    await storage.set_agent_state(
        AgentRuntimeState(tenant_id="t1", name="faq", status=AgentStatus.DISABLED, note="retired")
    )
    st = await storage.get_agent_state("faq", tenant_id="t1")
    assert st is not None and st.status is AgentStatus.DISABLED and st.note == "retired"
    # Upsert overwrites (one current row per agent).
    await storage.set_agent_state(
        AgentRuntimeState(tenant_id="t1", name="faq", status=AgentStatus.ACTIVE)
    )
    st = await storage.get_agent_state("faq", tenant_id="t1")
    assert st is not None and st.status is AgentStatus.ACTIVE
    # Tenant isolation + listing.
    assert await storage.get_agent_state("faq", tenant_id="t2") is None
    assert [s.name for s in await storage.list_agent_states(tenant_id="t1")] == ["faq"]
    assert await storage.list_agent_states(tenant_id="t2") == []


# ---------------------------------------------------------------------------
# Control-plane endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_catalog_defaults_to_active_status(client: TestClient, auth_header: dict[str, str]) -> None:
    _create_demo_agent(client, auth_header)
    r = client.get("/api/v1/agents", headers=auth_header)
    assert r.status_code == 200, r.text
    item = next(a for a in r.json()["agents"] if a["name"] == "demo")
    # Additive, defaulted fields — back-compat: an agent with no state row.
    assert item["status"] == "active"
    assert item["health"] == "unknown"  # no ?health probe requested


@pytest.mark.unit
def test_patch_status_then_filter_and_overlay(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    _create_demo_agent(client, auth_header)
    r = client.patch(
        "/api/v1/agents/demo/status",
        json={"status": "deprecated", "note": "use demo-v2"},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "deprecated" and r.json()["note"] == "use demo-v2"

    # Overlay: the catalog now reports deprecated.
    item = next(
        a
        for a in client.get("/api/v1/agents", headers=auth_header).json()["agents"]
        if a["name"] == "demo"
    )
    assert item["status"] == "deprecated"

    # Filter: ?status=active excludes it; ?status=deprecated includes it.
    active = client.get("/api/v1/agents?status=active", headers=auth_header).json()
    assert all(a["name"] != "demo" for a in active["agents"])
    dep = client.get("/api/v1/agents?status=deprecated", headers=auth_header).json()
    assert any(a["name"] == "demo" for a in dep["agents"])


@pytest.mark.unit
def test_patch_invalid_status_422(client: TestClient, auth_header: dict[str, str]) -> None:
    _create_demo_agent(client, auth_header)
    r = client.patch("/api/v1/agents/demo/status", json={"status": "banana"}, headers=auth_header)
    assert r.status_code == 422, r.text


@pytest.mark.unit
def test_patch_unknown_agent_404(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.patch(
        "/api/v1/agents/ghost/status", json={"status": "disabled"}, headers=auth_header
    )
    assert r.status_code == 404, r.text


@pytest.mark.unit
def test_disabled_agent_run_409(client: TestClient, auth_header: dict[str, str]) -> None:
    _create_demo_agent(client, auth_header)
    # Active agent: a run is accepted (queued) — NOT a 409.
    ok = client.post(
        "/api/v1/agents/demo/runs", json={"input": {"text": "hi"}}, headers=auth_header
    )
    assert ok.status_code in (200, 202), ok.text

    # Disable → run rejected with 409 and a 'disabled' signal.
    client.patch("/api/v1/agents/demo/status", json={"status": "disabled"}, headers=auth_header)
    blocked = client.post(
        "/api/v1/agents/demo/runs", json={"input": {"text": "hi"}}, headers=auth_header
    )
    assert blocked.status_code == 409, blocked.text
    assert "disabled" in blocked.text.lower()

    # Re-enable → runs again (reversible, unlike delete).
    client.patch("/api/v1/agents/demo/status", json={"status": "active"}, headers=auth_header)
    again = client.post(
        "/api/v1/agents/demo/runs", json={"input": {"text": "hi"}}, headers=auth_header
    )
    assert again.status_code in (200, 202), again.text


@pytest.mark.unit
def test_health_endpoint_reports_healthy(client: TestClient, auth_header: dict[str, str]) -> None:
    _create_demo_agent(client, auth_header)
    r = client.get("/api/v1/agents/demo/health", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "demo"
    assert body["healthy"] is True
    assert body["status"] == "active"
    assert body["probed_run"] is False


@pytest.mark.unit
def test_health_unknown_agent_404(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.get("/api/v1/agents/ghost/health", headers=auth_header)
    assert r.status_code == 404, r.text
