"""MovateClient round-trip for the managed skills + contexts surface
(ADR 060 D3).

Drives the real ``MovateClient`` skills/contexts methods through a
TestClient-backed runtime via ``httpx.ASGITransport`` — the same wire path
``mdk skills remote`` / ``mdk contexts remote`` use, no socket. Exercises the
exact request shapes the CLI sends and the response parsing the CLI renders.
Mirrors ``tests/test_batch_cli.py``'s client-round-trip layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import AgentBundleRecord
from movate.runtime import build_app
from movate.testing import InMemoryStorage


def _skill_files(name: str = "web-search", version: str = "1.0.0") -> dict[str, str]:
    return {
        "skill.yaml": (
            "api_version: movate/v1\n"
            "kind: Skill\n"
            f"name: {name}\n"
            f"version: {version}\n"
            "description: Search the web.\n"
            "schema:\n"
            "  input:\n    query: string\n"
            "  output:\n    result: string\n"
            "implementation:\n"
            "  kind: python\n"
            "  entry: myproject.skills.search:run\n"
        ),
    }


@pytest.fixture
async def runtime():
    """(storage, app, full_key) with an admin-scoped key + a seeded agent."""
    storage = InMemoryStorage()
    await storage.init()
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="sc-client", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    await storage.save_agent_bundle(
        AgentBundleRecord(
            name="faq-bot",
            tenant_id=tenant_id,
            version="v1",
            created_by="seed",
            content_hash="seed-hash",
            files={"agent.yaml": "name: faq-bot\nversion: v1\n"},
            created_at=datetime.now(UTC),
        )
    )
    app = build_app(storage)
    return storage, app, minted.full_key


def _client_for(app, key: str) -> MovateClient:
    return MovateClient(base_url="http://test", api_key=key, transport=httpx.ASGITransport(app=app))


@pytest.mark.unit
async def test_client_skills_full_round_trip(runtime) -> None:
    _storage, app, key = runtime
    async with _client_for(app, key) as client:
        created = await client.upsert_skill("web-search", version="1.0.0", files=_skill_files())
        assert created.name == "web-search"
        assert created.version == "1.0.0"

        listing = await client.list_skills()
        assert listing.count == 1
        assert listing.skills[0].name == "web-search"

        got = await client.get_skill("web-search")
        assert got.version == "1.0.0"

        await client.upsert_skill(
            "web-search", version="2.0.0", files=_skill_files(version="2.0.0")
        )
        versions = await client.list_skill_versions("web-search")
        assert [v.version for v in versions.versions] == ["2.0.0", "1.0.0"]

        attached = await client.attach_skill_to_agent("faq-bot", ref="web-search")
        assert attached.attached is True
        assert attached.kind == "skill"

        deleted = await client.delete_skill("web-search")
        assert deleted.name == "web-search"
        with pytest.raises(MovateClientError):
            await client.get_skill("web-search")


@pytest.mark.unit
async def test_client_contexts_full_round_trip(runtime) -> None:
    _storage, app, key = runtime
    async with _client_for(app, key) as client:
        created = await client.create_context(
            name="company-tone", body="# Tone\nBe concise.", description="voice"
        )
        assert created.name == "company-tone"
        assert created.version == "v1"

        listing = await client.list_contexts()
        assert listing.count == 1

        got = await client.get_context("company-tone")
        assert got.body == "# Tone\nBe concise."

        await client.upsert_context("company-tone", version="v2", body="# Tone v2")
        versions = await client.list_context_versions("company-tone")
        assert [v.version for v in versions.versions] == ["v2", "v1"]

        attached = await client.attach_context_to_agent("faq-bot", ref="company-tone")
        assert attached.attached is True
        assert attached.kind == "context"

        deleted = await client.delete_context("company-tone")
        assert deleted.name == "company-tone"
        with pytest.raises(MovateClientError):
            await client.get_context("company-tone")
