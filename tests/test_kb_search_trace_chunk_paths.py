"""Tests for per-chunk path tracking in SearchTrace (PR-S).

Extends PR-K's SearchTrace with an optional ``chunk_ids`` list on
each StageRecord. Operators can read down the trace to see which
chunks survived each stage — answering "where did chunk X drop out?"
by inspection.

Coverage:
* StageRecord exposes chunk_ids; default None
* SearchTrace.record(chunk_ids=...) round-trips
* SearchTrace.time(...) context manager exposes chunk_ids; round-trips
* End-to-end via search(trace=...) — retrieve[i] / rrf_fuse / rerank
  stages all carry chunk_ids; rewriter (which produces query
  variants, not chunks) stays None
* Multi-hop folded stages preserve chunk_ids per hop
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
    return [[1.0, 0.0] for _ in texts]


@pytest.fixture
async def seeded_storage() -> InMemoryStorage:
    storage = InMemoryStorage()
    for i, text in enumerate(["alpha", "beta", "gamma"]):
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
# Pure StageRecord / SearchTrace
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stage_record_chunk_ids_defaults_to_none() -> None:
    rec = StageRecord(name="s", duration_ms=10.0)
    assert rec.chunk_ids is None


@pytest.mark.unit
def test_trace_record_round_trips_chunk_ids() -> None:
    trace = SearchTrace()
    trace.record("retrieve[0]", 12.3, output_count=3, chunk_ids=["a", "b", "c"])
    assert trace.stages[0].chunk_ids == ["a", "b", "c"]


@pytest.mark.unit
def test_trace_time_context_records_chunk_ids() -> None:
    trace = SearchTrace()
    with trace.time("rerank") as rec:
        rec.chunk_ids = ["x", "y"]
    assert trace.stages[0].chunk_ids == ["x", "y"]


@pytest.mark.unit
def test_trace_record_copies_chunk_ids_to_decouple() -> None:
    """Mutating the source list AFTER record() must not change the
    stored stage — defensive copy at record time."""
    trace = SearchTrace()
    src = ["a", "b"]
    trace.record("s", 1.0, chunk_ids=src)
    src.append("c")
    assert trace.stages[0].chunk_ids == ["a", "b"]


# ---------------------------------------------------------------------------
# End-to-end via search()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_retrieve_stage_stamps_chunk_ids(
    seeded_storage: InMemoryStorage,
) -> None:
    """The default-path retrieve[0] stage records the chunk_ids it
    produced. Operator can see which 3 chunks came back."""
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
    retrieve = next(s for s in trace.stages if s.name == "retrieve[0]")
    assert retrieve.chunk_ids is not None
    assert len(retrieve.chunk_ids) == 3
    # Order matches the chunks' insertion order (all same embedding,
    # vector ranks by insertion).
    for cid in retrieve.chunk_ids:
        # chunk_id is generated; just confirm they're non-empty.
        assert cid


@pytest.mark.unit
async def test_search_rrf_fuse_stage_stamps_chunk_ids(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """With rewrite_variants > 0, rrf_fuse runs and records the
    post-fusion chunk path."""
    mock_litellm.return_value = _make_resp('{"variants": ["alt"]}')
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
    rrf = next(s for s in trace.stages if s.name == "rrf_fuse")
    assert rrf.chunk_ids is not None
    assert len(rrf.chunk_ids) > 0


@pytest.mark.unit
async def test_search_rerank_stage_stamps_chunk_ids(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """rerank=True → rerank stage records the post-rerank chunk
    order. Lets operators answer 'did rerank shuffle things?' by
    diffing chunk_ids vs the retrieve stage's."""
    mock_litellm.return_value = _make_resp(
        '{"rankings": [{"id": 1, "score": 0.9}, '
        '{"id": 2, "score": 0.7}, {"id": 3, "score": 0.5}]}'
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
    rerank = next(s for s in trace.stages if s.name == "rerank")
    assert rerank.chunk_ids is not None
    assert len(rerank.chunk_ids) == 3


@pytest.mark.unit
async def test_rewrite_stage_stays_none_for_chunk_ids(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """The rewriter produces QUERY VARIANTS, not chunks. Its stage
    record should leave chunk_ids = None so the trace table renders
    a dash placeholder."""
    mock_litellm.return_value = _make_resp('{"variants": ["alt"]}')
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
    rewrite = next(s for s in trace.stages if s.name == "rewrite")
    assert rewrite.chunk_ids is None


@pytest.mark.unit
async def test_multi_hop_folded_stages_carry_chunk_ids(
    seeded_storage: InMemoryStorage, mock_litellm: AsyncMock
) -> None:
    """When multi-hop folds inner-trace stages under hop_N: prefixes,
    chunk_ids carry through the fold."""
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
    hop_stages = [s for s in trace.stages if s.name.startswith("hop_")]
    assert hop_stages, "expected at least one hop_N:* stage"
    retrieve_hop = next(s for s in hop_stages if "retrieve[0]" in s.name)
    assert retrieve_hop.chunk_ids is not None
    assert len(retrieve_hop.chunk_ids) > 0
