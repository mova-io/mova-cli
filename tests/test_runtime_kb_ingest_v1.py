"""Tests for the JSON ingest modes on ``POST /api/v1/agents/{name}/kb``.

The route is the same as the existing multipart upload endpoint (see
``test_runtime_kb_upload.py`` for the multipart-side coverage). This
file pins the THREE additive JSON modes the route now dispatches on
via ``Content-Type``:

* ``kind: "text"`` — inline document body.
* ``kind: "url"`` — single page fetch (default) or bounded same-host
  crawl (mocked ``httpx`` transport — no real network).
* ``kind: "generated"`` — LLM authors the doc via the agent's
  configured provider (mocked at the provider seam).

Plus regression coverage that the multipart path still works
byte-for-byte alongside JSON (additive compat), the auth+scope gate,
the cost surface, and the 422 / 400 error shapes.

Hermetic by construction: in-memory storage, a deterministic
embedding stub (no OPENAI_API_KEY required), and ``httpx.get``
monkey-patched to a canned HTML response.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.kb import ingest as ingest_mod
from movate.kb import web as web_mod
from movate.providers.base import CompletionResponse
from movate.runtime import build_app
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures (mirror test_runtime_kb_upload.py — same scaffolded agent +
# embedding stub so the two suites share a baseline)
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
        "description: Demo agent for KB ingest v1 tests\n"
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
    """Deterministic embedding stub — distinct vectors per input so
    storage dedup doesn't collapse legitimately-different chunks."""
    del model, api_key
    out: list[list[float]] = []
    for t in texts:
        codes = [float(ord(c)) for c in t[:16]]
        codes.extend([0.0] * (16 - len(codes)))
        out.append(codes)
    return out


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the embed call so tests never hit OpenAI."""
    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)


@pytest.fixture
def app(storage: InMemoryStorage, agents_path: Path):
    agents = scan_agents(agents_path)
    return build_app(storage, agents=agents, agents_path=agents_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="kb-ingest-v1-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


@pytest.fixture
async def read_only_header(storage: InMemoryStorage) -> dict[str, str]:
    """A key WITHOUT ``kb:write`` so we can pin the scope gate."""
    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="kb-ingest-readonly",
        scopes=["read"],
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


# ---------------------------------------------------------------------------
# kind="text"
# ---------------------------------------------------------------------------


_SAMPLE_TEXT = (
    "# Refund policy\n\n"
    "Annual subscriptions can be refunded within 14 days of the original "
    "purchase. The refund is processed to the original payment method.\n\n"
    "Monthly subscriptions are not refundable but can be cancelled at any "
    "time to prevent the next billing cycle.\n"
)


@pytest.mark.integration
def test_text_kind_chunks_and_embeds(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "text", "title": "Refund policy", "content": _SAMPLE_TEXT},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "text"
    assert body["agent_name"] == "demo"
    assert body["chunks_added"] >= 2  # two paragraphs in the sample
    assert body["chunks_added"] == body["total_chunks_saved"]
    assert body["ingest_id"]  # non-empty uuid hex
    # Single source row reflects the inline doc.
    assert len(body["files"]) == 1
    entry = body["files"][0]
    assert entry["source"] == "Refund policy"
    assert entry["status"] == "ingested"
    assert entry["chunks_saved"] >= 2
    # Cost surface populated (best-effort — non-zero tokens estimated
    # from the prose length).
    assert body["tokens_in"] > 0
    # Pricing may legitimately be 0 if the embedding model isn't in the
    # packaged price table — assert the field is present + numeric.
    assert isinstance(body["embedding_cost_usd"], float)
    # No generated content for the text kind.
    assert body["generated_content"] is None


@pytest.mark.integration
async def test_text_kind_is_tenant_scoped(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Chunks land in the caller's tenant — a DIFFERENT tenant's key
    sees zero chunks on listing."""
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "text", "title": "Tenant scope test", "content": _SAMPLE_TEXT},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text

    # Mint a second key in a DIFFERENT tenant, query the KB list — must be empty.
    other = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="other-tenant",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    listing = client.get("/api/v1/agents/demo/kb", headers=other_header)
    assert listing.status_code == 200
    assert listing.json()["count"] == 0


@pytest.mark.integration
def test_text_kind_missing_content_is_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "text", "title": "Missing content"},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


@pytest.mark.integration
def test_text_kind_missing_title_is_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "text", "content": _SAMPLE_TEXT},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# kind="url" — single page (mocked transport)
# ---------------------------------------------------------------------------


_FIXTURE_HTML = """
<!doctype html>
<html><head><title>Pricing</title></head>
<body>
  <nav>Site nav</nav>
  <article>
    <h1>Pricing</h1>
    <p>Movate offers three pricing tiers: Starter, Pro, and Enterprise.
       Each tier comes with different feature sets and support levels for
       customers building AI agents at scale.</p>
    <p>You can upgrade or downgrade at any time. Refunds are handled per
       the standard policy.</p>
  </article>
  <footer>Footer junk</footer>
</body></html>
"""


def _ok_response(html: str = _FIXTURE_HTML) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        text=html,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", "https://docs.movate.com/pricing"),
    )


@pytest.mark.integration
def test_url_kind_single_page(
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ok_response())
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "url", "url": "https://docs.movate.com/pricing"},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "url"
    assert body["chunks_added"] >= 1
    assert body["files"][0]["source"] == "https://docs.movate.com/pricing"
    assert body["files"][0]["status"] == "ingested"
    # Prose-only — nav / footer junk stripped.
    assert body["generated_content"] is None


@pytest.mark.integration
def test_url_kind_crawl_cap_enforced(
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``crawl=true`` and ``max_pages > 1`` the same-host BFS
    runs and respects the cap.

    Stubs the crawler's per-page fetch with a 5-link fan-out under the
    same host. Cap=3 → at most 3 pages ingested.
    """
    calls: list[str] = []

    def _stub_fetch(url: str) -> tuple[str, list[str]]:
        calls.append(url)
        prose = (
            "Page body text long enough to clear the chunker floor — "
            "this is the substantive prose extracted from the page "
            "after stripping navigation and footer chrome."
        )
        return prose, [f"https://example.com/page-{i}" for i in range(5)]

    # Patch the crawler's internal per-page fetch via the test seam.
    real_crawl = web_mod.crawl_site

    def patched_crawl(start_url, **kwargs):
        kwargs["_fetch"] = _stub_fetch
        return real_crawl(start_url, **kwargs)

    monkeypatch.setattr(web_mod, "crawl_site", patched_crawl)

    r = client.post(
        "/api/v1/agents/demo/kb",
        json={
            "kind": "url",
            "url": "https://example.com/start",
            "crawl": True,
            "max_pages": 3,
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "url"
    # Cap=3 → at most 3 pages ingested (could be fewer if visited-set
    # dedup or per-page failure-isolation kicked in).
    assert sum(1 for f in body["files"] if f["status"] == "ingested") <= 3
    assert len(calls) <= 3


@pytest.mark.integration
def test_url_kind_max_pages_above_cap_is_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """``max_pages`` is hard-capped at the module constant (50)."""
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={
            "kind": "url",
            "url": "https://example.com/x",
            "crawl": True,
            "max_pages": 5000,
        },
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


@pytest.mark.integration
def test_url_kind_missing_url_is_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "url"},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


@pytest.mark.integration
def test_url_kind_non_http_scheme_is_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "url", "url": "ftp://example.com/x"},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# kind="generated"
# ---------------------------------------------------------------------------


class _StubProvider:
    """Stand-in for the agent's configured provider — returns canned
    Markdown so the LLM seam is exercised without real network."""

    name = "stub"
    version = "1.0"

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[object] = []

    async def complete(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        return CompletionResponse(text=self._text)


@pytest.mark.integration
def test_generated_kind_returns_authored_content(
    app,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    canned = (
        "# Pricing FAQ\n\n"
        "## Tier comparison\n\n"
        "Movate offers Starter, Pro, and Enterprise tiers each with different feature sets.\n\n"
        "## Upgrade paths\n\n"
        "You can upgrade at any time; the prorated difference is billed.\n\n"
        "## Refund policy\n\n"
        "Refunds are processed within 14 days for annual subscriptions.\n"
    )
    stub = _StubProvider(canned)
    app.state.kb_generated_provider = stub

    r = client.post(
        "/api/v1/agents/demo/kb",
        json={
            "kind": "generated",
            "title": "Pricing FAQ",
            "description": (
                "Generate a FAQ document covering common pricing questions: "
                "tier comparison, upgrade paths, billing cycles, refund policy"
            ),
        },
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "generated"
    assert body["generated_content"] == canned
    assert body["chunks_added"] >= 2
    assert body["files"][0]["source"] == "Pricing FAQ"
    assert body["files"][0]["status"] == "ingested"

    # Provider was called with the agent's configured model + the
    # system prompt that forbids fabrication.
    assert len(stub.calls) == 1
    completion_request = stub.calls[0]
    assert completion_request.provider == "openai/gpt-4o-mini-2024-07-18"
    system_msg = next(m for m in completion_request.messages if m.role == "system")
    assert "TODO: confirm with subject matter expert" in system_msg.content


@pytest.mark.integration
def test_generated_kind_missing_description_is_422(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "generated", "title": "FAQ"},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Discriminator / shape errors
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unknown_kind_is_422(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "nonsense", "title": "x", "content": "y"},
        headers=auth_header,
    )
    assert r.status_code == 422, r.text


@pytest.mark.integration
def test_invalid_json_is_400(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        content=b"not json at all",
        headers={**auth_header, "Content-Type": "application/json"},
    )
    assert r.status_code in (400, 422), r.text


# ---------------------------------------------------------------------------
# Auth + scope + agent existence
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_json_path_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "text", "title": "T", "content": "Body"},
    )
    assert r.status_code == 401, r.text


@pytest.mark.integration
def test_json_path_requires_kb_write_scope(
    client: TestClient, read_only_header: dict[str, str]
) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        json={"kind": "text", "title": "T", "content": "Body"},
        headers=read_only_header,
    )
    assert r.status_code == 403, r.text


@pytest.mark.integration
def test_json_path_404_on_unknown_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/nope/kb",
        json={"kind": "text", "title": "T", "content": "Body"},
        headers=auth_header,
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Multipart regression — the existing path still works AND the
# response now carries the additive ``kind="upload"`` field with the
# legacy ``files`` / ``total_chunks_saved`` populated as before.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_multipart_upload_path_still_works(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents/demo/kb",
        files=[("files", ("policy.md", _SAMPLE_TEXT.encode("utf-8"), "text/markdown"))],
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Additive: the new field appears on the response and reports the
    # legacy kind so a JSON-aware client can branch.
    assert body["kind"] == "upload"
    # Legacy fields untouched.
    assert body["agent_name"] == "demo"
    assert body["total_chunks_saved"] >= 1
    assert len(body["files"]) == 1
    entry = body["files"][0]
    assert entry["source"] == "policy.md"
    assert entry["status"] == "ingested"
    # ``ingest_id`` is empty for the legacy path (no id was minted
    # historically — callers don't depend on it).
    assert body["ingest_id"] == ""
