"""Knowledge base / RAG surface (Phase J-4).

**Intentionally minimal MVP.** The interface design ships now so
agents can declare knowledge dependencies in agent.yaml + workflows
can route through retrievers; the production engine (embeddings,
vector store, reranking) lands in v0.8 behind the same interface.

What ships today:

* :class:`Document` + :class:`Chunk` data classes — content-addressed
* :class:`KnowledgeStore` protocol — store/retrieve abstraction
* :class:`InMemoryStore` — substring + word-overlap scoring; no
  embeddings, no deps beyond stdlib
* :func:`load_knowledge` — parse ``knowledge.yaml``, ingest documents
* CLI: ``mdk knowledge {add, list, query}``

What does NOT ship (v0.8+ behind the same interface):

* Embeddings (TODO: pgvector, Azure AI Search, or Qdrant)
* Reranking (cross-encoder over top-k)
* PDF / Word / HTML ingestion (today: markdown + plain text only)
* Semantic chunking (today: fixed-size paragraph-split)
* Knowledge graphs (Apache AGE, item 75 in BACKLOG)

Why ship the surface now: the interface is the hard design call.
Without it, downstream features (knowledge-aware agents, retriever
workflow nodes) can't be authored. The substring retriever is good
enough for demos + unit tests; the real engine slots in transparently.
"""

from __future__ import annotations

from movate.knowledge.loader import (
    KnowledgeConfig,
    KnowledgeLoadError,
    load_knowledge,
)
from movate.knowledge.retriever import RetrievalResult, retrieve
from movate.knowledge.store import Chunk, Document, InMemoryStore, KnowledgeStore

__all__ = [
    "Chunk",
    "Document",
    "InMemoryStore",
    "KnowledgeConfig",
    "KnowledgeLoadError",
    "KnowledgeStore",
    "RetrievalResult",
    "load_knowledge",
    "retrieve",
]
