"""Task 2 (ADR 009): PostgresProvider vector search via pgvector.

Postgres-gated — skips unless ``MOVATE_PG_TEST_URL`` is set (CI's postgres job
runs against ``pgvector/pgvector:pg16``). Verifies:

* migration 001 converted ``kb_chunks.embedding`` to a ``vector`` column with
  an HNSW index;
* SQL ``<=>`` search returns the same top-K ordering as the Python cosine
  oracle (``_cosine.rank_chunks_by_cosine``), so the swap is behavior-preserving;
* the returned scores are cosine similarities in the existing 0-1-ish range;
* a dimension mismatch raises.

Uses the store's configured dimension (1536 by default) with sparse vectors so
it matches the column the migration created in this same DB.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from movate.core.models import KbChunk
from movate.storage._cosine import rank_chunks_by_cosine

pytestmark = pytest.mark.postgres

_AGENT = "pgvector-search-test"
_TENANT = "pgvector-test"


def _pg_url() -> str | None:
    return os.environ.get("MOVATE_PG_TEST_URL")


def _vec(dim: int, nonzero: dict[int, float]) -> list[float]:
    """A sparse ``dim``-length vector with the given index→value entries."""
    v = [0.0] * dim
    for i, val in nonzero.items():
        v[i] = val
    return v


def _chunk(dim: int, idx: int, nonzero: dict[int, float]) -> KbChunk:
    return KbChunk(
        chunk_id=f"{_AGENT}-{idx}",
        tenant_id=_TENANT,
        agent=_AGENT,
        source="test.md",
        text=f"chunk {idx}",
        embedding=_vec(dim, nonzero),
        embedding_model="text-embedding-3-small",
        content_hash=f"hash-{idx}",
        metadata=None,
        ocr=False,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_pgvector_search_matches_cosine_oracle() -> None:
    url = _pg_url()
    if url is None:
        pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres backend")

    from movate.storage.postgres import PostgresProvider, _embedding_dim  # noqa: PLC0415

    dim = _embedding_dim()
    storage = PostgresProvider(dsn=url)
    await storage.init()
    try:
        # Clean any residue from a prior run (kb_chunks isn't truncated by conftest).
        await storage.delete_kb_chunks(agent=_AGENT, tenant_id=_TENANT)

        # a≈query, c is close to a, b is orthogonal → expected order a, c, b.
        chunks = [
            _chunk(dim, 0, {0: 1.0}),  # a
            _chunk(dim, 1, {1: 1.0}),  # b (orthogonal to query)
            _chunk(dim, 2, {0: 0.9, 1: 0.1}),  # c (close to a)
        ]
        for c in chunks:
            await storage.save_kb_chunk(c)

        query = _vec(dim, {0: 1.0})
        results = await storage.search_kb_chunks(
            agent=_AGENT, tenant_id=_TENANT, query_embedding=query, limit=3
        )

        got_order = [r.chunk.chunk_id for r in results]
        oracle = rank_chunks_by_cosine(chunks, query, 3)
        want_order = [r.chunk.chunk_id for r in oracle]

        assert got_order == want_order, f"pgvector order {got_order} != oracle {want_order}"
        assert got_order[0] == f"{_AGENT}-0"  # exact match ranks first
        # Scores are cosine similarities: top is ~1.0, orthogonal is ~0.0.
        assert results[0].score == pytest.approx(1.0, abs=1e-3)
        assert results[-1].score == pytest.approx(0.0, abs=1e-3)
    finally:
        await storage.delete_kb_chunks(agent=_AGENT, tenant_id=_TENANT)
        await storage.close()


@pytest.mark.asyncio
async def test_migration_made_embedding_a_vector_column_with_hnsw() -> None:
    url = _pg_url()
    if url is None:
        pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres backend")

    from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

    storage = PostgresProvider(dsn=url)
    await storage.init()
    try:
        udt = await storage._db.fetchval(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'kb_chunks' AND column_name = 'embedding'"
        )
        assert udt == "vector", f"embedding column should be vector, got {udt!r}"

        has_hnsw = await storage._db.fetchval(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'kb_chunks' "
            "AND indexname = 'idx_kb_chunks_embedding_hnsw'"
        )
        assert has_hnsw == 1, "HNSW index on kb_chunks.embedding should exist"

        recorded = await storage._db.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = '001_kb_embedding_to_vector'"
        )
        assert recorded == 1, "migration 001 should be recorded in schema_migrations"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_search_rejects_dimension_mismatch() -> None:
    url = _pg_url()
    if url is None:
        pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres backend")

    from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

    storage = PostgresProvider(dsn=url)
    await storage.init()
    try:
        with pytest.raises(ValueError, match="dimension"):
            await storage.search_kb_chunks(
                agent=_AGENT, tenant_id=_TENANT, query_embedding=[1.0, 0.0, 0.0], limit=3
            )
    finally:
        await storage.close()
