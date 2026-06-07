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
    from movate.providers.pricing import PricingTable

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

    ``score`` is an OPTIONAL numeric grade (0..1) the judge may emit
    alongside the categorical verdict. The in-Executor reflection loop
    ignores it (it gates on the categorical verdict only), but the
    workflow ``JUDGE`` node (ADR 056 D2) uses it for the eval-gate form:
    ``score >= pass_threshold`` ⇒ accept. ``None`` when the judge emits
    no score — the canonical absent value, never coerced to ``0.0`` (so a
    "no score" judge is not mistaken for a "scored zero" one).

    ``tokens_in`` / ``tokens_out`` / ``cost_usd`` are surfaced so the
    executor can fold the judge's cost into the run's total. Tokens
    default to 0 on parse error (the call still happened; we just
    don't trust the response).
    """

    verdict: Literal["accept", "revise", "parse_error"]
    feedback: str = ""
    score: float | None = None
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
    pricing_lookup: PricingTable,
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
        cost_usd = pricing_lookup.cost_for(
            provider=config.judge_model,
            tokens=response.tokens,
        )
    except Exception as exc:
        log.warning(
            "reflection: failed to compute judge cost for %s: %s",
            config.judge_model,
            exc,
        )

    parsed = parse_verdict(raw)
    return JudgeVerdict(
        verdict=parsed.verdict,
        feedback=parsed.feedback,
        score=parsed.score,
        tokens_in=response.tokens.input,
        tokens_out=response.tokens.output,
        cost_usd=cost_usd,
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_code_fences(raw: str) -> str:
    """Drop ```json ... ``` (or bare ``` ... ```) fences a judge may emit.

    Shared by both verdict parsers so the fence-tolerance lives in one place.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _coerce_score(value: object) -> float | None:
    """Coerce a judge-emitted ``score`` into a clamped 0..1 float, or ``None``.

    Permissive: accepts int/float/numeric-string; clamps out-of-range values
    into ``[0, 1]`` (a judge that emits ``5`` on a 0-10 mental scale still
    yields a usable upper-bound gate rather than a crash). Anything
    non-numeric (or absent) yields ``None`` — the canonical "no score".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return max(0.0, min(1.0, num))


def parse_verdict(raw: str) -> JudgeVerdict:
    """Parse a judge's raw response into a :class:`JudgeVerdict` (verdict + feedback + score).

    The canonical, permissive judge-output parser (ADR 056 D2). Both the
    in-Executor reflection loop (via :func:`call_judge`) and the workflow
    ``JUDGE`` node (``core/workflow/runner.py`` + ``temporal_activities``)
    parse judge output through here so there is exactly one verdict shape and
    one parser — no backend invents its own.

    Permissive:
    * Strips markdown code fences if the judge emitted them despite the
      JSON-only instruction.
    * Accepts trailing whitespace / newlines.
    * Reads an optional ``score`` (0..1, clamped; ``None`` when absent).
    * Falls back to ``verdict="parse_error"`` if the response isn't valid
      JSON or is missing/unknown the ``verdict`` key — a flaky judge must
      never block the run (fail-open posture).

    ``tokens_*`` / ``cost_usd`` are left at their defaults here (this parses
    raw text only); :func:`call_judge` fills them from the provider response.
    """
    cleaned = _strip_code_fences(raw)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("reflection: judge returned non-JSON response: %r", raw[:200])
        return JudgeVerdict(verdict="parse_error", raw_response=raw)

    if not isinstance(obj, dict):
        log.warning("reflection: judge response is not a JSON object: %r", raw[:200])
        return JudgeVerdict(verdict="parse_error", raw_response=raw)

    verdict = obj.get("verdict")
    feedback = str(obj.get("feedback") or "")
    score = _coerce_score(obj.get("score"))
    if verdict in {"accept", "revise"}:
        return JudgeVerdict(verdict=verdict, feedback=feedback, score=score, raw_response=raw)
    log.warning("reflection: judge returned unknown verdict %r", verdict)
    return JudgeVerdict(verdict="parse_error", feedback=feedback, score=score, raw_response=raw)


def _parse_verdict(raw: str) -> tuple[Literal["accept", "revise", "parse_error"], str]:
    """Back-compat shim: ``(verdict, feedback)`` 2-tuple (see :func:`parse_verdict`).

    Retained for the in-tree callers/tests that destructure a 2-tuple. New
    code (the JUDGE node) should call :func:`parse_verdict` to also get the
    ``score``.
    """
    parsed = parse_verdict(raw)
    return (parsed.verdict, parsed.feedback)


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
