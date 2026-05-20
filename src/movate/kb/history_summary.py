"""History summarization — smarter alternative to raw budget truncation.

PR-U added a char-budget cap on the injected conversation_history
that drops OLDEST turns first when total bytes exceed the budget.
That's the right MVP behavior — but for long threads it means the
agent simply loses context from earlier turns.

This module (PR-Z) replaces raw truncation with summarization:
when total history exceeds the budget, send the OLDEST turns to a
small LLM and collapse them into a single synthetic "earlier in
this conversation: ..." entry. The most recent turns survive
verbatim (highest-value context); the older ones get compressed
to fit.

Trade-off vs raw truncation:

* **Pro**: agent sees the GIST of every prior turn instead of
  losing earlier ones entirely. Better long-thread quality.
* **Con**: adds one extra LLM call per threaded message
  (~200ms + ~$0.0002). Skipped when total fits under budget —
  no overhead on short threads.
* **Failure mode**: LLM call fails → fall back to PR-U's raw
  truncation. Same graceful-degradation pattern as the
  rewriter / reranker / multi-hop planner.

Opt-in via :class:`movate.core.models.RetrievalConfig.history_summarize`.
Default is the v0.9-default raw truncation (back-compat).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default summarizer model. Claude Haiku 4.5 — same default as the
# other LLM-stage layers (rewriter, reranker, multi-hop planner).
# Fast (~200ms), cheap (~$0.0002/call), good enough at the
# narrative-compression task.
DEFAULT_SUMMARIZER_MODEL = "anthropic/claude-haiku-4-5-20251001"

# Summarization prompt. Design choices:
#
# * **Compact verbatim instruction** ("retain key facts, names, ids")
#   so the summary doesn't lose the high-value tokens (account ids,
#   ticket numbers, names) that the agent's next response might need
#   to cite or reason about.
# * **Q&A structure** preserved — the model's output should read as
#   "user asked X about Y, agent said Z". Keeps the conversational
#   structure recognizable to the agent's prompt template.
# * **Fixed-length output cap** in the prompt + ``max_tokens`` so
#   the summary itself doesn't blow the budget it's supposed to fit.
_SUMMARIZE_PROMPT = """\
You are a conversation-history compressor for a multi-turn AI agent.

Below are {n_turns} prior turns of an ongoing conversation. Compress \
them into a single concise summary that:

- Preserves key facts the agent might need to recall (names, ids, \
  ticket numbers, account numbers, exact phrasings of decisions).
- Maintains the question→answer structure ("user asked X, agent \
  responded Y").
- Stays under {target_chars} characters total.

Output the summary as plain text — no markdown headers, no preamble \
("Here is the summary:"), just the compressed conversation.

Prior turns:
{turns_block}
"""

# Cap on the summary output size. Larger summaries defeat the
# budget-fitting purpose; smaller summaries lose too much detail.
# 1500 chars ≈ 375 tokens — enough for a 5-10 turn compression.
_SUMMARY_TARGET_CHARS = 1500


async def summarize_older_turns(
    turns: list[dict[str, Any]],
    *,
    keep_recent: int,
    model: str = DEFAULT_SUMMARIZER_MODEL,
    api_key: str | None = None,
    timeout_s: float = 15.0,
) -> list[dict[str, Any]]:
    """Compress turns older than the most-recent ``keep_recent`` into
    a single synthetic summary turn.

    Args:
        turns: Chronologically-ordered list of
            ``{"input": ..., "output": ...}`` dicts (earliest first).
        keep_recent: How many recent turns to preserve verbatim. The
            oldest ``len(turns) - keep_recent`` turns get summarized.
        model: LiteLLM-format model identifier.
        api_key: Override the API key.
        timeout_s: Per-call timeout.

    Returns:
        A new list with shape::

            [
                {
                    "input": {"summary": True, "n_turns": N},
                    "output": {"text": "<compressed conversation>"},
                },
                # ... keep_recent most-recent turns verbatim
            ]

        On any LLM failure, returns ``turns`` unchanged so the caller
        falls back to whatever its budget enforcement does. NEVER
        raises — same contract as the other LLM stages.

    When ``keep_recent >= len(turns)`` nothing gets summarized — the
    input is returned unchanged.
    """
    keep_recent = max(keep_recent, 0)
    if keep_recent >= len(turns):
        return list(turns)

    older = turns[:-keep_recent] if keep_recent > 0 else list(turns)
    recent = turns[-keep_recent:] if keep_recent > 0 else []
    if not older:
        return list(turns)

    try:
        # Lazy import — operators on the truncation path never pay
        # for the litellm bootstrap.
        import litellm  # noqa: PLC0415

        turns_block = _format_turns_for_summary(older)
        prompt = _SUMMARIZE_PROMPT.format(
            n_turns=len(older),
            target_chars=_SUMMARY_TARGET_CHARS,
            turns_block=turns_block,
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "num_retries": 0,
            "timeout": timeout_s,
            "max_tokens": 600,
            "temperature": 0.2,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        resp = await litellm.acompletion(**kwargs)
    except Exception as exc:
        logger.warning("history summarizer failed: %s; falling back to raw turns", exc)
        return list(turns)

    summary_text = _extract_content(resp)
    if not summary_text:
        logger.warning("history summarizer returned empty content; falling back")
        return list(turns)

    summary_turn = {
        "input": {"summary": True, "n_turns": len(older)},
        "output": {"text": summary_text.strip()},
    }
    return [summary_turn, *recent]


def _format_turns_for_summary(turns: list[dict[str, Any]]) -> str:
    """Render the to-be-summarized turns as a numbered Q&A block.

    Each turn becomes ``[N] user: ... | agent: ...``. We do NOT
    truncate per-turn here — the operator's content is what the
    summarizer needs to see in full to compress it well.
    """
    lines: list[str] = []
    for i, t in enumerate(turns, start=1):
        inp = json.dumps(t.get("input") or {}, default=str)
        out = json.dumps(t.get("output") or {}, default=str)
        lines.append(f"[{i}] user: {inp} | agent: {out}")
    return "\n".join(lines)


def _extract_content(resp: object) -> str:
    """Pull text content from a LiteLLM response. Same defensive
    extraction as the rewriter / reranker / multi-hop planner."""
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
