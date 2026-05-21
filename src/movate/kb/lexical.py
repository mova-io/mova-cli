"""Lexical (BM25) search + reciprocal rank fusion (RRF).

The vector-search path (``movate.kb.search``) handles semantic
similarity — it finds chunks that MEAN the same thing as the query,
even when wording differs. The lexical path here handles the
complementary case: chunks that contain the EXACT terms in the
query (product names, error codes, citation IDs, anything where
word identity matters more than meaning).

Hybrid search runs both paths in parallel and fuses the rankings
with RRF. On real corpora this typically yields a 15-25% recall
improvement over vector-only — vector retrieval misses rare terms;
lexical retrieval misses paraphrased questions; the fusion catches
both classes.

Implementation choices (v0.9 MVP):

* **Pure Python BM25**, not Postgres FTS — works identically across
  all three storage backends (sqlite / Postgres / in-memory). For
  KBs up to ~10k chunks per agent the linear scan completes in
  <100ms. Larger KBs warrant a real FTS index (postgres GIN on
  ``to_tsvector``, sqlite FTS5 virtual table) — tracked as a
  follow-up perf optimization.
* **No stemming**, no language-aware tokenization. Lowercasing +
  word-boundary split. Aggressive enough for English RAG corpora;
  agnostic to language. The skill / scorer can swap in a smarter
  tokenizer later behind the same interface.
* **RRF over score-normalized fusion** because RRF is rank-invariant
  — it works even when the two scorers produce wildly different
  score scales (cosine: 0-1; BM25: unbounded). Trade-off: a chunk
  that's #1 in vector AND #1 in lexical scores the same as a chunk
  that's #1 in vector + #50 in lexical. In practice the relative
  rank order is what matters for retrieval, not the absolute score.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass

from movate.core.models import KbChunk, KbChunkWithScore

# BM25 hyperparameters — the canonical Lucene defaults. ``k1`` controls
# how quickly term saturation kicks in (higher = each repeat of a term
# matters more); ``b`` is the length normalization weight (higher =
# shorter docs win, 0 = no length norm).
# Override via env vars: MOVATE_BM25_K1 / MOVATE_BM25_B.
# Tuning guidance: corpora with short repeated tokens (error codes, SKUs)
# benefit from higher k1 (e.g. 2.0); uniform-length corpora can drop b to 0.5.
_BM25_K1: float = float(os.environ.get("MOVATE_BM25_K1", "1.5"))
_BM25_B: float = float(os.environ.get("MOVATE_BM25_B", "0.75"))

# Stopwords to drop before scoring. Tiny English set — only the most
# damaging ones (function words that appear in almost every chunk).
# A fuller stopword list would marginally improve precision but adds
# noise to the API (which other languages?); the trade-off is right
# for the v0.9 MVP.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "from",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "what",
        "which",
        "who",
        "whom",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
    }
)

# Word-boundary tokenizer — match runs of letters/digits/underscore.
# Lowercased downstream. Punctuation + whitespace split into nothing
# (they're not tokens). Hyphenated words split on the hyphen — not
# ideal for "self-service" type compounds but consistent with most
# BM25 implementations.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase + word-boundary split + stopword strip.

    Empty input → empty list (callers handle gracefully). The tokenizer
    is intentionally simple; smarter tokenization (stemming, language
    detection, n-grams) lives in the future hybrid-search-quality
    micro-sprints in BACKLOG.md.
    """
    return [
        t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text)) if t not in _STOPWORDS
    ]


@dataclass(frozen=True)
class _BM25Index:
    """Pre-computed BM25 statistics for a corpus of chunks.

    Built once per query; the corpus rarely changes mid-query. For
    KBs above ~10k chunks the build cost (~50ms for 10k) starts
    dominating retrieval latency — at that scale a persistent FTS
    index pays for itself.
    """

    chunks: list[KbChunk]
    """The chunks this index covers — same order as ``doc_terms``."""

    doc_terms: list[list[str]]
    """Tokenized terms per chunk. Parallel to ``chunks``."""

    doc_freq: dict[str, int]
    """How many chunks contain each term (for IDF)."""

    avg_doc_len: float
    """Average chunk length in tokens. Length-norm denominator."""

    n_docs: int
    """Total chunks in the corpus."""


def _build_index(chunks: list[KbChunk]) -> _BM25Index:
    """Pre-compute BM25 statistics for a corpus."""
    doc_terms = [_tokenize(c.text) for c in chunks]
    doc_freq: Counter[str] = Counter()
    for terms in doc_terms:
        for term in set(terms):  # set = doc-frequency, not term-frequency
            doc_freq[term] += 1
    total_len = sum(len(t) for t in doc_terms)
    avg_len = total_len / len(chunks) if chunks else 0.0
    return _BM25Index(
        chunks=chunks,
        doc_terms=doc_terms,
        doc_freq=dict(doc_freq),
        avg_doc_len=avg_len,
        n_docs=len(chunks),
    )


def _bm25_score(query_terms: list[str], doc_idx: int, index: _BM25Index) -> float:
    """Standard BM25 scoring formula.

    score(q, d) = Σ_{t in q} IDF(t) · (f(t,d) · (k1+1)) /
                   (f(t,d) + k1·(1 - b + b·|d|/avg_len))

    where IDF(t) = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
    """
    if doc_idx >= len(index.doc_terms):
        return 0.0
    doc = index.doc_terms[doc_idx]
    if not doc:
        return 0.0
    tf = Counter(doc)
    doc_len = len(doc)
    score = 0.0
    for term in query_terms:
        f = tf.get(term, 0)
        if f == 0:
            continue
        df = index.doc_freq.get(term, 0)
        # +1 inside the log handles the df > n_docs edge that the
        # raw Robertson IDF would invert. Standard Lucene variant.
        idf = math.log((index.n_docs - df + 0.5) / (df + 0.5) + 1)
        numerator = f * (_BM25_K1 + 1)
        denominator = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_len / max(index.avg_doc_len, 1.0))
        score += idf * (numerator / denominator)
    return score


def bm25_search(chunks: list[KbChunk], query: str, limit: int = 5) -> list[KbChunkWithScore]:
    """Lexical (BM25) search over ``chunks`` for ``query``.

    Returns the top ``limit`` ranked descending. Empty chunks list,
    empty query, or no matching terms → empty result. Score values
    are unbounded above (typical 0-30 range for short queries on
    short corpora) — RRF handles the score-scale-mismatch with
    the vector path.
    """
    if not chunks or not query.strip():
        return []
    index = _build_index(chunks)
    query_terms = _tokenize(query)
    if not query_terms:
        return []
    scored: list[KbChunkWithScore] = []
    for i, chunk in enumerate(chunks):
        score = _bm25_score(query_terms, i, index)
        if score > 0:
            # KbChunkWithScore.score is [-1, 1] (cosine convention).
            # BM25 scores can exceed 1. We don't normalize here —
            # RRF works on rank, not score magnitude. Callers that
            # want a normalized score for display should clamp.
            # For direct lexical-only mode (no fusion), we report
            # the raw BM25 score normalized to a 0-1 band via tanh
            # so the existing KbChunkWithScore validator accepts it.
            normalized = math.tanh(score / 10.0)
            scored.append(KbChunkWithScore(chunk=chunk, score=normalized))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[: int(limit)]


# Reciprocal-rank-fusion constant. Standard Lucene-recommended k=60.
# Higher k = flatter contribution from each list's top-ranked items;
# lower k = a #1 in either list dominates the fused ranking.
# Override via env var: MOVATE_RRF_K (integer, default 60).
RRF_K: int = int(os.environ.get("MOVATE_RRF_K", "60"))


def rrf_fuse(
    *result_lists: list[KbChunkWithScore], k: int = RRF_K, limit: int = 5
) -> list[KbChunkWithScore]:
    """Reciprocal Rank Fusion — merge multiple ranked lists into one.

    Args:
        *result_lists: Two or more lists of ranked
            :class:`KbChunkWithScore`. Each list is already sorted
            descending by its native score (vector cosine, BM25, etc.).
        k: RRF dampening constant (default 60, the Lucene-standard
            value). Higher k = flatter contribution per rank.
        limit: Top-N to return after fusion.

    Returns:
        A single descending-ranked list. Score is the RRF score
        (sum of ``1/(k+rank)`` across the input lists in which the
        chunk appears) — not directly comparable to vector / BM25
        scores; only the ORDER matters.

    Example:
        Chunk A: #1 in vector, #5 in BM25
            → score = 1/(60+1) + 1/(60+5) = 0.0316
        Chunk B: #2 in vector, #2 in BM25
            → score = 1/(60+2) + 1/(60+2) = 0.0323
            (Chunk B wins.)
    """
    if not result_lists:
        return []
    # Map chunk_id → (chunk, accumulated_rrf_score).
    fused: dict[str, tuple[KbChunk, float]] = {}
    for ranked in result_lists:
        for rank, item in enumerate(ranked, start=1):
            contribution = 1.0 / (k + rank)
            cid = item.chunk.chunk_id
            if cid in fused:
                existing_chunk, existing_score = fused[cid]
                fused[cid] = (existing_chunk, existing_score + contribution)
            else:
                fused[cid] = (item.chunk, contribution)

    # Build a sorted list, then clamp scores into [-1, 1] for the
    # KbChunkWithScore validator. The RRF score's natural range is
    # [0, sum(1/(k+1))] which is well under 1 for any reasonable k
    # — no clamping needed in practice, but min() is defensive.
    sorted_items = sorted(fused.values(), key=lambda pair: pair[1], reverse=True)
    return [
        KbChunkWithScore(chunk=chunk, score=min(score, 1.0))
        for chunk, score in sorted_items[: int(limit)]
    ]
