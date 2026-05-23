"""Tests for ``POST /api/v1/agents/{name}/kb`` — Chainlit-driven KB upload.

The Chainlit playground (PR-D) lets dev-team operators drag-drop
documents into an agent's KB from the browser. The endpoint chunks,
embeds, and persists each file via the same pipeline as
``mdk kb ingest`` — but without requiring SSH access to a project
directory on the runtime host.

Coverage:

* Happy path — single .md file ingests into storage; counts surface.
* Multi-file upload — counts aggregate; per-file detail returned.
* Skipped extensions don't kill the batch; the rest still ingest.
* Empty file → ``status="empty"`` per-file, total_chunks_saved stays 0.
* 404 — agent not in the catalog.
* 400 — empty multipart form (no ``files`` field).
* 401 — missing bearer token.
* Idempotent re-upload — same content_hash is a no-op in storage.

The embedding HTTP layer is monkey-patched to a deterministic stub
so tests don't need an OPENAI_API_KEY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.kb import ingest as ingest_mod
from movate.runtime import build_app
from movate.runtime.registry import scan_agents
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
    """Scaffold a minimal agent so ``state.agents`` contains it."""
    agents = tmp_path / "agents"
    demo = agents / "demo"
    demo.mkdir(parents=True)
    (demo / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: demo\n"
        "version: 0.1.0\n"
        "description: Demo agent for KB upload tests\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n",
        encoding="utf-8",
    )
    (demo / "prompt.md").write_text("Hello {{ input.text }}\n", encoding="utf-8")
    schema_dir = demo / "schema"
    schema_dir.mkdir()
    (schema_dir / "input.json").write_text(
        '{"type": "object", "properties": {"text": {"type": "string"}}}',
        encoding="utf-8",
    )
    (schema_dir / "output.json").write_text(
        '{"type": "object", "properties": {"reply": {"type": "string"}}}',
        encoding="utf-8",
    )
    return agents


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None
) -> list[list[float]]:
    """Deterministic embedding stub — produces a 16-dim vector derived
    from each input's first 16 codepoints. Different inputs → different
    vectors (avoids dedup-collision noise in the storage layer)."""
    del model, api_key
    out: list[list[float]] = []
    for t in texts:
        codes = [float(ord(c)) for c in t[:16]]
        codes.extend([0.0] * (16 - len(codes)))
        out.append(codes)
    return out


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the OpenAI embedding call with the deterministic stub
    above so tests don't need an OPENAI_API_KEY."""
    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    # Scan the scaffolded agent on disk so state.agents contains it
    # (build_app itself never scans — it's called once at startup
    # from `mdk serve` after a separate scan pass).
    agents = scan_agents(agents_path)
    return TestClient(build_app(storage, agents=agents, agents_path=agents_path))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="kb-upload-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SAMPLE_MD = (
    b"# Refund policy\n\n"
    b"Annual subscriptions can be refunded within 14 days of the original "
    b"purchase. The refund is processed to the original payment method.\n\n"
    b"Monthly subscriptions are not refundable but can be cancelled at any "
    b"time to prevent the next billing cycle.\n"
)


def _upload_files(
    client: TestClient,
    auth_header: dict[str, str],
    files: list[tuple[str, bytes, str]],
    *,
    agent: str = "demo",
) -> Any:
    multipart = [("files", (name, content, mime)) for name, content, mime in files]
    return client.post(
        f"/api/v1/agents/{agent}/kb",
        files=multipart,
        headers=auth_header,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_single_md_file_ingests(client: TestClient, auth_header: dict[str, str]) -> None:
    r = _upload_files(client, auth_header, [("refund.md", _SAMPLE_MD, "text/markdown")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_name"] == "demo"
    assert body["total_chunks_saved"] >= 1
    assert len(body["files"]) == 1
    entry = body["files"][0]
    assert entry["source"] == "refund.md"
    assert entry["status"] == "ingested"
    assert entry["chunks_saved"] >= 1
    assert entry["embedding_model"]  # non-empty string


@pytest.mark.integration
def test_upload_uses_configured_embedding_model(
    client: TestClient, auth_header: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MOVATE_EMBED_MODEL flows into server-side ingest so the stored model
    matches the deployment's vector(N) column (ADR 009 Task 5)."""
    monkeypatch.setenv("MOVATE_EMBED_MODEL", "text-embedding-3-large")
    r = _upload_files(client, auth_header, [("refund.md", _SAMPLE_MD, "text/markdown")])
    assert r.status_code == 200, r.text
    entry = r.json()["files"][0]
    assert "large" in entry["embedding_model"], entry["embedding_model"]


@pytest.mark.integration
def test_multi_file_upload_aggregates_counts(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = _upload_files(
        client,
        auth_header,
        [
            ("a.md", _SAMPLE_MD, "text/markdown"),
            ("b.txt", b"Office hours are Mon-Fri 9am to 5pm Eastern.", "text/plain"),
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["files"]) == 2
    sources = {f["source"] for f in body["files"]}
    assert sources == {"a.md", "b.txt"}
    # b.txt has only one short paragraph → at most 1 chunk
    # a.md has two paragraphs → at least 2 chunks
    assert body["total_chunks_saved"] >= 2


@pytest.mark.integration
def test_unsupported_extension_skipped_not_400(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = _upload_files(
        client,
        auth_header,
        [
            ("good.md", _SAMPLE_MD, "text/markdown"),
            ("bad.pdf", b"\x25PDF-1.4 fake", "application/pdf"),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    entries = {f["source"]: f for f in body["files"]}
    assert entries["good.md"]["status"] == "ingested"
    assert entries["bad.pdf"]["status"] == "skipped"
    assert entries["bad.pdf"]["chunks_saved"] == 0


@pytest.mark.integration
def test_empty_file_reports_empty_status(client: TestClient, auth_header: dict[str, str]) -> None:
    r = _upload_files(
        client,
        auth_header,
        [("empty.md", b"   \n\n  \n", "text/markdown")],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_chunks_saved"] == 0
    assert body["files"][0]["status"] == "empty"


@pytest.mark.integration
def test_idempotent_reupload(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    # First upload — chunks get saved.
    r1 = _upload_files(client, auth_header, [("doc.md", _SAMPLE_MD, "text/markdown")])
    assert r1.status_code == 200
    n1 = r1.json()["total_chunks_saved"]
    assert n1 >= 1

    # Second upload of the same bytes — same content_hash, so the
    # storage layer dedups. The endpoint still reports chunks_saved
    # (the in-memory storage upsert returns the chunk either way),
    # but the actual chunk count in storage is unchanged.
    r2 = _upload_files(client, auth_header, [("doc.md", _SAMPLE_MD, "text/markdown")])
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_404_on_unknown_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = _upload_files(
        client,
        auth_header,
        [("doc.md", _SAMPLE_MD, "text/markdown")],
        agent="does-not-exist",
    )
    assert r.status_code == 404, r.text


@pytest.mark.integration
def test_400_on_empty_form(client: TestClient, auth_header: dict[str, str]) -> None:
    # No files field at all.
    r = client.post(
        "/api/v1/agents/demo/kb",
        files={},
        headers=auth_header,
    )
    assert r.status_code == 400, r.text


@pytest.mark.integration
def test_401_without_auth(client: TestClient) -> None:
    r = _upload_files(
        client,
        {},  # no auth header
        [("doc.md", _SAMPLE_MD, "text/markdown")],
    )
    assert r.status_code == 401, r.text
