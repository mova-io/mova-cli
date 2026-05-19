"""Knowledge-base ingest + retrieval — MDK 0.9 RAG core.

This package is the smallest end-to-end RAG slice MDK ships (per the
operator MVP brief 2026-05-19):

* :mod:`movate.kb.embed` — async wrapper around an embedding model
  (currently OpenAI ``text-embedding-3-small``). Future: pluggable
  via a ``EmbeddingProvider`` protocol; see tier 10.1 in BACKLOG.md.
* :mod:`movate.kb.chunk` — text → chunks. v0.9: paragraph splitter
  (``\\n\\n``); future: token-bounded recursive markdown chunker.
* :mod:`movate.kb.ingest` — orchestrates read → chunk → embed → save.
  Powers the ``mdk kb ingest`` CLI.
* :mod:`movate.kb.search` — orchestrates embed query → storage
  search → rank. Powers the ``mdk kb search`` CLI + the
  ``kb-vector-lookup`` skill.

Module boundaries deliberately keep each piece testable in isolation.
"""

from __future__ import annotations

__all__: list[str] = []
