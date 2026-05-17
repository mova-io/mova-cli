"""In-memory corpus store for the v0.7 RAG surface.

Loads a JSON-array corpus file once at agent boot and hands it to the
retriever. Deliberately minimal — no streaming, no persistence layer,
no chunking. The v0.8 production engine will replace this with
pgvector / Azure AI Search; agent code calling :meth:`InMemoryCorpus.entries`
won't notice the swap because the retriever interface stays uniform.

Scope:
* JSON array of objects on disk → ``list[dict[str, Any]]`` in memory.
* No transformation — raw entries are exposed; the retriever computes
  whatever index it needs.
* Missing file, malformed JSON, or non-array roots raise
  :class:`KnowledgeStoreError` so the caller can surface a clear
  config error (vs. crashing mid-query later).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class KnowledgeStoreError(ValueError):
    """Raised when a corpus file can't be loaded or has the wrong shape.

    Caller-actionable: the message names the file + the specific shape
    violation so an operator can fix the corpus without digging into
    a stack trace.
    """


class InMemoryCorpus:
    """A list of corpus entries kept in memory for the lifetime of the
    process. Created once per agent at boot, queried many times.

    Two ways to construct:
    * :meth:`from_path` — read a JSON-array file (the v0.7 default).
    * Direct constructor (``InMemoryCorpus(entries=[...])``) — handy
      for tests + fixtures.

    Entries are returned untransformed — the retriever decides which
    fields to index. This keeps the corpus format flexible across
    agents (kb-lookup uses ``symptom``/``resolution``; FAQ corpora use
    ``question``/``answer``; etc.).
    """

    def __init__(self, *, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    @classmethod
    def from_path(cls, path: str | Path) -> InMemoryCorpus:
        """Load a JSON-array corpus from disk.

        Raises :class:`KnowledgeStoreError` for missing files,
        malformed JSON, non-array roots, or any entry that isn't a
        JSON object. The error message names the offending file so
        the caller can surface it without inventing one.
        """
        p = Path(path)
        if not p.is_file():
            raise KnowledgeStoreError(f"corpus file not found: {p}")
        try:
            raw = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise KnowledgeStoreError(
                f"corpus file {p} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, list):
            raise KnowledgeStoreError(
                f"corpus file {p} must contain a JSON array at the root, "
                f"got {type(raw).__name__}"
            )
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise KnowledgeStoreError(
                    f"corpus file {p} entry [{i}] must be a JSON object, "
                    f"got {type(entry).__name__}"
                )
        return cls(entries=raw)

    @property
    def entries(self) -> list[dict[str, Any]]:
        """All loaded entries. The retriever indexes whatever fields
        its :class:`KnowledgeConfig` declares; everything else is
        carried through unchanged for the caller (e.g. a citation
        renderer that wants ``url`` or ``source``)."""
        return self._entries

    def __len__(self) -> int:
        return len(self._entries)
