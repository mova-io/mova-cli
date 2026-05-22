"""Tests for movate.kb.embed — multi-provider embedding routing.

Coverage:
- _is_openai_model() routing predicate
- qualified_model_name() normalization
- embed_texts() short-circuit on empty input
- embed_texts() OpenAI path (mocked httpx)
- embed_texts() LiteLLM path (mocked litellm.aembedding)
- embed_texts() error cases: no key, HTTP 401, bad status, bad shape,
  count mismatch, network error
- LiteLLM path: import error, provider exception, bad shape, count mismatch
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from movate.kb.embed import (
    DEFAULT_EMBEDDING_MODEL,
    EMBED_DIM_DEFAULT,
    EmbeddingError,
    _is_openai_model,
    embed_texts,
    embedding_dim,
    embedding_model,
    qualified_model_name,
)

# ---------------------------------------------------------------------------
# _is_openai_model
# ---------------------------------------------------------------------------


class TestIsOpenaiModel:
    def test_bare_text_embedding_prefix(self) -> None:
        assert _is_openai_model("text-embedding-3-small") is True

    def test_bare_text_embedding_large(self) -> None:
        assert _is_openai_model("text-embedding-3-large") is True

    def test_openai_slash_prefix(self) -> None:
        assert _is_openai_model("openai/text-embedding-3-small") is True

    def test_openai_prefix_custom_model(self) -> None:
        assert _is_openai_model("openai/text-embedding-ada-002") is True

    def test_cohere_model_is_not_openai(self) -> None:
        assert _is_openai_model("cohere/embed-english-v3.0") is False

    def test_voyage_model_is_not_openai(self) -> None:
        assert _is_openai_model("voyage/voyage-3") is False

    def test_bedrock_model_is_not_openai(self) -> None:
        assert _is_openai_model("bedrock/amazon.titan-embed-text-v2:0") is False

    def test_azure_model_is_not_openai(self) -> None:
        assert _is_openai_model("azure/text-embedding-3-small") is False

    def test_empty_string_is_not_openai(self) -> None:
        assert _is_openai_model("") is False


# ---------------------------------------------------------------------------
# qualified_model_name
# ---------------------------------------------------------------------------


class TestQualifiedModelName:
    def test_bare_name_gets_openai_prefix(self) -> None:
        assert qualified_model_name("text-embedding-3-small") == "openai/text-embedding-3-small"

    def test_default_gets_openai_prefix(self) -> None:
        result = qualified_model_name()
        assert result == f"openai/{DEFAULT_EMBEDDING_MODEL}"

    def test_already_qualified_openai_unchanged(self) -> None:
        assert (
            qualified_model_name("openai/text-embedding-3-small") == "openai/text-embedding-3-small"
        )

    def test_cohere_already_qualified_unchanged(self) -> None:
        assert qualified_model_name("cohere/embed-english-v3.0") == "cohere/embed-english-v3.0"

    def test_voyage_already_qualified_unchanged(self) -> None:
        assert qualified_model_name("voyage/voyage-3") == "voyage/voyage-3"

    def test_bedrock_already_qualified_unchanged(self) -> None:
        val = "bedrock/amazon.titan-embed-text-v2:0"
        assert qualified_model_name(val) == val


# ---------------------------------------------------------------------------
# embed_texts — empty input short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_texts_empty_input_returns_empty() -> None:
    result = await embed_texts([])
    assert result == []


# ---------------------------------------------------------------------------
# embed_texts — OpenAI path (mocked httpx)
# ---------------------------------------------------------------------------


def _make_openai_response(
    embeddings: list[list[float]],
    *,
    status_code: int = 200,
) -> MagicMock:
    """Build a fake httpx.Response-alike for the OpenAI embeddings endpoint."""
    data = [{"embedding": e, "index": i} for i, e in enumerate(embeddings)]
    body = json.dumps({"object": "list", "data": data, "model": "text-embedding-3-small"})
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json.loads(body)
    mock_resp.text = body
    return mock_resp


@pytest.mark.asyncio
async def test_embed_texts_openai_bare_model() -> None:
    vecs = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    fake_resp = _make_openai_response(vecs)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value = mock_client

        result = await embed_texts(
            ["hello", "world"],
            model="text-embedding-3-small",
            api_key="sk-test",
        )

    assert result == vecs


@pytest.mark.asyncio
async def test_embed_texts_openai_with_prefix() -> None:
    vecs = [[0.9, 0.8]]
    fake_resp = _make_openai_response(vecs)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value = mock_client

        result = await embed_texts(
            ["query"],
            model="openai/text-embedding-3-small",
            api_key="sk-test",
        )

    assert result == vecs
    # Verify the bare model name (without prefix) was sent to the API.
    call_kwargs: dict[str, Any] = mock_client.post.call_args.kwargs
    assert call_kwargs["json"]["model"] == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_embed_texts_openai_reads_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    vecs = [[0.1]]
    fake_resp = _make_openai_response(vecs)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value = mock_client

        result = await embed_texts(["hi"], api_key=None)

    assert result == vecs
    headers = mock_client.post.call_args.kwargs["headers"]
    assert "Bearer sk-from-env" in headers["Authorization"]


# ---------------------------------------------------------------------------
# OpenAI path — error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_texts_openai_no_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EmbeddingError, match="no OpenAI API key"):
        await embed_texts(["text"], api_key=None)


@pytest.mark.asyncio
async def test_embed_texts_openai_401_raises() -> None:
    fake_resp = _make_openai_response([], status_code=401)
    fake_resp.status_code = 401

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value = mock_client

        with pytest.raises(EmbeddingError, match="401"):
            await embed_texts(["text"], api_key="bad-key")


@pytest.mark.asyncio
async def test_embed_texts_openai_non_200_raises() -> None:
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "Internal Server Error"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value = mock_client

        with pytest.raises(EmbeddingError, match="HTTP 500"):
            await embed_texts(["text"], api_key="sk-test")


@pytest.mark.asyncio
async def test_embed_texts_openai_bad_shape_raises() -> None:
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"not_data": []}  # missing "data" key

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value = mock_client

        with pytest.raises(EmbeddingError, match="unexpected OpenAI embedding response"):
            await embed_texts(["text"], api_key="sk-test")


@pytest.mark.asyncio
async def test_embed_texts_openai_count_mismatch_raises() -> None:
    # API returns 2 embeddings but we sent 3 texts.
    vecs = [[0.1], [0.2]]
    fake_resp = _make_openai_response(vecs)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value = mock_client

        with pytest.raises(EmbeddingError, match="2 embeddings for 3 inputs"):
            await embed_texts(["a", "b", "c"], api_key="sk-test")


@pytest.mark.asyncio
async def test_embed_texts_openai_network_error_raises() -> None:
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client_cls.return_value = mock_client

        with pytest.raises(EmbeddingError, match="network error reaching OpenAI"):
            await embed_texts(["text"], api_key="sk-test")


# ---------------------------------------------------------------------------
# embed_texts — LiteLLM path
# ---------------------------------------------------------------------------


def _make_litellm_response(embeddings: list[list[float]]) -> MagicMock:
    """Build a fake litellm EmbeddingResponse-alike."""
    data = [{"embedding": e, "index": i} for i, e in enumerate(embeddings)]
    mock_resp = MagicMock()
    mock_resp.data = data
    return mock_resp


@pytest.mark.asyncio
async def test_embed_texts_litellm_path_routes_cohere() -> None:
    vecs = [[0.1, 0.2], [0.3, 0.4]]
    fake_resp = _make_litellm_response(vecs)

    mock_litellm = MagicMock()
    mock_litellm.aembedding = AsyncMock(return_value=fake_resp)

    with patch.dict("sys.modules", {"litellm": mock_litellm}):
        result = await embed_texts(
            ["hello", "world"],
            model="cohere/embed-english-v3.0",
            api_key="cohere-key",
        )

    assert result == vecs
    mock_litellm.aembedding.assert_awaited_once_with(
        model="cohere/embed-english-v3.0",
        input=["hello", "world"],
        api_key="cohere-key",
    )


@pytest.mark.asyncio
async def test_embed_texts_litellm_no_api_key_not_passed() -> None:
    """When api_key is None, it should NOT be forwarded to litellm."""
    vecs = [[0.5, 0.6]]
    fake_resp = _make_litellm_response(vecs)

    mock_litellm = MagicMock()
    mock_litellm.aembedding = AsyncMock(return_value=fake_resp)

    with patch.dict("sys.modules", {"litellm": mock_litellm}):
        result = await embed_texts(
            ["text"],
            model="voyage/voyage-3",
            api_key=None,
        )

    assert result == vecs
    call_kwargs = mock_litellm.aembedding.call_args.kwargs
    assert "api_key" not in call_kwargs


@pytest.mark.asyncio
async def test_embed_texts_litellm_import_error_raises() -> None:
    # Setting sys.modules["litellm"] = None causes `import litellm` to
    # raise ImportError even when the package is installed — Python's
    # import machinery treats a None entry as a "blocked" module.
    with (
        patch.dict("sys.modules", {"litellm": None}),
        pytest.raises(EmbeddingError, match="litellm is not installed"),
    ):
        await embed_texts(
            ["text"],
            model="cohere/embed-english-v3.0",
            api_key=None,
        )


@pytest.mark.asyncio
async def test_embed_texts_litellm_provider_exception_raises() -> None:
    mock_litellm = MagicMock()
    mock_litellm.aembedding = AsyncMock(side_effect=RuntimeError("cohere rate limit exceeded"))

    with (
        patch.dict("sys.modules", {"litellm": mock_litellm}),
        pytest.raises(EmbeddingError, match="LiteLLM embedding failed"),
    ):
        await embed_texts(
            ["text"],
            model="cohere/embed-english-v3.0",
            api_key="key",
        )


@pytest.mark.asyncio
async def test_embed_texts_litellm_bad_shape_raises() -> None:
    fake_resp = MagicMock()
    fake_resp.data = None  # missing / None data

    mock_litellm = MagicMock()
    mock_litellm.aembedding = AsyncMock(return_value=fake_resp)

    with (
        patch.dict("sys.modules", {"litellm": mock_litellm}),
        pytest.raises(EmbeddingError, match="unexpected LiteLLM embedding response"),
    ):
        await embed_texts(
            ["text"],
            model="voyage/voyage-3",
            api_key="key",
        )


@pytest.mark.asyncio
async def test_embed_texts_litellm_count_mismatch_raises() -> None:
    # 2 embeddings returned for 3 inputs.
    vecs = [[0.1], [0.2]]
    fake_resp = _make_litellm_response(vecs)

    mock_litellm = MagicMock()
    mock_litellm.aembedding = AsyncMock(return_value=fake_resp)

    with (
        patch.dict("sys.modules", {"litellm": mock_litellm}),
        pytest.raises(EmbeddingError, match="2 embeddings for 3 inputs"),
    ):
        await embed_texts(
            ["a", "b", "c"],
            model="cohere/embed-english-v3.0",
            api_key="key",
        )


# ---------------------------------------------------------------------------
# embedding_model() / embedding_dim() — deployment config resolvers (ADR 009 Task 5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_embedding_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOVATE_EMBED_MODEL", raising=False)
    assert embedding_model() == DEFAULT_EMBEDDING_MODEL


@pytest.mark.unit
def test_embedding_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_EMBED_MODEL", "cohere/embed-english-v3.0")
    assert embedding_model() == "cohere/embed-english-v3.0"


@pytest.mark.unit
def test_embedding_dim_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOVATE_EMBED_DIM", raising=False)
    assert embedding_dim() == EMBED_DIM_DEFAULT == 1536


@pytest.mark.unit
def test_embedding_dim_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_EMBED_DIM", "1024")
    assert embedding_dim() == 1024


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "  "])
def test_embedding_dim_invalid_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("MOVATE_EMBED_DIM", bad)
    assert embedding_dim() == EMBED_DIM_DEFAULT
