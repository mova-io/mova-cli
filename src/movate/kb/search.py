"""Search pipeline: question text → top-K retrieved chunks.

Embed the question via the same model used at ingest time, then
delegate to storage's ``search_kb_chunks`` for the cosine ranking.

Powers ``mdk kb search`` (the CLI command) AND the
``kb-vector-lookup`` skill (invoked at agent run time).
"""

from __future__ import annotations

from movate.core.models import KbChunkWithScore
from movate.kb.embed import (
    DEFAULT_EMBEDDING_MODEL,
    embed_texts,
)


async def search(
    *,
    storage: object,
    question: str,
    agent: str,
    tenant_id: str,
    limit: int = 5,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
) -> list[KbChunkWithScore]:
    """Embed ``question`` + return the top-``limit`` chunks ranked by
    cosine similarity.

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
    result: list[KbChunkWithScore] = await storage.search_kb_chunks(  # type: ignore[attr-defined]
        agent=agent,
        tenant_id=tenant_id,
        query_embedding=query_embedding,
        limit=limit,
    )
    return result
