"""Pure-Python cosine-similarity ranking for KB chunks.

Extracted from ``storage/postgres.py`` so the SQLite path can import
it without pulling in ``asyncpg`` (a [runtime] optional dep).  Both
``SqliteStorage`` and ``PostgresStorage`` import from here so the
ranking semantics are guaranteed identical.
"""

from __future__ import annotations

import math

from movate.core.models import KbChunk, KbChunkWithScore


def rank_chunks_by_cosine(
    chunks: list[KbChunk], query: list[float], limit: int
) -> list[KbChunkWithScore]:
    """Score every chunk against ``query`` by cosine similarity and
    return the top ``limit`` descending.

    Cosine = dot(a, b) / (||a|| * ||b||).  Both vectors are produced
    by the same embedding model so the normalization is well-defined;
    a length mismatch (e.g. caller embedded with a different model than
    the chunks) raises ValueError — silent dim-mismatch would return
    garbage scores.
    """
    if not chunks:
        return []
    q_norm = math.sqrt(sum(x * x for x in query))
    if q_norm == 0.0:
        return []
    scored: list[KbChunkWithScore] = []
    for c in chunks:
        if len(c.embedding) != len(query):
            raise ValueError(
                f"embedding dim mismatch: chunk {c.chunk_id} is "
                f"{len(c.embedding)}-dim but query is {len(query)}-dim. "
                f"Re-embed the query with {c.embedding_model!r} or "
                f"re-ingest the KB with the query's model."
            )
        c_norm = math.sqrt(sum(x * x for x in c.embedding))
        if c_norm == 0.0:
            continue  # zero-vector chunks (shouldn't happen) — skip
        dot = sum(a * b for a, b in zip(c.embedding, query, strict=True))
        scored.append(KbChunkWithScore(chunk=c, score=dot / (q_norm * c_norm)))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[: int(limit)]


# Legacy alias — postgres.py previously defined this as a private
# function and sqlite.py imported it under the old name.
_rank_chunks_by_cosine = rank_chunks_by_cosine
