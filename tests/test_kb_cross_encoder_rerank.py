"""Tests for cross-encoder rerank (PR-BB).

Coverage:
* cross_encoder_rerank — happy path with mocked CrossEncoder
* cross_encoder_rerank — falls back on ImportError (package not installed)
* cross_encoder_rerank — falls back on runtime exception
* cross_encoder_rerank — empty candidates → []
* cross_encoder_rerank — wrong score count → fallback
* RetrievalConfig.rerank_mode field + is_default()
* search() rerank_mode="cross_encoder" dispatches to cross_encoder_rerank
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from movate.core.models import KbChunk, KbChunkWithScore, RetrievalConfig
from movate.kb.rerank import (
    DEFAULT_CROSS_ENCODER_MODEL,
    cross_encoder_rerank,
)


def _make_chunk(chunk_id: str, text: str = "some text") -> KbChunkWithScore:
    chunk = KbChunk(
        chunk_id=chunk_id,
        tenant_id="t1",
        agent="a",
        source="f.md",
        text=text,
        embedding=[0.1] * 1536,
        embedding_model="text-embedding-3-small",
        content_hash=chunk_id,
        metadata=None,
        created_at=datetime.now(UTC),
    )
    return KbChunkWithScore(chunk=chunk, score=0.5)


# ---------------------------------------------------------------------------
# cross_encoder_rerank unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_ce_rerank_happy_path() -> None:
    """Mock CrossEncoder.predict → chunks re-sorted by score."""
    candidates = [
        _make_chunk("low", "barely relevant text"),
        _make_chunk("high", "extremely relevant answer to the question"),
        _make_chunk("mid", "somewhat related context"),
    ]

    mock_ce = MagicMock()
    # Scores: low→-2.0, high→5.0, mid→1.0 (logits, higher = more relevant)
    mock_ce.predict.return_value = [-2.0, 5.0, 1.0]

    with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
        import sentence_transformers  # noqa: PLC0415

        sentence_transformers.CrossEncoder = MagicMock(return_value=mock_ce)

        # Clear cache so the mock is loaded fresh.
        from movate.kb import rerank as rerank_mod  # noqa: PLC0415

        rerank_mod._CE_CACHE.clear()

        result = await cross_encoder_rerank(
            question="What is the answer?",
            candidates=candidates,
            limit=3,
            model=DEFAULT_CROSS_ENCODER_MODEL,
        )

    assert len(result) == 3
    # "high" chunk must rank first (score 5.0 → tanh(1.0) ≈ 0.76)
    assert result[0].chunk.chunk_id == "high"
    assert result[1].chunk.chunk_id == "mid"
    assert result[2].chunk.chunk_id == "low"
    # Scores should be tanh-normalized — all in (-1, 1)
    for r in result:
        assert -1.0 <= r.score <= 1.0


@pytest.mark.unit
async def test_ce_rerank_limit_applied() -> None:
    """limit=2 → only top 2 returned."""
    candidates = [_make_chunk(f"c{i}") for i in range(5)]

    mock_ce = MagicMock()
    mock_ce.predict.return_value = [1.0, 2.0, 3.0, 4.0, 5.0]

    with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
        import sentence_transformers  # noqa: PLC0415

        sentence_transformers.CrossEncoder = MagicMock(return_value=mock_ce)

        from movate.kb import rerank as rerank_mod  # noqa: PLC0415

        rerank_mod._CE_CACHE.clear()

        result = await cross_encoder_rerank(
            question="q",
            candidates=candidates,
            limit=2,
            model=DEFAULT_CROSS_ENCODER_MODEL,
        )

    assert len(result) == 2
    # c4 had score 5.0 — should be first
    assert result[0].chunk.chunk_id == "c4"


@pytest.mark.unit
async def test_ce_rerank_falls_back_on_import_error() -> None:
    """sentence_transformers not installed → returns candidates[:limit] unchanged."""
    candidates = [_make_chunk("c1"), _make_chunk("c2"), _make_chunk("c3")]

    with patch.dict("sys.modules", {"sentence_transformers": None}):
        result = await cross_encoder_rerank(
            question="q",
            candidates=candidates,
            limit=2,
        )

    assert len(result) == 2
    assert result[0].chunk.chunk_id == "c1"
    assert result[1].chunk.chunk_id == "c2"


@pytest.mark.unit
async def test_ce_rerank_falls_back_on_runtime_error() -> None:
    """predict() raises RuntimeError → fallback to input order."""
    candidates = [_make_chunk("c1"), _make_chunk("c2")]

    mock_ce = MagicMock()
    mock_ce.predict.side_effect = RuntimeError("CUDA OOM")

    with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
        import sentence_transformers  # noqa: PLC0415

        sentence_transformers.CrossEncoder = MagicMock(return_value=mock_ce)

        from movate.kb import rerank as rerank_mod  # noqa: PLC0415

        rerank_mod._CE_CACHE.clear()

        result = await cross_encoder_rerank(
            question="q",
            candidates=candidates,
            limit=2,
        )

    assert result[0].chunk.chunk_id == "c1"


@pytest.mark.unit
async def test_ce_rerank_empty_candidates() -> None:
    """Empty input → empty output, no model load."""
    result = await cross_encoder_rerank(question="q", candidates=[], limit=5)
    assert result == []


@pytest.mark.unit
async def test_ce_rerank_empty_question() -> None:
    """Empty question → returns candidates[:limit] unchanged."""
    candidates = [_make_chunk("c1"), _make_chunk("c2")]
    result = await cross_encoder_rerank(question="  ", candidates=candidates, limit=2)
    assert len(result) == 2
    assert result[0].chunk.chunk_id == "c1"


@pytest.mark.unit
async def test_ce_rerank_wrong_score_count() -> None:
    """predict() returns wrong number of scores → fallback."""
    candidates = [_make_chunk("c1"), _make_chunk("c2"), _make_chunk("c3")]

    mock_ce = MagicMock()
    mock_ce.predict.return_value = [1.0]  # only 1 score for 3 candidates

    with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
        import sentence_transformers  # noqa: PLC0415

        sentence_transformers.CrossEncoder = MagicMock(return_value=mock_ce)

        from movate.kb import rerank as rerank_mod  # noqa: PLC0415

        rerank_mod._CE_CACHE.clear()

        result = await cross_encoder_rerank(
            question="q",
            candidates=candidates,
            limit=2,
        )

    # Fallback: original order, trimmed to limit
    assert len(result) == 2
    assert result[0].chunk.chunk_id == "c1"


# ---------------------------------------------------------------------------
# RetrievalConfig.rerank_mode field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retrieval_config_rerank_mode_default() -> None:
    """Default rerank_mode is 'llm'."""
    cfg = RetrievalConfig()
    assert cfg.rerank_mode == "llm"
    assert cfg.is_default()


@pytest.mark.unit
def test_retrieval_config_rerank_mode_cross_encoder_not_default() -> None:
    """rerank_mode='cross_encoder' is non-default."""
    cfg = RetrievalConfig(rerank=True, rerank_mode="cross_encoder")
    assert not cfg.is_default()


# ---------------------------------------------------------------------------
# search() rerank dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_dispatches_cross_encoder_rerank() -> None:
    """search(rerank=True, rerank_mode='cross_encoder') calls cross_encoder_rerank."""
    from movate.kb import search as search_mod  # noqa: PLC0415
    from movate.kb.embed import DEFAULT_EMBEDDING_MODEL  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.init()

    # Stub embed_texts so no real HTTP call.
    async def _fake_embed(texts: list[str], **_: Any) -> list[list[float]]:
        return [[0.1] * 1536 for _ in texts]

    # Seed one chunk.
    from datetime import UTC, datetime  # noqa: PLC0415

    chunk = KbChunk(
        chunk_id="x",
        tenant_id="t1",
        agent="a",
        source="f.md",
        text="the answer",
        embedding=[0.1] * 1536,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        content_hash="x",
        metadata=None,
        created_at=datetime.now(UTC),
    )
    await storage.save_kb_chunk(chunk)

    ce_called: list[bool] = []

    async def _fake_ce_rerank(**kwargs: Any) -> list[KbChunkWithScore]:
        ce_called.append(True)
        return kwargs["candidates"][: kwargs["limit"]]

    with (
        patch.object(search_mod, "embed_texts", _fake_embed),
        patch("movate.kb.rerank.cross_encoder_rerank", _fake_ce_rerank),
    ):
        await search_mod.search(
            storage=storage,
            question="what is the answer",
            agent="a",
            tenant_id="t1",
            limit=1,
            rerank=True,
            rerank_mode="cross_encoder",
        )

    assert ce_called, "cross_encoder_rerank should have been called"
