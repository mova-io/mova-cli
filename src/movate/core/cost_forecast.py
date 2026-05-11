"""Cheap "what will running this eval cost?" estimate at validate time.

The forecast is intentionally conservative: it renders each case's
prompt, estimates tokens via ``chars / 4`` (the well-established
GPT-3/4 family approximation; ±20% for English), and multiplies by
the agent's model's input price. Output token cost is added using
``model.params.max_tokens`` as the upper bound, or a 500-token
default when unset.

The point isn't to be precise — provider-side cost depends on
caching, tool-call expansion, fallback hops, etc. The point is to
catch the "oh, that eval would cost $3 to run" surprise BEFORE the
engineer runs it. A user who sees "~$2.50" knows to think twice;
one who sees "~$0.01" runs without hesitation. Both reactions are
better than discovering cost from a Langfuse dashboard.

Returns ``None`` (gracefully) when any of the following hold:

* The agent has no dataset configured.
* The dataset file is missing.
* The model isn't in the pricing table (custom provider, etc.).
* The dataset is empty.

Callers (``movate validate``) print nothing when None is returned —
absence is the correct UX, not a noisy "couldn't estimate" message.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from movate.core.loader import AgentBundle
from movate.providers.pricing import PricingTable

# Char-to-token ratio. GPT family averages ~4 chars/token for English
# prose; Anthropic's tokenizer is roughly the same in practice. ±20%
# accuracy is fine for an "are we close to $0 or close to $10" signal.
_CHARS_PER_TOKEN = 4.0

# Output budget when the agent doesn't pin one. Most JSON-output agents
# produce <500 tokens; pinning higher would over-estimate cost; pinning
# lower would under-estimate the worst case. 500 is the rough middle.
_DEFAULT_OUTPUT_TOKENS = 500


@dataclass(frozen=True)
class CostForecast:
    """One forecast for one agent's eval dataset.

    All token counts are estimates (``int(chars/4)``); the cost is the
    pricing-table math applied to those estimates. Total assumes
    ``runs_per_case = 1`` — the eval default. Multiply by the
    ``--runs N`` flag at call time if needed.
    """

    model_provider: str
    cases: int
    input_tokens_per_call: int
    """Avg across all rendered cases — the prompt varies per case because
    Jinja interpolates the case's input fields."""
    output_tokens_per_call: int
    """``model.params.max_tokens`` if set, else a 500-token default."""
    cost_per_call_usd: float
    total_cost_usd: float
    """``cases * cost_per_call_usd`` for runs_per_case=1. The eval engine
    runs each case ``runs_per_case`` times for a mean; multiply by N
    in the caller if non-default."""


def estimate_eval_cost(bundle: AgentBundle, *, pricing: PricingTable) -> CostForecast | None:
    """Return a :class:`CostForecast` for ``bundle``'s eval dataset, or
    ``None`` when an estimate can't be produced (no dataset, missing
    file, no pricing entry, empty dataset).

    ``None`` is the right shape for "skip silently" — callers print
    nothing when there's nothing useful to say.
    """
    # 1. Dataset must be configured AND present.
    if not bundle.spec.evals.dataset:
        return None
    dataset_path = (bundle.agent_dir / bundle.spec.evals.dataset).resolve()
    if not dataset_path.exists():
        return None

    # 2. Model must be in the pricing table.
    model_provider = bundle.spec.model.provider
    prices = pricing.models.get(model_provider)
    if prices is None:
        return None

    # 3. Load + render each case to get a real char count. This is
    # cheap (microseconds per render) and captures Jinja interpolation
    # — important for agents where the per-case input dominates the
    # prompt length.
    rendered_chars: list[int] = []
    try:
        for raw_line in dataset_path.read_text().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            case = json.loads(line)
            if not isinstance(case, dict):
                continue
            input_data = case.get("input", {})
            if not isinstance(input_data, dict):
                continue
            try:
                rendered = bundle.render_prompt(input_data)
            except Exception:
                # A case whose input refs missing fields raises Jinja's
                # UndefinedError; we'd rather forecast nothing than
                # crash the validate command. The prompt linter
                # (UNDECLARED_INPUT_REF) is the right tool for THAT
                # diagnostic; we just skip the case here.
                continue
            rendered_chars.append(len(rendered))
    except (OSError, json.JSONDecodeError):
        return None

    if not rendered_chars:
        return None

    avg_input_tokens = int(sum(rendered_chars) / len(rendered_chars) / _CHARS_PER_TOKEN)
    output_tokens = _output_token_budget(bundle)

    cost_per_call = (
        avg_input_tokens / 1000.0 * prices.input_per_1k
        + output_tokens / 1000.0 * prices.output_per_1k
    )
    cases = len(rendered_chars)
    total = cost_per_call * cases

    return CostForecast(
        model_provider=model_provider,
        cases=cases,
        input_tokens_per_call=avg_input_tokens,
        output_tokens_per_call=output_tokens,
        # Round to 6 places to match the rest of movate's cost math
        # (``PricingTable.cost_for`` rounds to 6).
        cost_per_call_usd=round(cost_per_call, 6),
        total_cost_usd=round(total, 6),
    )


@dataclass(frozen=True)
class CallEstimate:
    """One forecast for ONE rendered prompt — the ``movate run --dry-run``
    shape. Same token math as :class:`CostForecast` but scoped to a single
    call rather than averaging across an eval dataset."""

    model_provider: str
    input_chars: int
    input_tokens: int
    output_tokens_budget: int
    cost_per_call_usd: float | None
    """``None`` when the model isn't in the pricing table (custom provider
    etc.) — the call still goes through; we just can't price it ahead of
    time."""


def estimate_call_cost(
    bundle: AgentBundle,
    *,
    rendered_chars: int,
    pricing: PricingTable,
) -> CallEstimate:
    """Forecast for a single agent call with a known rendered prompt size.

    Mirrors :func:`estimate_eval_cost`'s math but takes a pre-computed
    character count instead of re-rendering a whole dataset. Returns a
    populated :class:`CallEstimate`; the cost field is ``None`` rather
    than raising when the model isn't priced.
    """
    output_tokens = _output_token_budget(bundle)
    input_tokens = int(rendered_chars / _CHARS_PER_TOKEN)

    prices = pricing.models.get(bundle.spec.model.provider)
    cost: float | None
    if prices is None:
        cost = None
    else:
        cost = round(
            input_tokens / 1000.0 * prices.input_per_1k
            + output_tokens / 1000.0 * prices.output_per_1k,
            6,
        )

    return CallEstimate(
        model_provider=bundle.spec.model.provider,
        input_chars=rendered_chars,
        input_tokens=input_tokens,
        output_tokens_budget=output_tokens,
        cost_per_call_usd=cost,
    )


def _output_token_budget(bundle: AgentBundle) -> int:
    """``model.params.max_tokens`` if set, else a 500-token default.

    Returns an int; coerces float/str values that operators sometimes
    set in YAML. Falls through to the default on any parse failure
    rather than raising — the forecast is best-effort.
    """
    params = bundle.spec.model.params or {}
    raw = params.get("max_tokens")
    if raw is None:
        return _DEFAULT_OUTPUT_TOKENS
    try:
        # math.ceil so a fractional value rounds UP (worst-case).
        # math.ceil(float) returns an int, so no extra int() needed.
        return max(1, math.ceil(float(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_OUTPUT_TOKENS


__all__ = ["CallEstimate", "CostForecast", "estimate_call_cost", "estimate_eval_cost"]
