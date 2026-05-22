"""KB vector retrieval MVP (0.8.2.13) — storage + chunker + ingest.

Three layers covered:

* **Storage** — `save_kb_chunk` / `search_kb_chunks` round-trip,
  cosine ranking, dedup-via-content-hash semantics. Drives the
  InMemoryStorage path which shares the cosine routine with
  Postgres + sqlite.
* **Chunker** — paragraph splitter handles markdown, drops tiny
  fragments, sub-splits long paragraphs.
* **Ingest pipeline** — `ingest_path` walks a directory, calls the
  embedder (stubbed), persists, returns one summary per file.

Embedding HTTP calls are mocked end-to-end — no real OpenAI traffic
in the test suite. The mock returns deterministic vectors so the
ranking assertions are reproducible.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from movate.core.models import KbChunk
from movate.kb.chunk import (
    MAX_CHUNK_CHARS,
    split_paragraphs,
)
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunker_splits_on_paragraph_boundaries() -> None:
    """Two paragraphs separated by a blank line → two chunks."""
    text = "First paragraph with some content.\n\nSecond paragraph also has content."
    chunks = split_paragraphs(text)
    assert len(chunks) == 2
    assert "First paragraph" in chunks[0].text
    assert "Second paragraph" in chunks[1].text


@pytest.mark.unit
def test_chunker_drops_tiny_fragments() -> None:
    """Single-word paragraphs (< MIN_CHUNK_CHARS) get filtered out."""
    text = "ok\n\nthis is a substantive paragraph with enough content.\n\nx"
    chunks = split_paragraphs(text)
    # Only the middle paragraph qualifies.
    assert len(chunks) == 1
    assert "substantive" in chunks[0].text


@pytest.mark.unit
def test_chunker_subsplits_long_paragraphs() -> None:
    """A single 5000-char paragraph gets split into multiple chunks
    no larger than MAX_CHUNK_CHARS."""
    sentence = (
        "This is a deliberately long sentence with enough words to consume meaningful char count. "
    )
    # ~90 chars * 60 = ~5400 chars, well over MAX_CHUNK_CHARS (2000).
    long_paragraph = sentence * 60
    chunks = split_paragraphs(long_paragraph)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.text) <= MAX_CHUNK_CHARS + 100  # slack for sentence-boundary tolerance


@pytest.mark.unit
def test_chunker_content_hash_is_stable() -> None:
    """Same text → same hash. Two runs of the chunker on the same
    input produce identical content_hashes (the dedup key)."""
    text = "A sample paragraph for hashing.\n\nAnother distinct paragraph."
    first = split_paragraphs(text)
    second = split_paragraphs(text)
    assert [c.content_hash for c in first] == [c.content_hash for c in second]
    # And distinct chunks have distinct hashes.
    assert first[0].content_hash != first[1].content_hash


@pytest.mark.unit
def test_chunker_handles_empty_input() -> None:
    """Empty / whitespace-only input → no chunks (not an error)."""
    assert split_paragraphs("") == []
    assert split_paragraphs("   \n\n  \n") == []


# ---------------------------------------------------------------------------
# Storage roundtrip + cosine ranking
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_storage_save_and_search_round_trip() -> None:
    """save_kb_chunk + search_kb_chunks return ranked results."""
    storage = InMemoryStorage()
    await storage.init()

    # Three chunks, deliberately-chosen embeddings so cosine ranking
    # is predictable. Pretend these are "embeddings" in a 3-dim space
    # — the real OpenAI ones are 1536-dim but the algorithm is the same.
    chunks = [
        _kb_chunk(text="refund policy details", embedding=[1.0, 0.0, 0.0]),
        _kb_chunk(text="GPU instances and compute", embedding=[0.0, 1.0, 0.0]),
        _kb_chunk(text="refund window 30 days", embedding=[0.9, 0.1, 0.0]),
    ]
    for c in chunks:
        await storage.save_kb_chunk(c)

    # Query close to chunk 1 + 3 (refund-related vectors).
    results = await storage.search_kb_chunks(
        agent="rag-qa",
        tenant_id="test",
        query_embedding=[1.0, 0.0, 0.0],
        limit=2,
    )
    assert len(results) == 2
    # Chunk 1 (refund policy details) is dead-on; chunk 3 (refund
    # window) is close; chunk 2 (GPU) is orthogonal.
    assert "refund policy" in results[0].chunk.text
    assert "refund window" in results[1].chunk.text
    assert results[0].score > results[1].score


@pytest.mark.unit
async def test_storage_dedup_via_content_hash() -> None:
    """Re-saving a chunk with the same content_hash for the same
    agent + tenant updates in place (no duplicate row)."""
    storage = InMemoryStorage()
    chunk_v1 = _kb_chunk(text="hello world", embedding=[1.0, 0.0])
    chunk_v2 = _kb_chunk(
        text="hello world",  # same text => same content_hash
        embedding=[0.0, 1.0],  # different embedding (e.g. re-embed with different model)
        agent=chunk_v1.agent,
        tenant_id=chunk_v1.tenant_id,
        content_hash=chunk_v1.content_hash,
    )
    await storage.save_kb_chunk(chunk_v1)
    await storage.save_kb_chunk(chunk_v2)

    rows = await storage.list_kb_chunks(agent="rag-qa", tenant_id="test")
    assert len(rows) == 1
    # The embedding got updated.
    assert rows[0].embedding == [0.0, 1.0]


@pytest.mark.unit
async def test_storage_search_returns_empty_on_empty_kb() -> None:
    """Empty KB → empty results (no special-case needed in callers)."""
    storage = InMemoryStorage()
    results = await storage.search_kb_chunks(
        agent="rag-qa",
        tenant_id="test",
        query_embedding=[1.0, 0.0],
    )
    assert results == []


@pytest.mark.unit
async def test_storage_search_rejects_dim_mismatch() -> None:
    """Query embedding with wrong dimension raises ValueError —
    rejects cross-model queries explicitly rather than returning
    garbage scores."""
    storage = InMemoryStorage()
    await storage.save_kb_chunk(_kb_chunk(embedding=[1.0, 0.0, 0.0]))
    with pytest.raises(ValueError, match="dim mismatch"):
        await storage.search_kb_chunks(
            agent="rag-qa",
            tenant_id="test",
            query_embedding=[1.0, 0.0],  # 2-dim, chunk was 3-dim
        )


@pytest.mark.unit
async def test_storage_delete_kb_chunks() -> None:
    """delete_kb_chunks returns the count + removes the rows."""
    storage = InMemoryStorage()
    await storage.save_kb_chunk(_kb_chunk(embedding=[1.0, 0.0], content_hash="a", text="text-a"))
    await storage.save_kb_chunk(_kb_chunk(embedding=[0.0, 1.0], content_hash="b", text="text-b"))

    n = await storage.delete_kb_chunks(agent="rag-qa", tenant_id="test")
    assert n == 2
    assert await storage.list_kb_chunks(agent="rag-qa", tenant_id="test") == []


# ---------------------------------------------------------------------------
# Ingest pipeline
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_ingest_pipeline_end_to_end(tmp_path: Path) -> None:
    """ingest_path walks a directory, chunks each file, embeds via
    the stubbed OpenAI client, persists. Verifies chunk count, source
    paths, embedding model name."""
    from movate.kb.ingest import ingest_path  # noqa: PLC0415

    # Layout: two markdown files with multiple paragraphs each.
    (tmp_path / "policies.md").write_text(
        "# Refund Policy\n\n"
        "Annual subscriptions are refundable within 14 days, prorated.\n\n"
        "Monthly subscriptions are non-refundable but cancel-anytime.\n",
        encoding="utf-8",
    )
    (tmp_path / "features.md").write_text(
        "# Features\n\nWe offer SSO on the enterprise tier.\n\nCustom domains roadmapped for Q3.\n",
        encoding="utf-8",
    )
    # Hidden dir should be skipped.
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "ignored.md").write_text("ignored content")

    storage = InMemoryStorage()
    await storage.init()

    # Stub the embedding call so no OpenAI traffic. Returns
    # deterministic 4-dim vectors per text — close enough for ranking.
    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[float(len(t) % 7), 1.0, 0.5, 0.0] for t in texts]

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=fake_embed):
        summaries, _ = await ingest_path(
            storage=storage,
            path=tmp_path,
            agent="rag-qa",
            tenant_id="test",
            api_key="sk-stub",
        )

    # Two files, multiple chunks each.
    assert len(summaries) == 2
    # Hidden dir was excluded.
    assert all(".hidden" not in s.source for s in summaries)
    # All chunks landed in storage.
    rows = await storage.list_kb_chunks(agent="rag-qa", tenant_id="test")
    assert len(rows) == sum(s.chunks_saved for s in summaries)
    # Embedding model is qualified with the openai/ prefix.
    assert all(r.embedding_model.startswith("openai/") for r in rows)


@pytest.mark.unit
async def test_ingest_pipeline_is_idempotent(tmp_path: Path) -> None:
    """Running ingest twice on the same file is a no-op — same content
    hashes dedupe via the storage UPSERT path."""
    from movate.kb.ingest import ingest_path  # noqa: PLC0415

    (tmp_path / "doc.md").write_text(
        "Paragraph one with enough content.\n\nParagraph two with content.\n",
        encoding="utf-8",
    )

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    storage = InMemoryStorage()
    with mock.patch("movate.kb.ingest.embed_texts", side_effect=fake_embed):
        await ingest_path(
            storage=storage, path=tmp_path, agent="rag-qa", tenant_id="test", api_key="sk-x"
        )
        before = len(await storage.list_kb_chunks(agent="rag-qa", tenant_id="test"))
        # Re-ingest the same files.
        await ingest_path(
            storage=storage, path=tmp_path, agent="rag-qa", tenant_id="test", api_key="sk-x"
        )
        after = len(await storage.list_kb_chunks(agent="rag-qa", tenant_id="test"))

    assert before == after  # no duplicates


# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_pipeline_embeds_query_and_returns_top_k() -> None:
    """movate.kb.search.search() embeds the query, ranks, returns
    top-K. Validates the orchestrator wiring end-to-end with stubs."""
    from movate.kb.search import search as kb_search  # noqa: PLC0415

    storage = InMemoryStorage()
    # Seed two chunks with known embeddings.
    await storage.save_kb_chunk(_kb_chunk(text="refund stuff", embedding=[1.0, 0.0]))
    await storage.save_kb_chunk(_kb_chunk(text="GPU stuff", embedding=[0.0, 1.0]))

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        # Return a query vector close to chunk 1 ("refund stuff").
        return [[1.0, 0.0] for _ in texts]

    with mock.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="refund policy?",
            agent="rag-qa",
            tenant_id="test",
            limit=2,
        )

    assert len(results) == 2
    assert results[0].chunk.text == "refund stuff"
    assert results[0].score > results[1].score


@pytest.mark.unit
async def test_search_pipeline_returns_empty_for_blank_question() -> None:
    """Empty question → empty results, no embedding call (saves $0.00002)."""
    from movate.kb.search import search as kb_search  # noqa: PLC0415

    storage = InMemoryStorage()
    with mock.patch("movate.kb.search.embed_texts") as embed_mock:
        results = await kb_search(
            storage=storage,
            question="   ",
            agent="rag-qa",
            tenant_id="test",
        )
    assert results == []
    embed_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kb_chunk(
    *,
    text: str = "sample text",
    embedding: list[float] | None = None,
    agent: str = "rag-qa",
    tenant_id: str = "test",
    content_hash: str | None = None,
    embedding_model: str = "openai/text-embedding-3-small",
    source: str = "/tmp/source.md",
) -> KbChunk:
    """Test factory. ``content_hash`` defaults to a hash of ``text``
    so distinct texts get distinct hashes — matches the production
    chunker's behavior. Tests that want to exercise the dedup path
    (same hash, different text) pass an explicit ``content_hash``."""
    import hashlib  # noqa: PLC0415

    if embedding is None:
        embedding = [1.0, 0.0]
    if content_hash is None:
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return KbChunk(
        tenant_id=tenant_id,
        agent=agent,
        source=source,
        text=text,
        embedding=embedding,
        embedding_model=embedding_model,
        content_hash=content_hash,
    )
