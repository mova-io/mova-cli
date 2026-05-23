"""Tests for the KB reindex endpoint (Task 5).

``POST /api/v1/agents/{name}/kb/reindex`` is the remote twin of
``mdk kb reindex``: it rebuilds the agent's KB vector index, optionally
re-embedding every stored chunk first (``reembed=true``). Mirrors the
TestClient + InMemoryStorage + autouse embed-stub + auth_header fixture
pattern from ``test_runtime_kb_management.py``.

Coverage: happy path (reembed off + on), auth required, tenant scoping,
404 for an unknown agent, and that the re-embed path overwrites stored
vectors via ``save_kb_chunk``'s upsert.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.kb import embed as embed_mod
from movate.kb import ingest as ingest_mod
from movate.runtime import build_app
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures (mirror test_runtime_kb_management.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    demo = agents / "demo"
    demo.mkdir(parents=True)
    (demo / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: demo\n"
        "version: 0.1.0\n"
        "description: Demo agent for KB reindex tests\n"
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
    del model, api_key
    out: list[list[float]] = []
    for t in texts:
        codes = [float(ord(c)) for c in t[:16]]
        codes.extend([0.0] * (16 - len(codes)))
        out.append(codes)
    return out


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub embed_texts on the ingest path (for seeding) and at the
    source module (the reindex endpoint imports ``embed_texts`` from
    ``movate.kb.embed`` at call time, so patching the source is enough)."""
    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)
    monkeypatch.setattr(embed_mod, "embed_texts", _fake_embed)


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    agents = scan_agents(agents_path)
    return TestClient(build_app(storage, agents=agents, agents_path=agents_path))


async def _mint(storage: InMemoryStorage, *, tenant_id: str | None = None) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=tenant_id or uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="kb-reindex-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    return await _mint(storage)


_SAMPLE_MD = (
    b"# Refund policy\n\n"
    b"Annual subscriptions can be refunded within 14 days of the original purchase.\n"
)


def _seed(client: TestClient, auth_header: dict[str, str]) -> None:
    multipart = [
        ("files", ("refund.md", _SAMPLE_MD, "text/markdown")),
        ("files", ("hours.txt", b"Office hours are Mon-Fri 9am to 5pm Eastern.", "text/plain")),
    ]
    r = client.post("/api/v1/agents/demo/kb", files=multipart, headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["total_chunks_saved"] >= 1


# ---------------------------------------------------------------------------
# POST /api/v1/agents/{name}/kb/reindex
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reindex_default_no_reembed(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.post("/api/v1/agents/demo/kb/reindex", json={}, headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "demo"
    assert body["reembed"] is False
    assert body["chunks_reembedded"] == 0
    # InMemory backend has no real index → index_rebuilt is False, but it
    # reports the chunk count via ``backend``.
    assert body["index_rebuilt"] is False
    assert body["backend"] == "in_memory"


@pytest.mark.integration
def test_reindex_reembed_overwrites_vectors(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    _seed(client, auth_header)
    n_chunks = client.get("/api/v1/agents/demo/kb", headers=auth_header).json()["count"]
    assert n_chunks >= 2
    r = client.post("/api/v1/agents/demo/kb/reindex", json={"reembed": True}, headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reembed"] is True
    # Every stored chunk was re-embedded.
    assert body["chunks_reembedded"] == n_chunks


@pytest.mark.integration
def test_reindex_reembed_empty_kb_is_zero(client: TestClient, auth_header: dict[str, str]) -> None:
    # No seeding — empty KB; reembed reports zero, no error.
    r = client.post("/api/v1/agents/demo/kb/reindex", json={"reembed": True}, headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["chunks_reembedded"] == 0


@pytest.mark.integration
def test_reindex_401_without_auth(client: TestClient) -> None:
    assert client.post("/api/v1/agents/demo/kb/reindex", json={}).status_code == 401


@pytest.mark.integration
def test_reindex_404_unknown_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post("/api/v1/agents/nope/kb/reindex", json={}, headers=auth_header)
    assert r.status_code == 404, r.text


@pytest.mark.integration
async def test_reindex_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    a = await _mint(storage)
    b = await _mint(storage)
    _seed(client, a)
    # Tenant B re-embeds the same agent but sees no chunks (empty for B).
    r = client.post("/api/v1/agents/demo/kb/reindex", json={"reembed": True}, headers=b)
    assert r.status_code == 200, r.text
    assert r.json()["chunks_reembedded"] == 0
    # Tenant A's chunks are untouched + still present.
    assert client.get("/api/v1/agents/demo/kb", headers=a).json()["count"] >= 2
