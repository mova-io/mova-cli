"""Knowledge store — :class:`Document`, :class:`Chunk`, :class:`KnowledgeStore` Protocol.

The Protocol is the seam between today's :class:`InMemoryStore` (substring
retrieval, no embeddings, no deps) and tomorrow's vector-store impl
(pgvector / Azure AI Search / Qdrant — TBD per BACKLOG K-state).

Callers depend only on the Protocol, so swapping engines is a
one-import change at the runtime-construction site. No agent.yaml
schema change required.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Document:
    """A document registered in the knowledge base.

    ``id`` is operator-supplied (or auto-generated from path) and
    must be unique within a knowledge base. ``content_hash`` is the
    SHA-256 of the body — used to detect drift (a document whose
    content changed on disk but kept the same id).

    The MVP keeps the body in memory; the v0.8 engine swaps in lazy
    loading + embeddings without touching this shape.
    """

    id: str
    body: str
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    source_path: str = ""
    content_hash: str = ""


def make_document(
    *,
    doc_id: str,
    body: str,
    description: str = "",
    tags: tuple[str, ...] = (),
    source_path: str = "",
) -> Document:
    """Construct a :class:`Document` with the content hash computed.

    Caller-side hashing keeps the dataclass frozen / immutable.
    SHA-256 is overkill for change detection but matches the
    snapshot-cluster convention from Group K — content-addressed
    artifacts everywhere.
    """
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return Document(
        id=doc_id,
        body=body,
        description=description,
        tags=tags,
        source_path=source_path,
        content_hash=content_hash,
    )


@dataclass(frozen=True)
class Chunk:
    """A segment of a document. The atomic unit of retrieval.

    For MVP: chunks are paragraphs (split on double-newline). For
    v0.8, the chunker becomes a configurable component (sentence-
    boundary, fixed-token, semantic) — the data shape stays the same.

    ``offset`` is the byte position of the chunk's start in the
    original document body — useful for the retriever to produce
    citation links back to the source.
    """

    doc_id: str
    chunk_index: int
    text: str
    offset: int


def chunk_document(doc: Document) -> tuple[Chunk, ...]:
    """Split a document into paragraph-level chunks.

    MVP heuristic: split on blank lines. Drop empty chunks. Single-
    paragraph documents produce one chunk. Real chunking (sentence
    boundary, semantic, fixed-token) lands in v0.8 behind the same
    function signature.
    """
    text = doc.body
    chunks: list[Chunk] = []
    cursor = 0
    chunk_index = 0
    for raw in text.split("\n\n"):
        # Track byte offset of this segment in the original body
        # before stripping whitespace — citation accuracy depends on it.
        segment_start = cursor
        cursor = segment_start + len(raw) + len("\n\n")
        stripped = raw.strip()
        if not stripped:
            continue
        chunks.append(
            Chunk(
                doc_id=doc.id,
                chunk_index=chunk_index,
                text=stripped,
                offset=segment_start,
            )
        )
        chunk_index += 1
    return tuple(chunks)


class KnowledgeStore(Protocol):
    """Store + retrieve interface — the engine seam.

    Today: :class:`InMemoryStore` implements with substring matching.
    Tomorrow: a vector-store impl with the same Protocol shape slots
    in without callers noticing.
    """

    def add(self, doc: Document) -> None:
        """Register a document. Replaces any prior entry with the
        same ``doc.id`` — call sites are responsible for de-duping."""
        ...

    def get(self, doc_id: str) -> Document | None:
        """Look up a document by id. ``None`` if not registered."""
        ...

    def list_documents(self) -> tuple[Document, ...]:
        """Return every registered document. Order is registration order."""
        ...

    def all_chunks(self) -> tuple[Chunk, ...]:
        """Return every chunk across all documents. Used by the
        :func:`movate.knowledge.retriever.retrieve` helper to score
        against the full corpus."""
        ...


class InMemoryStore:
    """Reference implementation of :class:`KnowledgeStore`.

    Documents live in a dict; chunks are computed once at add time and
    cached. Zero external dependencies — pure stdlib. Suitable for
    demos, unit tests, and small corpora (~hundreds of documents).
    Scales worst-case linearly in corpus size on every retrieve —
    fine for the substring path, terrible if you graft embeddings on
    top. Swap to the vector impl when corpus crosses ~1K documents
    or sub-second latency matters.
    """

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._chunks_by_doc: dict[str, tuple[Chunk, ...]] = {}

    def add(self, doc: Document) -> None:
        self._docs[doc.id] = doc
        self._chunks_by_doc[doc.id] = chunk_document(doc)

    def get(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    def list_documents(self) -> tuple[Document, ...]:
        return tuple(self._docs.values())

    def all_chunks(self) -> tuple[Chunk, ...]:
        result: list[Chunk] = []
        for chunks in self._chunks_by_doc.values():
            result.extend(chunks)
        return tuple(result)
