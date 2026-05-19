"""LLM rerank — re-score retrieved chunks by relevance to the query.

The retrieval pipeline (vector + BM25 + RRF + query rewriting)
produces a ranked candidate set, typically 10-20 chunks. The
top-K of THAT ranking is often "noisy" — chunks with high cosine
or BM25 scores that don't actually answer the question. Reranking
solves the noise: a small LLM scores each candidate's
*semantic relevance to the query* on a 0-1 scale, then we re-sort.

Reranking is the standard third stage in production RAG pipelines
(retrieve → rerank → generate). The classic implementation is a
cross-encoder model (BAAI/bge-reranker-base via sentence-transformers
~300MB). This module uses an LLM instead — slightly slower per call
(~200ms vs ~50ms) but zero new dependencies and re-uses the LiteLLM
stack the rest of movate already trusts.

Why one batched call (not N pairwise calls):

* **Cost**: one ~500-token prompt vs N ~200-token prompts. For
  N=20 candidates, that's ~10x cheaper.
* **Latency**: one round-trip vs N parallel round-trips. The
  parallel path saves wall-time but burns N rate-limit slots.
* **Quality**: when scored together, the LLM can compare candidates
  against each other, not just against the query. Better
  relative ranking out of the box.

Trade-off: a single bad LLM response loses ALL the reranking
benefit for this query. We mitigate by degrading gracefully
(return the input order on any parse failure) — the operator
still gets the original hybrid/RRF ranking, never a worse-than-no-rerank
state.

Used by:

* ``mdk kb search --rerank`` (CLI) — operator-driven exploration.
* ``movate.kb.search.search(..., rerank=True)`` (programmatic) —
  invoked from the ``kb-vector-lookup`` skill at agent run time
  when the operator opts in.

Future: behind the same interface, swap the LLM call for a real
cross-encoder (sentence-transformers extra) when an operator
benchmarks the latency difference and needs the faster path.
"""

from __future__ import annotations

import json
import logging
import math
import re

from movate.core.models import KbChunkWithScore

logger = logging.getLogger(__name__)


# Default reranker model. Claude Haiku 4.5 — same default as the
# query rewriter for consistency + low cost / latency. The reranker
# prompt is longer than the rewriter's (it embeds all candidate
# texts) so the total token count per call is ~500-1500 tokens at
# typical K=10-20 candidates.
DEFAULT_RERANKER_MODEL = "anthropic/claude-haiku-4-5-20251001"

# Max chars of each candidate's text we feed to the reranker.
# Caps the prompt's total length so 20 candidates with 2000-char
# chunks don't blow past the model's context. 800 chars ≈ 200 tokens
# — enough to score relevance without paying for the long-tail
# of redundant text.
_MAX_CHUNK_CHARS_FOR_RERANK = 800

# Cap on number of candidates we'll ever rerank in one call. Past
# ~30 the prompt grows too big AND the model loses calibration
# across so many candidates. Operators wanting more should rerank
# in tiles (future enhancement).
MAX_RERANK_CANDIDATES = 30

# Prompt template. Design choices:
#
# * **Integer position IDs**, not chunk_ids. Keeps the prompt
#   compact + the model focused on relative ordering, not on
#   matching opaque hash strings.
# * **Scoring rubric in the prompt** so different runs / models
#   are comparable. 0-1 with anchor points (irrelevant / partial /
#   exact match).
# * **All candidates in one call** for the relative-comparison
#   benefit explained in the module docstring.
# * **JSON-only output** + tolerant parsing (same approach as
#   the query rewriter).
_RERANK_PROMPT = """\
You are a relevance scorer for a knowledge-base retrieval system.

Given a user's question and a list of candidate text chunks, score \
each chunk's relevance to the question on a 0.0-1.0 scale:

- 1.0 = the chunk directly and completely answers the question
- 0.7 = the chunk contains key facts needed to answer
- 0.4 = the chunk is on-topic but only partially relevant
- 0.1 = the chunk is loosely related but not useful
- 0.0 = the chunk is unrelated

Score each chunk INDEPENDENTLY on its own merits, not relative to
the others. Multiple chunks may have high scores; that's fine.

Question: {question}

Candidates:
{candidates}

Respond with ONLY a JSON object in this exact shape:
{{"rankings": [{{"id": 1, "score": 0.95}}, {{"id": 2, "score": 0.40}}, ...]}}

Score every candidate. Do not add commentary, explanations, or markdown.
"""

# Tolerant JSON extractor — finds the first ``{...}`` block in
# the raw response. Same pattern as the query rewriter.
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


async def llm_rerank(
    *,
    question: str,
    candidates: list[KbChunkWithScore],
    limit: int = 5,
    model: str = DEFAULT_RERANKER_MODEL,
    api_key: str | None = None,
    timeout_s: float = 15.0,
) -> list[KbChunkWithScore]:
    """Re-score ``candidates`` by LLM-judged relevance to ``question``.

    Args:
        question: The user's question that produced ``candidates``.
        candidates: Ranked KB chunks from the prior retrieval stage
            (vector / hybrid / rewriter-fused). Order is preserved
            on degraded fallback so the caller still gets the
            best-effort upstream ranking.
        limit: Top-K to return after reranking. Capped at the
            length of ``candidates``.
        model: LiteLLM-format model identifier. Defaults to
            :data:`DEFAULT_RERANKER_MODEL`.
        api_key: Override the API key (otherwise LiteLLM's standard
            env-var resolution).
        timeout_s: Per-call timeout. Reranker prompts are larger
            than the rewriter's so the default budget is wider.

    Returns:
        The top-``limit`` chunks ranked by LLM relevance score (which
        REPLACES the upstream score on the returned objects).
        On any failure (LLM error, malformed JSON, missing scores),
        returns ``candidates[:limit]`` with original scores intact.
        Never raises.
    """
    if not candidates:
        return []
    if not question.strip():
        return candidates[:limit]
    if limit <= 0:
        return []

    # Cap candidates we send to the LLM. Past MAX_RERANK_CANDIDATES
    # the prompt grows too large + the model's relative-ranking
    # calibration degrades.
    truncated = candidates[:MAX_RERANK_CANDIDATES]

    try:
        # Lazy import — same rationale as the rewriter. Keeps the
        # LiteLLM import cost off callers that never opt in.
        import litellm  # noqa: PLC0415

        candidates_block = _format_candidates(truncated)
        prompt = _RERANK_PROMPT.format(question=question.strip(), candidates=candidates_block)
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "num_retries": 0,
            "timeout": timeout_s,
            # Output budget: N rankings of ~25 tokens each + JSON
            # scaffolding. 800 tokens covers 30 candidates comfortably.
            "max_tokens": 800,
            # Low temperature — we want calibrated scoring, not
            # creative ranking. 0.0 would be ideal but some models
            # are buggy at exactly 0; 0.1 is the safe floor.
            "temperature": 0.1,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        resp = await litellm.acompletion(**kwargs)
    except Exception as exc:
        logger.warning("LLM reranker failed: %s; returning upstream order", exc)
        return candidates[:limit]

    content = _extract_content(resp)
    if not content:
        logger.warning("LLM reranker returned empty content; returning upstream order")
        return candidates[:limit]

    rankings = _parse_rankings(content, n_candidates=len(truncated))
    if not rankings:
        logger.warning(
            "LLM reranker returned unparseable content: %s; returning upstream order",
            content[:200],
        )
        return candidates[:limit]

    # Build the reranked list. Each ranking entry is (position, score)
    # where position is 1-indexed (1..len(truncated)). Replace the
    # upstream score on each chunk so downstream consumers see the
    # rerank score as the relevance signal.
    rescored: list[KbChunkWithScore] = []
    for position, score in rankings:
        if position < 1 or position > len(truncated):
            # Defensive: model returned an out-of-range id.
            continue
        original = truncated[position - 1]
        # KbChunkWithScore is frozen — build a new one with the new score.
        # Clamp to [-1, 1] for the pydantic validator (it expects
        # cosine-like values; rerank scores are 0-1 so no real clamp
        # happens in practice, but defensive).
        clamped = max(-1.0, min(1.0, float(score)))
        rescored.append(KbChunkWithScore(chunk=original.chunk, score=clamped))

    if not rescored:
        return candidates[:limit]

    # Sort by new score (descending) + take top-K. The model is
    # asked for a per-candidate score; we do the final sort here.
    rescored.sort(key=lambda x: x.score, reverse=True)
    return rescored[:limit]


def _format_candidates(candidates: list[KbChunkWithScore]) -> str:
    """Render the candidate list as a prompt-friendly text block.

    Each candidate becomes a ``[N] text...`` line. Text is truncated
    at :data:`_MAX_CHUNK_CHARS_FOR_RERANK` chars so the prompt
    doesn't balloon with long chunks. Newlines inside the chunk
    are replaced with spaces — the LLM doesn't need formatting
    nuance to score relevance, and it keeps the per-candidate
    output to a single line.
    """
    lines: list[str] = []
    for i, item in enumerate(candidates, start=1):
        text = item.chunk.text.replace("\n", " ").strip()
        if len(text) > _MAX_CHUNK_CHARS_FOR_RERANK:
            text = text[:_MAX_CHUNK_CHARS_FOR_RERANK] + "..."
        lines.append(f"[{i}] {text}")
    return "\n".join(lines)


def _extract_content(resp: object) -> str:
    """Pull the text content from a LiteLLM response. Same defensive
    extraction as the query rewriter — any structural surprise
    returns ``""`` so the caller triggers fallback."""
    try:
        choices = resp.choices  # type: ignore[attr-defined]
        first = choices[0]
        message = first.message
        content = message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    if not isinstance(content, str):
        return ""
    return content


def _parse_rankings(content: str, *, n_candidates: int) -> list[tuple[int, float]]:
    """Parse the LLM's JSON response into ``[(position, score), ...]``.

    Returns an empty list on any parse failure (caller falls back
    to upstream order). Filters out rankings whose ``id`` is out
    of range or whose ``score`` isn't a finite number.
    """
    stripped = content.strip()
    # Strip common markdown wrappers — some models add ```json```
    # despite the no-markdown instruction.
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()

    parsed = _try_json(stripped)
    if parsed is None:
        match = _JSON_BLOCK_RE.search(stripped)
        if match:
            parsed = _try_json(match.group(0))
    if parsed is None:
        return []

    rankings_raw = parsed.get("rankings") if isinstance(parsed, dict) else None
    if not isinstance(rankings_raw, list):
        return []

    out: list[tuple[int, float]] = []
    for entry in rankings_raw:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        raw_score = entry.get("score")
        if not isinstance(raw_id, int) or not isinstance(raw_score, int | float):
            continue
        if raw_id < 1 or raw_id > n_candidates:
            continue
        # NaN / inf protection — pydantic would reject them downstream
        # but we catch here for cleaner fallback semantics.
        score = float(raw_score)
        if math.isnan(score) or math.isinf(score):
            continue
        out.append((raw_id, score))
    return out


def _try_json(text: str) -> dict[str, object] | None:
    """``json.loads`` wrapper that returns ``None`` on failure."""
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None
