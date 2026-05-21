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

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from movate.core.models import KbChunk
from movate.kb.chunk import Chunk, split_paragraphs
from movate.kb.embed import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingError,
    embed_texts,
    qualified_model_name,
)

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

log = logging.getLogger(__name__)

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

    chunks_removed: int = field(default=0)
    """Old chunks deleted before re-ingest when ``clean_source=True``.
    Zero unless the caller passed ``--clean-source``."""


# File-size guard — default 50 MB. Operators with large scanned PDFs
# can raise this via MOVATE_MAX_FILE_MB. The guard fires before any
# parsing so it catches multi-hundred-MB uploads before pdf2image
# tries to rasterise every page.
_MAX_FILE_MB: float = float(os.environ.get("MOVATE_MAX_FILE_MB", "50"))


def find_files(path: Path) -> list[Path]:
    """Return all ingestible files under ``path``.

    Directory: walks recursively, returns files whose extension is
    in :data:`movate.kb.parsers.SUPPORTED_EXTENSIONS`
    (.md / .markdown / .txt / .pdf as of PR-G).
    File: returns ``[path]`` if its extension is supported, else
    ``[]``. Symlinks resolved; hidden directories (``.git``,
    ``.venv``) skipped.
    """
    from movate.kb.parsers import SUPPORTED_EXTENSIONS  # noqa: PLC0415 — keep parsers import lazy

    if not path.exists():
        return []
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED_EXTENSIONS else []

    out: list[Path] = []
    for p in sorted(path.rglob("*")):
        # Skip dotted directories (.git, .venv, etc.) and dotted files.
        if any(part.startswith(".") for part in p.relative_to(path).parts):
            continue
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            out.append(p)
    return out


async def ingest_path(
    *,
    storage: StorageProvider,
    path: Path,
    agent: str,
    tenant_id: str,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    clean_source: bool = False,
    on_file_start: Callable[[str, int, int], None] | None = None,
) -> tuple[list[IngestSummary], list[tuple[str, str]]]:
    """Ingest a file or directory tree. Returns one summary per file.

    Empty directory / unsupported file = empty list (not an error).

    :class:`EmbeddingError` from a single file is caught, logged at
    WARNING level, and recorded in ``failed_files`` — the remaining
    files in the batch are still attempted so one timeout or bad PDF
    doesn't abort an entire ``mdk kb ingest-all`` run.  The partial
    results are returned together with ``failed_files`` via the tuple;
    callers that previously ignored the return value are unaffected
    (the first element is the old ``list[IngestSummary]``).

    ``clean_source=True`` deletes all existing chunks for each file's
    source URI before ingesting it — use this when you want re-ingest
    to fully replace old content rather than dedup on content_hash.
    Equivalent to: delete old chunks → ingest new chunks. Reported
    in :attr:`IngestSummary.chunks_removed`.

    ``on_file_start`` is an optional progress callback invoked just
    before each file is ingested. Signature:
    ``(filename_str, current_1based_idx, total_files) -> None``.
    """
    files = find_files(path)
    summaries: list[IngestSummary] = []
    failed_files: list[tuple[str, str]] = []  # (filename, error_message)
    for i, file_path in enumerate(files):
        if on_file_start is not None:
            on_file_start(file_path.name, i + 1, len(files))
        try:
            summary = await _ingest_one_file(
                storage=storage,
                file_path=file_path,
                agent=agent,
                tenant_id=tenant_id,
                embedding_model=embedding_model,
                api_key=api_key,
                clean_source=clean_source,
            )
        except EmbeddingError as exc:
            # Transient network or rate-limit failure — continue with
            # the rest of the batch and surface at the end.
            log.warning("embedding failed for %s: %s", file_path.name, exc)
            failed_files.append((file_path.name, str(exc)))
            continue
        if summary is not None:
            summaries.append(summary)
    return summaries, failed_files


async def _ingest_one_file(
    *,
    storage: StorageProvider,
    file_path: Path,
    agent: str,
    tenant_id: str,
    embedding_model: str,
    api_key: str | None,
    clean_source: bool = False,
) -> IngestSummary | None:
    """Read + parse + ingest a single file.

    Dispatches via :func:`movate.kb.parsers.parse_document` so PDFs
    + future formats flow through the same code path as plain text.
    Parser failures (corrupt PDF, encrypted PDF, non-UTF8 .txt)
    return ``None`` — the orchestrator skips silently rather than
    surfacing one bad file as a batch-wide error.
    """
    from movate.kb.parsers import parse_document  # noqa: PLC0415 — keep parsers import lazy

    content = file_path.read_bytes()

    # File-size guard — warn and skip before any expensive parsing.
    # Default 50 MB; raise via MOVATE_MAX_FILE_MB env var.
    file_mb = len(content) / (1024 * 1024)
    if file_mb > _MAX_FILE_MB:
        log.warning(
            "Skipping %s (%.1f MB) — exceeds MOVATE_MAX_FILE_MB=%.0f MB. "
            "Raise the limit or split the file.",
            file_path.name,
            file_mb,
            _MAX_FILE_MB,
        )
        return None

    source = str(file_path.resolve())

    # --clean-source: delete all existing chunks for this source so
    # re-ingest fully replaces stale content. Without this flag the
    # dedup key (content_hash) means unchanged chunks are no-ops and
    # deleted paragraphs stick around forever.
    chunks_removed = 0
    if clean_source:
        chunks_removed = await storage.delete_kb_chunks(
            agent=agent,
            tenant_id=tenant_id,
            source=source,
        )

    result = parse_document(file_path.name, content)
    if result is None:
        return None
    summary = await ingest_text(
        storage=storage,
        text=result.text,
        source=source,
        agent=agent,
        tenant_id=tenant_id,
        embedding_model=embedding_model,
        api_key=api_key,
        ocr=result.ocr_used,
        page_texts=result.page_texts,
    )
    if summary is not None:
        summary.chunks_removed = chunks_removed
    return summary


async def ingest_text(
    *,
    storage: StorageProvider,
    text: str,
    source: str,
    agent: str,
    tenant_id: str,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    ocr: bool = False,
    page_texts: tuple[str, ...] | None = None,
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

    ``page_texts``, when provided (PDF ingest), contains one text
    string per page. The chunker is run per-page so that
    ``metadata["page"]`` (1-indexed) is stamped on every resulting
    :class:`~movate.core.models.KbChunk`. This lets the search table
    display the source page number alongside each result.

    Empty / whitespace-only input → ``None`` (idempotent no-op,
    matches :func:`_ingest_one_file`).
    """
    label = source or "<inline>"

    if page_texts:
        # Per-page chunking: run the splitter on each page individually
        # so we can stamp metadata["page"] = <1-indexed page number> on
        # every chunk. The paragraph_index from _make_chunk is kept too
        # so downstream code that reads it doesn't break.
        all_chunks: list[Chunk] = []
        for page_num, page_text in enumerate(page_texts, start=1):
            page_chunks = split_paragraphs(page_text, source=label)
            for chunk in page_chunks:
                chunk.metadata = {**(chunk.metadata or {}), "page": page_num}
            all_chunks.extend(page_chunks)
        chunks = all_chunks
    else:
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
                ocr=ocr,
            )
            # Duck-typed: any storage backend implementing
            # ``save_kb_chunk`` works (Postgres / sqlite / in-memory).
            await storage.save_kb_chunk(kb_chunk)
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
