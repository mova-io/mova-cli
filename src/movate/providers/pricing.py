"""Pricing layer — versioned, packaged YAML, never inferred from LiteLLM.

LiteLLM's ``ModelResponse._hidden_params['response_cost']`` is logged for
drift detection, but the executor uses *this* table as canonical. Drift
greater than 5% is logged loudly so price-table updates can't silently lag.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from movate.core.models import TokenUsage


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_1k: float
    output_per_1k: float
    cached_input_per_1k: float | None = None


class PricingTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    last_verified: str
    models: dict[str, ModelPrice]
    """Keyed by full LiteLLM provider string ('openai/gpt-4o-mini-2024-07-18')."""

    def cost_for(self, *, provider: str, tokens: TokenUsage) -> float:
        prices = self.models.get(provider)
        if prices is None:
            raise KeyError(f"no pricing entry for provider {provider!r}")
        cost = (
            tokens.input - tokens.cached_input
        ) / 1000.0 * prices.input_per_1k + tokens.output / 1000.0 * prices.output_per_1k
        if tokens.cached_input and prices.cached_input_per_1k is not None:
            cost += tokens.cached_input / 1000.0 * prices.cached_input_per_1k
        return round(cost, 6)


_PRICING_PATH = Path(__file__).parent / "pricing.yaml"


def load_pricing(path: Path | None = None) -> PricingTable:
    """Load the packaged movate pricing table."""
    p = path or _PRICING_PATH
    data = yaml.safe_load(p.read_text())
    return PricingTable.model_validate(data)
