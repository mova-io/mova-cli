"""Layered-defaults merge: fill gaps in a raw agent.yaml dict with
project-level defaults *before* it reaches Pydantic validation.

Two-layer system in v1:

  Layer 1: ``policy.yaml: defaults:`` (project-wide, applies to every agent)
  Layer 2: ``agent.yaml`` (per-agent, always wins on conflict)

Future-friendly: a tenant layer (Layer 1.5) and an invocation layer
(Layer 3, CLI flags) drop into the same merge stack without changing
this module's interface — see ``apply_defaults_to_raw`` docstring.

Why a raw-dict merge rather than a post-Pydantic-validation merge?
Pydantic auto-fills missing scalars (``timeouts.call_ms`` defaults
to 30_000) the instant the spec is constructed, so by the time the
loader has a parsed ``AgentSpec`` it can't tell whether the operator
*wrote* ``call_ms: 30000`` or just got Pydantic's default. Merging
at the raw-dict level preserves that distinction — keys absent in
the YAML get filled from project defaults; keys present in the YAML
are left alone.

This module owns the merge rules; the loader is the only caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.config import AgentDefaults


def apply_defaults_to_raw(
    raw: dict[str, Any],
    defaults: AgentDefaults,
) -> dict[str, Any]:
    """Return a new agent.yaml dict with project defaults merged in.

    ``raw`` is not mutated. Merge rules — agent.yaml always wins
    per-key:

    * ``model.params`` — deep merge. Default ``temperature: 0.0``
      applies only to agents whose ``model.params`` doesn't already
      have a ``temperature`` key. (``setdefault`` semantics, not
      ``dict.update``.)
    * ``timeouts.call_ms`` / ``timeouts.total_ms`` — per-field.
      Default ``call_ms: 15000`` applies only if the agent's
      ``timeouts`` block omits ``call_ms`` entirely. If the agent
      writes ``timeouts: {}``, defaults still fill — there's no
      meaningful "I explicitly wrote an empty dict to opt out of
      defaults" distinction here.
    * ``budget.max_cost_usd_per_run`` — per-field, same pattern.

    Adding a new merged field later (e.g. a ``tags`` default, a
    ``runtime`` default) means: extend ``AgentDefaults``, add a
    branch here. The function stays pure — no I/O, no
    Pydantic-validation side-effects — so the test suite owns the
    merge contract end-to-end.
    """
    out = _shallow_copy(raw)
    _merge_model_params(out, defaults)
    _merge_timeouts(out, defaults)
    _merge_budget(out, defaults)
    return out


def _shallow_copy(raw: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy is enough — every helper below creates its own
    fresh sub-dict before mutating, so the caller's dict tree stays
    untouched even when we drill in two levels deep."""
    return dict(raw)


def _merge_model_params(out: dict[str, Any], defaults: AgentDefaults) -> None:
    """Fill ``model.params`` keys that the agent didn't specify.

    Agent.yaml may not declare ``model:`` at all (impossible — model
    is required, but be defensive), may declare ``model:`` without
    ``params:``, may declare an empty ``params: {}``, or may declare
    some params. In all cases, agent values win for keys it provides;
    defaults fill the rest.
    """
    if not defaults.model.params:
        return
    agent_model = dict(out.get("model") or {})
    agent_params = dict(agent_model.get("params") or {})
    for key, value in defaults.model.params.items():
        agent_params.setdefault(key, value)
    agent_model["params"] = agent_params
    out["model"] = agent_model


def _merge_timeouts(out: dict[str, Any], defaults: AgentDefaults) -> None:
    """Fill ``timeouts.call_ms`` / ``.total_ms`` only if absent.

    No-op if neither default is set, so projects without timeout
    defaults pay no perf or mutation cost.
    """
    has_default = defaults.timeouts.call_ms is not None or defaults.timeouts.total_ms is not None
    if not has_default:
        return
    agent_timeouts = dict(out.get("timeouts") or {})
    if defaults.timeouts.call_ms is not None and "call_ms" not in agent_timeouts:
        agent_timeouts["call_ms"] = defaults.timeouts.call_ms
    if defaults.timeouts.total_ms is not None and "total_ms" not in agent_timeouts:
        agent_timeouts["total_ms"] = defaults.timeouts.total_ms
    out["timeouts"] = agent_timeouts


def _merge_budget(out: dict[str, Any], defaults: AgentDefaults) -> None:
    """Fill ``budget.max_cost_usd_per_run`` only if absent.

    See :class:`movate.core.config.BudgetDefaults` for how this
    relates to (and stays distinct from) the enforced
    :class:`ModelPolicy.max_cost_per_run_usd` ceiling.
    """
    if defaults.budget.max_cost_usd_per_run is None:
        return
    agent_budget = dict(out.get("budget") or {})
    if "max_cost_usd_per_run" not in agent_budget:
        agent_budget["max_cost_usd_per_run"] = defaults.budget.max_cost_usd_per_run
    out["budget"] = agent_budget
