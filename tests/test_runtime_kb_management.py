"""Tests for the KB remote-management endpoints (Task 4).

The upload endpoint (``POST /api/v1/agents/{name}/kb``) already lets the
playground push documents into a deployed agent's KB. These four
companion endpoints let an operator MANAGE that KB from afar — the
remote twins of ``mdk kb list / stats / clear / search``:

* ``GET    /api/v1/agents/{name}/kb``        — list chunk metadata
* ``GET    /api/v1/agents/{name}/kb/stats``  — server-side aggregate
* ``DELETE /api/v1/agents/{name}/kb``        — delete chunks (?source=)
* ``POST   /api/v1/agents/{name}/kb/search`` — server-side embed + search

Coverage per endpoint: happy path, 401 (no auth), 404 (unknown agent),
tenant scoping (a second tenant never sees the first's chunks), and the
``?source=`` filter where applicable. Mirrors the TestClient +
InMemoryStorage + autouse embed-stub + auth_header fixture pattern from
``test_runtime_kb_upload.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.kb import ingest as ingest_mod
from movate.kb import search as search_mod
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
        "description: Demo agent for KB management tests\n"
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
    """Deterministic 16-dim embedding stub — different inputs → different
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
    """Stub the embedding call on BOTH the ingest path (for seeding) and
    the search path (for the server-side query embed) so tests need no
    OPENAI_API_KEY."""
    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)
    monkeypatch.setattr(search_mod, "embed_texts", _fake_embed)


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    agents = scan_agents(agents_path)
    return TestClient(build_app(storage, agents=agents, agents_path=agents_path))


async def _mint(storage: InMemoryStorage, *, tenant_id: str | None = None) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=tenant_id or uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="kb-mgmt-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    return await _mint(storage)


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


def _upload(
    client: TestClient,
    auth_header: dict[str, str],
    files: list[tuple[str, bytes, str]],
    *,
    agent: str = "demo",
) -> Any:
    multipart = [("files", (name, content, mime)) for name, content, mime in files]
    return client.post(f"/api/v1/agents/{agent}/kb", files=multipart, headers=auth_header)


def _seed(client: TestClient, auth_header: dict[str, str]) -> None:
    """Seed the demo agent's KB with two source files for the tenant
    behind ``auth_header``."""
    r = _upload(
        client,
        auth_header,
        [
            ("refund.md", _SAMPLE_MD, "text/markdown"),
            ("hours.txt", b"Office hours are Mon-Fri 9am to 5pm Eastern.", "text/plain"),
        ],
    )
    assert r.status_code == 200, r.text
    assert r.json()["total_chunks_saved"] >= 1


# ---------------------------------------------------------------------------
# GET /api/v1/agents/{name}/kb  — list
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_happy_path(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.get("/api/v1/agents/demo/kb", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_name"] == "demo"
    assert body["count"] == len(body["chunks"])
    assert body["count"] >= 2
    chunk = body["chunks"][0]
    # Vectors are omitted from the wire shape.
    assert "embedding" not in chunk
    assert chunk["text"]
    assert chunk["source"]
    assert chunk["embedding_model"]


@pytest.mark.integration
def test_list_source_filter(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.get("/api/v1/agents/demo/kb", params={"source": "refund.md"}, headers=auth_header)
    assert r.status_code == 200, r.text
    sources = {c["source"] for c in r.json()["chunks"]}
    assert sources == {"refund.md"}


@pytest.mark.integration
def test_list_limit_caps_rows(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.get("/api/v1/agents/demo/kb", params={"limit": 1}, headers=auth_header)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


@pytest.mark.integration
def test_list_401_without_auth(client: TestClient) -> None:
    assert client.get("/api/v1/agents/demo/kb").status_code == 401


@pytest.mark.integration
def test_list_404_unknown_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.get("/api/v1/agents/nope/kb", headers=auth_header)
    assert r.status_code == 404, r.text


@pytest.mark.integration
async def test_list_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    a = await _mint(storage)
    b = await _mint(storage)
    _seed(client, a)
    # Tenant B sees nothing — chunks are scoped to tenant A.
    r = client.get("/api/v1/agents/demo/kb", headers=b)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0


# ---------------------------------------------------------------------------
# GET /api/v1/agents/{name}/kb/stats  — aggregate
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_stats_happy_path(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.get("/api/v1/agents/demo/kb/stats", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_name"] == "demo"
    assert body["total_chunks"] >= 2
    assert body["total_chars"] > 0
    # Two source files seeded.
    assert {s["source"] for s in body["sources"]} == {"refund.md", "hours.txt"}
    # Per-source counts sum to the total.
    assert sum(s["chunks"] for s in body["sources"]) == body["total_chunks"]
    assert len(body["models"]) == 1  # single embedding model in the stub


@pytest.mark.integration
def test_stats_401_without_auth(client: TestClient) -> None:
    assert client.get("/api/v1/agents/demo/kb/stats").status_code == 401


@pytest.mark.integration
def test_stats_404_unknown_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    assert client.get("/api/v1/agents/nope/kb/stats", headers=auth_header).status_code == 404


@pytest.mark.integration
async def test_stats_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    a = await _mint(storage)
    b = await _mint(storage)
    _seed(client, a)
    r = client.get("/api/v1/agents/demo/kb/stats", headers=b)
    assert r.status_code == 200, r.text
    assert r.json()["total_chunks"] == 0


# ---------------------------------------------------------------------------
# DELETE /api/v1/agents/{name}/kb  — delete
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_delete_full_wipe(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    before = client.get("/api/v1/agents/demo/kb", headers=auth_header).json()["count"]
    assert before >= 2
    r = client.request("DELETE", "/api/v1/agents/demo/kb", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == before
    assert body["source"] is None
    # KB is now empty.
    assert client.get("/api/v1/agents/demo/kb", headers=auth_header).json()["count"] == 0


@pytest.mark.integration
def test_delete_source_filter(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.request(
        "DELETE", "/api/v1/agents/demo/kb", params={"source": "refund.md"}, headers=auth_header
    )
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "refund.md"
    # Only the other source survives.
    remaining = client.get("/api/v1/agents/demo/kb", headers=auth_header).json()
    assert {c["source"] for c in remaining["chunks"]} == {"hours.txt"}


@pytest.mark.integration
def test_delete_401_without_auth(client: TestClient) -> None:
    assert client.request("DELETE", "/api/v1/agents/demo/kb").status_code == 401


@pytest.mark.integration
def test_delete_404_unknown_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.request("DELETE", "/api/v1/agents/nope/kb", headers=auth_header)
    assert r.status_code == 404, r.text


@pytest.mark.integration
async def test_delete_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    a = await _mint(storage)
    b = await _mint(storage)
    _seed(client, a)
    # Tenant B's delete removes nothing from tenant A's KB.
    r = client.request("DELETE", "/api/v1/agents/demo/kb", headers=b)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 0
    assert client.get("/api/v1/agents/demo/kb", headers=a).json()["count"] >= 2


# ---------------------------------------------------------------------------
# POST /api/v1/agents/{name}/kb/search  — server-side embed + search
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_search_happy_path(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.post(
        "/api/v1/agents/demo/kb/search",
        json={"question": "Can I get a refund?", "k": 3},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_name"] == "demo"
    assert body["question"] == "Can I get a refund?"
    assert body["count"] == len(body["results"])
    assert body["count"] >= 1
    hit = body["results"][0]
    assert "embedding" not in hit  # vector omitted
    assert "score" in hit
    assert hit["text"]


@pytest.mark.integration
def test_search_hybrid_flag(client: TestClient, auth_header: dict[str, str]) -> None:
    _seed(client, auth_header)
    r = client.post(
        "/api/v1/agents/demo/kb/search",
        json={"question": "office hours", "k": 5, "hybrid": True},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    assert r.json()["count"] >= 1


@pytest.mark.integration
def test_search_401_without_auth(client: TestClient) -> None:
    r = client.post("/api/v1/agents/demo/kb/search", json={"question": "x"})
    assert r.status_code == 401


@pytest.mark.integration
def test_search_404_unknown_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post("/api/v1/agents/nope/kb/search", json={"question": "x"}, headers=auth_header)
    assert r.status_code == 404, r.text


@pytest.mark.integration
async def test_search_tenant_scoped(client: TestClient, storage: InMemoryStorage) -> None:
    a = await _mint(storage)
    b = await _mint(storage)
    _seed(client, a)
    # Tenant B searches the same agent but sees no results (empty KB).
    r = client.post(
        "/api/v1/agents/demo/kb/search",
        json={"question": "Can I get a refund?"},
        headers=b,
    )
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0
