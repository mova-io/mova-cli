"""Knowledge retrieval surface for the v0.7 RAG MVP.

Public exports compose into the v0.8 production retriever (pgvector /
Azure AI Search) without API breakage:

* :class:`InMemoryCorpus` — JSON-array corpus loaded once at agent boot.
* :class:`Retriever` — uniform interface (``query(text, top_k)``).
* :class:`BM25Retriever`, :class:`SubstringRetriever` — v0.7 backends.
* :class:`RetrievalHit` — one result; carries doc_id, score, raw entry.
* :func:`load_knowledge_config`, :func:`build_retriever` — build the
  configured retriever from a ``knowledge.yaml`` on disk.

Embeddings + reranking + chunking land in v0.8 as new retriever
implementations of the same protocol. Agent code that calls
``retriever.query(...)`` doesn't change when the backend swaps.
"""

from __future__ import annotations

from movate.knowledge.loader import (
    KnowledgeLoadError,
    build_retriever,
    load_knowledge_config,
)
from movate.knowledge.retriever import (
    BM25Retriever,
    RetrievalHit,
    Retriever,
    SubstringRetriever,
)
from movate.knowledge.store import InMemoryCorpus, KnowledgeStoreError

__all__ = [
    "BM25Retriever",
    "InMemoryCorpus",
    "KnowledgeLoadError",
    "KnowledgeStoreError",
    "RetrievalHit",
    "Retriever",
    "SubstringRetriever",
    "build_retriever",
    "load_knowledge_config",
]
