"""Pricing layer — versioned, packaged YAML, never inferred from LiteLLM.

LiteLLM's ``ModelResponse._hidden_params['response_cost']`` is logged for
drift detection, but the executor uses *this* table as canonical. Drift
greater than 5% is logged loudly so price-table updates can't silently lag.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from movate.core.models import TokenUsage

#: Trailing date suffix on a model id — either OpenAI-style (``-2024-07-18``)
#: or Anthropic-style (``-20251001``). Used by :meth:`PricingTable.resolve_key`
#: to bridge undated aliases (``openai/gpt-4o-mini``) and concrete dated
#: snapshots (``openai/gpt-4o-mini-2024-07-18``) — both naming forms appear in
#: real agent.yaml files and provider responses, while the table keys exactly
#: one of them per model.
_DATE_SUFFIX_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_1k: float
    output_per_1k: float
    cached_input_per_1k: float | None = None
    """Cache-READ rate (~0.1x ``input_per_1k``). Applied to
    ``TokenUsage.cached_input``."""
    cache_write_per_1k: float | None = None
    """Cache-WRITE rate (~1.25x ``input_per_1k`` for the default 5-minute
    TTL). Applied to ``TokenUsage.cache_write``. When omitted, the
    write premium is derived as ``1.25 * input_per_1k`` so existing
    pricing rows need no edit to bill cache writes — see ``cost_for``."""


# Anthropic's default (5-minute TTL) cache-write multiplier. Cache reads are
# ~0.1x and already carried explicitly via ``cached_input_per_1k`` in the
# table; cache writes are ~1.25x and derived from ``input_per_1k`` when a
# row doesn't pin an explicit ``cache_write_per_1k``.
_CACHE_WRITE_MULTIPLIER = 1.25


class PricingTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    last_verified: str
    models: dict[str, ModelPrice]
    """Keyed by full LiteLLM provider string ('openai/gpt-4o-mini-2024-07-18')."""

    def resolve_key(self, provider: str) -> str | None:
        """Resolve *provider* to a pricing-table key, or ``None`` on a miss.

        Agents commonly declare the undated alias (``openai/gpt-4o-mini``)
        while the table keys the dated snapshot
        (``openai/gpt-4o-mini-2024-07-18``) — and vice versa. A naive exact
        lookup turned that naming gap into a silent ``cost_usd=0.0`` on every
        run (first surfaced as zero-cost per-node run facts in the Temporal
        certification suite). Fallback order, first hit wins:

        1. Exact key match.
        2. *provider* is undated → the latest date-suffixed table entry whose
           base (suffix stripped) equals *provider*.
        3. *provider* is date-suffixed → the undated base entry, else the
           latest date-suffixed sibling of the same base (covers a provider
           reporting a newer snapshot than the table has).
        """
        if provider in self.models:
            return provider
        dated_siblings = sorted(
            key
            for key in self.models
            if _DATE_SUFFIX_RE.search(key) and _DATE_SUFFIX_RE.sub("", key) == provider
        )
        if dated_siblings:
            return dated_siblings[-1]
        match = _DATE_SUFFIX_RE.search(provider)
        if match:
            base = provider[: match.start()]
            if base in self.models:
                return base
            dated_siblings = sorted(
                key
                for key in self.models
                if _DATE_SUFFIX_RE.search(key) and _DATE_SUFFIX_RE.sub("", key) == base
            )
            if dated_siblings:
                return dated_siblings[-1]
        return None

    def cost_for(self, *, provider: str, tokens: TokenUsage) -> float:
        # ``TokenUsage`` convention (all adapters normalize to it):
        #
        #   * ``input``        — total prompt tokens, with ``cached_input``
        #                        as a SUBSET (the OpenAI / LiteLLM shape).
        #                        The native Anthropic adapter folds cache
        #                        reads back into ``input`` so this holds
        #                        there too — see ``_tokens_from_usage``.
        #   * ``cached_input`` — cache-READ tokens (subset of ``input``),
        #                        billed at ~0.1x instead of full rate.
        #   * ``cache_write``  — cache-WRITE tokens, a SEPARATE billable
        #                        bucket (not part of ``input``), billed at
        #                        the ~1.25x write premium. Always 0 for
        #                        non-caching / non-Anthropic paths.
        #
        # Cost = (input - cached) at full rate + cached at read rate +
        #        writes at write rate + output.
        key = self.resolve_key(provider)
        if key is None:
            raise KeyError(
                f"no pricing entry for provider {provider!r} "
                f"(no exact or date-suffix-normalized match in table {self.version})"
            )
        prices = self.models[key]
        full_price_input = max(tokens.input - tokens.cached_input, 0)
        cost = (
            full_price_input / 1000.0 * prices.input_per_1k
            + tokens.output / 1000.0 * prices.output_per_1k
        )
        if tokens.cached_input and prices.cached_input_per_1k is not None:
            cost += tokens.cached_input / 1000.0 * prices.cached_input_per_1k
        if tokens.cache_write:
            write_rate = (
                prices.cache_write_per_1k
                if prices.cache_write_per_1k is not None
                else prices.input_per_1k * _CACHE_WRITE_MULTIPLIER
            )
            cost += tokens.cache_write / 1000.0 * write_rate
        return round(cost, 6)


_PRICING_PATH = Path(__file__).parent / "pricing.yaml"


def load_pricing(path: Path | None = None) -> PricingTable:
    """Load the packaged movate pricing table."""
    p = path or _PRICING_PATH
    data = yaml.safe_load(p.read_text())
    return PricingTable.model_validate(data)
