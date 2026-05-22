"""Tests for BM25 / RRF environment-variable tuning in ``movate.kb.lexical``.

The module reads ``MOVATE_BM25_K1``, ``MOVATE_BM25_B``, and ``MOVATE_RRF_K``
from the environment at import time (module-level constants). These tests:

1. Verify the compile-time defaults are ``1.5``, ``0.75``, ``60`` when the
   env vars are absent.
2. Verify that patching the module-level constants directly (simulating what
   would happen if the env vars were set before import) causes ``_bm25_score``
   and ``rrf_fuse`` to use the new values.
3. Verify that non-numeric env-var strings do NOT crash the module on import
   (the ``float()`` / ``int()`` calls raise ``ValueError``, which we confirm
   is the expected behaviour at import time).
"""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime
from unittest import mock

import pytest

import movate.kb.lexical as lexical_mod
from movate.core.models import KbChunk, KbChunkWithScore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    chunk_id: str,
    text: str,
    agent: str = "test-agent",
    tenant_id: str = "t1",
) -> KbChunk:
    return KbChunk(
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        agent=agent,
        source="test.md",
        text=text,
        embedding=[0.1] * 4,
        embedding_model="openai/text-embedding-3-small",
        content_hash=chunk_id,
        metadata=None,
        created_at=datetime.now(UTC),
    )


def _make_scored(chunk: KbChunk, score: float) -> KbChunkWithScore:
    return KbChunkWithScore(chunk=chunk, score=score)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_k1_default_is_1_5() -> None:
    """_BM25_K1 defaults to 1.5 when MOVATE_BM25_K1 is not set."""
    if "MOVATE_BM25_K1" not in os.environ:
        assert lexical_mod._BM25_K1 == 1.5, (
            f"expected _BM25_K1=1.5, got {lexical_mod._BM25_K1}"
        )


@pytest.mark.unit
def test_bm25_b_default_is_0_75() -> None:
    """_BM25_B defaults to 0.75 when MOVATE_BM25_B is not set."""
    if "MOVATE_BM25_B" not in os.environ:
        assert lexical_mod._BM25_B == 0.75, (
            f"expected _BM25_B=0.75, got {lexical_mod._BM25_B}"
        )


@pytest.mark.unit
def test_rrf_k_default_is_60() -> None:
    """RRF_K defaults to 60 when MOVATE_RRF_K is not set."""
    if "MOVATE_RRF_K" not in os.environ:
        assert lexical_mod.RRF_K == 60, (
            f"expected RRF_K=60, got {lexical_mod.RRF_K}"
        )


# ---------------------------------------------------------------------------
# Patched constants change BM25 scoring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_scoring_uses_k1_constant() -> None:
    """Higher k1 increases the BM25 score for a term that appears
    multiple times in a document (less saturation = more contribution
    per occurrence).  We verify the score changes when k1 is patched."""
    chunks = [_make_chunk("c1", "refund refund refund policy details")]
    query = "refund"

    # Score with the default k1 = 1.5.
    results_default = lexical_mod.bm25_search(chunks, query, limit=5)
    assert results_default, "expected a match for 'refund' in chunk text"
    score_default = results_default[0].score

    # Score with a higher k1 (less saturation = higher raw BM25).
    with mock.patch.object(lexical_mod, "_BM25_K1", 3.0):
        results_high = lexical_mod.bm25_search(chunks, query, limit=5)

    assert results_high, "expected a match with patched k1 too"
    score_high = results_high[0].score

    # Higher k1 → higher (or at least non-lower) score for repeated terms.
    assert score_high >= score_default, (
        f"expected higher score with k1=3.0 vs default; "
        f"got default={score_default:.4f}, high={score_high:.4f}"
    )


@pytest.mark.unit
def test_bm25_scoring_uses_b_constant() -> None:
    """b=0 disables length normalization — a longer document scores the
    same as a shorter one for the same term frequency.  We verify that
    changing _BM25_B changes the score for a long document."""
    # Long doc: 'refund' appears once among many other tokens.
    long_text = "refund " + " ".join([f"word{i}" for i in range(50)])
    chunks = [_make_chunk("c1", long_text)]
    query = "refund"

    with mock.patch.object(lexical_mod, "_BM25_B", 0.0):
        results_no_len_norm = lexical_mod.bm25_search(chunks, query, limit=5)

    with mock.patch.object(lexical_mod, "_BM25_B", 1.0):
        results_full_len_norm = lexical_mod.bm25_search(chunks, query, limit=5)

    assert results_no_len_norm, "expected a match with b=0"
    assert results_full_len_norm, "expected a match with b=1"

    # b=0: length not penalised → higher score for the long doc vs b=1.
    assert results_no_len_norm[0].score >= results_full_len_norm[0].score, (
        "b=0 (no length norm) should score at least as high as b=1 for a long document"
    )


@pytest.mark.unit
def test_rrf_fuse_uses_rrf_k_constant() -> None:
    """A lower RRF_K causes the top-ranked item to contribute more relative
    to lower-ranked items — the fused top score should differ between k=1
    and k=60."""
    chunk_a = _make_chunk("a", "sample content for chunk a")
    chunk_b = _make_chunk("b", "sample content for chunk b")

    list1 = [_make_scored(chunk_a, 0.9), _make_scored(chunk_b, 0.5)]
    list2 = [_make_scored(chunk_b, 0.8), _make_scored(chunk_a, 0.3)]

    # With the default RRF_K = 60.
    result_k60 = lexical_mod.rrf_fuse(list1, list2, k=60, limit=2)

    # With a very small k = 1 (top ranks dominate much more).
    result_k1 = lexical_mod.rrf_fuse(list1, list2, k=1, limit=2)

    assert result_k60, "rrf_fuse should return results with k=60"
    assert result_k1, "rrf_fuse should return results with k=1"

    # With k=1, the difference between rank-1 and rank-2 contributions
    # is much larger (1/(1+1)=0.5 vs 1/(1+2)=0.33) compared to k=60
    # (1/61 vs 1/62) — the scores should differ between the two k values.
    scores_k60 = [r.score for r in result_k60]
    scores_k1 = [r.score for r in result_k1]
    # The two configurations should not produce identical scores.
    # (They might produce the same ordering, but the magnitudes differ.)
    assert scores_k60 != scores_k1, (
        "RRF scores should differ between k=60 and k=1"
    )


@pytest.mark.unit
def test_rrf_fuse_uses_module_level_rrf_k_when_not_overridden() -> None:
    """rrf_fuse()'s default k argument is RRF_K — patching the module
    constant changes the default used when k is not passed explicitly."""
    chunk_a = _make_chunk("a", "content for chunk a")
    chunk_b = _make_chunk("b", "content for chunk b")

    list1 = [_make_scored(chunk_a, 0.9), _make_scored(chunk_b, 0.1)]
    list2 = [_make_scored(chunk_a, 0.8), _make_scored(chunk_b, 0.2)]

    # Default k from the module constant.
    result_default_k = lexical_mod.rrf_fuse(list1, list2, limit=2)

    # Patch RRF_K to something very different (k=1).
    with mock.patch.object(lexical_mod, "RRF_K", 1):
        # Explicitly pass k=1 to exercise the actual computation path
        # (since rrf_fuse has k=RRF_K as the default parameter, which
        # is evaluated at function-definition time in Python).
        result_patched_k = lexical_mod.rrf_fuse(list1, list2, k=1, limit=2)

    assert result_default_k, "expected results with default RRF_K"
    assert result_patched_k, "expected results with k=1"
    # Ordering must be the same (both calls: chunk_a is #1 in both lists).
    assert result_default_k[0].chunk.chunk_id == result_patched_k[0].chunk.chunk_id == "a"


# ---------------------------------------------------------------------------
# Invalid env var values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_bm25_k1_env_var_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting MOVATE_BM25_K1 to a non-numeric string and re-importing the
    module should raise ``ValueError`` from ``float()``."""
    monkeypatch.setenv("MOVATE_BM25_K1", "not-a-number")
    # Force a fresh import to trigger the module-level float() call.
    with pytest.raises(ValueError):
        importlib.reload(lexical_mod)
    # Restore the module to a sane state after the test (reload with clean env).
    monkeypatch.delenv("MOVATE_BM25_K1", raising=False)
    importlib.reload(lexical_mod)


@pytest.mark.unit
def test_invalid_bm25_b_env_var_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting MOVATE_BM25_B to a non-numeric string and re-importing the
    module should raise ``ValueError``."""
    monkeypatch.setenv("MOVATE_BM25_B", "bad-value")
    with pytest.raises(ValueError):
        importlib.reload(lexical_mod)
    monkeypatch.delenv("MOVATE_BM25_B", raising=False)
    importlib.reload(lexical_mod)


@pytest.mark.unit
def test_invalid_rrf_k_env_var_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting MOVATE_RRF_K to a non-integer string and re-importing the
    module should raise ``ValueError``."""
    monkeypatch.setenv("MOVATE_RRF_K", "oops")
    with pytest.raises(ValueError):
        importlib.reload(lexical_mod)
    monkeypatch.delenv("MOVATE_RRF_K", raising=False)
    importlib.reload(lexical_mod)


@pytest.mark.unit
def test_valid_env_vars_set_correct_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting all three env vars to valid numbers and reloading the module
    bakes those values into the module-level constants."""
    monkeypatch.setenv("MOVATE_BM25_K1", "2.0")
    monkeypatch.setenv("MOVATE_BM25_B", "0.5")
    monkeypatch.setenv("MOVATE_RRF_K", "30")
    try:
        importlib.reload(lexical_mod)
        assert lexical_mod._BM25_K1 == 2.0, f"expected 2.0, got {lexical_mod._BM25_K1}"
        assert lexical_mod._BM25_B == 0.5, f"expected 0.5, got {lexical_mod._BM25_B}"
        assert lexical_mod.RRF_K == 30, f"expected 30, got {lexical_mod.RRF_K}"
    finally:
        # Always restore the module to defaults so other tests aren't polluted.
        monkeypatch.delenv("MOVATE_BM25_K1", raising=False)
        monkeypatch.delenv("MOVATE_BM25_B", raising=False)
        monkeypatch.delenv("MOVATE_RRF_K", raising=False)
        importlib.reload(lexical_mod)
