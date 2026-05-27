"""D7c (#134) — the grounding-gap detector.

Tests the small, reusable predicate behind the "RAG agent but empty KB"
proactive offer (:mod:`movate.kb.grounding_gap`):

* ``is_rag_shaped`` — true for a kb-vector skill OR an ADR-023
  ``retrieval.auto_into`` agent; false for a plain agent.
* ``kb_is_empty`` — true on 0 chunks, false on >=1 (against the real
  ``InMemoryStorage`` chunk-list path).
* ``has_grounding_gap`` — RAG-shaped + empty → True; RAG-shaped + populated
  → False; non-RAG → False (and never queries storage).

Hermetic: a real :class:`InMemoryStorage` backs the chunk count, no DB / no
network.
"""

from __future__ import annotations

import pytest
import yaml

from movate.core.models import AgentSpec, KbChunk
from movate.kb.grounding_gap import has_grounding_gap, is_rag_shaped, kb_is_empty
from movate.testing import InMemoryStorage

# A minimal valid agent.yaml. Tests layer skills / retrieval on top.
_BASE_AGENT_YAML = """
api_version: movate/v1
kind: Agent
name: rag-agent
version: 0.1.0
description: Test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""


def _spec(*, skills: list[str] | None = None, auto_into: str | None = None) -> AgentSpec:
    """Build an AgentSpec with the given skills / ADR-023 retrieval marker."""
    data = yaml.safe_load(_BASE_AGENT_YAML)
    if skills is not None:
        data["skills"] = skills
    if auto_into is not None:
        data["retrieval"] = {"auto_into": auto_into}
    return AgentSpec.model_validate(data)


def _chunk(agent: str) -> KbChunk:
    return KbChunk(
        tenant_id="local",
        agent=agent,
        source="kb/faq.md",
        text="some grounded content",
        embedding=[0.1, 0.2, 0.3],
        embedding_model="openai/text-embedding-3-small",
        content_hash="deadbeef",
    )


# ---------------------------------------------------------------------------
# is_rag_shaped
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsRagShaped:
    def test_kb_vector_skill_is_rag(self) -> None:
        assert is_rag_shaped(_spec(skills=["kb-vector-lookup"]))

    def test_kb_vector_skill_case_insensitive(self) -> None:
        assert is_rag_shaped(_spec(skills=["KB-Vector-Lookup"]))

    def test_auto_into_is_rag(self) -> None:
        # No kb-vector skill — RAG-shaped purely via ADR-023 pre-retrieval.
        assert is_rag_shaped(_spec(skills=["some-other-skill"], auto_into="context"))

    def test_plain_agent_is_not_rag(self) -> None:
        assert not is_rag_shaped(_spec(skills=[]))

    def test_non_kb_skill_is_not_rag(self) -> None:
        assert not is_rag_shaped(_spec(skills=["send-email", "kb-lookup"]))


# ---------------------------------------------------------------------------
# kb_is_empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKbIsEmpty:
    async def test_empty_when_no_chunks(self) -> None:
        storage = InMemoryStorage()
        await storage.init()
        try:
            assert await kb_is_empty(storage, agent="rag-agent") is True
        finally:
            await storage.close()

    async def test_not_empty_with_chunk(self) -> None:
        storage = InMemoryStorage()
        await storage.init()
        try:
            await storage.save_kb_chunk(_chunk("rag-agent"))
            assert await kb_is_empty(storage, agent="rag-agent") is False
        finally:
            await storage.close()

    async def test_scoped_by_agent(self) -> None:
        """A chunk for another agent doesn't count — the gap is per-agent."""
        storage = InMemoryStorage()
        await storage.init()
        try:
            await storage.save_kb_chunk(_chunk("other-agent"))
            assert await kb_is_empty(storage, agent="rag-agent") is True
        finally:
            await storage.close()


# ---------------------------------------------------------------------------
# has_grounding_gap
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHasGroundingGap:
    async def test_rag_shaped_and_empty_is_gap(self) -> None:
        storage = InMemoryStorage()
        await storage.init()
        try:
            assert await has_grounding_gap(_spec(skills=["kb-vector-lookup"]), storage) is True
        finally:
            await storage.close()

    async def test_rag_shaped_and_populated_is_not_gap(self) -> None:
        storage = InMemoryStorage()
        await storage.init()
        try:
            await storage.save_kb_chunk(_chunk("rag-agent"))
            assert await has_grounding_gap(_spec(skills=["kb-vector-lookup"]), storage) is False
        finally:
            await storage.close()

    async def test_non_rag_is_not_gap_and_skips_storage(self) -> None:
        """A non-RAG agent short-circuits before any storage query — so even a
        storage that would explode on list never gets called."""

        class _ExplodingStorage:
            async def list_kb_chunks(self, **_: object) -> list[KbChunk]:
                raise AssertionError("storage must not be queried for a non-RAG agent")

        # Cast through Any-ish: the detector only calls list_kb_chunks, and
        # short-circuits before that for a non-RAG spec.
        assert await has_grounding_gap(_spec(skills=[]), _ExplodingStorage()) is False  # type: ignore[arg-type]

    async def test_auto_into_rag_and_empty_is_gap(self) -> None:
        storage = InMemoryStorage()
        await storage.init()
        try:
            spec = _spec(skills=["some-other-skill"], auto_into="context")
            assert await has_grounding_gap(spec, storage) is True
        finally:
            await storage.close()
