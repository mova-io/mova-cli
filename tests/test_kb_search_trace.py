"""Tests for ``movate.kb.trace`` — per-stage retrieval telemetry (PR-K).

Covers:

* ``SearchTrace`` / ``StageRecord`` model — record, time context manager,
  total_ms across stages.
* End-to-end via ``search(trace=...)`` — each stage that fires records
  exactly one entry, with the right name + counts. Default search
  (no trace) doesn't touch the trace path at all.
* CLI ``--trace`` flag renders the table.

LiteLLM mocked module-wide so multi-stage tests run hermetically.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock as um
from unittest.mock import AsyncMock

import litellm
import pytest

from movate.core.models import KbChunk
from movate.kb.search import search as kb_search
from movate.kb.trace import SearchTrace, StageRecord
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _make_resp(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def mock_litellm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(litellm, "acompletion", mock)
    return mock


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
) -> list[list[float]]:
    """Stub embedder — same vector for every input so vector
    retrieval ranks by insertion order."""
    return [[1.0, 0.0] for _ in texts]


@pytest.fixture
async def seeded_storage() -> InMemoryStorage:
    storage = InMemoryStorage()
    for i, text in enumerate(["chunk one", "chunk two", "chunk three"]):
        await storage.save_kb_chunk(
            KbChunk(
                tenant_id="t1",
                agent="rag-qa",
                source=f"/tmp/{i}.md",
                text=text,
                embedding=[1.0, 0.0],
                embedding_model="openai/text-embedding-3-small",
                content_hash=f"c_{i}",
            )
        )
    return storage


# ---------------------------------------------------------------------------
# Pure SearchTrace / StageRecord
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_starts_empty() -> None:
    trace = SearchTrace()
    assert trace.stages == []
    assert trace.total_ms() == 0.0


@pytest.mark.unit
def test_trace_record_appends_in_order() -> None:
    trace = SearchTrace()
    trace.record("a", 10.0, output_count=3)
    trace.record("b", 20.0, input_count=3, output_count=2)
    assert [s.name for s in trace.stages] == ["a", "b"]
    assert trace.stages[0].output_count == 3
    assert trace.stages[1].input_count == 3
    assert trace.total_ms() == 30.0


@pytest.mark.unit
def test_trace_time_context_manager_measures_elapsed() -> None:
    """The ``time()`` context manager records a stage with a
    non-negative duration. Body can mutate counts + details."""
    trace = SearchTrace()
    with trace.time("test_stage") as rec:
        rec.output_count = 5
        rec.details["key"] = "value"
    assert len(trace.stages) == 1
    stage = trace.stages[0]
    assert stage.name == "test_stage"
    assert stage.duration_ms >= 0.0
    assert stage.output_count == 5
    assert stage.details["key"] == "value"


@pytest.mark.unit
def test_trace_time_context_records_on_exception() -> None:
    """Even when the body raises, the stage record gets appended.
    Partial traces are useful for debugging timeouts."""
    trace = SearchTrace()
    with pytest.raises(RuntimeError), trace.time("crash") as rec:
        rec.output_count = 1
        raise RuntimeError("kaboom")
    assert len(trace.stages) == 1
    assert trace.stages[0].name == "crash"
    assert trace.stages[0].output_count == 1


@pytest.mark.unit
def test_stage_record_default_details_is_independent_per_instance() -> None:
    """Mutable default sanity: appending to one StageRecord's details
    must not bleed into another's. (Standard `field(default_factory=dict)`
    gotcha — guard via test.)"""
    a = StageRecord(name="a", duration_ms=0)
    b = StageRecord(name="b", duration_ms=0)
    a.details["x"] = 1
    assert "x" not in b.details


# ---------------------------------------------------------------------------
# search(trace=None) — zero overhead path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_default_no_trace_records_nothing(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """trace=None (default) → search runs with no trace bookkeeping.
    Behavior byte-for-byte unchanged from pre-PR-K."""
    with um.patch("movate.kb.search.embed_texts", side_effect=_fake_embed):
        results = await kb_search(
            storage=seeded_storage,
            question="anything",
            agent="rag-qa",
            tenant_id="t1",
            limit=3,
            # trace defaults to None
        )
    assert len(results) == 3
    # Sanity: no LLM stages fired, so the mock should be untouched.
    mock_litellm.assert_not_called()


# ---------------------------------------------------------------------------
# search(trace=X) — stages populated correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_with_trace_records_retrieve_only(
    seeded_storage: InMemoryStorage,
) -> None:
    """Default-flags search + trace → one retrieve[0] stage, no others."""
    trace = SearchTrace()
    with um.patch("movate.kb.search.embed_texts", side_effect=_fake_embed):
        results = await kb_search(
            storage=seeded_storage,
            question="anything",
            agent="rag-qa",
            tenant_id="t1",
            limit=3,
            trace=trace,
        )
    assert len(results) == 3
    names = [s.name for s in trace.stages]
    assert names == ["retrieve[0]"]
    assert trace.stages[0].output_count == 3
    assert trace.stages[0].details["mode"] == "vector"


@pytest.mark.unit
async def test_search_trace_includes_rewriter_stage(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """With rewrite_variants > 0, the trace shows a ``rewrite`` stage
    + one ``retrieve[i]`` per variant + an ``rrf_fuse`` stage."""
    mock_litellm.return_value = _make_resp('{"variants": ["alt 1"]}')

    trace = SearchTrace()
    with um.patch("movate.kb.search.embed_texts", side_effect=_fake_embed):
        await kb_search(
            storage=seeded_storage,
            question="anything",
            agent="rag-qa",
            tenant_id="t1",
            limit=3,
            rewrite_variants=1,
            trace=trace,
        )
    names = [s.name for s in trace.stages]
    # rewrite + retrieve[0] + retrieve[1] + rrf_fuse (2 variants → fusion runs)
    assert "rewrite" in names
    assert "retrieve[0]" in names
    assert "retrieve[1]" in names
    assert "rrf_fuse" in names
    # Rewriter stage carries the variant list in details.
    rewrite_stage = next(s for s in trace.stages if s.name == "rewrite")
    assert rewrite_stage.output_count == 2  # original + 1 variant
    assert "variants" in rewrite_stage.details


@pytest.mark.unit
async def test_search_trace_includes_rerank_stage_with_overlap(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """rerank=True → trace has a ``rerank`` stage with top_k_overlap."""
    mock_litellm.return_value = _make_resp(
        '{"rankings": [{"id": 1, "score": 0.9}, {"id": 2, "score": 0.7}, {"id": 3, "score": 0.5}]}'
    )

    trace = SearchTrace()
    with um.patch("movate.kb.search.embed_texts", side_effect=_fake_embed):
        await kb_search(
            storage=seeded_storage,
            question="anything",
            agent="rag-qa",
            tenant_id="t1",
            limit=3,
            rerank=True,
            trace=trace,
        )
    names = [s.name for s in trace.stages]
    assert "rerank" in names
    rerank_stage = next(s for s in trace.stages if s.name == "rerank")
    assert rerank_stage.input_count > 0  # widened pool from rerank_candidate_multiplier
    assert rerank_stage.output_count == 3
    # Overlap metric is present and in [0, 1].
    assert "top_k_overlap" in rerank_stage.details
    overlap = rerank_stage.details["top_k_overlap"]
    assert 0.0 <= overlap <= 1.0


@pytest.mark.unit
async def test_search_trace_multi_hop_prefixed_stages(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """multi_hop > 0 → each hop's stages get a ``hop_N:`` prefix
    in the trace so the operator can see per-hop breakdown."""
    # Planner says "done" after hop 1.
    mock_litellm.return_value = _make_resp('{"action": "done"}')

    trace = SearchTrace()
    with um.patch("movate.kb.search.embed_texts", side_effect=_fake_embed):
        await kb_search(
            storage=seeded_storage,
            question="anything",
            agent="rag-qa",
            tenant_id="t1",
            limit=3,
            multi_hop=2,
            trace=trace,
        )
    names = [s.name for s in trace.stages]
    # First hop's retrieve gets the hop_0: prefix.
    assert any(n.startswith("hop_0:retrieve[0]") for n in names), names
    # Each prefixed stage carries the sub_query in details.
    hop_stages = [s for s in trace.stages if s.name.startswith("hop_")]
    for s in hop_stages:
        assert "sub_query" in s.details


# ---------------------------------------------------------------------------
# Per-stage details / counts sanity
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_trace_total_ms_is_positive(
    seeded_storage: InMemoryStorage,
) -> None:
    trace = SearchTrace()
    with um.patch("movate.kb.search.embed_texts", side_effect=_fake_embed):
        await kb_search(
            storage=seeded_storage,
            question="anything",
            agent="rag-qa",
            tenant_id="t1",
            limit=3,
            trace=trace,
        )
    assert trace.total_ms() > 0.0


@pytest.mark.unit
async def test_search_trace_empty_question_records_nothing(
    seeded_storage: InMemoryStorage,
) -> None:
    """Empty question short-circuits in search() BEFORE any stage
    fires. Trace stays empty."""
    trace = SearchTrace()
    results = await kb_search(
        storage=seeded_storage,
        question="   ",
        agent="rag-qa",
        tenant_id="t1",
        limit=3,
        trace=trace,
    )
    assert results == []
    assert trace.stages == []
