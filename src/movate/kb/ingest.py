"""Ingest pipeline: file paths → embedded chunks in storage.

Reads .md / .txt files under the given path (or a single file),
splits each into paragraph chunks, embeds them in batches via the
OpenAI embedding helper, and persists via the storage layer's
``save_kb_chunk`` method.

Idempotent: re-ingesting the same document is a no-op via the
``(agent, tenant_id, content_hash)`` unique constraint on the
``kb_chunks`` table.

Powers ``mdk kb ingest`` (the CLI command lives in ``cli/kb.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from movate.core.models import KbChunk
from movate.kb.chunk import Chunk, split_paragraphs
from movate.kb.embed import (
    DEFAULT_EMBEDDING_MODEL,
    embed_texts,
    qualified_model_name,
)

# How many chunks to embed per OpenAI API call. OpenAI accepts up to
# 2048 inputs per request — we use 64 because that's the largest size
# that consistently stays under the 8192-token request limit even when
# chunks approach the max-chunk-chars cap (~500 tokens each).
EMBEDDING_BATCH_SIZE = 64


@dataclass
class IngestSummary:
    """Per-source result of an ingest call. Useful for CLI rendering +
    tests that want to assert on counts without re-querying storage."""

    source: str
    """Source identifier (file path, normalized to absolute)."""

    chunks_total: int
    """Total chunks produced by the splitter for this source."""

    chunks_saved: int
    """Chunks that actually hit storage (i.e. were new or replaced).
    Equal to ``chunks_total`` in v0.9 since we always overwrite via
    upsert; a future ``--skip-existing`` flag could diverge them."""

    embedding_model: str
    """The full ``provider/model`` identifier embedded with."""


def find_files(path: Path) -> list[Path]:
    """Return all ingestible files under ``path``.

    Directory: walks recursively, returns .md + .txt files.
    File: returns [path] if its extension is supported, else [].
    Symlinks resolved; hidden directories (``.git``, ``.venv``) skipped.
    """
    if not path.exists():
        return []
    supported = {".md", ".txt", ".markdown"}
    if path.is_file():
        return [path] if path.suffix.lower() in supported else []

    out: list[Path] = []
    for p in sorted(path.rglob("*")):
        # Skip dotted directories (.git, .venv, etc.) and dotted files.
        if any(part.startswith(".") for part in p.relative_to(path).parts):
            continue
        if p.is_file() and p.suffix.lower() in supported:
            out.append(p)
    return out


async def ingest_path(
    *,
    storage: object,
    path: Path,
    agent: str,
    tenant_id: str,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
) -> list[IngestSummary]:
    """Ingest a file or directory tree. Returns one summary per file.

    Empty directory / unsupported file = empty list (not an error).
    Embedding-call failures propagate as :class:`EmbeddingError` —
    the storage state is left at whatever was saved before the
    failure (no rollback, but the dedup key makes the next attempt
    pick up where this one stopped).
    """
    files = find_files(path)
    summaries: list[IngestSummary] = []
    for file_path in files:
        summary = await _ingest_one_file(
            storage=storage,
            file_path=file_path,
            agent=agent,
            tenant_id=tenant_id,
            embedding_model=embedding_model,
            api_key=api_key,
        )
        if summary is not None:
            summaries.append(summary)
    return summaries


async def _ingest_one_file(
    *,
    storage: object,
    file_path: Path,
    agent: str,
    tenant_id: str,
    embedding_model: str,
    api_key: str | None,
) -> IngestSummary | None:
    text = file_path.read_text(encoding="utf-8")
    source = str(file_path.resolve())
    return await ingest_text(
        storage=storage,
        text=text,
        source=source,
        agent=agent,
        tenant_id=tenant_id,
        embedding_model=embedding_model,
        api_key=api_key,
    )


async def ingest_text(
    *,
    storage: object,
    text: str,
    source: str,
    agent: str,
    tenant_id: str,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
) -> IngestSummary | None:
    """Chunk + embed + persist ``text`` as KB content for ``agent``.

    Same pipeline as :func:`ingest_path` but takes the document body
    in memory rather than reading from disk. Used by:

    * The runtime's KB upload endpoint
      (``POST /api/v1/agents/{name}/kb``) — accepts multipart file
      uploads and never writes them to a project ``kb/`` directory.
    * Future programmatic ingest from notebooks / scripts.

    ``source`` is a free-form label that ends up on each
    :class:`KbChunk` for traceability (e.g. the uploaded filename or
    ``"chainlit-upload/<filename>"``). It doesn't have to be an
    actual filesystem path. Pass an empty string to get a generic
    ``"<inline>"`` label.

    Empty / whitespace-only input → ``None`` (idempotent no-op,
    matches :func:`_ingest_one_file`).
    """
    label = source or "<inline>"
    chunks = split_paragraphs(text, source=label)
    if not chunks:
        return None

    full_model_name = qualified_model_name(embedding_model)
    saved = 0
    # Batch the embedding calls. ``save_kb_chunk`` is async-but-fast
    # so we save sequentially; embedding HTTP calls are the bottleneck
    # and are already batched into 64-per-call.
    for batch in _batched(chunks, EMBEDDING_BATCH_SIZE):
        texts = [c.text for c in batch]
        embeddings = await embed_texts(
            texts,
            model=embedding_model,
            api_key=api_key,
        )
        for chunk, embedding in zip(batch, embeddings, strict=True):
            kb_chunk = KbChunk(
                tenant_id=tenant_id,
                agent=agent,
                source=label,
                text=chunk.text,
                embedding=embedding,
                embedding_model=full_model_name,
                content_hash=chunk.content_hash,
                metadata=chunk.metadata,
            )
            # Duck-typed: any storage backend implementing
            # ``save_kb_chunk`` works (Postgres / sqlite / in-memory).
            await storage.save_kb_chunk(kb_chunk)  # type: ignore[attr-defined]
            saved += 1
    return IngestSummary(
        source=label,
        chunks_total=len(chunks),
        chunks_saved=saved,
        embedding_model=full_model_name,
    )


def _batched(items: list[Chunk], n: int) -> list[list[Chunk]]:
    """Yield ``items`` in chunks of size ``n``. Last chunk may be
    shorter. Returns a list (not a generator) so the caller can
    iterate twice if needed for progress reporting."""
    return [items[i : i + n] for i in range(0, len(items), n)]
