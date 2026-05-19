"""Query rewriter — expand one question into N retrieval variants.

The retrieval pipeline (vector + BM25 + RRF) finds chunks that match
the *exact* phrasing of the query. Vague or under-specified questions
("refunds?", "how do I cancel?") miss chunks that use precise
terminology ("refund window", "subscription cancellation"). The
rewriter closes that gap by asking a small LLM for N paraphrases of
the original question, then fanning out retrieval across all N+1
variants and deduping the results by chunk_id.

Trade-offs (v0.9 MVP):

* **Small fast model**, not the agent's primary model. The rewrite
  prompt is short (~150 tokens out) and called once per search; using
  ``claude-haiku`` or ``gpt-4o-mini`` keeps the added latency under
  300ms and the cost under $0.0001 per query. Operators can override
  via the function signature.
* **No multi-step "chain of thought"** — the prompt asks for N
  variants in one shot and we parse the JSON. Cheaper than a
  thought-then-revise loop with marginal quality difference at this
  scale.
* **Strict JSON output** — the prompt asks for ``{"variants": [...]}``
  and we parse it. Failure modes (model returns prose, malformed
  JSON, fewer than N variants) all fall back to ``[question]`` —
  the rewriter never blocks retrieval, just degrades to single-query
  behavior.
* **No provider abstraction layer** — direct ``litellm.acompletion``
  call. The full ``LiteLLMProvider`` retry / fallback / cost-accounting
  machinery is overkill for a one-shot rewrite; if the call fails
  for any reason we degrade gracefully.

Used by:

* ``mdk kb search --rewrite N`` (CLI) — operator-driven exploration.
* ``movate.kb.search.search(..., rewrite_variants=N)`` (programmatic)
  — invoked from the ``kb-vector-lookup`` skill at agent run time
  once the operator opts in.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


# Default rewriter model. Claude Haiku 4.5 — fast (~200ms), cheap
# (~$0.0001/query), and good enough at the simple paraphrase task.
# Operators can override via the ``model=`` kwarg if they have a
# different cheap model preference (gpt-4o-mini, gemini-flash, etc.).
DEFAULT_REWRITER_MODEL = "anthropic/claude-haiku-4-5-20251001"

# Prompt template. Critical design choices:
#
# * **JSON-only output** — the schema is dead simple so any model
#   above the gpt-3.5 floor follows it reliably. We parse with
#   ``json.loads`` and have a tolerant regex fallback for the
#   "model wraps JSON in markdown fences" case.
# * **Explicit variant count in the prompt** — models follow
#   "give exactly N variants" better than "give a few variants."
# * **No examples in the prompt** — examples bias the rewrites
#   toward the example domain. The naked instruction generalizes
#   better across query types (factual, how-to, troubleshooting).
# * **Variant style guidance** — "rephrase using DIFFERENT wording"
#   prevents the model from returning N near-identical variants
#   (a common failure mode without this hint).
_REWRITE_PROMPT = """\
You are a query expansion assistant for a knowledge-base retrieval system.

Given an original question, generate exactly {n} alternative phrasings.
Each alternative MUST:
- Preserve the original intent.
- Use DIFFERENT wording (synonyms, paraphrases, expanded terminology).
- Be a complete question or noun-phrase suitable for retrieval.

Respond with ONLY a JSON object in this exact shape:
{{"variants": ["...", "...", "..."]}}

Do not add commentary, explanations, or markdown formatting.

Original question: {question}
"""

# Maximum variants we'll ever ask for. The retrieval fan-out
# multiplies latency + cost linearly, and the marginal quality
# improvement plateaus around 3-4 variants for most query types.
# Hard-cap so operators don't accidentally type ``--rewrite 50``.
MAX_VARIANTS = 8

# Tolerant JSON extractor — finds the first ``{...}`` block in the
# raw response. Handles the "model wraps output in ```json``` fences"
# case without needing a markdown parser. Greedy match to handle
# nested braces (rare but possible).
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


async def rewrite_query(
    question: str,
    *,
    n: int = 3,
    model: str = DEFAULT_REWRITER_MODEL,
    api_key: str | None = None,
    timeout_s: float = 10.0,
) -> list[str]:
    """Return N+1 retrieval variants (original + N paraphrases).

    Args:
        question: The original user question. Empty / whitespace-only
            returns an empty list (caller handles).
        n: How many alternative variants to generate. Clamped to
            ``[0, MAX_VARIANTS]``. ``n=0`` short-circuits and returns
            ``[question]`` with no LLM call.
        model: The LiteLLM-format model identifier
            (``provider/model-id``). Defaults to
            :data:`DEFAULT_REWRITER_MODEL`.
        api_key: Override the API key (otherwise read from env vars
            via LiteLLM's standard resolution).
        timeout_s: Per-call timeout. Generous default (10s) because
            the rewriter is one-shot; the agent's primary call is on
            a separate timeout budget.

    Returns:
        A list of strings, original first, then up to ``n`` variants.
        On any failure (LLM error, malformed response, network),
        returns ``[question]`` and logs a warning. Never raises.
    """
    if not question.strip():
        return []
    if n <= 0:
        return [question]
    n_clamped = min(int(n), MAX_VARIANTS)

    try:
        # Lazy import — LiteLLM is heavy to import and many callers
        # of the kb module never need the rewriter. Keep the cost
        # on the rewriter path only.
        import litellm  # noqa: PLC0415

        prompt = _REWRITE_PROMPT.format(n=n_clamped, question=question.strip())
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "num_retries": 0,
            "timeout": timeout_s,
            # Cap output tokens — N variants of a short question
            # rarely exceed 200 tokens. The cap protects us from
            # a runaway response.
            "max_tokens": 400,
            # Slight temperature — we want some lexical diversity
            # across variants, not deterministic restatements.
            "temperature": 0.4,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        resp = await litellm.acompletion(**kwargs)
    except Exception as exc:
        logger.warning("query rewriter failed: %s; falling back to original-only", exc)
        return [question]

    content = _extract_content(resp)
    if not content:
        logger.warning("query rewriter returned empty content; falling back")
        return [question]

    variants = _parse_variants(content)
    if not variants:
        logger.warning(
            "query rewriter returned unparseable content: %s; falling back", content[:200]
        )
        return [question]

    # Dedup + preserve order. Strip the original from the variants
    # if the model happened to include it; we add it back at position
    # 0 unconditionally so downstream retrieval always has the
    # original phrasing.
    seen: set[str] = {question.strip().lower()}
    out: list[str] = [question]
    for variant in variants:
        normalized = variant.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
        if len(out) >= n_clamped + 1:
            break
    return out


def _extract_content(resp: object) -> str:
    """Pull the text content out of a LiteLLM response.

    LiteLLM normalizes to OpenAI's response shape:
    ``resp.choices[0].message.content``. Defensive about missing
    fields — any structural surprise returns ``""`` so the caller
    triggers the fallback.
    """
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


def _parse_variants(content: str) -> list[str]:
    """Parse the LLM's response into a list of variant strings.

    Tries plain ``json.loads`` first (the prompt asks for JSON-only
    output). Falls back to extracting the first ``{...}`` block
    (handles markdown-fence-wrapped responses). Returns ``[]`` if
    neither path produces a list of strings under the ``variants`` key.
    """
    stripped = content.strip()
    # Strip common markdown wrappers — some models add ```json``` or
    # ``` regardless of the "no markdown" instruction.
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()

    # Try direct parse.
    parsed = _try_json(stripped)
    if parsed is None:
        # Fallback — find the first JSON object in the content.
        match = _JSON_BLOCK_RE.search(stripped)
        if match:
            parsed = _try_json(match.group(0))
    if parsed is None:
        return []

    variants = parsed.get("variants") if isinstance(parsed, dict) else None
    if not isinstance(variants, list):
        return []
    return [v for v in variants if isinstance(v, str) and v.strip()]


def _try_json(text: str) -> dict[str, object] | None:
    """``json.loads`` wrapper that returns ``None`` on failure."""
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None
