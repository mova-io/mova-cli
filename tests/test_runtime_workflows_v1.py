"""Tests for ``/api/v1/workflows`` — workflow API parity (ADR 037 D1).

Mirrors ``tests/test_runtime_agents_v1.py`` row-for-row. Covers:

* **Happy path — JSON body, individual multipart fields, zipped bundle.**
* **CRUD lifecycle**: create → list → get → update (with If-Match) →
  publish → revert → delete.
* **Versioning**: ``GET /versions`` returns history newest-first; the
  current is flagged ``is_current``; the published one is flagged
  ``is_published``.
* **Optimistic concurrency**: PUT with a stale ``If-Match`` returns 409.
* **Validation**: ``POST /validate`` runs the Pydantic+compiler path
  WITHOUT persisting; returns ``passed=true`` for a valid spec and
  ``passed=false`` with errors for a malformed one.
* **Tenant scoping**: a different tenant's bundle is invisible (404).
* **Auth / scope**: ``read`` for GET/validate, ``admin`` for mutating.
* **Hermetic**: ``InMemoryStorage`` + ``TestClient`` — no real DB / no
  network / no live server.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
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
def workflows_path(tmp_path: Path) -> Path:
    p = tmp_path / "workflows"
    p.mkdir()
    return p


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    # Some tests use both; declared here so build_app gets a sibling agents/
    # location, matching mdk serve's layout. Workflows don't reference agents
    # at the API edge so the dir contents are not material.
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, workflows_path: Path, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path, workflows_path=workflows_path))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    """Mint a fresh full-scope API key. Fresh tenant per test → hermetic."""
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="workflows-v1-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Canonical bundle helpers
# ---------------------------------------------------------------------------


def _workflow_yaml(
    *,
    name: str = "demo-workflow",
    version: str = "0.1.0",
    description: str = "A demo workflow",
) -> str:
    return yaml.safe_dump(
        {
            "api_version": "movate/v1",
            "kind": "Workflow",
            "name": name,
            "version": version,
            "description": description,
            "state_schema": "./schema/state.json",
            "entrypoint": "first",
            "tags": ["demo"],
            "nodes": [
                {"id": "first", "type": "human", "prompt": "approve?"},
            ],
            "edges": [],
        }
    )


_STATE_SCHEMA = json.dumps(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
    }
)


def _json_body(*, name: str = "demo-workflow", version: str = "0.1.0") -> dict[str, object]:
    return {
        "workflow_yaml": _workflow_yaml(name=name, version=version),
        "files": {"schema/state.json": _STATE_SCHEMA},
    }


def _zipped_bundle(*, name: str = "demo-workflow", prefix: str = "") -> bytes:
    buf = io.BytesIO()
    pre = f"{prefix}/" if prefix else ""
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{pre}workflow.yaml", _workflow_yaml(name=name))
        zf.writestr(f"{pre}schema/state.json", _STATE_SCHEMA)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Auth / scope
# ---------------------------------------------------------------------------


def test_unauthed_create_returns_401(client: TestClient) -> None:
    r = client.post("/api/v1/workflows/from-spec", json=_json_body())
    assert r.status_code == 401


def test_unauthed_list_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/workflows")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# CRUD happy path
# ---------------------------------------------------------------------------


def test_create_from_json_body_persists_canonical_layout(
    client: TestClient,
    workflows_path: Path,
    auth_header: dict[str, str],
) -> None:
    r = client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "demo-workflow"
    assert body["version"] == "0.1.0"
    assert body["workflow_dir"] == "demo-workflow"
    assert sorted(body["files_persisted"]) == [
        "schema/state.json",
        "workflow.yaml",
    ]
    # Files actually on disk under the canonical layout.
    workflow_dir = workflows_path / "demo-workflow"
    assert (workflow_dir / "workflow.yaml").is_file()
    assert (workflow_dir / "schema/state.json").is_file()
    # Registry write happened — published_version set, changed=True for a new bundle.
    assert body["published_version"] == "0.1.0"
    assert body["changed"] is True


def test_create_from_multipart_individual_fields(
    client: TestClient,
    workflows_path: Path,
    auth_header: dict[str, str],
) -> None:
    files = [
        ("workflow_yaml", ("workflow.yaml", _workflow_yaml().encode(), "application/x-yaml")),
        ("state_schema", ("state.json", _STATE_SCHEMA.encode(), "application/json")),
    ]
    r = client.post("/api/v1/workflows", files=files, headers=auth_header)
    assert r.status_code == 201, r.text
    workflow_dir = workflows_path / "demo-workflow"
    assert (workflow_dir / "workflow.yaml").is_file()
    assert (workflow_dir / "schema/state.json").is_file()


def test_create_from_zipped_bundle(
    client: TestClient,
    workflows_path: Path,
    auth_header: dict[str, str],
) -> None:
    bundle_bytes = _zipped_bundle(prefix="demo-workflow")
    files = [("bundle", ("demo-workflow.zip", bundle_bytes, "application/zip"))]
    r = client.post("/api/v1/workflows", files=files, headers=auth_header)
    assert r.status_code == 201, r.text
    workflow_dir = workflows_path / "demo-workflow"
    assert (workflow_dir / "workflow.yaml").is_file()


def test_create_returns_409_on_conflict(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    r1 = client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    assert r1.status_code == 201
    r2 = client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    assert r2.status_code == 409


def test_create_rejects_multiple_modes(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    """Supplying BOTH individual workflow_yaml fields AND a zipped bundle in
    one multipart request is a 400 — mirrors the agent endpoint contract."""
    files = [
        ("bundle", ("x.zip", _zipped_bundle(prefix="x"), "application/zip")),
        ("workflow_yaml", ("workflow.yaml", _workflow_yaml().encode(), "application/x-yaml")),
    ]
    r = client.post("/api/v1/workflows", files=files, headers=auth_header)
    assert r.status_code == 400, r.text


def test_create_rejects_malformed_yaml(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    body = {
        "workflow_yaml": "not: valid: yaml: ::",
        "files": {"schema/state.json": _STATE_SCHEMA},
    }
    r = client.post("/api/v1/workflows/from-spec", json=body, headers=auth_header)
    assert r.status_code == 422


def test_list_returns_created_workflow(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    r = client.get("/api/v1/workflows", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    item = body["workflows"][0]
    assert item["name"] == "demo-workflow"
    assert item["version"] == "0.1.0"
    assert item["description"] == "A demo workflow"
    assert item["tags"] == ["demo"]
    assert item["published_version"] is None  # not promoted yet


def test_get_returns_full_detail(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    r = client.get("/api/v1/workflows/demo-workflow", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "demo-workflow"
    assert body["version"] == "0.1.0"
    assert body["entrypoint"] == "first"
    assert body["state_schema_path"] == "./schema/state.json"
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["id"] == "first"
    assert "workflow.yaml" in body["files"]
    assert "schema/state.json" in body["files"]
    assert body["is_published"] is False


def test_get_missing_returns_404(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.get("/api/v1/workflows/ghost", headers=auth_header)
    assert r.status_code == 404


def test_get_with_version_pins(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    r = client.get("/api/v1/workflows/demo-workflow?version=0.1.0", headers=auth_header)
    assert r.status_code == 200
    r404 = client.get("/api/v1/workflows/demo-workflow?version=99.0.0", headers=auth_header)
    assert r404.status_code == 404


def test_list_versions_newest_first(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    # PUT a new version.
    body = _json_body(version="0.2.0")
    r = client.put("/api/v1/workflows/demo-workflow/from-spec", json=body, headers=auth_header)
    assert r.status_code == 200, r.text
    rv = client.get("/api/v1/workflows/demo-workflow/versions", headers=auth_header)
    assert rv.status_code == 200
    versions = rv.json()["versions"]
    assert [v["version"] for v in versions] == ["0.2.0", "0.1.0"]
    assert versions[0]["is_current"] is True
    assert versions[1]["is_current"] is False


# ---------------------------------------------------------------------------
# Update + If-Match optimistic concurrency
# ---------------------------------------------------------------------------


def test_update_replaces_in_place(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    body = _json_body(version="0.2.0")
    r = client.put("/api/v1/workflows/demo-workflow/from-spec", json=body, headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["previous_version"] == "0.1.0"
    assert r.json()["version"] == "0.2.0"


def test_update_404_when_workflow_does_not_exist(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    r = client.put(
        "/api/v1/workflows/ghost/from-spec",
        json=_json_body(name="ghost"),
        headers=auth_header,
    )
    assert r.status_code == 404


def test_update_rejects_name_mismatch(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    body = _json_body(name="different-name", version="0.2.0")
    r = client.put("/api/v1/workflows/demo-workflow/from-spec", json=body, headers=auth_header)
    assert r.status_code == 422


def test_update_with_matching_if_match_succeeds(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    body = _json_body(version="0.2.0")
    r = client.put(
        "/api/v1/workflows/demo-workflow/from-spec",
        json=body,
        headers={**auth_header, "If-Match": "0.1.0"},
    )
    assert r.status_code == 200, r.text


def test_update_with_stale_if_match_returns_409(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    """A wrong If-Match (caller thinks they're updating 0.0.5 but 0.1.0 is
    current) is the optimistic-concurrency 409 — mirrors agent PUT semantics."""
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    body = _json_body(version="0.2.0")
    r = client.put(
        "/api/v1/workflows/demo-workflow/from-spec",
        json=body,
        headers={**auth_header, "If-Match": "0.0.5"},
    )
    assert r.status_code == 409


def test_update_with_quoted_if_match_strips_etag_envelope(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    """ETag values arrive quoted with optional W/ prefix (RFC 7232) — we
    parse leniently so the matcher just sees the version."""
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    body = _json_body(version="0.2.0")
    r = client.put(
        "/api/v1/workflows/demo-workflow/from-spec",
        json=body,
        headers={**auth_header, "If-Match": 'W/"0.1.0"'},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


def test_validate_passes_for_valid_spec(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    r = client.post(
        "/api/v1/workflows/anything/validate/from-spec",
        json=_json_body(),
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True
    assert body["errors"] == []


def test_validate_fails_for_malformed_spec(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    bad_yaml = yaml.safe_dump(
        {
            "api_version": "movate/v1",
            "kind": "Workflow",
            "name": "bad-workflow",
            "version": "0.1.0",
            # missing state_schema + entrypoint → load_workflow_spec fails.
            "nodes": [],
            "edges": [],
        }
    )
    body = {
        "workflow_yaml": bad_yaml,
        "files": {"schema/state.json": _STATE_SCHEMA},
    }
    r = client.post(
        "/api/v1/workflows/anything/validate/from-spec",
        json=body,
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["passed"] is False
    assert len(out["errors"]) >= 1


def test_validate_on_disk_when_no_body_supplied(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    # No body / no multipart — validates the persisted bundle.
    r = client.post("/api/v1/workflows/demo-workflow/validate", headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["passed"] is True


# ---------------------------------------------------------------------------
# Publish + revert
# ---------------------------------------------------------------------------


def test_publish_promotes_latest_version(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    r = client.post("/api/v1/workflows/demo-workflow/publish", headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["published_version"] == "0.1.0"
    # versions endpoint reflects the promote.
    rv = client.get("/api/v1/workflows/demo-workflow/versions", headers=auth_header)
    assert rv.json()["versions"][0]["is_published"] is True


def test_publish_specific_version(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    client.put(
        "/api/v1/workflows/demo-workflow/from-spec",
        json=_json_body(version="0.2.0"),
        headers=auth_header,
    )
    # Promote v0.1.0 even though v0.2.0 is the latest — the "blessed != latest"
    # drift the front-end catalog renders.
    r = client.post(
        "/api/v1/workflows/demo-workflow/publish?version=0.1.0",
        headers=auth_header,
    )
    assert r.status_code == 200
    assert r.json()["published_version"] == "0.1.0"
    # List flagging.
    rl = client.get("/api/v1/workflows", headers=auth_header)
    item = rl.json()["workflows"][0]
    assert item["version"] == "0.2.0"  # latest
    assert item["published_version"] == "0.1.0"  # blessed (drift)


def test_publish_missing_version_404s(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    r = client.post(
        "/api/v1/workflows/demo-workflow/publish?version=99.0.0",
        headers=auth_header,
    )
    assert r.status_code == 404


def test_revert_re_publishes_target_forward(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    client.put(
        "/api/v1/workflows/demo-workflow/from-spec",
        json=_json_body(version="0.2.0"),
        headers=auth_header,
    )
    r = client.post(
        "/api/v1/workflows/demo-workflow/revert",
        json={"to_version": "0.1.0"},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reverted_from"] == "0.1.0"
    # New latest is a +revert.1 suffix.
    assert "+revert.1" in body["version"]
    # History is intact + the new latest is the revert row.
    rv = client.get("/api/v1/workflows/demo-workflow/versions", headers=auth_header)
    assert [v["version"] for v in rv.json()["versions"]] == [
        body["version"],
        "0.2.0",
        "0.1.0",
    ]


def test_revert_missing_version_404s(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    r = client.post(
        "/api/v1/workflows/demo-workflow/revert",
        json={"to_version": "ghost"},
        headers=auth_header,
    )
    assert r.status_code == 404


def test_revert_requires_target_version(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    # No body, no query param → the handler raises BAD_REQUEST(400). Sending
    # ``json={}`` instead would 422 at Pydantic because to_version is required
    # on the WorkflowRevertSubmission model.
    r = client.post("/api/v1/workflows/demo-workflow/revert", headers=auth_header)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_soft_deletes_bundle(
    client: TestClient,
    workflows_path: Path,
    auth_header: dict[str, str],
) -> None:
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    assert (workflows_path / "demo-workflow").is_dir()
    r = client.delete("/api/v1/workflows/demo-workflow", headers=auth_header)
    assert r.status_code == 200
    assert not (workflows_path / "demo-workflow").is_dir()
    # Soft-deleted (renamed); a sibling .deleted-... dir exists.
    siblings = list(workflows_path.iterdir())
    assert any(s.name.startswith(".deleted-demo-workflow-") for s in siblings)


def test_delete_404_when_missing(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.delete("/api/v1/workflows/ghost", headers=auth_header)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


def test_workflow_is_invisible_to_other_tenant(
    client: TestClient,
    storage: InMemoryStorage,
    auth_header: dict[str, str],
) -> None:
    """A different tenant's bundle is 404, NOT 403 — same no-leak contract
    as the agent endpoint."""
    client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=auth_header)
    # Mint a second tenant's API key.
    other = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="other-tenant",
        scopes=list(ALL_SCOPES),
    )

    async def _save() -> None:
        await storage.save_api_key(other.record)

    asyncio.get_event_loop().run_until_complete(_save())
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    r = client.get("/api/v1/workflows/demo-workflow", headers=other_header)
    assert r.status_code == 404
    rl = client.get("/api/v1/workflows", headers=other_header)
    assert rl.json()["count"] == 0


# ---------------------------------------------------------------------------
# Scope enforcement on individual routes
# ---------------------------------------------------------------------------


def test_read_scope_cannot_create(
    client: TestClient,
    storage: InMemoryStorage,
) -> None:
    """A read-only key is 403 on POST/PUT/DELETE/publish/revert."""
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="read-only",
        scopes=["read"],
    )

    async def _save() -> None:
        await storage.save_api_key(minted.record)

    asyncio.get_event_loop().run_until_complete(_save())
    h = {"Authorization": f"Bearer {minted.full_key}"}
    assert (
        client.post("/api/v1/workflows/from-spec", json=_json_body(), headers=h).status_code == 403
    )
    # Listing IS allowed.
    assert client.get("/api/v1/workflows", headers=h).status_code == 200
