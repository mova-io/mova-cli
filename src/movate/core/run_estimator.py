"""Pre-flight cost + latency estimate for an agent run (Cost Prediction).

``estimate_run`` answers "what will THIS run cost + how long will it take?"
*before* committing the run — no LLM call, no job enqueued, no charge. The
estimate is honest because it reflects this agent's **real** assembled prompt
and this agent's **real** historical runs, not a generic guess:

* **tokens_in** — assembles the prompt exactly the way
  :meth:`movate.core.executor.Executor.execute` does (``bundle.render_prompt``,
  ADR 002 contexts prepended) and counts tokens with the same tokenizer the
  runtime uses (chars/4 fallback, ``tiktoken`` when it's importable — it ships
  transitively, no new dep). For RAG agents (ADR 023 auto-retrieval) the
  caller can opt in to running retrieval so retrieved-chunk tokens are folded
  in; this is the ONLY operation that may spend money (an embedding for the
  retrieval query), and it is OFF by default — see ``estimate_retrieval``.

* **tokens_out_expected** — the mean output tokens across this agent's
  historical :class:`movate.core.models.RunRecord` rows (``historical_mean``),
  falling back to the agent's ``model.params.max_tokens`` /
  :data:`_DEFAULT_OUTPUT_TOKENS` when there's no history
  (``max_tokens_fallback``).

* **cost** — the packaged pricing table (the SAME
  :func:`movate.providers.pricing.load_pricing` table ``mdk pricing`` and the
  executor's cost-retention path use), applied to three token bands:
  ``min`` (tokens_in only — cache hit / refusal), ``expected`` (tokens_in +
  expected out), ``max`` (tokens_in + max_tokens out).

* **latency** — p50/p95 of this agent's historical run durations
  (``historical_p50p95``); ``unavailable`` when there's no history.

* **budget_check** — compares ``cost_usd_max`` against the agent's
  ``budget.max_cost_usd_per_run`` (always present, default 1.0).

The whole module is pure + backend-agnostic: it touches only the
:class:`movate.storage.base.StorageProvider` Protocol (``list_runs``) and the
loaded :class:`movate.core.loader.AgentBundle` — never a concrete backend.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from movate.core.cost_forecast import _CHARS_PER_TOKEN, _output_token_budget
from movate.core.models import JobStatus

if TYPE_CHECKING:
    from movate.core.executor import Executor
    from movate.core.loader import AgentBundle
    from movate.storage.base import StorageProvider


# How many historical RunRecords to fetch for the mean-output / latency
# stats. A larger window smooths noise; 200 is enough to be representative
# for an active agent without a heavy storage scan. The storage layer caps
# its own limit, so this is an upper bound, not a guarantee.
_HISTORY_LIMIT = 200


def _count_tokens(text: str) -> int:
    """Estimate the token count of ``text``.

    Prefers ``tiktoken`` (the tokenizer the OpenAI/LiteLLM stack uses at
    runtime — it ships transitively, so importing it adds no dependency)
    and falls back to the codebase's established ``chars / 4`` heuristic
    (see :mod:`movate.core.cost_forecast`) when it can't be imported or
    an encoding can't be resolved. Never raises — a token *estimate* must
    not be able to crash the caller.
    """
    if not text:
        return 0
    try:
        import tiktoken  # noqa: PLC0415

        # cl100k_base is the GPT-4 / GPT-4o family encoding and the
        # closest single tokenizer we can name without a model lookup
        # (the estimate spans providers). Anthropic's BPE is within a
        # few percent for English prose, so this is an honest cross-
        # provider approximation — far tighter than chars/4.
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return int(len(text) / _CHARS_PER_TOKEN)


def _tokenizer_method() -> str:
    """Name the tokenizer that :func:`_count_tokens` will actually use, so
    the estimate's ``basis`` is honest about its own precision."""
    try:
        import tiktoken  # noqa: F401, PLC0415

        return "assembled+tiktoken"
    except Exception:
        return "assembled+chars-per-4"


@dataclass(frozen=True)
class RunEstimatePrediction:
    """The numeric prediction. All token counts are estimates; costs are
    the pricing-table math applied to those estimates."""

    tokens_in: int
    tokens_out_max: int
    tokens_out_expected: int
    cost_usd_min: float
    cost_usd_expected: float
    cost_usd_max: float
    latency_ms_p50: int | None = None
    latency_ms_p95: int | None = None


@dataclass(frozen=True)
class RunEstimateBasis:
    """How each field was derived — surfaced so the caller can tell a
    history-informed estimate from a cold-start fallback."""

    prompt_tokens_method: str
    out_expected_method: str  # "historical_mean" | "max_tokens_fallback"
    latency_method: str  # "historical_p50p95" | "unavailable"
    sample_size: int


@dataclass(frozen=True)
class RunEstimateBudgetCheck:
    """Agent per-run budget comparison (the agent's
    ``budget.max_cost_usd_per_run``). Always present — the field defaults
    to 1.0, so every agent has a per-run budget."""

    within_per_run_budget: bool
    per_run_budget_usd: float


@dataclass(frozen=True)
class RunEstimate:
    """A complete pre-flight estimate for one agent + one input.

    ``retrieval_embedded`` records whether the estimate actually ran
    retrieval (the only money-spending step) so the caller can be honest
    about whether the "estimate" cost a fraction of a cent.
    """

    agent_name: str
    model: str
    predicted: RunEstimatePrediction
    basis: RunEstimateBasis
    budget_check: RunEstimateBudgetCheck
    retrieval_embedded: bool = False
    notes: list[str] = field(default_factory=list)


async def estimate_run(
    bundle: AgentBundle,
    input_data: dict[str, Any],
    *,
    storage: StorageProvider,
    tenant_id: str = "local",
    executor: Executor | None = None,
    estimate_retrieval: bool = False,
) -> RunEstimate:
    """Estimate the cost + latency of running ``bundle`` against ``input_data``.

    Does **NOT** call the LLM, enqueue a job, persist a run, or charge.
    The only operation that can spend money is retrieval embedding for a
    RAG agent, and it happens ONLY when ``estimate_retrieval=True`` AND the
    agent has ADR 023 auto-retrieval configured (``retrieval.auto_into``).

    Args:
        bundle: the loaded, fully-resolved agent.
        input_data: the same ``input`` dict a real run would receive.
        storage: the StorageProvider — read-only here (``list_runs`` for
            historical stats). Never written.
        tenant_id: scopes the historical-run query so the estimate reflects
            THIS tenant's history (and never leaks another tenant's runs).
        executor: an Executor to reuse for retrieval assembly when
            ``estimate_retrieval`` is on. Optional — when omitted and
            retrieval is requested, retrieval is skipped (with a note)
            rather than the estimator building its own provider stack.
        estimate_retrieval: opt in to running the agent's retrieval so
            retrieved-chunk tokens are folded into ``tokens_in``. Default
            ``False`` keeps the estimate zero-LLM-cost. Has no effect on
            non-RAG agents.

    Returns:
        a :class:`RunEstimate` — see the module docstring for per-field
        derivation.
    """
    from movate.providers.pricing import load_pricing  # noqa: PLC0415

    spec = bundle.spec
    pricing = load_pricing()
    notes: list[str] = []

    # 1. Assemble the prompt. Optionally run retrieval first so the
    #    rendered prompt includes retrieved-chunk text — exactly what the
    #    executor does (pre-retrieve mutates input[auto_into], THEN render).
    retrieval_embedded = False
    effective_input = dict(input_data)  # never mutate the caller's dict
    if estimate_retrieval and spec.retrieval.auto_retrieval_enabled:
        if executor is not None:
            retrieval_embedded = await _populate_retrieval(
                executor, bundle, effective_input, tenant_id=tenant_id, notes=notes
            )
        else:
            notes.append(
                "estimate_retrieval requested but no executor supplied; "
                "retrieved-chunk tokens are NOT included in tokens_in"
            )
    elif spec.retrieval.auto_retrieval_enabled:
        notes.append(
            "agent uses auto-retrieval; tokens_in excludes retrieved chunks "
            "(pass estimate_retrieval=true to include them — small embedding cost)"
        )

    # Render via the EXACT executor path (ADR 002 contexts prepended).
    # render_prompt can raise on a missing template var; surface that as a
    # zero-token estimate with a note rather than crashing the caller.
    try:
        rendered = bundle.render_prompt(effective_input)
    except Exception as exc:  # noqa: BLE001 — best-effort estimate
        notes.append(f"prompt render failed ({type(exc).__name__}); tokens_in is 0")
        rendered = ""
    tokens_in = _count_tokens(rendered)

    # 2. Output bands. max comes from max_tokens (or the shared default);
    #    expected comes from historical mean, else falls back to max.
    tokens_out_max = _output_token_budget(bundle)
    history = await storage.list_runs(
        agent=spec.name, tenant_id=tenant_id, status=JobStatus.SUCCESS.value, limit=_HISTORY_LIMIT
    )
    out_tokens = [r.metrics.tokens.output for r in history if r.metrics.tokens.output > 0]
    if out_tokens:
        tokens_out_expected = int(round(statistics.fmean(out_tokens)))
        out_expected_method = "historical_mean"
    else:
        tokens_out_expected = tokens_out_max
        out_expected_method = "max_tokens_fallback"

    # 3. Cost bands via the pricing table. When the model isn't in the
    #    table (custom provider) costs are 0.0 with a note (token counts
    #    are still accurate).
    cost_min, cost_exp, cost_max = _cost_bands(
        pricing=pricing,
        provider=spec.model.provider,
        tokens_in=tokens_in,
        tokens_out_expected=tokens_out_expected,
        tokens_out_max=tokens_out_max,
        notes=notes,
    )

    # 4. Latency p50/p95 from historical durations.
    latencies = [r.metrics.latency_ms for r in history if r.metrics.latency_ms > 0]
    if latencies:
        p50: int | None = _percentile(latencies, 50)
        p95: int | None = _percentile(latencies, 95)
        latency_method = "historical_p50p95"
    else:
        p50 = None
        p95 = None
        latency_method = "unavailable"

    # 5. Budget check — agent per-run ceiling (always present, default 1.0).
    per_run_budget = float(spec.budget.max_cost_usd_per_run)
    budget_check = RunEstimateBudgetCheck(
        within_per_run_budget=cost_max <= per_run_budget,
        per_run_budget_usd=per_run_budget,
    )

    return RunEstimate(
        agent_name=spec.name,
        model=spec.model.provider,
        predicted=RunEstimatePrediction(
            tokens_in=tokens_in,
            tokens_out_max=tokens_out_max,
            tokens_out_expected=tokens_out_expected,
            cost_usd_min=cost_min,
            cost_usd_expected=cost_exp,
            cost_usd_max=cost_max,
            latency_ms_p50=p50,
            latency_ms_p95=p95,
        ),
        basis=RunEstimateBasis(
            prompt_tokens_method=_tokenizer_method(),
            out_expected_method=out_expected_method,
            latency_method=latency_method,
            sample_size=len(history),
        ),
        budget_check=budget_check,
        retrieval_embedded=retrieval_embedded,
        notes=notes,
    )


def _cost_bands(
    *,
    pricing: Any,
    provider: str,
    tokens_in: int,
    tokens_out_expected: int,
    tokens_out_max: int,
    notes: list[str],
) -> tuple[float, float, float]:
    """Compute (min, expected, max) USD via the pricing table.

    * min — tokens_in only (a cache hit or an immediate refusal still
      pays for the prompt; output is ~0).
    * expected — tokens_in + expected output.
    * max — tokens_in + max output.

    Returns ``(0.0, 0.0, 0.0)`` with a note when the model isn't in the
    pricing table (custom provider) — the same graceful shape
    :func:`movate.core.cost_forecast.estimate_eval_cost` uses.
    """
    prices = pricing.models.get(provider)
    if prices is None:
        notes.append(
            f"model {provider!r} not in the pricing table; cost bands are 0.0 "
            f"(token counts are still accurate)"
        )
        return 0.0, 0.0, 0.0
    in_cost = tokens_in / 1000.0 * prices.input_per_1k
    cost_min = round(in_cost, 6)
    cost_exp = round(in_cost + tokens_out_expected / 1000.0 * prices.output_per_1k, 6)
    cost_max = round(in_cost + tokens_out_max / 1000.0 * prices.output_per_1k, 6)
    return cost_min, cost_exp, cost_max


def _percentile(values: list[int], pct: int) -> int:
    """Nearest-rank percentile of ``values`` (e.g. ``pct=95`` → p95).

    Nearest-rank (not interpolated) so a tiny sample gives a real observed
    duration rather than a fabricated between-points value. Rounds to an int
    millisecond — latency is reported in whole ms.
    """
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return int(ordered[min(rank, len(ordered)) - 1])


async def _populate_retrieval(
    executor: Executor,
    bundle: AgentBundle,
    effective_input: dict[str, Any],
    *,
    tenant_id: str,
    notes: list[str],
) -> bool:
    """Run the agent's ADR 023 auto-retrieval to populate
    ``effective_input[auto_into]`` so the rendered prompt counts
    retrieved-chunk tokens — reusing the executor's exact pre-retrieval
    path (no second retrieval code path).

    This may embed the retrieval query (small cost). Returns ``True`` when
    retrieval actually ran, ``False`` (with a note) when it was skipped or
    failed — a retrieval failure must never break an *estimate*.
    """
    from movate.core.models import RunRequest  # noqa: PLC0415

    cfg = bundle.spec.retrieval
    auto_into = cfg.auto_into
    if auto_into is None:  # defensive — caller already gated on enabled
        return False
    # `if_empty` (default) skips retrieval when the field is already
    # populated; mirror that so the estimate matches the real run.
    if cfg.when == "if_empty" and effective_input.get(auto_into):
        notes.append(f"retrieval skipped: {auto_into!r} already populated (when=if_empty)")
        return False

    request = RunRequest(agent=bundle.spec.name, input=effective_input)
    span = executor.tracer.start_span(
        "estimate.pre_retrieve",
        {"agent": bundle.spec.name, "estimate": True},
    )
    try:
        await executor._maybe_pre_retrieve(  # noqa: SLF001 — intentional reuse of the run path
            bundle=bundle,
            request=request,
            span=span,
            run_id="estimate",
            tenant_id=tenant_id,
            skill_calls=[],
        )
        # _maybe_pre_retrieve mutates request.input in place.
        effective_input.clear()
        effective_input.update(request.input)
        return True
    except Exception as exc:  # noqa: BLE001 — estimate must not crash on retrieval failure
        notes.append(
            f"retrieval failed during estimate ({type(exc).__name__}); "
            f"retrieved-chunk tokens excluded from tokens_in"
        )
        return False
    finally:
        executor.tracer.end_span(span)


__all__ = [
    "RunEstimate",
    "RunEstimateBasis",
    "RunEstimateBudgetCheck",
    "RunEstimatePrediction",
    "estimate_run",
]
