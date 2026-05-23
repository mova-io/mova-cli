"""Conformance tests for ``StorageProvider.reindex_kb`` (Task 5).

Runs against all three backends via the parametrized ``storage`` fixture
in conftest.py (memory / sqlite / postgres — the last skips unless
``MOVATE_PG_TEST_URL`` is set). The contract:

* ``reindex_kb`` returns the chunk count for ``(agent, tenant_id)``;
* it NEVER raises (brute-force backends are a graceful no-op);
* it is tenant + agent scoped in its returned count;
* on Postgres it actually drops + re-creates the HNSW index (gated on
  ``MOVATE_PG_TEST_URL``).
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime

import pytest

from movate.core.models import KbChunk
from movate.kb.embed import embedding_dim
from movate.storage.base import StorageProvider

pytestmark = pytest.mark.unit


def _pad(vec: list[float]) -> list[float]:
    """Pad a short test vector to the configured embedding dim with zeros
    so it fits the Postgres ``vector(N)`` column (ADR 009 D1)."""
    dim = embedding_dim()
    return vec if len(vec) >= dim else vec + [0.0] * (dim - len(vec))


def _chunk(
    chunk_id: str,
    *,
    agent: str = "a1",
    tenant_id: str = "t1",
    source: str = "doc.md",
) -> KbChunk:
    return KbChunk(
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        agent=agent,
        source=source,
        text=f"text for {chunk_id}",
        embedding=_pad([1.0, 0.0, 0.0]),
        embedding_model="openai/text-embedding-3-small",
        content_hash=hashlib.sha256(chunk_id.encode()).hexdigest(),
        created_at=datetime.now(UTC),
    )


async def test_reindex_kb_returns_chunk_count(storage: StorageProvider) -> None:
    for i in range(3):
        await storage.save_kb_chunk(_chunk(f"c{i}"))
    indexed = await storage.reindex_kb(agent="a1", tenant_id="t1")
    assert indexed == 3


async def test_reindex_kb_empty_kb_is_zero_no_raise(storage: StorageProvider) -> None:
    # No chunks for this agent — graceful, returns 0, never raises.
    indexed = await storage.reindex_kb(agent="empty-agent", tenant_id="t1")
    assert indexed == 0


async def test_reindex_kb_count_is_agent_and_tenant_scoped(storage: StorageProvider) -> None:
    await storage.save_kb_chunk(_chunk("c1", agent="a1", tenant_id="t1"))
    await storage.save_kb_chunk(_chunk("c2", agent="a1", tenant_id="t1"))
    await storage.save_kb_chunk(_chunk("c3", agent="a2", tenant_id="t1"))
    await storage.save_kb_chunk(_chunk("c4", agent="a1", tenant_id="t2"))
    # Count is scoped to (a1, t1) even though the postgres index rebuild
    # is global to the table.
    assert await storage.reindex_kb(agent="a1", tenant_id="t1") == 2
    assert await storage.reindex_kb(agent="a2", tenant_id="t1") == 1
    assert await storage.reindex_kb(agent="a1", tenant_id="t2") == 1


# ---------------------------------------------------------------------------
# Postgres-specific: the index is actually dropped + recreated.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reindex_kb_recreates_hnsw_index_on_postgres() -> None:
    url = os.environ.get("MOVATE_PG_TEST_URL")
    if url is None:
        pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres backend")

    from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

    agent = "reindex-pg-test"
    tenant = "reindex-pg-test"
    storage = PostgresProvider(dsn=url)
    await storage.init()
    try:
        await storage.delete_kb_chunks(agent=agent, tenant_id=tenant)
        await storage.save_kb_chunk(_chunk("c1", agent=agent, tenant_id=tenant))

        # Drop the global HNSW index out from under reindex to prove it
        # gets re-created (not just "happens to already exist").
        await storage._db.execute("DROP INDEX IF EXISTS idx_kb_chunks_embedding_hnsw")
        gone = await storage._db.fetchval(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'kb_chunks' "
            "AND indexname = 'idx_kb_chunks_embedding_hnsw'"
        )
        assert gone is None, "precondition: index should be dropped"

        indexed = await storage.reindex_kb(agent=agent, tenant_id=tenant)
        assert indexed == 1

        rebuilt = await storage._db.fetchval(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'kb_chunks' "
            "AND indexname = 'idx_kb_chunks_embedding_hnsw'"
        )
        assert rebuilt == 1, "reindex_kb should re-create the HNSW index"
    finally:
        await storage.delete_kb_chunks(agent=agent, tenant_id=tenant)
        await storage.close()
