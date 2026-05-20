"""Embedding helper — multi-provider via direct OpenAI httpx or LiteLLM.

Routing logic
-------------
* Models that are bare names (``text-embedding-3-small``) or carry an
  ``openai/`` prefix are sent through a **direct httpx path** — no
  LiteLLM overhead, clean error surfaces, straightforward cost accounting.
* Any other ``provider/model`` string (``cohere/embed-english-v3.0``,
  ``voyage/voyage-3``, ``bedrock/amazon.titan-embed-text-v2:0``, etc.) is
  routed through **LiteLLM** ``aembedding()``, which handles provider-
  specific auth, batching limits, and response normalisation.

This gives operators a single ``--model`` flag on ``mdk kb ingest`` /
``mdk kb search`` that unlocks the full LiteLLM embedding catalogue while
keeping the hot-path (OpenAI) dependency-free and fast.

The provider/model strings we persist in ``KbChunk.embedding_model``
already follow the ``provider/model`` convention, so existing chunks
stored under ``openai/text-embedding-3-small`` are unaffected.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Default model — ``text-embedding-3-small`` is the cheapest serviceable
# OpenAI embedding at the time of writing (1536 dims, $0.02 / 1M tokens,
# strong retrieval quality on standard benchmarks).
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
"""Default OpenAI model identifier (bare, without provider prefix).
The ``openai/`` prefix is added via :func:`qualified_model_name` when
we persist to ``KbChunk.embedding_model``."""

OPENAI_EMBEDDINGS_ENDPOINT = "https://api.openai.com/v1/embeddings"

# Generous per-request timeout. Embedding calls are typically <1s but
# can spike during provider incidents.
DEFAULT_TIMEOUT_S = 60.0

# HTTP status constants — avoids PLR2004 (magic number) lint warnings.
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401

# Model name prefixes that we route through the direct OpenAI httpx path.
# Everything else goes through LiteLLM.
_OPENAI_MODEL_PREFIXES = ("openai/", "text-embedding-")


class EmbeddingError(Exception):
    """Raised when the embedding provider rejects the request or returns
    a malformed response.  Callers should treat as fatal for the current
    ingest / query — the storage layer's idempotency makes caller-side
    retries safe."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def embed_texts(
    texts: list[str],
    *,
    model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[list[float]]:
    """Embed a batch of texts and return one float-list per input.

    Empty input → empty output (no API call).

    Routing:
    - Bare model names (``text-embedding-3-small``) and ``openai/...``
      models → direct httpx call to the OpenAI embeddings endpoint.
    - Any other ``provider/model`` string → LiteLLM ``aembedding()``.

    ``api_key`` is forwarded to the direct OpenAI path or as the
    ``api_key`` kwarg in LiteLLM (if provided).  For non-OpenAI models
    via LiteLLM, the provider-specific API key is usually read from the
    corresponding env var (e.g. ``COHERE_API_KEY``, ``VOYAGE_API_KEY``);
    ``api_key`` overrides that.

    Raises :class:`EmbeddingError` on auth failure, API errors, or
    unparseable responses.
    """
    if not texts:
        return []

    if _is_openai_model(model):
        return await _embed_via_openai(texts, model=model, api_key=api_key, timeout_s=timeout_s)
    return await _embed_via_litellm(texts, model=model, api_key=api_key)


def qualified_model_name(model: str = DEFAULT_EMBEDDING_MODEL) -> str:
    """Return the ``provider/model`` form persisted in ``KbChunk.embedding_model``.

    The storage layer uses this string for cross-model dim-mismatch
    detection.  Rules:

    * If *model* already contains a ``/`` it is assumed to be fully
      qualified (e.g. ``cohere/embed-english-v3.0``).
    * Otherwise it is treated as a bare OpenAI model name and the
      ``openai/`` prefix is prepended (e.g. ``text-embedding-3-small``
      → ``openai/text-embedding-3-small``).
    """
    if "/" in model:
        return model
    return f"openai/{model}"


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def _is_openai_model(model: str) -> bool:
    """Return True for bare OpenAI model names and ``openai/`` prefixed models."""
    return any(model.startswith(prefix) for prefix in _OPENAI_MODEL_PREFIXES)


async def _embed_via_openai(
    texts: list[str],
    *,
    model: str,
    api_key: str | None,
    timeout_s: float,
) -> list[list[float]]:
    """Direct httpx call to the OpenAI embeddings endpoint.

    Strips the ``openai/`` prefix before sending — the API accepts the
    bare model name only.
    """
    bare_model = model.removeprefix("openai/")
    key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise EmbeddingError(
            "no OpenAI API key — set OPENAI_API_KEY or run ``mdk auth login openai``."
        )

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"model": bare_model, "input": texts}

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        try:
            resp = await client.post(OPENAI_EMBEDDINGS_ENDPOINT, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"network error reaching OpenAI embeddings: {type(exc).__name__}: {exc}"
            ) from exc

    if resp.status_code == _HTTP_UNAUTHORIZED:
        raise EmbeddingError(
            "OpenAI rejected the key (HTTP 401). Run "
            "``mdk auth status`` to verify, or ``mdk auth login openai`` to rotate."
        )
    if resp.status_code != _HTTP_OK:
        body = resp.text[:500]
        raise EmbeddingError(f"OpenAI returned HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
        embeddings = [row["embedding"] for row in data["data"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingError(
            f"unexpected OpenAI embedding response shape: {type(exc).__name__}: {exc}"
        ) from exc

    if len(embeddings) != len(texts):
        raise EmbeddingError(
            f"OpenAI returned {len(embeddings)} embeddings for "
            f"{len(texts)} inputs — refusing to align them."
        )
    return embeddings


async def _embed_via_litellm(
    texts: list[str],
    *,
    model: str,
    api_key: str | None,
) -> list[list[float]]:
    """Route embedding request through LiteLLM's ``aembedding()`` call.

    LiteLLM supports Cohere, Voyage, Bedrock, Azure OpenAI, Mistral,
    and many others.  Provider auth is read from the corresponding env
    var (``COHERE_API_KEY``, ``VOYAGE_API_KEY``, ``AWS_*``, etc.) or
    overridden via ``api_key``.

    LiteLLM's response normalises to ``response.data[i]["embedding"]``
    regardless of provider, so we don't need provider-specific parsing.
    """
    try:
        import litellm  # noqa: PLC0415 — optional dep, imported lazily
    except ImportError as exc:  # pragma: no cover
        raise EmbeddingError(
            f"litellm is not installed — cannot embed with model {model!r}. "
            "Run: pip install litellm"
        ) from exc

    kwargs: dict[str, Any] = {"model": model, "input": texts}
    if api_key:
        kwargs["api_key"] = api_key

    try:
        response = await litellm.aembedding(**kwargs)
    except Exception as exc:
        # LiteLLM raises various provider-specific exceptions; wrap all
        # into EmbeddingError so callers have a single catch target.
        raise EmbeddingError(
            f"LiteLLM embedding failed for model {model!r}: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        embeddings = [row["embedding"] for row in response.data]
    except (AttributeError, KeyError, TypeError) as exc:
        raise EmbeddingError(
            f"unexpected LiteLLM embedding response shape: {type(exc).__name__}: {exc}"
        ) from exc

    if len(embeddings) != len(texts):
        raise EmbeddingError(
            f"LiteLLM returned {len(embeddings)} embeddings for "
            f"{len(texts)} inputs — refusing to align them."
        )
    return embeddings
