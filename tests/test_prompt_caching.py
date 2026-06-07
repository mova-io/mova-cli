"""Prompt-caching cost accounting + LiteLLM cache_control placement.

Covers the two halves of the prompt-caching feature that don't live in
``test_anthropic_provider.py`` (which owns the native-Anthropic breakpoint
placement):

1. :meth:`PricingTable.cost_for` applies the cache-READ discount and the
   cache-WRITE premium, and reconciles Anthropic's disjoint token counts
   with OpenAI's overlapping convention.
2. The LiteLLM provider injects ``cache_control`` on the stable prefix for
   Anthropic-routed models, leaves non-Anthropic models untouched, and
   maps ``cache_creation_input_tokens`` → ``TokenUsage.cache_write``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from movate.core.models import TokenUsage
from movate.providers.litellm import (
    _cache_creation_tokens,
    _cache_last_tool,
    _cache_prefix_messages,
    _is_anthropic_model,
    _pop_cache_prompt,
    _to_completion_response,
)
from movate.providers.pricing import ModelPrice, PricingTable

# ---------------------------------------------------------------------------
# Pricing — cache read discount + cache write premium
# ---------------------------------------------------------------------------


def _table(**overrides: Any) -> PricingTable:
    price = {
        "input_per_1k": 1.0,
        "output_per_1k": 2.0,
        "cached_input_per_1k": 0.1,  # 0.1x input — the cache-read rate
    }
    price.update(overrides)
    return PricingTable(
        version="t",
        last_verified="2026-05-30",
        models={"anthropic/claude-test": ModelPrice(**price)},
    )


@pytest.mark.unit
def test_cost_for_applies_cache_read_discount() -> None:
    """``cached_input`` is a SUBSET of ``input``: 6000 total prompt tokens,
    5000 of them cache reads. Reads bill at 0.1x, the 1000 uncached at
    full rate."""
    table = _table()
    cost = table.cost_for(
        provider="anthropic/claude-test",
        tokens=TokenUsage(input=6000, output=0, cached_input=5000),
    )
    # (6000-5000)/1000 * 1.0  +  5000/1000 * 0.1  = 1.0 + 0.5 = 1.5
    assert cost == pytest.approx(1.5)


@pytest.mark.unit
def test_cost_for_applies_cache_write_premium_derived() -> None:
    """Cache writes bill at 1.25x input when no explicit write rate is
    pinned in the table (the common case — existing rows need no edit).
    Writes are a separate bucket, NOT part of ``input``."""
    table = _table()
    cost = table.cost_for(
        provider="anthropic/claude-test",
        tokens=TokenUsage(input=1000, output=0, cache_write=4000),
    )
    # 1000/1000 * 1.0  +  4000/1000 * (1.0 * 1.25) = 1.0 + 5.0 = 6.0
    assert cost == pytest.approx(6.0)


@pytest.mark.unit
def test_cost_for_uses_explicit_cache_write_rate_when_set() -> None:
    """An explicit ``cache_write_per_1k`` overrides the derived 1.25x."""
    table = _table(cache_write_per_1k=3.0)
    cost = table.cost_for(
        provider="anthropic/claude-test",
        tokens=TokenUsage(input=0, output=0, cache_write=2000),
    )
    # 2000/1000 * 3.0 = 6.0
    assert cost == pytest.approx(6.0)


@pytest.mark.unit
def test_cost_for_full_cache_hit_is_cheap() -> None:
    """A request served almost entirely from cache costs a fraction of
    the uncached price — the savings the dashboards should reflect."""
    table = _table()
    uncached = table.cost_for(
        provider="anthropic/claude-test",
        tokens=TokenUsage(input=10_000, output=0),
    )
    cached = table.cost_for(
        provider="anthropic/claude-test",
        # Same 10k prompt, now mostly a cache hit: 10000 total, 9900 read.
        tokens=TokenUsage(input=10_000, output=0, cached_input=9900),
    )
    assert uncached == pytest.approx(10.0)
    # (100 * 1.0 + 9900 * 0.1)/1000 = 0.1 + 0.99 = ~1.09 — an order of
    # magnitude cheaper.
    assert cached < uncached / 5


@pytest.mark.unit
def test_cost_for_openai_overlap_not_double_counted() -> None:
    """OpenAI's ``input`` INCLUDES cached tokens. ``cost_for`` subtracts
    them so they aren't billed at full rate AND at the cache rate."""
    table = PricingTable(
        version="t",
        last_verified="2026-05-30",
        models={
            "openai/gpt-x": ModelPrice(input_per_1k=1.0, output_per_1k=2.0, cached_input_per_1k=0.5)
        },
    )
    cost = table.cost_for(
        provider="openai/gpt-x",
        # input=1000 is the TOTAL; 400 of it were cache reads.
        tokens=TokenUsage(input=1000, output=0, cached_input=400),
    )
    # (1000-400)/1000 * 1.0  +  400/1000 * 0.5 = 0.6 + 0.2 = 0.8
    assert cost == pytest.approx(0.8)


@pytest.mark.unit
def test_cost_for_zero_cache_is_byte_for_byte_legacy() -> None:
    """No cache activity ⇒ identical to the pre-caching formula."""
    table = _table()
    cost = table.cost_for(
        provider="anthropic/claude-test",
        tokens=TokenUsage(input=2000, output=500),
    )
    # 2.0 + 1.0 = 3.0
    assert cost == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# LiteLLM — cache_control placement + token mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic/claude-sonnet-4-6", True),
        ("bedrock/anthropic.claude-3-5-sonnet", True),
        ("vertex_ai/claude-opus-4-6", True),
        ("openai/gpt-4o-mini-2024-07-18", False),
        ("azure/gpt-4.1", False),
    ],
)
def test_is_anthropic_model(provider: str, expected: bool) -> None:
    assert _is_anthropic_model(provider) is expected


@pytest.mark.unit
def test_pop_cache_prompt_default_on() -> None:
    params, enabled = _pop_cache_prompt({"temperature": 0.3})
    assert enabled is True
    assert "cache_prompt" not in params
    assert params == {"temperature": 0.3}


@pytest.mark.unit
@pytest.mark.parametrize("falsey", [False, "false", "0", "no", "off", ""])
def test_pop_cache_prompt_opt_out(falsey: Any) -> None:
    _params, enabled = _pop_cache_prompt({"cache_prompt": falsey})
    assert enabled is False


@pytest.mark.unit
def test_cache_prefix_marks_first_user_message() -> None:
    """When there's no system message, the rendered prompt rides the
    first user turn — that's the stable prefix breakpoint."""
    out = _cache_prefix_messages(
        [
            {"role": "user", "content": "RENDERED PROMPT"},
            {"role": "user", "content": "follow-up"},
        ]
    )
    assert out[0]["content"] == [
        {"type": "text", "text": "RENDERED PROMPT", "cache_control": {"type": "ephemeral"}}
    ]
    # Later turns untouched.
    assert out[1] == {"role": "user", "content": "follow-up"}


@pytest.mark.unit
def test_cache_prefix_prefers_system_message() -> None:
    out = _cache_prefix_messages(
        [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert out[0]["content"][0]["text"] == "SYSTEM"
    assert out[1] == {"role": "user", "content": "hi"}


@pytest.mark.unit
def test_cache_prefix_does_not_mutate_input() -> None:
    msgs = [{"role": "user", "content": "RENDERED"}]
    _cache_prefix_messages(msgs)
    assert msgs == [{"role": "user", "content": "RENDERED"}]


@pytest.mark.unit
def test_cache_last_tool_marks_only_last() -> None:
    tools = [
        {"type": "function", "function": {"name": "a"}},
        {"type": "function", "function": {"name": "b"}},
    ]
    out = _cache_last_tool(tools)
    assert out is not None
    assert "cache_control" not in out[0]
    assert out[-1]["cache_control"] == {"type": "ephemeral"}
    # Input untouched.
    assert "cache_control" not in tools[-1]


@pytest.mark.unit
def test_cache_last_tool_none_passthrough() -> None:
    assert _cache_last_tool(None) is None
    assert _cache_last_tool([]) == []


@pytest.mark.unit
def test_cache_creation_tokens_extracted() -> None:
    usage = SimpleNamespace(cache_creation_input_tokens=128)
    assert _cache_creation_tokens(usage) == 128


@pytest.mark.unit
def test_cache_creation_tokens_absent_is_zero() -> None:
    assert _cache_creation_tokens(SimpleNamespace()) == 0
    assert _cache_creation_tokens(None) == 0


@pytest.mark.unit
def test_to_completion_response_maps_cache_write() -> None:
    """LiteLLM ModelResponse → CompletionResponse carries cache writes."""
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            cache_creation_input_tokens=64,
        ),
        model="anthropic/claude-sonnet-4-6",
    )
    out = _to_completion_response(resp)
    assert out.tokens.input == 100
    assert out.tokens.output == 10
    assert out.tokens.cache_write == 64
