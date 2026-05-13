"""Tests for ``apply_defaults_to_raw`` — the layered-defaults merge that
fills gaps in a raw agent.yaml dict from ``policy.yaml: defaults:``.

Two layers in v1:

* Pure-function unit tests cover every merge rule against synthetic
  raw dicts — no I/O, no Pydantic. Locks down the contract before
  the loader integration.
* Loader integration tests live in ``test_loader.py``; they confirm
  the merge fires end-to-end through ``load_agent``.

Headline invariant: **agent.yaml always wins per-key**. Defaults only
fill keys the agent didn't write — they never override.
"""

from __future__ import annotations

from movate.core.config import (
    AgentDefaults,
    BudgetDefaults,
    ModelParamDefaults,
    TimeoutDefaults,
)
from movate.core.layered_defaults import apply_defaults_to_raw

# ---------------------------------------------------------------------------
# Helpers — build defaults from kwargs without ceremony.
# ---------------------------------------------------------------------------


def _defaults(
    *,
    params: dict | None = None,
    call_ms: int | None = None,
    total_ms: int | None = None,
    max_cost: float | None = None,
) -> AgentDefaults:
    return AgentDefaults(
        model=ModelParamDefaults(params=params or {}),
        timeouts=TimeoutDefaults(call_ms=call_ms, total_ms=total_ms),
        budget=BudgetDefaults(max_cost_usd_per_run=max_cost),
    )


# ---------------------------------------------------------------------------
# No-op: empty defaults must not mutate the raw dict
# ---------------------------------------------------------------------------


def test_empty_defaults_is_a_no_op() -> None:
    raw = {"name": "x", "model": {"provider": "openai/x"}, "version": "0.1.0"}
    result = apply_defaults_to_raw(raw, _defaults())
    assert result == raw


def test_apply_does_not_mutate_input() -> None:
    """``raw`` must be left exactly as the caller passed it. The loader
    relies on this to keep the original YAML available for diagnostics."""
    raw = {"model": {"provider": "openai/x", "params": {"temperature": 0.5}}}
    snapshot = {
        "model": {"provider": "openai/x", "params": {"temperature": 0.5}},
    }
    apply_defaults_to_raw(raw, _defaults(params={"max_tokens": 200}))
    assert raw == snapshot


# ---------------------------------------------------------------------------
# model.params merge — the headline case
# ---------------------------------------------------------------------------


def test_model_params_fills_keys_agent_omitted() -> None:
    """Headline scenario: project sets ``temperature: 0.0``, every
    agent without its own ``temperature`` gets it."""
    raw = {"model": {"provider": "openai/x"}}
    result = apply_defaults_to_raw(raw, _defaults(params={"temperature": 0.0}))
    assert result["model"]["params"] == {"temperature": 0.0}


def test_model_params_agent_wins_on_conflict() -> None:
    """Agent.yaml's ``temperature: 0.5`` survives even if defaults
    declare ``temperature: 0.0``."""
    raw = {"model": {"provider": "openai/x", "params": {"temperature": 0.5}}}
    result = apply_defaults_to_raw(raw, _defaults(params={"temperature": 0.0}))
    assert result["model"]["params"] == {"temperature": 0.5}


def test_model_params_deep_merge_per_key() -> None:
    """Agent specifies one param, defaults fill another — both end
    up in the resolved params dict."""
    raw = {"model": {"provider": "openai/x", "params": {"max_tokens": 500}}}
    result = apply_defaults_to_raw(raw, _defaults(params={"temperature": 0.0, "top_p": 0.9}))
    assert result["model"]["params"] == {
        "max_tokens": 500,
        "temperature": 0.0,
        "top_p": 0.9,
    }


def test_model_params_empty_dict_still_gets_filled() -> None:
    """Agent writes ``params: {}`` — defaults still fill, because there's
    no meaningful 'I explicitly want no params' distinction in YAML."""
    raw = {"model": {"provider": "openai/x", "params": {}}}
    result = apply_defaults_to_raw(raw, _defaults(params={"temperature": 0.0}))
    assert result["model"]["params"] == {"temperature": 0.0}


def test_model_params_no_model_block_still_works() -> None:
    """Defensive: if agent.yaml somehow lacks ``model:`` entirely
    (Pydantic will reject it later, but the merger shouldn't crash),
    we still produce a valid intermediate dict."""
    raw = {"name": "x"}
    result = apply_defaults_to_raw(raw, _defaults(params={"temperature": 0.0}))
    assert result["model"] == {"params": {"temperature": 0.0}}


# ---------------------------------------------------------------------------
# timeouts merge — per-field, agent wins
# ---------------------------------------------------------------------------


def test_timeouts_call_ms_fills_when_absent() -> None:
    raw = {"model": {"provider": "openai/x"}}
    result = apply_defaults_to_raw(raw, _defaults(call_ms=15_000))
    assert result["timeouts"]["call_ms"] == 15_000
    # total_ms still absent — defaults didn't set it, agent didn't either.
    assert "total_ms" not in result["timeouts"]


def test_timeouts_call_ms_agent_wins() -> None:
    raw = {"timeouts": {"call_ms": 5_000}}
    result = apply_defaults_to_raw(raw, _defaults(call_ms=15_000))
    assert result["timeouts"]["call_ms"] == 5_000


def test_timeouts_independent_per_field() -> None:
    """Agent pins call_ms, defaults pin total_ms — both survive."""
    raw = {"timeouts": {"call_ms": 5_000}}
    result = apply_defaults_to_raw(raw, _defaults(call_ms=15_000, total_ms=60_000))
    assert result["timeouts"] == {"call_ms": 5_000, "total_ms": 60_000}


def test_timeouts_no_defaults_no_block_added() -> None:
    """Defaults absent → no ``timeouts:`` block synthesised. Pydantic
    will fill its own defaults at validation time."""
    raw = {"model": {"provider": "openai/x"}}
    result = apply_defaults_to_raw(raw, _defaults())
    assert "timeouts" not in result


# ---------------------------------------------------------------------------
# budget merge — same pattern
# ---------------------------------------------------------------------------


def test_budget_fills_when_absent() -> None:
    raw = {"model": {"provider": "openai/x"}}
    result = apply_defaults_to_raw(raw, _defaults(max_cost=0.50))
    assert result["budget"]["max_cost_usd_per_run"] == 0.50


def test_budget_agent_wins() -> None:
    raw = {"budget": {"max_cost_usd_per_run": 1.00}}
    result = apply_defaults_to_raw(raw, _defaults(max_cost=0.50))
    assert result["budget"]["max_cost_usd_per_run"] == 1.00


def test_budget_no_defaults_no_block_added() -> None:
    raw = {"model": {"provider": "openai/x"}}
    result = apply_defaults_to_raw(raw, _defaults())
    assert "budget" not in result


# ---------------------------------------------------------------------------
# Composite — all three layers at once, as a realistic agent.yaml
# ---------------------------------------------------------------------------


def test_realistic_compound_merge() -> None:
    """End-to-end: every default kind set, agent specifies some
    overrides, every rule fires in one merge."""
    raw = {
        "name": "compound-agent",
        "version": "0.1.0",
        "model": {
            "provider": "openai/gpt-4o-mini-2024-07-18",
            "params": {"temperature": 0.2},
        },
        "budget": {"max_cost_usd_per_run": 0.05},
    }
    defaults = _defaults(
        params={"temperature": 0.0, "max_tokens": 1024},
        call_ms=15_000,
        total_ms=60_000,
        max_cost=0.50,
    )
    result = apply_defaults_to_raw(raw, defaults)
    # Agent's temperature survives; max_tokens filled from defaults.
    assert result["model"]["params"] == {"temperature": 0.2, "max_tokens": 1024}
    # Timeouts came purely from defaults.
    assert result["timeouts"] == {"call_ms": 15_000, "total_ms": 60_000}
    # Agent's budget wins over default cap.
    assert result["budget"]["max_cost_usd_per_run"] == 0.05
    # Unmerged fields pass through.
    assert result["name"] == "compound-agent"
    assert result["model"]["provider"] == "openai/gpt-4o-mini-2024-07-18"
