"""Retrieval — substring + word-overlap scoring (MVP).

The scoring function is intentionally simple: lowercase substring
match + token-overlap fraction. It catches obvious matches and
gives a deterministic, dependency-free signal good enough for
demos + unit tests.

Real retrieval (BM25, dense embeddings, hybrid, reranking) lands in
v0.8 behind the same :func:`retrieve` signature. The interface is
the lock-in; the scoring function is the swap-in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from movate.knowledge.store import Chunk, KnowledgeStore


@dataclass(frozen=True)
class RetrievalResult:
    """One scored hit.

    ``score`` is a unitless similarity in [0.0, 1.0] — higher is better.
    Tied scores are broken by chunk order in the corpus so results
    are deterministic across runs.

    ``snippet`` is the chunk text truncated to ``max_snippet_chars``
    in :func:`retrieve` for display; the caller can fetch the full
    chunk via ``chunk.text`` if needed.
    """

    chunk: Chunk
    score: float
    snippet: str


# Tokens for word-overlap scoring. Alphanumeric only — punctuation
# is dropped before comparing so "SQL." matches "SQL".
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


# Defaults for the retrieve() public API. Kept as module constants
# so tests + callers can reference the same values.
#
# ``_DEFAULT_MIN_SCORE`` requires a STRICTLY positive score by default
# — a chunk with no substring match and no token overlap (e.g. an
# all-unique query like "zzzzzzzz" against unrelated content) lands
# at 0.0 and would otherwise still be returned, which is more
# confusing than useful. Callers can override with ``min_score=0.0``
# if they want every chunk ranked even with zero signal.
_DEFAULT_TOP_K = 5
_DEFAULT_MIN_SCORE = 0.01
_DEFAULT_SNIPPET_CHARS = 200


def retrieve(
    query: str,
    store: KnowledgeStore,
    *,
    top_k: int = _DEFAULT_TOP_K,
    min_score: float = _DEFAULT_MIN_SCORE,
    max_snippet_chars: int = _DEFAULT_SNIPPET_CHARS,
) -> list[RetrievalResult]:
    """Return the top-k chunks scored against the query.

    Two scoring signals combine:

    * **Substring bonus**: +0.5 if the lowercased query appears
      as a substring in the chunk's lowercased text. Captures
      exact-phrase matches the operator obviously expects.
    * **Word-overlap score**: fraction of query tokens that appear
      anywhere in the chunk's tokens, in [0.0, 0.5]. Captures
      keyword matches that aren't contiguous.

    Final score is in [0.0, 1.0]. Sorted descending, ties broken
    by chunk's natural order. Filtered by ``min_score``.

    A query with zero alphanumeric tokens (e.g. just punctuation)
    returns no results — substring match alone isn't enough to call
    a chunk relevant.
    """
    query_lower = query.lower()
    query_tokens = _tokenize(query)

    if not query_tokens:
        return []

    scored: list[tuple[float, int, Chunk]] = []
    for i, chunk in enumerate(store.all_chunks()):
        score = _score(chunk, query_lower=query_lower, query_tokens=query_tokens)
        if score >= min_score:
            scored.append((score, i, chunk))

    # Sort by (-score, natural index) so ties break to corpus order.
    scored.sort(key=lambda triple: (-triple[0], triple[1]))

    results: list[RetrievalResult] = []
    for score, _i, chunk in scored[:top_k]:
        snippet = chunk.text[:max_snippet_chars]
        if len(chunk.text) > max_snippet_chars:
            snippet = snippet.rstrip() + "..."
        results.append(RetrievalResult(chunk=chunk, score=score, snippet=snippet))
    return results


# ---------------------------------------------------------------------------
# Scoring internals (swap-in points for the v0.8 engine)
# ---------------------------------------------------------------------------


def _score(chunk: Chunk, *, query_lower: str, query_tokens: set[str]) -> float:
    """Combine substring bonus + word-overlap into [0, 1]."""
    chunk_lower = chunk.text.lower()
    chunk_tokens = set(_tokenize(chunk.text))

    substring_bonus = 0.5 if query_lower in chunk_lower else 0.0
    if not query_tokens:
        # Defensive — caller already filters this case.
        overlap = 0.0
    else:
        overlap = len(query_tokens & chunk_tokens) / len(query_tokens) * 0.5
    return substring_bonus + overlap


def _tokenize(text: str) -> set[str]:
    """Lowercased alphanumeric tokens. Used for word-overlap scoring."""
    return {tok.lower() for tok in _TOKEN_RE.findall(text)}
