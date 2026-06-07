"""Tests for the unified agent-creation endpoint.

``POST /api/v1/projects/{project_id}/agents`` is an additive
convenience layer that routes to one of five existing creation
paths based on a discriminated-union body or a multipart upload.

Coverage:

* **Contract pin** — route + the five supported source shapes.
* **source: "bundle"** — multipart upload (same canonical path as
  ``POST /api/v1/agents``).
* **source: "spec"** — spec JSON → bundle bytes → persist.
* **source: "wizard"** — wizard form → bundle bytes → persist
  (same as ``POST /agents/from-wizard``).
* **source: "llm"** — 202 + SSE stream URL; the stream emits
  ``stage_skipped`` events because the scaffold-preview /
  eval-gen / judge-engineer / KB-ingest upstream PRs aren't on
  main yet.
* **source: "catalog"** — 503 when the catalog read API isn't
  deployed (graceful degrade); happy path verified through a
  monkey-patched storage method.
* **Failure modes** — unknown source, empty body, invalid JSON,
  tenant isolation, project-storage absent.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import Project
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
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="unified-create-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    # Seed the project the happy-path tests attach into. The endpoint now
    # validates the project exists — a typo'd ``project_id`` 404s instead of
    # silently attaching the agent to a phantom project — so ``proj-1`` must be
    # a real, tenant-owned project for the sync create paths to return 200.
    await storage.create_project(
        Project(
            project_id="proj-1",
            tenant_id=tenant_id,
            name="proj-1",
            owner_principal_id="api_key:test",
        )
    )
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Contract test — route + 5 source shapes pinned
# ---------------------------------------------------------------------------


def test_endpoint_is_registered_at_canonical_path(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Route lives at POST /api/v1/projects/{project_id}/agents.

    A truly missing route would 404; a present-but-unprocessed body
    would 422. We expect the latter — proves the route exists and
    accepts both the path param and the JSON body.
    """
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={"source": "nope"},
        headers=auth_header,
    )
    # 422 (unknown source) — proves the route exists.
    assert r.status_code in (400, 422), r.text


def test_unknown_source_returns_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={"source": "definitely-not-a-source"},
        headers=auth_header,
    )
    assert r.status_code in (400, 422)


def test_empty_body_returns_400(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        content="",
        headers={**auth_header, "content-type": "application/json"},
    )
    assert r.status_code == 400


def test_invalid_json_returns_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        content="not json{",
        headers={**auth_header, "content-type": "application/json"},
    )
    assert r.status_code == 422


def test_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={"source": "spec", "name": "x", "spec": {}, "prompt": "x"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# source: "spec"
# ---------------------------------------------------------------------------


def _spec_body(name: str = "faq-bot") -> dict:
    return {
        "source": "spec",
        "name": name,
        "spec": {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": name,
            "version": "0.1.0",
            "description": "A simple FAQ bot",
            "model": {"provider": "openai/gpt-4o-mini"},
            "prompt": "./prompt.md",
            "schema": {
                "input": {"input": "string"},
                "output": {"output": "string"},
            },
        },
        "prompt": "You are a helpful FAQ bot.",
    }


def test_spec_source_persists_canonical_bundle(
    client: TestClient,
    agents_path: Path,
    auth_header: dict[str, str],
) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json=_spec_body(),
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "spec"
    assert body["project_id"] == "proj-1"
    assert body["agent_name"] == "faq-bot"
    assert body["version"] == "0.1.0"
    assert sorted(body["files_persisted"]) == ["agent.yaml", "prompt.md"]
    # Bundle is on disk under the canonical path.
    on_disk_yaml = (agents_path / "faq-bot" / "agent.yaml").read_text()
    spec = yaml.safe_load(on_disk_yaml)
    assert spec["name"] == "faq-bot"
    assert spec["model"]["provider"] == "openai/gpt-4o-mini"
    # The projects-storage layer (ADR 040 / #549) is now part of the
    # integrated backend, so the freshly-persisted agent is attached to
    # the project rather than degrading to attached=False.
    assert body["attached"] is True


def test_spec_name_in_url_overrides_spec_name(
    client: TestClient,
    agents_path: Path,
    auth_header: dict[str, str],
) -> None:
    """When ``req.name`` differs from ``spec['name']``, the request's
    name wins — it's the canonical answer the URL/path promised."""
    body = _spec_body(name="renamed-bot")
    body["spec"]["name"] = "other-name"
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json=body,
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    assert r.json()["agent_name"] == "renamed-bot"


def test_spec_missing_required_field_returns_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={"source": "spec", "name": "x"},  # missing spec + prompt
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# source: "wizard"
# ---------------------------------------------------------------------------


def _wizard_body(name: str = "code-analyzer") -> dict:
    return {
        "source": "wizard",
        "wizard_form": {
            "name": name,
            "agent_prompt": "Analyze the code: {{ input.input }}",
            "ai_model": "openai/gpt-4o-mini",
        },
    }


def test_wizard_source_persists_via_existing_pipeline(
    client: TestClient,
    agents_path: Path,
    auth_header: dict[str, str],
) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json=_wizard_body(),
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "wizard"
    assert body["agent_name"] == "code-analyzer"
    assert (agents_path / "code-analyzer" / "agent.yaml").exists()


def test_wizard_form_malformed_returns_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={"source": "wizard", "wizard_form": {"name": "x"}},
        headers=auth_header,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# source: "bundle" (multipart)
# ---------------------------------------------------------------------------


def _bundle_yaml(name: str = "bundle-bot") -> str:
    return yaml.safe_dump(
        {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": name,
            "version": "0.1.0",
            "description": "Bundle test agent",
            "model": {"provider": "openai/gpt-4o-mini"},
            "prompt": "./prompt.md",
            "schema": {"input": {"input": "string"}, "output": {"output": "string"}},
        },
        sort_keys=False,
    )


def test_bundle_source_via_multipart(
    client: TestClient,
    agents_path: Path,
    auth_header: dict[str, str],
) -> None:
    # Use a zipped bundle so the inline-shorthand schema in agent.yaml
    # (the loader compiles them at validate-time) is enough — the
    # multipart "individual files" mode requires explicit schema files.
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, "w") as zf:
        zf.writestr("agent.yaml", _bundle_yaml())
        zf.writestr("prompt.md", "You are a helpful bot.")
    files = {
        "bundle": ("bundle.zip", zip_bytes.getvalue(), "application/zip"),
    }
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        files=files,
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "bundle"
    assert body["agent_name"] == "bundle-bot"
    assert (agents_path / "bundle-bot").is_dir()


# ---------------------------------------------------------------------------
# source: "llm" — async, SSE
# ---------------------------------------------------------------------------


def test_llm_source_returns_202_with_stream_url(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={
            "source": "llm",
            "description": "An agent that triages support tickets",
            "include_evals": True,
            "include_judge": True,
            "auto_seed_kb": True,
        },
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert "job_id" in body
    assert "stream_url" in body
    assert "status_url" in body
    assert "/projects/proj-1/agents/create-stream/" in body["stream_url"]


def test_llm_stream_emits_stage_skipped_when_upstream_missing(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Without scaffold-preview / eval-gen / judge / KB-ingest on main,
    the SSE stream surfaces ``stage_skipped`` events with a clear
    reason and terminates with ``error: upstream_unavailable``."""
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={
            "source": "llm",
            "description": "Triage support tickets",
            "include_evals": True,
        },
        headers=auth_header,
    )
    assert r.status_code == 202
    stream_url = r.json()["stream_url"]
    # The stream_url is absolute; the test client only handles relative URLs.
    # Strip the scheme + host to land on the same path on this client.
    path = urlparse(stream_url).path
    with client.stream("GET", path, headers=auth_header) as resp:
        assert resp.status_code == 200
        events = []
        for chunk in resp.iter_text():
            events.append(chunk)
        joined = "".join(events)
    # Scaffold stage starts, then is skipped.
    assert "stage_started" in joined
    assert "stage_skipped" in joined
    # Terminal error because scaffold is required.
    assert "upstream_unavailable" in joined


def test_llm_stream_404s_on_unknown_job_id(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.get(
        "/api/v1/projects/proj-1/agents/create-stream/no-such-job",
        headers=auth_header,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# source: "catalog" — graceful 503 when catalog read API absent
# ---------------------------------------------------------------------------


def test_catalog_source_503s_when_catalog_api_unavailable(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """The catalog read API ships in a parallel PR; until it lands,
    source=catalog returns 503 with a clear message."""
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={
            "source": "catalog",
            "slug": "support-ticket-triage",
            "rename_to": "acme-support-bot",
        },
        headers=auth_header,
    )
    assert r.status_code == 503
    assert "catalog" in r.json()["detail"]["error"]["message"].lower()


def test_catalog_source_clones_when_storage_supports_it(
    storage: InMemoryStorage,
    agents_path: Path,
    auth_header: dict[str, str],
) -> None:
    """When the storage backend implements ``get_catalog_entry``, the
    catalog source clones the bundle, applies the rename, and persists
    a NEW decoupled agent (no auto-sync, per ADR 041 D6)."""

    async def _fake_get_catalog_entry(*, slug: str, version: str | None, tenant_id: str) -> dict:
        return {
            "version": "2.1.0",
            "files": {
                "agent.yaml": _bundle_yaml("support-ticket-triage").encode(),
                "prompt.md": b"Triage the ticket.",
            },
        }

    storage.get_catalog_entry = _fake_get_catalog_entry  # type: ignore[attr-defined]
    client = TestClient(build_app(storage, agents_path=agents_path))

    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={
            "source": "catalog",
            "slug": "support-ticket-triage",
            "rename_to": "acme-support-bot",
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "catalog"
    assert body["agent_name"] == "acme-support-bot"
    # NEW agent on disk under the renamed name — decoupled from source.
    assert (agents_path / "acme-support-bot" / "agent.yaml").exists()
    spec = yaml.safe_load((agents_path / "acme-support-bot" / "agent.yaml").read_text())
    assert spec["name"] == "acme-support-bot"


def test_catalog_source_applies_overrides(
    storage: InMemoryStorage,
    agents_path: Path,
    auth_header: dict[str, str],
) -> None:
    """Overrides shallow-deep-merge into the cloned agent.yaml — the
    ``model`` block is replaced, the rest is preserved."""

    async def _fake_get_catalog_entry(*, slug: str, version: str | None, tenant_id: str) -> dict:
        return {
            "version": "2.1.0",
            "files": {
                "agent.yaml": _bundle_yaml("cat-bot").encode(),
                "prompt.md": b"X",
            },
        }

    storage.get_catalog_entry = _fake_get_catalog_entry  # type: ignore[attr-defined]
    client = TestClient(build_app(storage, agents_path=agents_path))

    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={
            "source": "catalog",
            "slug": "cat-bot",
            "overrides": {"model": {"provider": "anthropic/claude-sonnet"}},
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    spec = yaml.safe_load((agents_path / "cat-bot" / "agent.yaml").read_text())
    assert spec["model"]["provider"] == "anthropic/claude-sonnet"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_header_b(storage: InMemoryStorage) -> dict[str, str]:
    """Second auth header bound to a DIFFERENT tenant id from
    :func:`auth_header`. For tenant-isolation tests.
    """
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="tenant-b",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def test_llm_stream_404s_across_tenants(
    client: TestClient,
    auth_header: dict[str, str],
    auth_header_b: dict[str, str],
) -> None:
    """Tenant A's llm-create job_id is invisible to tenant B."""
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json={"source": "llm", "description": "test"},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    stream_url = r.json()["stream_url"]
    path = urlparse(stream_url).path
    # Tenant B tries to read tenant A's stream → 404.
    r2 = client.get(path, headers=auth_header_b)
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Failure modes — both multipart and JSON, no agents_path
# ---------------------------------------------------------------------------


def test_no_agents_path_returns_503(storage: InMemoryStorage, auth_header: dict[str, str]) -> None:
    """When build_app got no agents_path, the unified endpoint
    returns 503 — never silently writes to a default location."""
    client = TestClient(build_app(storage))
    r = client.post(
        "/api/v1/projects/p/agents",
        json=_spec_body(),
        headers=auth_header,
    )
    assert r.status_code == 503


def test_create_under_missing_project_returns_404(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """A non-existent ``project_id`` 404s instead of silently attaching the
    agent to a phantom project.

    Regression guard: the junction insert is ``INSERT OR IGNORE`` on every
    backend (no FK enforcement of ``project_id``), so before the existence
    check a typo'd id returned 200 / ``attached=true`` against a project that
    was never created. The endpoint now validates the project exists first.
    """
    r = client.post(
        "/api/v1/projects/prj_does_not_exist/agents",
        json=_spec_body(),
        headers=auth_header,
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"]["code"] == "not_found"


def test_create_under_other_tenants_project_returns_404(
    client: TestClient,
    storage: InMemoryStorage,
    auth_header: dict[str, str],
    auth_header_b: dict[str, str],
) -> None:
    """Tenant B cannot attach an agent into tenant A's project.

    The existence check is tenant-scoped (``get_project(tenant_id, ...)``), so
    a project that exists for another tenant is invisible — the create 404s
    rather than cross the tenant boundary (rule 6)."""
    # ``proj-1`` was seeded for tenant A (auth_header). Tenant B (auth_header_b)
    # must not be able to attach into it.
    r = client.post(
        "/api/v1/projects/proj-1/agents",
        json=_spec_body(name="b-bot"),
        headers=auth_header_b,
    )
    assert r.status_code == 404, r.text
