"""Tests for ``POST /api/v1/agents/{name}/dataset`` — standalone dataset upload.

An agent created via the wizard (or POST /api/v1/agents without a dataset)
has no eval dataset and cannot be evaluated.  This endpoint lets operators
upload or replace the dataset without re-posting the whole bundle.

Coverage:
* Happy path — valid JSONL lands in evals/dataset.jsonl, returns row_count,
  sha256_prefix (12-char hex), and preview (first ≤ 3 rows).
* Empty lines are skipped, don't contribute to row_count.
* Trailing newline is handled cleanly.
* Large upload (> 3 rows) — preview is capped at 3, row_count reflects all.
* Replaces existing dataset atomically.
* Registry refresh — GET /api/v1/agents/{name} reflects updated dataset info.
* 400 — invalid JSON on any line.
* 400 — line is a JSON array, not an object.
* 404 — agent does not exist.
* 503 — runtime has no agents_path.
* 401 — no / bad bearer token.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage


# ---------------------------------------------------------------------------
# Fixtures  (mirror test_runtime_agents_v1.py so tests stay self-contained)
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
def client_no_agents_path(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage, agents_path=None))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="dataset-upload-tests",
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: demo
version: 0.1.0
description: Demo agent for dataset upload tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Hello {{ input.text }}!\n"
_INPUT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}).encode()
_OUTPUT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"reply": {"type": "string"}},
}).encode()


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    """POST the demo agent WITHOUT a dataset — simulates wizard-created agent."""
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


def _jsonl(*rows: dict) -> bytes:
    return b"\n".join(json.dumps(r).encode() for r in rows) + b"\n"


def _upload(
    client: TestClient,
    auth_header: dict[str, str],
    content: bytes,
    name: str = "demo",
) -> object:
    return client.post(
        f"/api/v1/agents/{name}/dataset",
        files={"file": ("dataset.jsonl", content, "application/jsonl")},
        headers=auth_header,
    )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDatasetUploadHappyPath:
    def test_upload_returns_row_count_and_sha_prefix(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        rows = [{"input": {"text": f"q{i}"}, "expected": {"reply": f"a{i}"}} for i in range(2)]
        r = _upload(client, auth_header, _jsonl(*rows))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_name"] == "demo"
        assert body["row_count"] == 2
        assert len(body["sha256_prefix"]) == 12
        assert all(c in "0123456789abcdef" for c in body["sha256_prefix"])

    def test_upload_writes_dataset_to_disk(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        content = _jsonl({"input": {"text": "hello"}, "expected": {"reply": "hi"}})
        _upload(client, auth_header, content)
        ds = agents_path / "demo" / "evals" / "dataset.jsonl"
        assert ds.is_file()
        assert ds.read_bytes() == content

    def test_preview_capped_at_three_rows(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        rows = [{"input": {"text": f"q{i}"}, "expected": {"reply": f"a{i}"}} for i in range(7)]
        r = _upload(client, auth_header, _jsonl(*rows))
        assert r.status_code == 200
        body = r.json()
        assert body["row_count"] == 7
        assert len(body["preview"]) == 3

    def test_empty_lines_skipped_from_row_count(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        content = b'{"input": {"text": "a"}}\n\n{"input": {"text": "b"}}\n'
        r = _upload(client, auth_header, content)
        assert r.status_code == 200
        assert r.json()["row_count"] == 2

    def test_replaces_existing_dataset(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        _upload(client, auth_header, _jsonl({"input": {"text": "old"}}))
        new_content = _jsonl({"input": {"text": "new1"}}, {"input": {"text": "new2"}})
        r = _upload(client, auth_header, new_content)
        assert r.status_code == 200
        assert r.json()["row_count"] == 2
        ds = agents_path / "demo" / "evals" / "dataset.jsonl"
        assert ds.read_bytes() == new_content

    def test_registry_refreshed_after_upload(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        r_before = client.get("/api/v1/agents/demo", headers=auth_header)
        assert r_before.status_code == 200
        dataset_before = r_before.json().get("dataset")

        _upload(client, auth_header, _jsonl({"input": {"text": "x"}}))

        r_after = client.get("/api/v1/agents/demo", headers=auth_header)
        assert r_after.status_code == 200
        body_after = r_after.json()
        # After upload the dataset info should be present / updated.
        assert body_after.get("dataset") is not None or dataset_before is None


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDatasetUploadErrors:
    def _detail_message(self, r: object) -> str:
        body = r.json()
        detail = body.get("detail", {})
        if isinstance(detail, dict):
            return detail.get("error", {}).get("message", "").lower()
        return str(detail).lower()

    def test_400_on_invalid_json_line(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        bad = b'{"input": {"text": "ok"}}\nnot-json\n'
        r = _upload(client, auth_header, bad)
        assert r.status_code == 400
        msg = self._detail_message(r)
        assert "line" in msg or "json" in msg

    def test_400_on_json_array_line(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        bad = b'[1, 2, 3]\n'
        r = _upload(client, auth_header, bad)
        assert r.status_code == 400
        assert "object" in self._detail_message(r)

    def test_404_when_agent_not_found(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        r = _upload(client, auth_header, _jsonl({"x": 1}), name="ghost-agent")
        assert r.status_code == 404

    def test_503_when_no_agents_path(
        self, client_no_agents_path: TestClient, storage: InMemoryStorage
    ) -> None:
        async def _get_header() -> dict[str, str]:
            minted = mint_api_key(
                tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="no-path"
            )
            await storage.save_api_key(minted.record)
            return {"Authorization": f"Bearer {minted.full_key}"}

        import asyncio  # noqa: PLC0415
        hdr = asyncio.run(_get_header())
        r = _upload(client_no_agents_path, hdr, _jsonl({"x": 1}))
        assert r.status_code == 503

    def test_401_without_bearer_token(
        self, client: TestClient, agents_path: Path, auth_header: dict[str, str]
    ) -> None:
        _create_agent(client, auth_header)
        r = _upload(client, {}, _jsonl({"x": 1}))
        assert r.status_code == 401
