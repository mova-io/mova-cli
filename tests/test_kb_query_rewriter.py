"""Tests for ``movate.kb.rewrite`` — LLM-based query expansion.

The rewriter must never block retrieval — every failure mode
(bad LLM response, network timeout, malformed JSON) must degrade
to "return [question]" rather than raise. Tests cover both the
happy parse paths and the graceful-degradation cases.

The LiteLLM call is mocked end-to-end via ``monkeypatch`` on
``litellm.acompletion`` so tests run hermetically.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import litellm
import pytest

from movate.kb.rewrite import (
    DEFAULT_REWRITER_MODEL,
    MAX_VARIANTS,
    _extract_content,
    _parse_variants,
    rewrite_query,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_response(content: str) -> Any:
    """Build a fake LiteLLM response with the same shape ``acompletion``
    produces — ``resp.choices[0].message.content``."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def mock_litellm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch ``litellm.acompletion`` with an AsyncMock the test sets up.

    The fixture imports ``litellm`` at module scope so the monkeypatch
    sees the same module ``rewrite_query`` lazy-imports."""
    mock = AsyncMock()
    monkeypatch.setattr(litellm, "acompletion", mock)
    return mock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rewrite_returns_original_plus_variants(mock_litellm: AsyncMock) -> None:
    """Standard happy path: model returns clean JSON, we parse + dedup."""
    mock_litellm.return_value = _make_response(
        '{"variants": ["What is the refund window for annual plans?", '
        '"How long do I have to get a refund?", '
        '"Refund eligibility timeframe"]}'
    )
    out = await rewrite_query("refunds?", n=3)
    # Original is always first.
    assert out[0] == "refunds?"
    # Then up to N variants.
    assert len(out) == 4
    assert "What is the refund window for annual plans?" in out


@pytest.mark.unit
async def test_rewrite_strips_markdown_fences(mock_litellm: AsyncMock) -> None:
    """Some models wrap JSON in ```json...``` despite the no-markdown
    instruction. Tolerant parsing handles that."""
    mock_litellm.return_value = _make_response('```json\n{"variants": ["A", "B"]}\n```')
    out = await rewrite_query("test", n=2)
    assert out == ["test", "A", "B"]


@pytest.mark.unit
async def test_rewrite_dedups_case_insensitive(mock_litellm: AsyncMock) -> None:
    """A variant that exactly matches the original (or another variant)
    in lowercase doesn't get re-added."""
    mock_litellm.return_value = _make_response(
        '{"variants": ["TEST", "another", "test", "ANOTHER"]}'
    )
    out = await rewrite_query("test", n=4)
    # Original "test" + "another" only — case-folded dups dropped.
    assert out == ["test", "another"]


@pytest.mark.unit
async def test_rewrite_clamps_to_max_variants(mock_litellm: AsyncMock) -> None:
    """``n`` is clamped to MAX_VARIANTS to prevent runaway fan-out."""
    mock_litellm.return_value = _make_response('{"variants": []}')
    await rewrite_query("test", n=999)
    # Inspect the prompt the model received.
    call_kwargs = mock_litellm.call_args.kwargs
    prompt = call_kwargs["messages"][0]["content"]
    assert f"exactly {MAX_VARIANTS} alternative" in prompt


# ---------------------------------------------------------------------------
# Short-circuit cases (no LLM call)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rewrite_empty_question_returns_empty(mock_litellm: AsyncMock) -> None:
    """An empty / whitespace-only question short-circuits to []."""
    assert await rewrite_query("", n=3) == []
    assert await rewrite_query("   \n  ", n=3) == []
    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_rewrite_n_zero_skips_llm(mock_litellm: AsyncMock) -> None:
    """``n=0`` returns the original only, with no LLM call."""
    out = await rewrite_query("hello", n=0)
    assert out == ["hello"]
    mock_litellm.assert_not_called()


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rewrite_llm_exception_falls_back(mock_litellm: AsyncMock) -> None:
    """LLM call raises → log warning, return [question]. Retrieval
    must never be blocked by a rewriter failure."""
    mock_litellm.side_effect = RuntimeError("API down")
    out = await rewrite_query("test", n=3)
    assert out == ["test"]


@pytest.mark.unit
async def test_rewrite_malformed_json_falls_back(mock_litellm: AsyncMock) -> None:
    """Model returns prose instead of JSON → fall back."""
    mock_litellm.return_value = _make_response(
        "Here are some variants you might consider: A, B, and C."
    )
    out = await rewrite_query("test", n=3)
    assert out == ["test"]


@pytest.mark.unit
async def test_rewrite_wrong_schema_falls_back(mock_litellm: AsyncMock) -> None:
    """Valid JSON but missing the ``variants`` key → fall back."""
    mock_litellm.return_value = _make_response('{"options": ["A", "B"]}')
    out = await rewrite_query("test", n=3)
    assert out == ["test"]


@pytest.mark.unit
async def test_rewrite_empty_content_falls_back(mock_litellm: AsyncMock) -> None:
    """Model returns empty string → fall back."""
    mock_litellm.return_value = _make_response("")
    out = await rewrite_query("test", n=3)
    assert out == ["test"]


@pytest.mark.unit
async def test_rewrite_variants_with_non_strings_skipped(
    mock_litellm: AsyncMock,
) -> None:
    """Defensive: ``variants`` contains non-string entries (model
    hallucination). We filter to strings only."""
    mock_litellm.return_value = _make_response('{"variants": ["valid", 42, null, "also valid"]}')
    out = await rewrite_query("test", n=4)
    assert out == ["test", "valid", "also valid"]


# ---------------------------------------------------------------------------
# Model selection + API key passthrough
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rewrite_uses_default_model(mock_litellm: AsyncMock) -> None:
    mock_litellm.return_value = _make_response('{"variants": []}')
    await rewrite_query("test", n=1)
    assert mock_litellm.call_args.kwargs["model"] == DEFAULT_REWRITER_MODEL


@pytest.mark.unit
async def test_rewrite_respects_custom_model_and_api_key(
    mock_litellm: AsyncMock,
) -> None:
    mock_litellm.return_value = _make_response('{"variants": []}')
    await rewrite_query(
        "test",
        n=1,
        model="openai/gpt-4o-mini",
        api_key="sk-test",
    )
    kwargs = mock_litellm.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["api_key"] == "sk-test"


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_content_handles_missing_fields() -> None:
    """Defensive: a response object missing ``choices`` /
    ``message`` / ``content`` returns ``""`` rather than raising."""
    assert _extract_content(object()) == ""
    assert _extract_content(SimpleNamespace(choices=[])) == ""
    assert _extract_content(SimpleNamespace(choices=[SimpleNamespace()])) == ""


@pytest.mark.unit
def test_parse_variants_extracts_embedded_json() -> None:
    """A response with surrounding prose still parses via the
    ``{...}`` regex fallback."""
    raw = 'Here you go: {"variants": ["A", "B"]} hope this helps.'
    assert _parse_variants(raw) == ["A", "B"]


@pytest.mark.unit
def test_parse_variants_strips_empty_strings() -> None:
    """Whitespace-only / empty variants are dropped before return."""
    raw = '{"variants": ["valid", "", "   ", "also valid"]}'
    assert _parse_variants(raw) == ["valid", "also valid"]


@pytest.mark.unit
def test_parse_variants_returns_empty_on_garbage() -> None:
    assert _parse_variants("not json at all") == []
    assert _parse_variants("") == []
    assert _parse_variants("[]") == []  # array, not the expected dict


# ---------------------------------------------------------------------------
# search() integration — rewriter fans out, results dedup via RRF
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_with_rewrite_fans_out_and_fuses(
    mock_litellm: AsyncMock,
) -> None:
    """``search(rewrite_variants=2)`` calls the rewriter, runs
    retrieval per variant, and RRF-fuses the result lists.

    Constructs a corpus where one chunk is the best match for the
    ORIGINAL phrasing and a DIFFERENT chunk is the best match for
    a paraphrase variant. The fused result must contain both.
    """
    from unittest import mock as um  # noqa: PLC0415

    from movate.core.models import KbChunk  # noqa: PLC0415
    from movate.kb.search import search as kb_search  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    storage = InMemoryStorage()
    # Two chunks, both lexically present but with very different
    # embeddings so the query for each variant ranks them differently.
    chunks = [
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="Annual subscriptions get a 14-day refund window.",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="refund_doc",
        ),
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/b.md",
            text="Cancellation prevents the next billing cycle.",
            embedding=[0.0, 1.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="cancel_doc",
        ),
    ]
    for c in chunks:
        await storage.save_kb_chunk(c)

    # Rewriter returns one variant — "cancel" — so retrieval runs
    # once for the original and once for the variant.
    mock_litellm.return_value = _make_response('{"variants": ["how do I cancel?"]}')

    # Embedder returns the embedding that exactly matches whichever
    # chunk's text corresponds to the query — so each variant's
    # retrieval pass returns a DIFFERENT chunk at rank #1.
    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        q = texts[0].lower()
        if "refund" in q:
            return [[1.0, 0.0]]
        if "cancel" in q:
            return [[0.0, 1.0]]
        return [[0.5, 0.5]]

    with um.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="annual refund timeline",
            agent="rag-qa",
            tenant_id="test",
            limit=2,
            rewrite_variants=1,
        )

    # Both chunks should surface — one from the original query, one
    # from the rewritten "cancel" variant. RRF dedup means each
    # appears once, not twice.
    texts = [r.chunk.text for r in results]
    assert any("refund" in t.lower() for t in texts)
    assert any("cancellation" in t.lower() for t in texts)
    assert len(results) == 2  # No dup row even though rewriter ran


@pytest.mark.unit
async def test_search_rewrite_zero_skips_llm(mock_litellm: AsyncMock) -> None:
    """``rewrite_variants=0`` (default) doesn't call the rewriter
    even if the param is plumbed through. Preserves cost on the
    default code path."""
    from unittest import mock as um  # noqa: PLC0415

    from movate.core.models import KbChunk  # noqa: PLC0415
    from movate.kb.search import search as kb_search  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="Some KB content.",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="x",
        )
    )

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    with um.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="anything",
            agent="rag-qa",
            tenant_id="test",
            limit=1,
            rewrite_variants=0,
        )

    assert len(results) == 1
    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_search_rewrite_failure_falls_back_to_single_query(
    mock_litellm: AsyncMock,
) -> None:
    """Rewriter exception → retrieval still runs with [original]
    only. End-to-end graceful degradation."""
    from unittest import mock as um  # noqa: PLC0415

    from movate.core.models import KbChunk  # noqa: PLC0415
    from movate.kb.search import search as kb_search  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="test",
            agent="rag-qa",
            source="/tmp/a.md",
            text="Important KB content.",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="x",
        )
    )

    # Rewriter blows up → search should still produce the result
    # from the original query.
    mock_litellm.side_effect = RuntimeError("LLM unavailable")

    async def fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    with um.patch("movate.kb.search.embed_texts", side_effect=fake_embed):
        results = await kb_search(
            storage=storage,
            question="anything",
            agent="rag-qa",
            tenant_id="test",
            limit=1,
            rewrite_variants=3,
        )

    assert len(results) == 1
    assert "Important" in results[0].chunk.text
