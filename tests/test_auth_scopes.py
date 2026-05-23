"""Scope-based least-privilege authorization (ADR 013 L2 / D3).

Covers the headline new behavior end-to-end over the FastAPI runtime + the
core ``effective_scopes`` resolver + storage round-trip:

* a scopeless (legacy) key → read/run/eval succeed, an ADMIN endpoint 403s;
* a key minted with ``--scope admin`` (here: ``scopes=["admin"]``) → admin
  endpoint succeeds;
* ``require_scope`` 403s with a clear message when the scope is missing,
  allows when present;
* the legacy single ``scope == "fleet-admin"`` still grants admin (it now
  resolves to the full scope set);
* storage round-trips ``scopes`` (and a legacy null row reads as the
  default).

The opaque-key path is exercised through real HTTP requests; the
``effective_scopes`` rule is also asserted in isolation as a pure function.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import (
    ALL_SCOPES,
    LEGACY_DEFAULT_SCOPES,
    ApiKeyEnv,
    effective_scopes,
    mint_api_key,
)
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# A canonical agent.yaml bundle for the admin POST /agents path.
_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: scopes-demo
version: 0.1.0
description: scope test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: prompt.md
schema:
  input: {message: string}
  output: {answer: string}
"""
_PROMPT = b"You are a helpful agent. Reply in JSON with an answer field."
_INPUT_SCHEMA = b'{"type": "object", "properties": {"message": {"type": "string"}}}'
_OUTPUT_SCHEMA = b'{"type": "object", "properties": {"answer": {"type": "string"}}}'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage, tmp_path) -> TestClient:
    # agents_path so POST /api/v1/agents has somewhere to land.
    return TestClient(build_app(storage, agents_path=tmp_path))


async def _save_key(storage: InMemoryStorage, *, scopes=None, scope=None):
    """Mint + persist a key. ``scopes`` sets the new field; ``scope`` patches
    the LEGACY single-scope column (to exercise the fleet-admin back-compat).
    Returns the bearer header value."""
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="scope-test", scopes=scopes
    )
    record = minted.record
    if scope is not None:
        record = record.model_copy(update={"scope": scope})
    await storage.save_api_key(record)
    return f"Bearer {minted.full_key}", record


# ---------------------------------------------------------------------------
# effective_scopes — the read-time back-compat resolver (pure function)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEffectiveScopes:
    def test_null_scopes_resolve_to_legacy_default(self) -> None:
        minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
        # Scopeless mint → empty list on the record.
        assert minted.record.scopes == []
        assert effective_scopes(minted.record) == set(LEGACY_DEFAULT_SCOPES)

    def test_explicit_scopes_used_verbatim(self) -> None:
        minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=["admin", "read"])
        assert effective_scopes(minted.record) == {"admin", "read"}

    def test_legacy_fleet_admin_scope_expands_to_full_set(self) -> None:
        minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
        record = minted.record.model_copy(update={"scope": "fleet-admin"})
        # Empty `scopes` + legacy `scope == fleet-admin` → full set.
        assert effective_scopes(record) == set(ALL_SCOPES)

    def test_explicit_scopes_win_over_legacy_scope(self) -> None:
        minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=["read"])
        record = minted.record.model_copy(update={"scope": "fleet-admin"})
        # New field takes precedence — the legacy column is ignored.
        assert effective_scopes(record) == {"read"}


# ---------------------------------------------------------------------------
# Legacy default: read/run/eval succeed, admin 403s (the headline behavior)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLegacyDefaultGrant:
    async def test_scopeless_key_can_read(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=None)
        # GET /agents needs `read` — in the legacy default → 200.
        r = client.get("/agents", headers={"Authorization": header})
        assert r.status_code == 200, r.text

    async def test_scopeless_key_can_run(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=None)
        # POST /run needs `run` — in the legacy default → 202.
        r = client.post(
            "/run",
            json={"kind": "agent", "target": "scopes-demo", "input": {"message": "hi"}},
            headers={"Authorization": header},
        )
        assert r.status_code == 202, r.text

    async def test_scopeless_key_can_eval(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=None)
        # POST /api/v1/bench/{agent} needs `eval`. The agent doesn't exist
        # (404) but crucially it is NOT a 403 — the eval scope is present.
        r = client.post(
            "/api/v1/bench/scopes-demo",
            json={"input": {"message": "hi"}, "models": ["openai/gpt-4o-mini-2024-07-18"]},
            headers={"Authorization": header},
        )
        assert r.status_code != 403, r.text

    async def test_scopeless_key_403s_on_admin_endpoint(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=None)
        # POST /api/v1/auth/keys needs `admin` — NOT in the legacy default → 403.
        r = client.post(
            "/api/v1/auth/keys",
            json={"label": "nope"},
            headers={"Authorization": header},
        )
        assert r.status_code == 403, r.text
        # FastAPI wraps our envelope under ``detail``.
        err = r.json()["detail"]["error"]
        assert err["code"] == "forbidden"
        assert "admin" in err["message"]

    async def test_scopeless_key_403s_on_create_agent(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=None)
        files = [
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ]
        r = client.post("/api/v1/agents", files=files, headers={"Authorization": header})
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Admin scope grants admin; fleet-admin preserved
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminScope:
    async def test_admin_key_mints_a_key(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=["admin"])
        r = client.post(
            "/api/v1/auth/keys",
            json={"label": "ci-bot"},
            headers={"Authorization": header},
        )
        assert r.status_code == 201, r.text

    async def test_admin_key_creates_agent(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=["admin"])
        files = [
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ]
        r = client.post("/api/v1/agents", files=files, headers={"Authorization": header})
        assert r.status_code == 201, r.text

    async def test_admin_key_403s_when_read_endpoint_needs_read(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        # Flat model, no hierarchy: an admin-only key can't hit a read
        # endpoint. (Confirms require_scope is exact, not "admin implies all".)
        header, _ = await _save_key(storage, scopes=["admin"])
        r = client.get("/agents", headers={"Authorization": header})
        assert r.status_code == 403, r.text

    async def test_legacy_fleet_admin_key_still_admins(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        # A key with the LEGACY single scope="fleet-admin" (and no new
        # `scopes`) keeps full admin reach — back-compat preserved.
        header, _ = await _save_key(storage, scopes=None, scope="fleet-admin")
        r = client.post(
            "/api/v1/auth/keys",
            json={"label": "fleet-mints"},
            headers={"Authorization": header},
        )
        assert r.status_code == 201, r.text

    async def test_mint_with_unknown_scope_rejected(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=["admin"])
        r = client.post(
            "/api/v1/auth/keys",
            json={"label": "bad", "scopes": ["read", "superuser"]},
            headers={"Authorization": header},
        )
        assert r.status_code == 400, r.text
        assert "superuser" in r.json()["detail"]["error"]["message"]

    async def test_minted_key_carries_requested_scopes(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=["admin"])
        r = client.post(
            "/api/v1/auth/keys",
            json={"label": "scoped", "scopes": ["read", "run"]},
            headers={"Authorization": header},
        )
        assert r.status_code == 201, r.text
        new_key_id = r.json()["key_id"]
        record = await storage.get_api_key(new_key_id)
        assert record is not None
        assert sorted(record.scopes) == ["read", "run"]


# ---------------------------------------------------------------------------
# whoami surfaces resolved scopes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWhoamiScopes:
    async def test_whoami_reports_legacy_default(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=None)
        r = client.get("/api/v1/auth/me", headers={"Authorization": header})
        assert r.status_code == 200, r.text
        assert sorted(r.json()["scopes"]) == ["eval", "read", "run"]

    async def test_whoami_reports_explicit_scopes(
        self, client: TestClient, storage: InMemoryStorage
    ) -> None:
        header, _ = await _save_key(storage, scopes=["admin", "read"])
        r = client.get("/api/v1/auth/me", headers={"Authorization": header})
        assert r.status_code == 200, r.text
        assert sorted(r.json()["scopes"]) == ["admin", "read"]


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStorageRoundTrip:
    async def test_scopes_round_trip(self, storage: InMemoryStorage) -> None:
        minted = mint_api_key(
            tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=["read", "kb:write"]
        )
        await storage.save_api_key(minted.record)
        fetched = await storage.get_api_key(minted.record.key_id)
        assert fetched is not None
        assert sorted(fetched.scopes) == ["kb:write", "read"]

    async def test_legacy_null_row_reads_as_empty_then_defaults(
        self, storage: InMemoryStorage
    ) -> None:
        minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=None)
        await storage.save_api_key(minted.record)
        fetched = await storage.get_api_key(minted.record.key_id)
        assert fetched is not None
        assert fetched.scopes == []
        assert effective_scopes(fetched) == set(LEGACY_DEFAULT_SCOPES)
