"""Search pipeline: question text → top-K retrieved chunks.

Two modes:

* **Vector-only** (default) — embed the question via the same model
  used at ingest time, return cosine-ranked chunks. Best for
  paraphrase-heavy questions where word identity doesn't match the
  KB's wording.
* **Hybrid** (``hybrid=True``) — run vector + BM25 lexical search in
  parallel, then fuse the rankings with RRF. Typically 15-25% better
  recall on real corpora, especially for queries containing rare
  terms (product names, error codes, citation IDs) that vector
  retrieval blurs out.

Powers ``mdk kb search`` (the CLI command) AND the
``kb-vector-lookup`` skill (invoked at agent run time).
"""

from __future__ import annotations

from movate.core.models import KbChunkWithScore
from movate.kb.embed import (
    DEFAULT_EMBEDDING_MODEL,
    embed_texts,
)
from movate.kb.lexical import bm25_search, rrf_fuse


async def search(
    *,
    storage: object,
    question: str,
    agent: str,
    tenant_id: str,
    limit: int = 5,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    hybrid: bool = False,
    fetch_multiplier: int = 4,
) -> list[KbChunkWithScore]:
    """Embed ``question`` + return the top-``limit`` chunks ranked.

    When ``hybrid=False`` (default): pure vector / cosine similarity
    via the storage layer.

    When ``hybrid=True``: fetch ``limit * fetch_multiplier`` candidates
    via BOTH vector and BM25 lexical paths, then fuse with reciprocal
    rank fusion (RRF) and return the top ``limit``. The multiplier
    ensures the fusion has enough candidates from each path to find
    the cross-method overlap that RRF rewards. Default ``4`` fetches
    20 per path for a 5-result query — proven sweet spot.

    The ``embedding_model`` MUST match what was used at ingest time —
    different models produce incomparable vector spaces. The storage
    layer raises :class:`ValueError` on dim-mismatch, so a wrong-model
    query fails loudly rather than returning garbage. The default is
    the same default as ingest, so the common case just works.
    """
    if not question.strip():
        return []
    [query_embedding] = await embed_texts(
        [question],
        model=embedding_model,
        api_key=api_key,
    )

    if not hybrid:
        result: list[KbChunkWithScore] = await storage.search_kb_chunks(  # type: ignore[attr-defined]
            agent=agent,
            tenant_id=tenant_id,
            query_embedding=query_embedding,
            limit=limit,
        )
        return result

    # Hybrid path: fetch a wider candidate set from BOTH the vector
    # path AND the lexical (BM25) path, then fuse with RRF. The
    # multiplier widens each path's individual top-K so the fusion
    # has enough overlap to be useful — a 5-result query fetches
    # 20 from each path by default.
    candidate_limit = max(limit, int(limit * fetch_multiplier))

    # Vector path: same as the default branch above, just with a
    # wider limit.
    vector_results: list[KbChunkWithScore] = await storage.search_kb_chunks(  # type: ignore[attr-defined]
        agent=agent,
        tenant_id=tenant_id,
        query_embedding=query_embedding,
        limit=candidate_limit,
    )

    # Lexical path: we need the raw chunks for BM25 scoring. Fetch
    # ALL chunks for the agent (BM25's index needs corpus-wide
    # statistics); the limit gets applied inside ``bm25_search``.
    all_chunks = await storage.list_kb_chunks(  # type: ignore[attr-defined]
        agent=agent,
        tenant_id=tenant_id,
        limit=100_000,
    )
    lexical_results = bm25_search(all_chunks, question, limit=candidate_limit)

    # Fuse + clamp to final limit. RRF ignores score scale + only
    # looks at rank, so it doesn't matter that vector scores are 0-1
    # while BM25 scores can exceed 1.
    return rrf_fuse(vector_results, lexical_results, limit=limit)
