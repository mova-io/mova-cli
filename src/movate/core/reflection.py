"""Self-critique / judge-in-the-loop reflection (Phase J-1).

When an agent's :class:`ReflectionConfig` has ``enabled: true``, the
executor invokes this module after the primary completion lands.

Flow:

1. Build a judge prompt: rubric + agent's output + JSON-only instruction.
2. Call the judge model (different provider family from the agent).
3. Parse a structured ``{verdict: "accept" | "revise", feedback: "..."}``.
4. On ``accept`` — return the original output unchanged.
5. On ``revise`` — return the judge's feedback so the executor can
   re-prompt the agent with the feedback as a correction directive.

The executor owns the re-prompt loop bound by ``max_iterations`` —
this module is purely the judge-call helper, kept side-effect-free
so it's trivially testable with :class:`MockProvider`.

Cost / tracing:
* Each judge call surfaces as a separate tracer span under
  ``agent.execute`` so operators can see reflection overhead in Langfuse.
* Judge tokens contribute to the run's total cost via the same
  pricing-table lookup the agent's primary call uses.
* The agent's :class:`Budget.max_cost_usd_per_run` ceiling still
  applies — a reflection loop can't blow the budget.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    Message,
)

if TYPE_CHECKING:
    from movate.core.models import ReflectionConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeVerdict:
    """Structured outcome of one judge call.

    ``verdict`` drives the executor's re-prompt decision:
    * ``"accept"`` — output passes the rubric; return as-is.
    * ``"revise"`` — output fails; ``feedback`` carries the specific
      correction directive the executor folds into a re-prompt.
    * ``"parse_error"`` — judge returned a malformed response. Treated
      as a soft accept (return original output + log a warning) rather
      than blocking the run; a flaky judge shouldn't fail the whole
      pipeline. Tracked separately from ``accept`` for telemetry.

    ``tokens_in`` / ``tokens_out`` / ``cost_usd`` are surfaced so the
    executor can fold the judge's cost into the run's total. Tokens
    default to 0 on parse error (the call still happened; we just
    don't trust the response).
    """

    verdict: Literal["accept", "revise", "parse_error"]
    feedback: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    raw_response: str = ""


JUDGE_PROMPT_TEMPLATE = """You are an impartial judge evaluating another model's output.

# Rubric

{rubric}

# Output to evaluate

{output}

# Your task

Evaluate the output against the rubric. Respond with ONE JSON object on a single line:

{{"verdict": "accept", "feedback": ""}}

— OR —

{{"verdict": "revise", "feedback": "<one sentence describing the specific correction needed>"}}

Rules:
- Use `accept` ONLY if the output fully satisfies the rubric.
- Use `revise` when the output violates one or more rubric criteria. \
The `feedback` field MUST name the specific violation in a single sentence; \
the agent will re-prompt with this as a correction directive.
- Output ONLY the JSON object. No markdown fences, no prose, no explanation outside the JSON.
"""


async def call_judge(
    *,
    config: ReflectionConfig,
    output_text: str,
    judge_provider: BaseLLMProvider,
    pricing_lookup: object,
) -> JudgeVerdict:
    """Invoke the judge once against ``output_text``.

    Pure async helper — no executor state required. The caller passes
    in the judge's provider (already resolved from
    :class:`ProviderRegistry` by family) and a pricing-lookup callable
    so this module doesn't have to know about the pricing table's
    shape.

    Returns a :class:`JudgeVerdict`. Never raises on judge mis-behavior
    — a malformed response surfaces as ``verdict="parse_error"`` so
    the executor can decide whether to soft-accept (default) or treat
    as a hard failure.
    """
    judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
        rubric=config.rubric.strip(),
        output=output_text,
    )

    request = CompletionRequest(
        provider=config.judge_model,
        messages=[Message(role="user", content=judge_prompt)],
        params={
            # Determinism for grading — a judge that flips between
            # accept and revise on identical input is useless.
            "temperature": 0.0,
            # Tight cap. The judge's response is one JSON line, ~50
            # tokens. Anything larger is the judge ignoring instructions.
            "max_tokens": 256,
        },
    )

    response = await judge_provider.complete(request)
    raw = response.text.strip()

    # Pricing: ask the lookup for the cost of this judge call.
    # ``compute_cost`` is the canonical surface on PricingTable.
    cost_usd = 0.0
    try:
        cost_usd = pricing_lookup.cost_for(  # type: ignore[attr-defined]
            provider=config.judge_model,
            tokens=response.tokens,
        )
    except Exception as exc:
        log.warning(
            "reflection: failed to compute judge cost for %s: %s",
            config.judge_model,
            exc,
        )

    verdict, feedback = _parse_verdict(raw)
    return JudgeVerdict(
        verdict=verdict,
        feedback=feedback,
        tokens_in=response.tokens.input,
        tokens_out=response.tokens.output,
        cost_usd=cost_usd,
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_verdict(raw: str) -> tuple[Literal["accept", "revise", "parse_error"], str]:
    """Extract verdict + feedback from the judge's raw response.

    Permissive parser:
    * Strips markdown code fences if the judge emitted them despite
      the instruction.
    * Accepts trailing whitespace / newlines.
    * Falls back to ``parse_error`` if the response isn't valid JSON
      or is missing the ``verdict`` key — we don't want a flaky judge
      to block the run.
    """
    cleaned = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if the judge added them.
    if cleaned.startswith("```"):
        # Drop first line (``` or ```json) and the trailing ```
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("reflection: judge returned non-JSON response: %r", raw[:200])
        return ("parse_error", "")

    if not isinstance(obj, dict):
        log.warning("reflection: judge response is not a JSON object: %r", raw[:200])
        return ("parse_error", "")

    verdict = obj.get("verdict")
    feedback = str(obj.get("feedback") or "")
    if verdict in {"accept", "revise"}:
        return (verdict, feedback)
    log.warning("reflection: judge returned unknown verdict %r", verdict)
    return ("parse_error", "")


def build_revision_prompt(original_user_message: str, feedback: str) -> str:
    """Build the corrected re-prompt for the agent.

    On a ``revise`` verdict, the executor re-renders the prompt with
    the judge's feedback appended as a system-style correction
    directive. We keep the original user content intact and add a
    correction block at the END so the model sees the original task
    first, then learns what it got wrong.

    Returned text is what the executor sends as the next user message
    in the conversation history. The agent's ``prompt.md`` template is
    NOT re-rendered — the correction is purely a feedback turn.
    """
    return (
        f"{original_user_message}\n\n"
        "# Correction required\n\n"
        f"Your previous response was rejected: {feedback}\n\n"
        "Produce a corrected response that addresses this specific "
        "issue. Output only the corrected JSON — no apology, no prose, "
        "no explanation."
    )
