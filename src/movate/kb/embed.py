"""Embedding helper — OpenAI ``text-embedding-3-small`` via httpx.

Deliberately avoids LiteLLM here. LiteLLM's embeddings path has
specific quirks (sync-wrap of async, custom retry policy, etc.) that
we don't want for the KB pipeline — we want a clean direct path with
straightforward error surfaces. Also keeps cost accounting cleaner:
embedding calls don't share the litellm cost ledger, which is set up
for completion calls.

Future (tier 10.1 of BACKLOG.md): wrap in an ``EmbeddingProvider``
protocol so Voyage / Cohere / BGE local can swap in. For the v0.9
MVP, one provider is enough.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Default model — ``text-embedding-3-small`` is the cheapest serviceable
# OpenAI embedding at the time of writing (1536 dims, $0.02 / 1M tokens,
# strong retrieval quality on standard benchmarks). Operators who need
# a different model can pass it explicitly; the storage layer is
# model-agnostic.
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
"""Default OpenAI model identifier. NOT prefixed with ``openai/`` —
the prefix is added when we persist via ``KbChunk.embedding_model``."""

OPENAI_EMBEDDINGS_ENDPOINT = "https://api.openai.com/v1/embeddings"
"""OpenAI's embeddings endpoint. No regional alternates yet (unlike
OpenAI's chat endpoint with Azure variants)."""

# Generous per-request timeout. Embedding calls are typically <1s but
# can spike during OpenAI incidents; a 60s ceiling lets the caller
# retry rather than waiting forever.
DEFAULT_TIMEOUT_S = 60.0

# HTTP status constants — surfaced as named constants so the ``!= 200``
# / ``== 401`` checks below pass lint's PLR2004 (no magic numbers).
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401


class EmbeddingError(Exception):
    """Raised when the embedding provider rejects the request or
    returns a malformed response. Callers should treat as fatal for
    the current ingest / query — no automatic retry here (the storage
    layer's idempotency makes retries safe but they're the caller's
    decision)."""


async def embed_texts(
    texts: list[str],
    *,
    model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[list[float]]:
    """Embed a batch of texts. Returns one float-list per input text.

    Empty input → empty output (no API call). OpenAI accepts batches
    up to ~8192 tokens total per request; for large batches the
    ingest pipeline pre-splits into chunks of 100 texts. This function
    is single-batch — chunking lives in the caller.

    ``api_key`` defaults to ``OPENAI_API_KEY`` from the environment.
    Raises :class:`EmbeddingError` on auth failure / API errors /
    unparseable responses.
    """
    if not texts:
        return []
    key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise EmbeddingError(
            "no OpenAI API key — set OPENAI_API_KEY or run ``mdk auth login openai``."
        )

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "input": texts,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        try:
            resp = await client.post(
                OPENAI_EMBEDDINGS_ENDPOINT,
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"network error reaching OpenAI embeddings: {type(exc).__name__}: {exc}"
            ) from exc

    if resp.status_code == _HTTP_UNAUTHORIZED:
        raise EmbeddingError(
            "OpenAI rejected the key (HTTP 401). Run "
            "``mdk auth status`` to verify, or ``mdk auth login openai`` "
            "to rotate."
        )
    if resp.status_code != _HTTP_OK:
        # Surface the error body so the operator can see e.g. quota /
        # rate-limit / model-not-found details directly. Truncate at
        # 500 chars to keep the message readable.
        body = resp.text[:500]
        raise EmbeddingError(f"OpenAI returned HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
        rows = data["data"]
        embeddings = [row["embedding"] for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingError(
            f"unexpected embedding response shape: {type(exc).__name__}: {exc}"
        ) from exc

    if len(embeddings) != len(texts):
        raise EmbeddingError(
            f"OpenAI returned {len(embeddings)} embeddings for "
            f"{len(texts)} inputs — refusing to align them."
        )

    return embeddings


def qualified_model_name(model: str = DEFAULT_EMBEDDING_MODEL) -> str:
    """Return the ``provider/model`` form we persist in ``KbChunk.embedding_model``.

    The storage layer uses this string for cross-model dim-mismatch
    detection. By convention: ``"openai/text-embedding-3-small"``.
    Mirrors LiteLLM's ``provider/model`` notation so future
    multi-provider work doesn't change the format on disk.
    """
    return f"openai/{model}"
