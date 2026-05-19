"""Text → chunks.

v0.9 MVP: paragraph splitter (``\\n\\n`` boundaries). Honest about
what this isn't:

* Not token-bounded — large paragraphs become large chunks.
  Acceptable for the MVP because OpenAI embeddings handle inputs up
  to ~8192 tokens; markdown paragraphs rarely exceed 500 tokens in
  the documents we care about.
* Not heading-aware — section context isn't preserved across chunks.
  A future recursive splitter (tier 10.1 / sprint 5 in BACKLOG.md)
  will honor heading hierarchies + keep heading paths in metadata.
* Not language-aware — assumes Western text. CJK / RTL documents
  may chunk oddly; the embedding model still handles them but
  retrieval boundaries match paragraph breaks, not sentence breaks.

The v0.9 MVP optimizes for: ships tonight, correct semantics on
markdown corpora, easy to swap out behind the same API.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Match one or more blank lines (with optional whitespace) — robust
# to CRLF / Windows line endings + indented blanks.
_PARAGRAPH_BOUNDARY = re.compile(r"\n[ \t]*\n+")

# Min chunk length (chars). Smaller fragments — single-word lines,
# stray punctuation — get dropped because they're not retrievable
# signal. 20 chars is short enough to keep meaningful one-liners.
MIN_CHUNK_CHARS = 20

# Soft cap on chunk size (chars). Paragraphs longer than this get
# split into roughly-equal pieces on word boundaries. 2000 chars ≈
# 500 tokens for English; comfortably under any embedding model's
# input limit. Soft = best-effort, may overshoot if a single word is
# longer than the cap (rare).
MAX_CHUNK_CHARS = 2000


@dataclass
class Chunk:
    """A single chunk produced by the splitter — text + dedup hash.

    The caller adds the rest of :class:`KbChunk` fields (agent, source,
    embedding, etc.) on top of this.
    """

    text: str
    content_hash: str
    """SHA-256 hex digest of ``text``. Combined with ``(agent, tenant_id)``
    in storage, this is the dedup key — re-ingesting an unchanged
    document is idempotent."""

    metadata: dict[str, int | str] | None = None
    """Optional bag of per-chunk metadata — e.g. paragraph index
    within the source document. The chunker fills ``paragraph_index``;
    future heading-aware chunkers will add ``heading_path``."""


def split_paragraphs(text: str, *, source: str = "") -> list[Chunk]:
    """Split ``text`` into chunks on paragraph boundaries.

    ``source`` is informational only — it's not embedded in the chunk
    or used for dedup; it's just there for future debugging.

    Algorithm:

    1. Split on blank-line boundaries (``\\n\\n`` + variants).
    2. Strip whitespace from each piece.
    3. Drop pieces shorter than :data:`MIN_CHUNK_CHARS` (typically
       single-word lines or stray fragments).
    4. For pieces longer than :data:`MAX_CHUNK_CHARS`, sub-split on
       sentence-boundary heuristics (``. `` + ``\\n``) into roughly
       equal pieces — paragraph identity is preserved via the
       ``paragraph_index`` metadata field.
    5. Hash each resulting chunk for dedup.
    """
    del source  # unused for now; reserved for future debug hooks
    if not text or not text.strip():
        return []

    raw_paragraphs = [p.strip() for p in _PARAGRAPH_BOUNDARY.split(text)]
    chunks: list[Chunk] = []
    for paragraph_idx, paragraph in enumerate(raw_paragraphs):
        if len(paragraph) < MIN_CHUNK_CHARS:
            continue
        if len(paragraph) <= MAX_CHUNK_CHARS:
            chunks.append(_make_chunk(paragraph, paragraph_idx))
        else:
            for sub in _subsplit_long_paragraph(paragraph):
                chunks.append(_make_chunk(sub, paragraph_idx))
    return chunks


def _make_chunk(text: str, paragraph_idx: int) -> Chunk:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Chunk(
        text=text,
        content_hash=digest,
        metadata={"paragraph_index": paragraph_idx},
    )


def _subsplit_long_paragraph(paragraph: str) -> list[str]:
    """Split a paragraph that exceeds ``MAX_CHUNK_CHARS``.

    Honors sentence boundaries (period + space, or newline) when
    possible. Falls back to a word-boundary split if a single
    "sentence" is itself longer than the cap (rare — code blocks
    with no paragraph breaks, for example).
    """
    # Split on sentence boundaries first.
    sentences = re.split(r"(?<=[.!?])\s+|\n", paragraph)
    sentences = [s.strip() for s in sentences if s.strip()]

    out: list[str] = []
    buf = ""
    for s in sentences:
        # If a single sentence exceeds the cap, force-split on words.
        if len(s) > MAX_CHUNK_CHARS:
            if buf:
                out.append(buf)
                buf = ""
            words = s.split()
            chunk_words: list[str] = []
            chunk_len = 0
            for w in words:
                if chunk_len + len(w) + 1 > MAX_CHUNK_CHARS and chunk_words:
                    out.append(" ".join(chunk_words))
                    chunk_words = [w]
                    chunk_len = len(w)
                else:
                    chunk_words.append(w)
                    chunk_len += len(w) + 1
            if chunk_words:
                out.append(" ".join(chunk_words))
            continue

        if len(buf) + len(s) + 1 > MAX_CHUNK_CHARS and buf:
            out.append(buf)
            buf = s
        else:
            buf = (buf + " " + s).strip() if buf else s
    if buf:
        out.append(buf)
    return out
