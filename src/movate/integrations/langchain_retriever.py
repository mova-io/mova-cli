"""LangChain ``BaseRetriever`` adapter for the mdk knowledge-base system.

Bridges the LangChain/LangGraph ecosystem with mdk's KB so that LangGraph
nodes (or any LCEL chain) can query an agent's knowledge base via the
standard ``retriever.invoke("question")`` interface.

Two modes:

* **Remote** (``runtime_url`` provided) — issues ``POST
  /api/v1/agents/{name}/kb/search`` against a running ``mdk serve``
  instance.  No local storage or embedding model required; the runtime
  handles embedding + retrieval server-side.
* **Local** (``storage`` provided) — calls
  :func:`movate.kb.search.search` in-process through the
  :class:`~movate.storage.base.StorageProvider` Protocol.  Requires
  ``mdk[langchain]`` AND an embedding model reachable from the current
  process.

Usage::

    from movate.integrations.langchain_retriever import MdkRetriever

    # Remote mode (most common — talks to `mdk serve`):
    retriever = MdkRetriever(
        agent_name="my-agent",
        runtime_url="https://my-runtime.example.com",
        api_key="Bearer ...",
    )
    docs = retriever.invoke("what is the refund policy?")

    # Local mode (direct storage access):
    retriever = MdkRetriever(
        agent_name="my-agent",
        storage=my_storage_provider,
    )
    docs = await retriever.ainvoke("what is the refund policy?")

Each KB chunk is returned as a LangChain
``Document(page_content=chunk_text, metadata={score, title, chunk_id})``.

Requires the ``mdk[langchain]`` extra (``langchain-core>=0.3``).
The module is **import-safe** without the extra — ``langchain_core`` is
imported lazily at class-instantiation time, not at module import time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_log = logging.getLogger(__name__)


def _import_langchain() -> tuple[type, type]:
    """Lazy-import ``langchain_core`` and return ``(BaseRetriever, Document)``.

    Raises :class:`ImportError` with a helpful message when the extra
    is missing.
    """
    try:
        from langchain_core.documents import Document as _Doc  # noqa: PLC0415
        from langchain_core.retrievers import BaseRetriever as _Base  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "MdkRetriever requires the mdk[langchain] extra. "
            "Install it with: pip install movate-cli[langchain]"
        ) from exc
    return _Base, _Doc


# Build the real class only when needed — this keeps the module itself
# importable even when langchain-core is absent (e.g. during mypy runs
# on the base install, or in downstream code that only TYPE_CHECKs the
# symbol).
_MdkRetrieverClass: type | None = None


def _build_class() -> type:
    """Construct and cache the ``MdkRetriever`` class.

    The class must inherit from ``BaseRetriever`` at *definition* time
    (Pydantic v2 model), so we defer the ``class`` statement until we
    can import the base.
    """
    global _MdkRetrieverClass  # noqa: PLW0603
    if _MdkRetrieverClass is not None:
        return _MdkRetrieverClass

    base_retriever, lc_document = _import_langchain()

    from pydantic import ConfigDict as PydConfigDict  # noqa: PLC0415
    from pydantic import Field as PydField  # noqa: PLC0415

    class _MdkRetriever(base_retriever):  # type: ignore[misc,valid-type]
        """LangChain retriever backed by an mdk agent's knowledge base.

        Parameters
        ----------
        agent_name:
            Name of the agent whose KB to search (must match the
            ``name:`` field in the agent's ``agent.yaml``).
        storage:
            A :class:`~movate.storage.base.StorageProvider` instance for
            local/in-process retrieval.  Mutually preferred with
            ``runtime_url`` — when *both* are supplied, ``storage``
            wins (avoids a network round-trip).
        top_k:
            Maximum number of chunks to retrieve per query.
        api_key:
            Bearer token for the runtime API (remote mode) **or** the
            embedding-provider API key (local mode).  Passed as-is.
        runtime_url:
            Base URL of a running ``mdk serve`` instance (e.g.
            ``https://my-runtime.example.com``).  Enables remote mode.
        tenant_id:
            Tenant scope for KB queries.  Defaults to ``"default"``.
        hybrid:
            Combine vector + BM25 lexical search via reciprocal rank
            fusion (same as ``mdk kb search --hybrid``).
        """

        model_config = PydConfigDict(arbitrary_types_allowed=True)

        agent_name: str
        storage: Any = None
        top_k: int = PydField(default=5, ge=1, le=50)
        api_key: str | None = None
        runtime_url: str | None = None
        tenant_id: str = "default"
        hybrid: bool = False

        def _validate_config(self) -> None:
            if self.storage is None and self.runtime_url is None:
                msg = (
                    "MdkRetriever requires either 'storage' (local mode) "
                    "or 'runtime_url' (remote mode)"
                )
                raise ValueError(msg)

        def model_post_init(self, __context: Any) -> None:
            """Validate that at least one retrieval path is configured."""
            super().model_post_init(__context)
            self._validate_config()

        # -- sync entrypoint ------------------------------------------------

        def _get_relevant_documents(
            self,
            query: str,
            *,
            run_manager: Any = None,
        ) -> list[Any]:
            """Synchronous retrieval — bridges to the async implementation.

            LangChain's ``retriever.invoke()`` calls this method.
            """
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                # Already inside an event loop (e.g. Jupyter, FastAPI).
                # Spin up a background thread to avoid deadlock.
                import concurrent.futures  # noqa: PLC0415

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(
                        asyncio.run,
                        self._aget_relevant_documents(query, run_manager=None),
                    ).result()

            return asyncio.run(
                self._aget_relevant_documents(query, run_manager=None),
            )

        # -- async entrypoint -----------------------------------------------

        async def _aget_relevant_documents(
            self,
            query: str,
            *,
            run_manager: Any = None,
        ) -> list[Any]:
            """Asynchronous retrieval — the primary implementation."""
            if self.storage is not None:
                return await self._search_local(query)
            return await self._search_remote(query)

        # -- local path (StorageProvider) -----------------------------------

        async def _search_local(self, query: str) -> list[Any]:
            """In-process retrieval via :func:`movate.kb.search.search`."""
            from movate.kb.search import search as kb_search  # noqa: PLC0415

            results = await kb_search(
                storage=self.storage,
                question=query,
                agent=self.agent_name,
                tenant_id=self.tenant_id,
                limit=self.top_k,
                api_key=self.api_key,
                hybrid=self.hybrid,
            )
            return [_chunk_to_doc(r) for r in results]

        # -- remote path (runtime HTTP API) ---------------------------------

        async def _search_remote(self, query: str) -> list[Any]:
            """HTTP retrieval via ``POST /api/v1/agents/{name}/kb/search``."""
            import httpx  # noqa: PLC0415

            url = f"{self.runtime_url!s}/api/v1/agents/{self.agent_name}/kb/search"
            headers: dict[str, str] = {}
            if self.api_key is not None:
                headers["Authorization"] = (
                    self.api_key
                    if self.api_key.lower().startswith("bearer ")
                    else f"Bearer {self.api_key}"
                )

            payload: dict[str, Any] = {
                "question": query,
                "k": self.top_k,
                "hybrid": self.hybrid,
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()

            results: list[dict[str, Any]] = body.get("results", [])
            return [_remote_hit_to_doc(hit) for hit in results]

    # -- converters (module-scope helpers that close over lc_document) -------

    def _chunk_to_doc(result: Any) -> Any:
        """Convert a :class:`~movate.core.models.KbChunkWithScore`."""
        chunk = result.chunk
        metadata: dict[str, Any] = {
            "score": result.score,
            "chunk_id": chunk.chunk_id,
            "source": chunk.source,
        }
        if chunk.metadata:
            metadata.update(chunk.metadata)
        return lc_document(page_content=chunk.text, metadata=metadata)

    def _remote_hit_to_doc(hit: dict[str, Any]) -> Any:
        """Convert a JSON hit from the runtime API response."""
        metadata: dict[str, Any] = {
            "score": hit.get("score", 0.0),
            "chunk_id": hit.get("chunk_id", ""),
            "source": hit.get("source", ""),
        }
        if hit.get("metadata"):
            metadata.update(hit["metadata"])
        return lc_document(page_content=hit.get("text", ""), metadata=metadata)

    _MdkRetrieverClass = _MdkRetriever
    return _MdkRetrieverClass


class _MdkRetrieverProxy:
    """Module-level proxy that defers class construction until first use.

    This lets ``from movate.integrations.langchain_retriever import
    MdkRetriever`` succeed even when ``langchain-core`` is not installed.
    The actual class (inheriting ``BaseRetriever``) is built on first
    instantiation.
    """

    def __call__(self, **kwargs: Any) -> Any:
        cls = _build_class()
        return cls(**kwargs)

    def __instancecheck__(self, instance: Any) -> bool:
        if _MdkRetrieverClass is None:
            return False
        return isinstance(instance, _MdkRetrieverClass)

    def __repr__(self) -> str:
        return "<MdkRetriever (lazy proxy — instantiate to build)>"


MdkRetriever: Any = _MdkRetrieverProxy()

__all__ = ["MdkRetriever"]
