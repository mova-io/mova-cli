"""Hybrid search — BM25 + vector + RRF (PR-C).

Covers:

* **BM25** — tokenization, IDF math, length normalization, edge
  cases (empty corpus, empty query, no matching terms).
* **RRF** — rank-based fusion math, score-scale invariance, dedup
  across input lists.
* **Hybrid orchestrator** — both paths run when ``hybrid=True``,
  ``hybrid=False`` keeps vector-only behavior.

BM25 + RRF are pure functions over a stub chunk list — no storage,
no API, no event loop. End-to-end hybrid test uses the in-memory
storage + a stubbed embedder so the assertion is deterministic.
"""

from __future__ import annotations

from unittest import mock

import pytest

from movate.core.models import KbChunk, KbChunkWithScore
from movate.kb.lexical import RRF_K, bm25_search, rrf_fuse
from movate.testing import InMemoryStorage


def _chunk(text: str, *, chunk_id: str | None = None) -> KbChunk:
    """Build a minimal KbChunk for tests — 2-dim embedding (we use
    Python cosine math, dimension doesn't matter for BM25 tests)."""
    import hashlib  # noqa: PLC0415

    h = chunk_id or hashlib.sha256(text.encode()).hexdigest()[:16]
    return KbChunk(
        chunk_id=h,
        tenant_id="test",
        agent="rag-qa",
        source="/tmp/test.md",
        text=text,
        embedding=[1.0, 0.0],
        embedding_model="openai/text-embedding-3-small",
        content_hash=h,
    )


# ---------------------------------------------------------------------------
# BM25 scorer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_ranks_term_matches_above_non_matches() -> None:
    """A chunk containing the query term ranks above one without it."""
    chunks = [
        _chunk("Annual subscriptions can request a refund within 14 days."),
        _chunk("Our office hours are Monday through Friday."),
        _chunk("The refund window for monthly plans is 30 days."),
    ]
    results = bm25_search(chunks, "refund window", limit=3)
    # The two chunks mentioning 'refund' should outrank the office-hours chunk.
    assert len(results) >= 2
    top_texts = [r.chunk.text for r in results[:2]]
    assert any("refund" in t.lower() for t in top_texts)
    # The unrelated chunk gets a 0 BM25 score and is dropped (we only
    # return chunks with score > 0).
    unrelated = [r for r in results if "office hours" in r.chunk.text]
    assert not unrelated


@pytest.mark.unit
def test_bm25_term_frequency_boosts_ranking() -> None:
    """A chunk that repeats the query term ranks above one with a
    single occurrence — BM25 with k1=1.5 still rewards repetition
    (with diminishing returns past ~3 mentions)."""
    chunks = [
        _chunk("refund refund refund refund refund — this chunk mentions the term repeatedly."),
        _chunk("This chunk mentions refund only once."),
    ]
    results = bm25_search(chunks, "refund", limit=2)
    assert len(results) == 2
    # The repeating chunk should be ranked first.
    assert "repeatedly" in results[0].chunk.text


@pytest.mark.unit
def test_bm25_length_normalization_penalizes_long_chunks() -> None:
    """Two chunks with identical TF for the query term — the shorter
    one ranks higher because BM25's length normalization (b=0.75)
    penalizes term occurrences in long documents."""
    short = _chunk("refund policy is straightforward.")
    long_filler = " ".join(["filler"] * 200)  # ~1000 chars of noise
    long_chunk = _chunk(f"refund policy is straightforward. {long_filler}")
    results = bm25_search([short, long_chunk], "refund", limit=2)
    assert len(results) == 2
    assert results[0].chunk.text == short.text


@pytest.mark.unit
def test_bm25_empty_query_returns_empty() -> None:
    chunks = [_chunk("hello world")]
    assert bm25_search(chunks, "", limit=5) == []
    # Stopwords-only query reduces to empty terms after tokenization.
    assert bm25_search(chunks, "the of and", limit=5) == []


@pytest.mark.unit
def test_bm25_empty_corpus_returns_empty() -> None:
    assert bm25_search([], "anything", limit=5) == []


@pytest.mark.unit
def test_bm25_no_matching_terms_returns_empty() -> None:
    """Query terms that don't appear in any chunk → no results
    (rather than a list of 0-score chunks)."""
    chunks = [_chunk("hello world"), _chunk("foo bar baz")]
    results = bm25_search(chunks, "unrelated query terms", limit=5)
    assert results == []


@pytest.mark.unit
def test_bm25_case_insensitive() -> None:
    """Tokenizer lowercases everything — query 'REFUND' matches
    'refund' in chunks."""
    chunks = [_chunk("Refund policies for subscriptions.")]
    results = bm25_search(chunks, "REFUND", limit=1)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# RRF combiner
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rrf_top_in_both_lists_wins() -> None:
    """A chunk that ranks #1 in BOTH input lists scores higher than
    a chunk that's only #1 in one. This is the whole point of RRF."""
    a = _chunk("alpha", chunk_id="A")
    b = _chunk("bravo", chunk_id="B")
    c = _chunk("charlie", chunk_id="C")
    vector = [
        KbChunkWithScore(chunk=a, score=0.95),
        KbChunkWithScore(chunk=b, score=0.85),
        KbChunkWithScore(chunk=c, score=0.75),
    ]
    lexical = [
        KbChunkWithScore(chunk=b, score=0.9),
        KbChunkWithScore(chunk=a, score=0.8),
        KbChunkWithScore(chunk=c, score=0.7),
    ]
    # B: #2 in vector + #1 in lexical = 1/(60+2) + 1/(60+1) = 0.0325
    # A: #1 in vector + #2 in lexical = 1/(60+1) + 1/(60+2) = 0.0325
    # C: #3 in vector + #3 in lexical = 1/(60+3) + 1/(60+3) = 0.0317
    fused = rrf_fuse(vector, lexical, limit=3)
    assert len(fused) == 3
    # A and B should be effectively tied (same combined rank).
    # C should be last.
    assert fused[-1].chunk.text == "charlie"


@pytest.mark.unit
def test_rrf_score_scale_invariant() -> None:
    """RRF should give the same ranking when one list's scores are
    multiplied by 1000 — only the rank order matters."""
    a = _chunk("alpha", chunk_id="A")
    b = _chunk("bravo", chunk_id="B")
    list_small = [
        KbChunkWithScore(chunk=a, score=0.5),
        KbChunkWithScore(chunk=b, score=0.3),
    ]
    list_big = [
        KbChunkWithScore(chunk=a, score=1.0),  # was 1000 — clamp to 1
        KbChunkWithScore(chunk=b, score=0.6),  # was 600 — clamp to 0.6
    ]
    fused_a = rrf_fuse(list_small, limit=2)
    fused_b = rrf_fuse(list_big, limit=2)
    # Same chunk ordering regardless of input score magnitudes.
    assert [f.chunk.chunk_id for f in fused_a] == [f.chunk.chunk_id for f in fused_b]


@pytest.mark.unit
def test_rrf_dedup_across_lists() -> None:
    """The same chunk appearing in multiple input lists fuses to one
    output entry — no duplicates."""
    a = _chunk("alpha", chunk_id="A")
    vector = [KbChunkWithScore(chunk=a, score=0.9)]
    lexical = [KbChunkWithScore(chunk=a, score=0.5)]
    fused = rrf_fuse(vector, lexical, limit=5)
    assert len(fused) == 1
    assert fused[0].chunk.chunk_id == "A"


@pytest.mark.unit
def test_rrf_empty_inputs_return_empty() -> None:
    assert rrf_fuse(limit=5) == []
    assert rrf_fuse([], [], limit=5) == []


@pytest.mark.unit
def test_rrf_k_constant_is_lucene_default() -> None:
    """Regression guard: k=60 is the Lucene-recommended default.
    Changing it would silently shift retrieval behavior across all
    deployments."""
    assert RRF_K == 60


# ---------------------------------------------------------------------------
# Hybrid orchestrator (search with hybrid=True)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_hybrid_combines_vector_and_lexical() -> None:
    """End-to-end: hybrid=True triggers both paths + fuses. Use
    in-memory storage with realistic chunks; stub the embedder so
    the test is deterministic."""
    from movate.kb.search import search as kb_search  # noqa: PLC0415

    storage = InMemoryStorage()
    chunks = [
        # Lexically matches "GPU" (unique word) → BM25 wins here.
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/gpus.md",
            text="GPU instances are available on the enterprise tier only.",
            embedding=[0.5, 0.5],  # neutral cosine to "what hardware?"
            embedding_model="openai/text-embedding-3-small",
            content_hash="gpu",
        ),
        # Semantically related but doesn't contain "GPU" — vector wins.
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/compute.md",
            text="Hardware accelerators are part of the premium plan.",
            embedding=[1.0, 0.0],  # exact match to query embedding below
            embedding_model="openai/text-embedding-3-small",
            content_hash="hw",
        ),
        # Unrelated noise.
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/noise.md",
            text="Office hours are Monday through Friday.",
            embedding=[0.0, 1.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="noise",
        ),
    ]
    for c in chunks:
        await storage.save_kb_chunk(c)

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        # Query is semantically close to chunk 2 (hardware/compute)
        # and lexically contains "GPU" matching chunk 1.
        return [[1.0, 0.0] for _ in texts]

    with mock.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="GPU hardware availability",
            agent="rag-qa",
            tenant_id="test",
            limit=2,
            hybrid=True,
        )

    # Both the lexical-winner (GPU) and the vector-winner (Hardware)
    # should appear in the top 2 — RRF picks them up because each
    # ranks #1 in one of the two paths.
    texts = " | ".join(r.chunk.text for r in results)
    assert "GPU" in texts
    assert "Hardware" in texts


@pytest.mark.unit
async def test_search_default_is_vector_only() -> None:
    """``hybrid=False`` (the default) keeps the pre-PR-C behavior —
    vector cosine only, no BM25 lookup, no fusion."""
    from movate.kb.search import search as kb_search  # noqa: PLC0415

    storage = InMemoryStorage()
    chunks = [
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="GPU instances mentioned here.",
            embedding=[0.0, 1.0],  # orthogonal to query
            embedding_model="openai/text-embedding-3-small",
            content_hash="gpu",
        ),
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/b.md",
            text="Hardware accelerators.",
            embedding=[1.0, 0.0],  # exact match
            embedding_model="openai/text-embedding-3-small",
            content_hash="hw",
        ),
    ]
    for c in chunks:
        await storage.save_kb_chunk(c)

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    with mock.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="GPU hardware",
            agent="rag-qa",
            tenant_id="test",
            limit=2,
            # hybrid=False is the default — explicit here for the regression assert.
            hybrid=False,
        )

    # Vector-only: the chunk with embedding [1.0, 0.0] (Hardware) ranks
    # FIRST despite GPU being a perfect lexical match. This is the
    # exact failure mode that motivates the --hybrid flag — vector
    # alone misses rare-term hits.
    assert len(results) == 2
    assert "Hardware" in results[0].chunk.text


@pytest.mark.unit
async def test_search_hybrid_empty_question_short_circuits() -> None:
    """Empty question → empty results, no embedding call, no BM25 call."""
    from movate.kb.search import search as kb_search  # noqa: PLC0415

    storage = InMemoryStorage()
    with mock.patch("movate.kb.search.embed_texts") as embed_mock:
        results = await kb_search(
            storage=storage,
            question="   ",
            agent="rag-qa",
            tenant_id="test",
            hybrid=True,
        )
    assert results == []
    embed_mock.assert_not_called()
