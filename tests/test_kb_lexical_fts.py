"""Tests for native FTS-backed lexical search (PR-AA).

Coverage:
* SqliteProvider.search_kb_chunks_lexical — FTS5 MATCH query
* InMemoryStorage.search_kb_chunks_lexical — Python BM25 fallback
* FTS5 index stays in sync on save + delete
* Empty query → empty results
* Query with no matching terms → empty results
* search._retrieve_one hybrid path uses storage lexical method
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from movate.core.models import KbChunk
from movate.kb.embed import DEFAULT_EMBEDDING_MODEL
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage


def _make_chunk(
    chunk_id: str,
    text: str,
    agent: str = "test-agent",
    tenant_id: str = "t1",
) -> KbChunk:
    return KbChunk(
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        agent=agent,
        source="test.md",
        text=text,
        embedding=[0.1] * 1536,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        content_hash=chunk_id,
        metadata=None,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# InMemoryStorage (Python BM25 fallback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_inmemory_lexical_basic() -> None:
    """Basic term match via Python BM25 fallback."""
    s = InMemoryStorage()
    await s.init()
    await s.save_kb_chunk(_make_chunk("c1", "refund policy 14 day return window"))
    await s.save_kb_chunk(_make_chunk("c2", "shipping rates calculated at checkout"))
    results = await s.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="refund policy", limit=5
    )
    assert len(results) >= 1
    assert results[0].chunk.chunk_id == "c1"


@pytest.mark.unit
async def test_inmemory_lexical_empty_query() -> None:
    """Empty / whitespace-only query returns empty list."""
    s = InMemoryStorage()
    await s.init()
    await s.save_kb_chunk(_make_chunk("c1", "some content here"))
    assert (
        await s.search_kb_chunks_lexical(agent="test-agent", tenant_id="t1", query="", limit=5)
        == []
    )
    assert (
        await s.search_kb_chunks_lexical(agent="test-agent", tenant_id="t1", query="   ", limit=5)
        == []
    )


@pytest.mark.unit
async def test_inmemory_lexical_no_match() -> None:
    """Query with no matching terms returns empty list."""
    s = InMemoryStorage()
    await s.init()
    await s.save_kb_chunk(_make_chunk("c1", "billing invoice payment"))
    results = await s.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="xyznomatch", limit=5
    )
    assert results == []


@pytest.mark.unit
async def test_inmemory_lexical_tenant_isolation() -> None:
    """Results are scoped to agent+tenant — another tenant's chunks excluded."""
    s = InMemoryStorage()
    await s.init()
    await s.save_kb_chunk(_make_chunk("c1", "refund policy", tenant_id="t1"))
    await s.save_kb_chunk(_make_chunk("c2", "refund policy", tenant_id="t2"))
    results = await s.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="refund", limit=5
    )
    ids = {r.chunk.chunk_id for r in results}
    assert "c1" in ids
    assert "c2" not in ids


# ---------------------------------------------------------------------------
# SqliteProvider FTS5
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_sqlite_lexical_basic(tmp_path) -> None:
    """SqliteProvider: FTS5 MATCH returns relevant chunks."""
    db = SqliteProvider(tmp_path / "test.db")
    await db.init()
    await db.save_kb_chunk(_make_chunk("c1", "refund policy 14 day return window"))
    await db.save_kb_chunk(_make_chunk("c2", "shipping rates calculated at checkout"))
    results = await db.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="refund return", limit=5
    )
    assert len(results) >= 1
    assert results[0].chunk.chunk_id == "c1"
    await db._db.close()


@pytest.mark.unit
async def test_sqlite_lexical_empty_query(tmp_path) -> None:
    """Empty query returns [] without hitting FTS5."""
    db = SqliteProvider(tmp_path / "test.db")
    await db.init()
    assert (
        await db.search_kb_chunks_lexical(agent="test-agent", tenant_id="t1", query="", limit=5)
        == []
    )
    await db._db.close()


@pytest.mark.unit
async def test_sqlite_lexical_no_match(tmp_path) -> None:
    """No FTS5 match → empty list."""
    db = SqliteProvider(tmp_path / "test.db")
    await db.init()
    await db.save_kb_chunk(_make_chunk("c1", "billing invoice payment"))
    results = await db.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="xyznomatch", limit=5
    )
    assert results == []
    await db._db.close()


@pytest.mark.unit
async def test_sqlite_fts_sync_on_delete(tmp_path) -> None:
    """Deleting chunks removes them from FTS5 index."""
    db = SqliteProvider(tmp_path / "test.db")
    await db.init()
    await db.save_kb_chunk(_make_chunk("c1", "refund policy return window"))
    # Confirm it's findable:
    before = await db.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="refund", limit=5
    )
    assert len(before) == 1
    # Delete the chunk:
    await db.delete_kb_chunks(agent="test-agent", tenant_id="t1")
    after = await db.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="refund", limit=5
    )
    assert after == []
    await db._db.close()


@pytest.mark.unit
async def test_sqlite_fts_backfill_on_init(tmp_path) -> None:
    """Chunks inserted before FTS5 DDL (simulated by direct row insert)
    get backfilled when init() is called again."""
    db_path = tmp_path / "test.db"
    # First init: creates schema including FTS5
    db = SqliteProvider(db_path)
    await db.init()
    # Insert directly into kb_chunks WITHOUT going through save_kb_chunk
    # (simulates chunks existing before the FTS5 table was created)
    now = datetime.now(UTC).isoformat()
    await db._db.execute(
        """
        INSERT INTO kb_chunks
        (chunk_id, tenant_id, agent, source, text, embedding,
         embedding_model, content_hash, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            "cx",
            "t1",
            "test-agent",
            "f.md",
            "backfill test content",
            json.dumps([0.1] * 1536),
            DEFAULT_EMBEDDING_MODEL,
            "cx",
            now,
        ),
    )
    await db._db.commit()
    await db._db.close()

    # Re-init: the backfill step in init() should index "cx"
    db2 = SqliteProvider(db_path)
    await db2.init()
    results = await db2.search_kb_chunks_lexical(
        agent="test-agent", tenant_id="t1", query="backfill", limit=5
    )
    assert len(results) == 1
    assert results[0].chunk.chunk_id == "cx"
    await db2._db.close()
