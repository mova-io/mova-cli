"""Multi-hop retrieval — iterative retrieve → reason → retrieve.

The single-shot retrieval pipeline (vector / hybrid / rewriter / rerank)
returns the top-K chunks for ONE query. For questions that require
chaining multiple facts ("How does the refund policy interact with
the SAML SSO tier requirement?"), one retrieval pass often misses
half the story — the chunks discussing refund policy don't mention
SSO, and vice versa.

Multi-hop closes the gap by alternating:

1. **Retrieve** chunks for the current query.
2. **Reason** — ask a small LLM: "given these chunks + the original
   question, do you have enough info to answer? If yes, return DONE.
   If no, return a refined sub-query that would fill the gap."
3. If DONE OR max_hops reached → return aggregated chunks.
   Otherwise → loop with the refined sub-query.

The aggregated result is the UNION of chunks retrieved across all
hops (deduped by chunk_id), capped at ``max_total_chunks`` so a
runaway loop can't return a 100-chunk context window.

Trade-offs (v0.9 MVP):

* **Budget-bounded**: max_hops (default 3) + max_total_chunks
  (default 15) so a bad question can't loop forever or blow up
  the agent's context window. Both are operator-tunable.
* **LLM termination, not rule-based** — heuristic "is the answer
  in the chunks?" detectors are brittle. An LLM with a tight
  prompt + structured output is the right tier.
* **Refined sub-query, not parallel fan-out**. Query rewriting
  (PR-E) fans out N paraphrases in parallel; multi-hop sequences
  N different sub-questions. They compose: each hop's retrieval
  can use the rewriter independently.
* **Graceful degradation**: any LLM failure terminates the loop
  with the chunks gathered so far. Never blocks retrieval, never
  raises.

Used by:

* ``mdk kb search --multi-hop N`` (CLI) — operator exploration.
* ``movate.kb.search.search(..., multi_hop=N)`` — programmatic
  composition with the other retrieval stages.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from movate.core.models import KbChunkWithScore

logger = logging.getLogger(__name__)


# Default termination model. Same default as the rewriter / reranker
# for consistency + low cost. The termination prompt is small (~300
# tokens in + ~50 tokens out) so this is the cheapest of the three
# LLM stages.
DEFAULT_TERMINATION_MODEL = "anthropic/claude-haiku-4-5-20251001"

# Hard cap on hops. Past ~5 hops the loop yields diminishing returns
# (the LLM either keeps asking for marginal data or starts repeating
# itself). Operators wanting deeper exploration should redesign the
# question; the cap protects them from runaway cost.
MAX_HOPS = 5

# Hard cap on total chunks across all hops. The aggregated chunks
# feed into the agent's prompt as context — 15 chunks at ~2000 chars
# each is already ~7500 tokens of context. More than that crowds out
# the agent's reasoning budget.
MAX_TOTAL_CHUNKS_CAP = 30

# Termination prompt — design choices:
#
# * **Strict DONE-or-refined-query output schema**, parsed via JSON.
#   Same pattern as the rewriter / reranker — tolerant parsing for
#   the markdown-fenced variant.
# * **Both the original question AND the current sub-query** in the
#   prompt. Without the original, the model loses track of what the
#   USER actually wants and starts refining around the sub-query.
# * **Numbered chunk preview** (truncated to 500 chars each) so the
#   model can reason about what's covered without paying for the
#   long-tail of redundant text.
# * **Explicit refined-query guidance** — "ask about a SPECIFIC missing
#   fact" — prevents the model from generating vague variations of
#   the original question (a common failure mode without this hint).
_TERMINATION_PROMPT = """\
You are a retrieval planner for a knowledge-base question-answering system.

Original question: {original_question}

Current sub-query: {current_query}

Chunks gathered so far (across all hops):
{chunks_block}

Decide ONE of the following:

A) The gathered chunks contain enough information to fully answer
   the ORIGINAL question. Return ``{{"action": "done"}}``.

B) A specific fact is still missing. Return
   ``{{"action": "refine", "query": "..."}}`` where ``query`` is
   a SPECIFIC follow-up question targeting the missing fact —
   NOT a paraphrase of the original.

Respond with ONLY the JSON object. No commentary, no markdown.
"""

# Max chars of each chunk's text we include in the termination prompt.
# 500 chars ≈ 125 tokens per chunk — enough for the planner to
# reason about coverage without paying for the long tail.
_MAX_CHUNK_CHARS_FOR_PLANNER = 500

# Tolerant JSON extractor — same pattern as the rewriter / reranker.
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


# Type alias for the retrieval function multi_hop_search calls each
# iteration. Decoupled from `movate.kb.search.search` so tests can
# inject a stub without needing storage + embedding scaffolding.
if TYPE_CHECKING:
    RetrieveFn = Callable[[str], Awaitable[list[KbChunkWithScore]]]


async def multi_hop_search(
    *,
    question: str,
    retrieve_fn: RetrieveFn,
    max_hops: int = 3,
    max_total_chunks: int = 15,
    model: str = DEFAULT_TERMINATION_MODEL,
    api_key: str | None = None,
    timeout_s: float = 15.0,
) -> list[KbChunkWithScore]:
    """Iterative retrieve → reason → retrieve loop.

    Args:
        question: The original user question. Empty / whitespace-only
            returns [].
        retrieve_fn: Async function that takes a query string and
            returns ranked KB chunks. Multi-hop calls it once per
            hop with the current sub-query. Pass a partial of
            ``movate.kb.search.search`` (storage + agent + tenant_id
            bound) — keeps the multi-hop module decoupled from the
            full search machinery.
        max_hops: Maximum number of retrieve-then-reason iterations.
            Clamped to ``[1, MAX_HOPS]``. Default 3 covers most
            two-step reasoning chains.
        max_total_chunks: Cap on the aggregated result. Chunks past
            this count are dropped. Clamped to
            ``[1, MAX_TOTAL_CHUNKS_CAP]``.
        model: LiteLLM-format model identifier for the termination
            decision. Defaults to :data:`DEFAULT_TERMINATION_MODEL`.
        api_key: Override the API key (otherwise LiteLLM env-var
            resolution).
        timeout_s: Per-call timeout for the LLM termination check.

    Returns:
        Aggregated ranked chunks from across all hops, deduped by
        ``chunk_id``, capped at ``max_total_chunks``. On any LLM
        termination failure (or if the loop is exhausted), returns
        what was gathered up to that point. Never raises.
    """
    if not question.strip():
        return []
    n_hops = max(1, min(int(max_hops), MAX_HOPS))
    n_chunks = max(1, min(int(max_total_chunks), MAX_TOTAL_CHUNKS_CAP))

    # Aggregated chunks across hops. Order = insertion order (newest
    # hop appended); the dedup-by-chunk_id keeps the FIRST occurrence
    # so earlier hops' rankings dominate.
    seen_ids: set[str] = set()
    aggregated: list[KbChunkWithScore] = []
    current_query = question

    for hop_idx in range(n_hops):
        hop_results = await retrieve_fn(current_query)

        # Aggregate + dedup. Preserve the upstream rank order
        # within each hop's results.
        for item in hop_results:
            cid = item.chunk.chunk_id
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            aggregated.append(item)
            if len(aggregated) >= n_chunks:
                # Cap reached — stop early. The termination call
                # would be wasted because we'd discard any further
                # chunks anyway.
                return aggregated[:n_chunks]

        # Final hop's results are already in the aggregate; no
        # planner call needed — we have nothing left to spend.
        if hop_idx == n_hops - 1:
            break

        # Plan the next hop. If the planner says "done" OR the call
        # fails, terminate with what we have.
        decision = await _decide_next_hop(
            original_question=question,
            current_query=current_query,
            chunks=aggregated,
            model=model,
            api_key=api_key,
            timeout_s=timeout_s,
        )
        if decision is None or decision.action == "done":
            break

        next_query = decision.refined_query
        if not next_query or next_query.strip() == current_query.strip():
            # Defensive: the planner returned the same query (no
            # progress) or an empty refinement. Stop rather than
            # loop with identical retrievals.
            break
        current_query = next_query

    return aggregated[:n_chunks]


class _Decision:
    """Planner's verdict on whether to keep going."""

    __slots__ = ("action", "refined_query")

    def __init__(self, action: str, refined_query: str = "") -> None:
        self.action = action
        self.refined_query = refined_query


async def _decide_next_hop(
    *,
    original_question: str,
    current_query: str,
    chunks: list[KbChunkWithScore],
    model: str,
    api_key: str | None,
    timeout_s: float,
) -> _Decision | None:
    """Ask the planner LLM whether to continue or stop.

    Returns:
        :class:`_Decision` with ``action="done"`` (terminate) or
        ``action="refine"`` + ``refined_query`` (continue).
        ``None`` on any LLM failure → caller terminates with
        chunks gathered so far.
    """
    try:
        # Lazy import — same rationale as the rewriter / reranker.
        import litellm  # noqa: PLC0415

        chunks_block = _format_chunks_for_planner(chunks)
        prompt = _TERMINATION_PROMPT.format(
            original_question=original_question.strip(),
            current_query=current_query.strip(),
            chunks_block=chunks_block,
        )
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "num_retries": 0,
            "timeout": timeout_s,
            # Output is a tiny JSON object — 200 tokens covers
            # even a verbose refined query.
            "max_tokens": 200,
            # Slight temperature for refined-query diversity, but
            # low enough that "done" decisions stay calibrated.
            "temperature": 0.2,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        resp = await litellm.acompletion(**kwargs)
    except Exception as exc:
        logger.warning("multi-hop planner failed: %s; terminating loop", exc)
        return None

    content = _extract_content(resp)
    if not content:
        logger.warning("multi-hop planner returned empty content; terminating loop")
        return None

    return _parse_decision(content)


def _format_chunks_for_planner(chunks: list[KbChunkWithScore]) -> str:
    """Render the aggregated chunks as a prompt-friendly block.

    Same pattern as the reranker's _format_candidates but with a
    smaller per-chunk truncation (the planner cares about coverage,
    not relevance scoring, so shorter excerpts are sufficient).
    """
    if not chunks:
        return "(none yet — this is the first hop)"
    lines: list[str] = []
    for i, item in enumerate(chunks, start=1):
        text = item.chunk.text.replace("\n", " ").strip()
        if len(text) > _MAX_CHUNK_CHARS_FOR_PLANNER:
            text = text[:_MAX_CHUNK_CHARS_FOR_PLANNER] + "..."
        lines.append(f"[{i}] {text}")
    return "\n".join(lines)


def _extract_content(resp: object) -> str:
    """Pull text content from a LiteLLM response. Defensive — any
    structural surprise returns ``""``."""
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


def _parse_decision(content: str) -> _Decision | None:
    """Parse the planner's JSON output into a :class:`_Decision`.

    Returns ``None`` on parse failure (caller terminates the loop
    with chunks gathered so far). Same tolerant-JSON pattern as
    the rewriter / reranker.
    """
    stripped = content.strip()
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
        return None

    action = parsed.get("action") if isinstance(parsed, dict) else None
    if action not in ("done", "refine"):
        return None
    if action == "done":
        return _Decision(action="done")
    # Refine path requires a non-empty query.
    query = parsed.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    return _Decision(action="refine", refined_query=query.strip())


def _try_json(text: str) -> dict[str, object] | None:
    """``json.loads`` wrapper that returns ``None`` on failure."""
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None
