"""Model catalog — pricing + capability metadata, in one place.

The pricing table (``pricing.yaml`` via :func:`movate.providers.pricing.load_pricing`)
tracks *cost* data only. Capability metadata — context window, tool-use
support, vision support — is maintained in the ``_CAPABILITY_CATALOGUE`` dict
below, keyed by the full LiteLLM provider string so it stays in sync with the
pricing table.

This module is the **shared seam** between the control plane (the
``mdk models`` CLI command in :mod:`movate.cli.models_cmd`) and the execution
plane (the read-only ``GET /api/v1/models`` runtime endpoints). Both import
:func:`model_catalog` / :func:`model_info` from here; neither duplicates the
capability table, and the runtime never imports from ``cli``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from movate.providers.pricing import ModelPrice, PricingTable, load_pricing


@dataclass
class ModelCaps:
    """Static capability metadata for one model."""

    context_window: int
    supports_tools: bool = True
    supports_vision: bool = False
    notes: str = ""


# Default capabilities by provider prefix (used as fallback if a model ID
# isn't listed explicitly below).
_PROVIDER_DEFAULTS: dict[str, ModelCaps] = {
    "openai": ModelCaps(context_window=128_000, supports_tools=True, supports_vision=False),
    "azure": ModelCaps(context_window=128_000, supports_tools=True, supports_vision=False),
    "anthropic": ModelCaps(context_window=200_000, supports_tools=True, supports_vision=False),
}

_CAPABILITY_CATALOGUE: dict[str, ModelCaps] = {
    # OpenAI
    "openai/gpt-4o-2024-08-06": ModelCaps(
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "openai/gpt-4o-mini-2024-07-18": ModelCaps(
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "openai/o1-2024-12-17": ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
        notes="Reasoning model; extended thinking built in.",
    ),
    # Azure OpenAI
    "azure/gpt-4o-2024-08-06": ModelCaps(
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
        notes="Azure-hosted GPT-4o; slight markup vs first-party.",
    ),
    # Anthropic
    "anthropic/claude-opus-4-6": ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "anthropic/claude-sonnet-4-6": ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "anthropic/claude-haiku-4-5-20251001": ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
    ),
}


def caps_for(model_id: str) -> ModelCaps:
    """Return capability metadata for *model_id*, falling back to provider defaults."""
    if model_id in _CAPABILITY_CATALOGUE:
        return _CAPABILITY_CATALOGUE[model_id]
    provider = model_id.split("/", maxsplit=1)[0] if "/" in model_id else ""
    return _PROVIDER_DEFAULTS.get(
        provider,
        ModelCaps(context_window=0, supports_tools=False, supports_vision=False),
    )


@dataclass
class ModelInfo:
    """Combined pricing + capability view of one model.

    The canonical entity behind both ``mdk models`` and
    ``GET /api/v1/models``. Prices are stored per-1M-tokens (the unit the
    CLI table and the API both surface), derived from the per-1K values in
    the pricing table.
    """

    model_id: str
    provider: str
    context_window: int
    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float | None
    supports_tools: bool
    supports_vision: bool
    notes: str = field(default="")
    in_pricing_table: bool = field(default=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "context_window": self.context_window,
            "input_per_1m": self.input_per_1m,
            "output_per_1m": self.output_per_1m,
            "cached_input_per_1m": self.cached_input_per_1m,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "notes": self.notes,
            "in_pricing_table": self.in_pricing_table,
        }


def _to_info(model_id: str, price: ModelPrice, caps: ModelCaps) -> ModelInfo:
    provider = model_id.split("/", maxsplit=1)[0] if "/" in model_id else model_id
    return ModelInfo(
        model_id=model_id,
        provider=provider,
        context_window=caps.context_window,
        input_per_1m=price.input_per_1k * 1000,
        output_per_1m=price.output_per_1k * 1000,
        cached_input_per_1m=(
            price.cached_input_per_1k * 1000 if price.cached_input_per_1k is not None else None
        ),
        supports_tools=caps.supports_tools,
        supports_vision=caps.supports_vision,
        notes=caps.notes,
        in_pricing_table=True,
    )


def model_catalog(table: PricingTable | None = None) -> list[ModelInfo]:
    """Return every known model as a combined pricing + capability view.

    Sorted by ``(provider, model_id)`` ascending — the stable order both
    the CLI table and the API list rely on. Pass *table* to reuse an
    already-loaded :class:`PricingTable`; defaults to :func:`load_pricing`.
    """
    table_data = table or load_pricing()
    infos = [
        _to_info(model_id, price, caps_for(model_id))
        for model_id, price in table_data.models.items()
    ]
    infos.sort(key=lambda i: (i.provider, i.model_id))
    return infos


def model_info(model_id: str, table: PricingTable | None = None) -> ModelInfo | None:
    """Return the combined pricing + capability view for *model_id*.

    Returns ``None`` when the model is not in the pricing table (the
    caller decides whether that is a 404 or a CLI exit-1).
    """
    table_data = table or load_pricing()
    price = table_data.models.get(model_id)
    if price is None:
        return None
    return _to_info(model_id, price, caps_for(model_id))


__all__ = [
    "ModelCaps",
    "ModelInfo",
    "caps_for",
    "model_catalog",
    "model_info",
]
