"""GraphRAG retrieval MODE (ADR 010 / 046 D9) wired into kb-vector-lookup.

Completes ADR 010's unsurfaced half: an agent can opt into graph-neighbor
retrieval via ``retrieval.graph`` in agent.yaml, and the ``kb-vector-lookup``
skill — ALONGSIDE its vector chunk search — appends a rendered graph context
block. These tests cover:

* the ``GraphRetrievalConfig`` model + its parsing on ``AgentSpec``,
* default OFF → no graph lookup, result byte-for-byte unchanged,
* enabled → a real seeded graph yields a graph-context chunk appended to the
  vector chunks (complement, not replacement),
* tolerance — a graph failure / empty graph degrades to vector-only.

The vector ``kb_search`` is mocked; the graph side runs against a real
``InMemoryStorage`` with stubbed embeddings (offline + deterministic).
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

import movate.kb.graph_retrieval as graph_retrieval_mod
import movate.kb.search as kb_search_mod
from movate.core.models import AgentSpec, Entity, GraphRetrievalConfig, Relation, RetrievalConfig
from movate.core.skill_backend.base import SkillExecutionContext
from movate.templates.skill_kb_vector_lookup import impl as kb_lookup_skill
from movate.testing import InMemoryStorage

pytestmark = pytest.mark.unit


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# GraphRetrievalConfig model + AgentSpec parsing
# ---------------------------------------------------------------------------


def test_graph_config_defaults_off() -> None:
    cfg = GraphRetrievalConfig()
    assert cfg.enabled is False
    assert cfg.hops == 1
    assert cfg.seed_limit == 5
    assert cfg.max_relations == 50


def test_retrieval_config_graph_default_keeps_is_default() -> None:
    """An agent with no graph block stays 'default' (no behavior change)."""
    assert RetrievalConfig().is_default() is True
    assert RetrievalConfig().graph.enabled is False


def test_retrieval_config_graph_enabled_breaks_is_default() -> None:
    cfg = RetrievalConfig(graph=GraphRetrievalConfig(enabled=True))
    assert cfg.is_default() is False


_BASE_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: graph-agent
version: 0.1.0
description: Test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""


def test_agent_spec_parses_graph_block() -> None:
    raw = yaml.safe_load(
        _BASE_AGENT_YAML
        + "retrieval:\n"
        + "  graph:\n"
        + "    enabled: true\n"
        + "    hops: 2\n"
        + "    max_relations: 25\n"
    )
    spec = AgentSpec.model_validate(raw)
    assert spec.retrieval.graph.enabled is True
    assert spec.retrieval.graph.hops == 2
    assert spec.retrieval.graph.max_relations == 25
    assert not spec.retrieval.is_default()


def test_agent_spec_no_graph_block_defaults_off() -> None:
    spec = AgentSpec.model_validate(yaml.safe_load(_BASE_AGENT_YAML))
    assert spec.retrieval.graph.enabled is False
    assert spec.retrieval.is_default()


# ---------------------------------------------------------------------------
# Skill wiring — graph context complements vector chunks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_kb_search(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Vector search returns one chunk (so we can see graph adds a SECOND)."""

    class _Chunk:
        text = "vector chunk text"
        source = "doc.md"
        ocr = False

    class _Result:
        chunk = _Chunk()
        score = 0.9

    mock = AsyncMock(return_value=[_Result()])
    monkeypatch.setattr(kb_search_mod, "search", mock)
    return mock


@pytest.fixture
def stub_graph_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the graph query embedding so the seed deterministically hits Alpha."""

    async def fake_embed(texts, *, model="", api_key=None, timeout_s=60.0):
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(graph_retrieval_mod, "embed_texts", fake_embed)


async def _seed_graph(storage: InMemoryStorage) -> None:
    a = Entity(
        tenant_id="t1",
        agent="a1",
        name="Alpha",
        type="Feature",
        description="The A feature.",
        embedding=[1.0, 0.0, 0.0],
        embedding_model="test-embed",
        content_hash=_hash("a1", "t1", "Alpha", "Feature"),
    )
    b = Entity(
        tenant_id="t1",
        agent="a1",
        name="Beta",
        type="Tier",
        embedding=[0.0, 1.0, 0.0],
        embedding_model="test-embed",
        content_hash=_hash("a1", "t1", "Beta", "Tier"),
    )
    await storage.upsert_entity(a)
    await storage.upsert_entity(b)
    await storage.upsert_relation(
        Relation(
            tenant_id="t1",
            agent="a1",
            src_entity_id=a.entity_id,
            dst_entity_id=b.entity_id,
            type="REQUIRES",
            weight=0.9,
            content_hash=_hash("a1", "t1", a.entity_id, b.entity_id, "REQUIRES"),
        )
    )


async def test_graph_disabled_no_graph_chunk(mock_kb_search: AsyncMock) -> None:
    """Default (graph off) → only vector chunks; result unchanged."""
    storage = InMemoryStorage()
    await storage.init()
    ctx = SkillExecutionContext(
        agent_name="a1",
        tenant_id="t1",
        storage=storage,
        retrieval=RetrievalConfig(),  # graph.enabled defaults False
    )
    out = await kb_lookup_skill.run({"question": "tell me about alpha"}, ctx=ctx)
    assert out["chunks_found"] == 1
    assert out["graph_context_added"] is False
    assert all(c.get("kind") != "graph" for c in out["chunks"])


async def test_graph_enabled_appends_graph_context(
    mock_kb_search: AsyncMock, stub_graph_embed: None
) -> None:
    """Enabled + a seeded graph → a graph-context chunk is appended ALONGSIDE
    the vector chunk (complement, not replacement)."""
    storage = InMemoryStorage()
    await storage.init()
    await _seed_graph(storage)
    ctx = SkillExecutionContext(
        agent_name="a1",
        tenant_id="t1",
        storage=storage,
        retrieval=RetrievalConfig(graph=GraphRetrievalConfig(enabled=True, hops=1)),
    )
    out = await kb_lookup_skill.run({"question": "tell me about alpha"}, ctx=ctx)

    # Vector count unchanged; graph appended as an extra chunk.
    assert out["chunks_found"] == 1
    assert out["graph_context_added"] is True
    assert len(out["chunks"]) == 2
    graph_chunk = next(c for c in out["chunks"] if c.get("kind") == "graph")
    assert graph_chunk["source"] == "knowledge-graph"
    assert "Alpha" in graph_chunk["text"]
    assert "REQUIRES" in graph_chunk["text"]
    assert graph_chunk["entities"] >= 1
    # The original vector chunk still present.
    assert any(c.get("text") == "vector chunk text" for c in out["chunks"])


async def test_graph_enabled_empty_graph_is_vector_only(
    mock_kb_search: AsyncMock, stub_graph_embed: None
) -> None:
    """Enabled but the graph is empty → no graph chunk, vector path intact."""
    storage = InMemoryStorage()
    await storage.init()
    ctx = SkillExecutionContext(
        agent_name="a1",
        tenant_id="t1",
        storage=storage,
        retrieval=RetrievalConfig(graph=GraphRetrievalConfig(enabled=True)),
    )
    out = await kb_lookup_skill.run({"question": "anything"}, ctx=ctx)
    assert out["chunks_found"] == 1
    assert out["graph_context_added"] is False
    assert len(out["chunks"]) == 1


async def test_graph_retrieval_failure_degrades_to_vector(
    mock_kb_search: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph-retrieval exception must NOT break the vector path (complement)."""

    async def boom(**_kwargs: Any):
        raise RuntimeError("graph backend down")

    monkeypatch.setattr(graph_retrieval_mod, "graph_retrieve", boom)
    storage = InMemoryStorage()
    await storage.init()
    ctx = SkillExecutionContext(
        agent_name="a1",
        tenant_id="t1",
        storage=storage,
        retrieval=RetrievalConfig(graph=GraphRetrievalConfig(enabled=True)),
    )
    out = await kb_lookup_skill.run({"question": "alpha"}, ctx=ctx)
    assert out["chunks_found"] == 1
    assert out["graph_context_added"] is False
