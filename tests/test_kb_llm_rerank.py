"""Tests for ``movate.kb.rerank`` — LLM-based candidate rerank stage.

Same graceful-degradation contract as the query rewriter: every
failure mode must return ``candidates[:limit]`` (the upstream order)
rather than raising. Tests cover happy parse, format/prompt sanity,
defensive parsing, and end-to-end ``search(..., rerank=True)``
integration.

LiteLLM is monkey-patched module-wide so tests run hermetically.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock as um
from unittest.mock import AsyncMock

import litellm
import pytest

from movate.core.models import KbChunk, KbChunkWithScore
from movate.kb.rerank import (
    DEFAULT_RERANKER_MODEL,
    MAX_RERANK_CANDIDATES,
    _extract_content,
    _format_candidates,
    _parse_rankings,
    llm_rerank,
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rerank_reorders_by_llm_scores(mock_litellm: AsyncMock) -> None:
    """The LLM's relevance scores override the upstream order.

    Upstream order is A > B > C (by upstream score). The LLM says
    C is most relevant, then A, then B → rerank reorders accordingly.
    """
    mock_litellm.return_value = _make_response(
        '{"rankings": [{"id": 1, "score": 0.6}, {"id": 2, "score": 0.2}, {"id": 3, "score": 0.95}]}'
    )
    candidates = [
        _chunk("Refund policy text", chunk_id="A", score=0.9),
        _chunk("Office hours text", chunk_id="B", score=0.8),
        _chunk("Detailed refund window with examples", chunk_id="C", score=0.7),
    ]
    out = await llm_rerank(question="refund window?", candidates=candidates, limit=3)

    # New order: C (0.95) > A (0.6) > B (0.2).
    assert [r.chunk.chunk_id for r in out] == ["C", "A", "B"]
    # Scores REPLACED with the rerank scores, not upstream's.
    assert out[0].score == pytest.approx(0.95)
    assert out[1].score == pytest.approx(0.6)
    assert out[2].score == pytest.approx(0.2)


@pytest.mark.unit
async def test_rerank_trims_to_limit(mock_litellm: AsyncMock) -> None:
    """``limit`` caps the returned list AFTER reranking — the LLM
    sees all 5 candidates so it can score them in context."""
    mock_litellm.return_value = _make_response(
        '{"rankings": [{"id": 1, "score": 0.1}, {"id": 2, "score": 0.9}, '
        '{"id": 3, "score": 0.8}, {"id": 4, "score": 0.5}, {"id": 5, "score": 0.3}]}'
    )
    candidates = [_chunk(f"text {i}", chunk_id=f"chunk_{i}", score=0.5) for i in range(1, 6)]
    out = await llm_rerank(question="any?", candidates=candidates, limit=2)
    assert len(out) == 2
    # Top 2 by rerank: chunk_2 (0.9), chunk_3 (0.8).
    assert [r.chunk.chunk_id for r in out] == ["chunk_2", "chunk_3"]


@pytest.mark.unit
async def test_rerank_strips_markdown_fences(mock_litellm: AsyncMock) -> None:
    """Model wraps JSON in ```json...``` → tolerant parsing handles it."""
    mock_litellm.return_value = _make_response(
        '```json\n{"rankings": [{"id": 1, "score": 0.7}]}\n```'
    )
    out = await llm_rerank(
        question="test",
        candidates=[_chunk("a", chunk_id="A")],
        limit=1,
    )
    assert len(out) == 1
    assert out[0].chunk.chunk_id == "A"
    assert out[0].score == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Short-circuit cases (no LLM call)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rerank_empty_candidates_returns_empty(mock_litellm: AsyncMock) -> None:
    out = await llm_rerank(question="anything", candidates=[], limit=5)
    assert out == []
    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_rerank_empty_question_returns_upstream_order(
    mock_litellm: AsyncMock,
) -> None:
    """Empty question → return candidates unchanged (we have no
    signal to rerank against)."""
    candidates = [_chunk("a", chunk_id="A"), _chunk("b", chunk_id="B")]
    out = await llm_rerank(question="   ", candidates=candidates, limit=5)
    assert [r.chunk.chunk_id for r in out] == ["A", "B"]
    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_rerank_zero_limit_returns_empty(mock_litellm: AsyncMock) -> None:
    candidates = [_chunk("a", chunk_id="A")]
    out = await llm_rerank(question="test", candidates=candidates, limit=0)
    assert out == []
    mock_litellm.assert_not_called()


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rerank_llm_exception_returns_upstream(mock_litellm: AsyncMock) -> None:
    """LLM call raises → return ``candidates[:limit]`` with original scores."""
    mock_litellm.side_effect = RuntimeError("API timeout")
    candidates = [
        _chunk("a", chunk_id="A", score=0.9),
        _chunk("b", chunk_id="B", score=0.5),
    ]
    out = await llm_rerank(question="test", candidates=candidates, limit=2)
    assert [r.chunk.chunk_id for r in out] == ["A", "B"]
    assert out[0].score == pytest.approx(0.9)  # original score, not LLM


@pytest.mark.unit
async def test_rerank_malformed_json_returns_upstream(mock_litellm: AsyncMock) -> None:
    mock_litellm.return_value = _make_response("Sure, here are my rankings: ...")
    candidates = [_chunk("a", chunk_id="A", score=0.9)]
    out = await llm_rerank(question="test", candidates=candidates, limit=1)
    assert [r.chunk.chunk_id for r in out] == ["A"]
    assert out[0].score == pytest.approx(0.9)


@pytest.mark.unit
async def test_rerank_wrong_schema_returns_upstream(mock_litellm: AsyncMock) -> None:
    """Valid JSON but missing the ``rankings`` key → fall back."""
    mock_litellm.return_value = _make_response('{"scores": [0.9, 0.5]}')
    candidates = [_chunk("a", chunk_id="A", score=0.9)]
    out = await llm_rerank(question="test", candidates=candidates, limit=1)
    assert [r.chunk.chunk_id for r in out] == ["A"]


@pytest.mark.unit
async def test_rerank_out_of_range_ids_dropped(mock_litellm: AsyncMock) -> None:
    """The model hallucinates id=99 in a 2-candidate set → that
    entry gets dropped; the rest still rerank correctly."""
    mock_litellm.return_value = _make_response(
        '{"rankings": [{"id": 99, "score": 0.99}, '
        '{"id": 1, "score": 0.3}, {"id": 2, "score": 0.7}]}'
    )
    candidates = [_chunk("a", chunk_id="A"), _chunk("b", chunk_id="B")]
    out = await llm_rerank(question="test", candidates=candidates, limit=2)
    # B ranks above A because of the legitimate (in-range) rankings.
    assert [r.chunk.chunk_id for r in out] == ["B", "A"]


@pytest.mark.unit
async def test_rerank_nan_scores_dropped(mock_litellm: AsyncMock) -> None:
    """NaN / inf scores get filtered. The remaining valid entry
    still ranks correctly. The all-invalid case falls back to
    upstream order."""
    mock_litellm.return_value = _make_response(
        '{"rankings": [{"id": 1, "score": "not a number"}, {"id": 2, "score": 0.7}]}'
    )
    candidates = [_chunk("a", chunk_id="A"), _chunk("b", chunk_id="B")]
    out = await llm_rerank(question="test", candidates=candidates, limit=2)
    # Only B has a valid score; A was filtered. Result: [B].
    assert [r.chunk.chunk_id for r in out] == ["B"]


# ---------------------------------------------------------------------------
# MAX_RERANK_CANDIDATES truncation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rerank_truncates_to_max_candidates(mock_litellm: AsyncMock) -> None:
    """When more than MAX_RERANK_CANDIDATES are passed, only the
    first N go to the LLM. Prevents prompt-size blowout."""
    mock_litellm.return_value = _make_response('{"rankings": []}')
    candidates = [_chunk(f"text {i}", chunk_id=f"c_{i}") for i in range(MAX_RERANK_CANDIDATES + 10)]
    await llm_rerank(question="any", candidates=candidates, limit=5)
    # Inspect the prompt to confirm only MAX_RERANK_CANDIDATES were sent.
    prompt = mock_litellm.call_args.kwargs["messages"][0]["content"]
    # Last sent candidate should be [MAX], not [MAX+1] or later.
    assert f"[{MAX_RERANK_CANDIDATES}]" in prompt
    assert f"[{MAX_RERANK_CANDIDATES + 1}]" not in prompt


# ---------------------------------------------------------------------------
# Model + API key passthrough
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rerank_uses_default_model(mock_litellm: AsyncMock) -> None:
    mock_litellm.return_value = _make_response('{"rankings": []}')
    await llm_rerank(
        question="test",
        candidates=[_chunk("a", chunk_id="A")],
        limit=1,
    )
    assert mock_litellm.call_args.kwargs["model"] == DEFAULT_RERANKER_MODEL


@pytest.mark.unit
async def test_rerank_respects_custom_model_and_api_key(
    mock_litellm: AsyncMock,
) -> None:
    mock_litellm.return_value = _make_response('{"rankings": []}')
    await llm_rerank(
        question="test",
        candidates=[_chunk("a", chunk_id="A")],
        limit=1,
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
def test_format_candidates_renders_numbered_list() -> None:
    candidates = [
        _chunk("First chunk text", chunk_id="A"),
        _chunk("Second chunk text", chunk_id="B"),
    ]
    formatted = _format_candidates(candidates)
    assert formatted.startswith("[1] First chunk text")
    assert "[2] Second chunk text" in formatted


@pytest.mark.unit
def test_format_candidates_truncates_long_text() -> None:
    """A 2000-char chunk gets clipped to MAX_CHUNK_CHARS_FOR_RERANK + '...'.

    Prevents one huge chunk from blowing out the prompt token budget."""
    long_text = "x" * 2000
    candidates = [_chunk(long_text, chunk_id="A")]
    formatted = _format_candidates(candidates)
    # Truncated text + ellipsis. Total < 1000 chars per candidate line.
    assert "..." in formatted
    assert len(formatted) < 1500  # Sanity: not 2000+ chars


@pytest.mark.unit
def test_format_candidates_replaces_newlines() -> None:
    """Multi-paragraph chunks get newlines replaced with spaces so
    each candidate stays on one line in the prompt."""
    candidates = [_chunk("Line one.\nLine two.\n\nLine three.", chunk_id="A")]
    formatted = _format_candidates(candidates)
    assert "\n" not in formatted.split("[1]", 1)[1].split("\n", 1)[0] if "\n" in formatted else True
    assert "Line one. Line two." in formatted


@pytest.mark.unit
def test_parse_rankings_filters_out_of_range() -> None:
    """Rankings with ``id`` outside ``1..n_candidates`` get dropped."""
    raw = (
        '{"rankings": [{"id": 0, "score": 0.5}, {"id": 1, "score": 0.7}, {"id": 5, "score": 0.3}]}'
    )
    out = _parse_rankings(raw, n_candidates=2)
    # Only id=1 survives (id=0 and id=5 are out of range for n=2).
    assert out == [(1, 0.7)]


@pytest.mark.unit
def test_parse_rankings_returns_empty_on_garbage() -> None:
    assert _parse_rankings("not json", n_candidates=3) == []
    assert _parse_rankings("", n_candidates=3) == []
    assert _parse_rankings('{"rankings": "not a list"}', n_candidates=3) == []


@pytest.mark.unit
def test_extract_content_handles_missing_fields() -> None:
    assert _extract_content(object()) == ""
    assert _extract_content(SimpleNamespace(choices=[])) == ""


# ---------------------------------------------------------------------------
# search() integration — end-to-end rerank wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_with_rerank_invokes_reranker(mock_litellm: AsyncMock) -> None:
    """``search(rerank=True)`` fetches a wider upstream pool then
    LLM-reranks down to ``limit``."""
    storage = InMemoryStorage()
    # Three chunks, all with the same embedding so vector ranking is
    # by insertion order. Reranker will reorder.
    chunks = [
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source=f"/tmp/{cid}.md",
            text=text,
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash=cid,
        )
        for cid, text in [
            ("first", "Less relevant chunk that came first in upstream."),
            ("second", "Highly relevant chunk about the actual question."),
            ("third", "Tangentially related chunk."),
        ]
    ]
    for c in chunks:
        await storage.save_kb_chunk(c)

    # Reranker scores second highest.
    mock_litellm.return_value = _make_response(
        '{"rankings": [{"id": 1, "score": 0.3}, {"id": 2, "score": 0.95}, {"id": 3, "score": 0.2}]}'
    )

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    with um.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="the actual question",
            agent="rag-qa",
            tenant_id="test",
            limit=2,
            rerank=True,
        )

    # Reranker said "second" is most relevant → it's first.
    assert results[0].chunk.content_hash == "second"
    # Score on the returned object is the rerank score, not vector.
    assert results[0].score == pytest.approx(0.95)


@pytest.mark.unit
async def test_search_rerank_false_skips_llm(mock_litellm: AsyncMock) -> None:
    """``rerank=False`` (default) doesn't call the LLM. Preserves
    cost on the default code path."""
    storage = InMemoryStorage()
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="Some KB content.",
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
            rerank=False,
        )

    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_search_rerank_failure_falls_back(mock_litellm: AsyncMock) -> None:
    """Rerank LLM exception → search returns the upstream top-K
    (not an exception). End-to-end graceful degradation."""
    storage = InMemoryStorage()
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="Important content.",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="x",
        )
    )

    mock_litellm.side_effect = RuntimeError("Reranker down")

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    with um.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="anything",
            agent="rag-qa",
            tenant_id="test",
            limit=1,
            rerank=True,
        )

    assert len(results) == 1
    assert "Important" in results[0].chunk.text
