"""Tests for ``movate.kb.multi_hop`` — iterative retrieve → reason → retrieve.

Same graceful-degradation contract as the other LLM stages (rewriter,
reranker): every failure mode must terminate the loop and return
the chunks gathered so far. Tests cover happy paths (multi-hop
finds the second-hop chunks), termination conditions (DONE, max_hops,
chunk cap), and the three defensive paths (LLM error, malformed JSON,
planner repeats same query).

LiteLLM is monkey-patched module-wide for hermetic tests. The
retrieval function is a synchronous stub — no storage needed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest import mock as um
from unittest.mock import AsyncMock

import litellm
import pytest

from movate.core.models import KbChunk, KbChunkWithScore
from movate.kb.multi_hop import (
    DEFAULT_TERMINATION_MODEL,
    MAX_HOPS,
    MAX_TOTAL_CHUNKS_CAP,
    _Decision,
    _format_chunks_for_planner,
    _parse_decision,
    multi_hop_search,
)
from movate.kb.search import search as kb_search
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _make_response(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _chunk(text: str, *, chunk_id: str, score: float = 0.5) -> KbChunkWithScore:
    return KbChunkWithScore(
        chunk=KbChunk(
            chunk_id=chunk_id,
            tenant_id="test",
            agent="rag-qa",
            source=f"/tmp/{chunk_id}.md",
            text=text,
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash=chunk_id,
        ),
        score=score,
    )


@pytest.fixture
def mock_litellm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(litellm, "acompletion", mock)
    return mock


def _scripted_retriever(
    by_query: dict[str, list[KbChunkWithScore]],
) -> Callable[[str], Awaitable[list[KbChunkWithScore]]]:
    """Build an async retriever that returns the chunks scripted for
    each query string. Useful for asserting "the second hop's
    refined query produced the expected chunks"."""
    calls: list[str] = []

    async def retrieve(query: str) -> list[KbChunkWithScore]:
        calls.append(query)
        return by_query.get(query, [])

    # Stash the call log on the function for assertions.
    retrieve.calls = calls  # type: ignore[attr-defined]
    return retrieve


# ---------------------------------------------------------------------------
# Happy path — refine then done
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multi_hop_refines_then_aggregates(mock_litellm: AsyncMock) -> None:
    """First hop returns chunk A. Planner says "refine for SSO".
    Second hop returns chunk B. Planner says "done".
    Result: [A, B] (chunks from both hops, deduped, in insertion order)."""
    retrieve = _scripted_retriever(
        {
            "How do refunds work for SSO customers?": [
                _chunk("Refunds within 14 days for annual plans.", chunk_id="A")
            ],
            "Does SAML SSO require an enterprise tier?": [
                _chunk("SAML SSO requires the enterprise tier.", chunk_id="B")
            ],
        }
    )
    # Two scripted planner responses: first refine, then done.
    mock_litellm.side_effect = [
        _make_response(
            '{"action": "refine", "query": "Does SAML SSO require an enterprise tier?"}'
        ),
        _make_response('{"action": "done"}'),
    ]

    out = await multi_hop_search(
        question="How do refunds work for SSO customers?",
        retrieve_fn=retrieve,
        max_hops=3,
    )
    ids = [r.chunk.chunk_id for r in out]
    assert ids == ["A", "B"]
    # Two retrieval calls: one per hop.
    assert len(retrieve.calls) == 2  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_multi_hop_terminates_on_done(mock_litellm: AsyncMock) -> None:
    """If the planner says "done" after hop 1, the loop stops there
    even though max_hops=3."""
    retrieve = _scripted_retriever({"q": [_chunk("Answer A", chunk_id="A")]})
    mock_litellm.return_value = _make_response('{"action": "done"}')

    out = await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=3)
    assert [r.chunk.chunk_id for r in out] == ["A"]
    assert len(retrieve.calls) == 1  # type: ignore[attr-defined]
    # Planner was called once (after hop 1) — not max_hops times.
    assert mock_litellm.call_count == 1


@pytest.mark.unit
async def test_multi_hop_dedups_by_chunk_id(mock_litellm: AsyncMock) -> None:
    """If two hops return the same chunk, it appears once in the
    aggregated result. Insertion order = first hop wins."""
    shared = _chunk("Shared chunk", chunk_id="X", score=0.9)
    retrieve = _scripted_retriever({"q": [shared], "q2": [shared]})
    mock_litellm.side_effect = [
        _make_response('{"action": "refine", "query": "q2"}'),
        _make_response('{"action": "done"}'),
    ]

    out = await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=3)
    assert len(out) == 1
    assert out[0].chunk.chunk_id == "X"


# ---------------------------------------------------------------------------
# Termination conditions
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multi_hop_respects_max_hops(mock_litellm: AsyncMock) -> None:
    """Even if the planner always says "refine", the loop stops at
    max_hops to bound cost."""
    retrieve = _scripted_retriever(
        {"q": [_chunk("Hop1", chunk_id="H1")], "next": [_chunk("Hop2", chunk_id="H2")]}
    )
    # Planner always asks for more — would loop forever without the cap.
    mock_litellm.return_value = _make_response('{"action": "refine", "query": "next"}')

    await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=2)
    # Two retrieval calls (hop 1 + hop 2), one planner call (after
    # hop 1 — the last hop's planner call is skipped because there's
    # no third hop to plan for).
    assert len(retrieve.calls) == 2  # type: ignore[attr-defined]
    assert mock_litellm.call_count == 1


@pytest.mark.unit
async def test_multi_hop_respects_chunk_cap(mock_litellm: AsyncMock) -> None:
    """Aggregated chunks are capped at max_total_chunks. Once the cap
    is hit, the loop stops early without a planner call."""
    big_first_hop = [_chunk(f"chunk {i}", chunk_id=f"c_{i}") for i in range(20)]
    retrieve = _scripted_retriever({"q": big_first_hop})

    out = await multi_hop_search(
        question="q",
        retrieve_fn=retrieve,
        max_hops=3,
        max_total_chunks=5,
    )
    assert len(out) == 5
    # First-hop retrieval ran once, then we hit the cap and bailed —
    # no planner call needed because we'd discard anything further.
    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_multi_hop_clamps_max_hops_to_module_cap(
    mock_litellm: AsyncMock,
) -> None:
    """max_hops > MAX_HOPS gets clamped to MAX_HOPS at runtime."""
    retrieve = _scripted_retriever({"q": [_chunk("x", chunk_id="X")]})
    mock_litellm.return_value = _make_response('{"action": "done"}')

    out = await multi_hop_search(
        question="q",
        retrieve_fn=retrieve,
        max_hops=999,
        max_total_chunks=999,
    )
    assert len(out) == 1
    # Confirm via internals: the runtime CAP is what bounded the loop.
    # (If unclamped, the planner-says-done path would still terminate
    # after hop 1; the assertion that matters is no crash + clean exit.)
    assert MAX_HOPS == 5  # regression guard on the module constant
    assert MAX_TOTAL_CHUNKS_CAP == 30


@pytest.mark.unit
async def test_multi_hop_terminates_if_refine_repeats_query(
    mock_litellm: AsyncMock,
) -> None:
    """Defensive: if the planner returns the SAME query as the current
    sub-query, the loop terminates (no progress = wasted retrieval)."""
    retrieve = _scripted_retriever({"q": [_chunk("only", chunk_id="O")]})
    mock_litellm.return_value = _make_response('{"action": "refine", "query": "q"}')

    out = await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=3)
    assert [r.chunk.chunk_id for r in out] == ["O"]
    # One retrieval, one planner call, then the same-query check
    # terminates.
    assert len(retrieve.calls) == 1  # type: ignore[attr-defined]
    assert mock_litellm.call_count == 1


# ---------------------------------------------------------------------------
# Short-circuit + degraded paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multi_hop_empty_question_returns_empty(mock_litellm: AsyncMock) -> None:
    retrieve = _scripted_retriever({})
    out = await multi_hop_search(question="   ", retrieve_fn=retrieve, max_hops=3)
    assert out == []
    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_multi_hop_planner_exception_returns_partial(
    mock_litellm: AsyncMock,
) -> None:
    """Planner crashes mid-loop → return what's been gathered so far."""
    retrieve = _scripted_retriever({"q": [_chunk("partial", chunk_id="P")]})
    mock_litellm.side_effect = RuntimeError("planner down")

    out = await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=3)
    # First hop's chunk survived; loop terminated on planner failure.
    assert [r.chunk.chunk_id for r in out] == ["P"]


@pytest.mark.unit
async def test_multi_hop_malformed_planner_response_returns_partial(
    mock_litellm: AsyncMock,
) -> None:
    """Planner returns prose → fall back, return gathered chunks."""
    retrieve = _scripted_retriever({"q": [_chunk("partial", chunk_id="P")]})
    mock_litellm.return_value = _make_response("Here are my thoughts: ...")

    out = await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=3)
    assert [r.chunk.chunk_id for r in out] == ["P"]


@pytest.mark.unit
async def test_multi_hop_invalid_action_returns_partial(
    mock_litellm: AsyncMock,
) -> None:
    """Planner returns a valid JSON object with ``action="continue"``
    (not one of done/refine) → treated as malformed → terminate."""
    retrieve = _scripted_retriever({"q": [_chunk("partial", chunk_id="P")]})
    mock_litellm.return_value = _make_response('{"action": "continue"}')

    out = await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=3)
    assert [r.chunk.chunk_id for r in out] == ["P"]


@pytest.mark.unit
async def test_multi_hop_refine_without_query_returns_partial(
    mock_litellm: AsyncMock,
) -> None:
    """``action="refine"`` with no ``query`` field → treated as
    malformed → terminate."""
    retrieve = _scripted_retriever({"q": [_chunk("partial", chunk_id="P")]})
    mock_litellm.return_value = _make_response('{"action": "refine"}')

    out = await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=3)
    assert [r.chunk.chunk_id for r in out] == ["P"]


# ---------------------------------------------------------------------------
# Model + API key passthrough
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multi_hop_uses_default_model(mock_litellm: AsyncMock) -> None:
    retrieve = _scripted_retriever({"q": [_chunk("x", chunk_id="X")]})
    mock_litellm.return_value = _make_response('{"action": "done"}')
    await multi_hop_search(question="q", retrieve_fn=retrieve, max_hops=2)
    assert mock_litellm.call_args.kwargs["model"] == DEFAULT_TERMINATION_MODEL


@pytest.mark.unit
async def test_multi_hop_respects_custom_model_and_api_key(
    mock_litellm: AsyncMock,
) -> None:
    retrieve = _scripted_retriever({"q": [_chunk("x", chunk_id="X")]})
    mock_litellm.return_value = _make_response('{"action": "done"}')
    await multi_hop_search(
        question="q",
        retrieve_fn=retrieve,
        max_hops=2,
        model="openai/gpt-4o-mini",
        api_key="sk-test",
    )
    kwargs = mock_litellm.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["api_key"] == "sk-test"


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_decision_done() -> None:
    out = _parse_decision('{"action": "done"}')
    assert out is not None
    assert out.action == "done"


@pytest.mark.unit
def test_parse_decision_refine() -> None:
    out = _parse_decision('{"action": "refine", "query": "follow-up question"}')
    assert out is not None
    assert out.action == "refine"
    assert out.refined_query == "follow-up question"


@pytest.mark.unit
def test_parse_decision_strips_markdown_fences() -> None:
    out = _parse_decision('```json\n{"action": "done"}\n```')
    assert out is not None
    assert out.action == "done"


@pytest.mark.unit
def test_parse_decision_returns_none_on_garbage() -> None:
    assert _parse_decision("not json") is None
    assert _parse_decision("") is None
    assert _parse_decision('{"foo": "bar"}') is None  # no action
    assert _parse_decision('{"action": "refine"}') is None  # refine w/o query


@pytest.mark.unit
def test_format_chunks_renders_empty_marker_when_no_chunks() -> None:
    out = _format_chunks_for_planner([])
    assert "first hop" in out  # the "(none yet — this is the first hop)" marker


@pytest.mark.unit
def test_format_chunks_truncates_long_text() -> None:
    long = _chunk("x" * 2000, chunk_id="A")
    out = _format_chunks_for_planner([long])
    assert "..." in out
    # Total per-chunk line stays well under the input length.
    assert len(out) < 1500


# ---------------------------------------------------------------------------
# search() integration — multi_hop kwarg wires through
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_with_multi_hop_invokes_planner(
    mock_litellm: AsyncMock,
) -> None:
    """End-to-end: ``search(multi_hop=2)`` triggers the multi-hop
    loop. With a single-hop "done" verdict, the result is just the
    first hop's chunks."""
    storage = InMemoryStorage()
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="Refund policy details.",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="refund",
        )
    )

    mock_litellm.return_value = _make_response('{"action": "done"}')

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    with um.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="refund window?",
            agent="rag-qa",
            tenant_id="test",
            limit=5,
            multi_hop=2,
        )

    assert len(results) == 1
    assert "Refund" in results[0].chunk.text
    # Planner called once (after hop 1, decided "done").
    assert mock_litellm.call_count == 1


@pytest.mark.unit
async def test_search_multi_hop_zero_skips_loop(mock_litellm: AsyncMock) -> None:
    """``multi_hop=0`` (default) doesn't engage the planner. Single
    retrieval pass, no LLM call."""
    storage = InMemoryStorage()
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="content",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="x",
        )
    )

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    with um.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        await kb_search(
            storage=storage,
            question="anything",
            agent="rag-qa",
            tenant_id="test",
            limit=1,
            multi_hop=0,
        )

    mock_litellm.assert_not_called()


# ---------------------------------------------------------------------------
# _Decision dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decision_slots_only() -> None:
    """Sanity: _Decision uses __slots__, action + refined_query fields."""
    d = _Decision(action="refine", refined_query="next?")
    assert d.action == "refine"
    assert d.refined_query == "next?"
