"""Governance layer (ADR 093) Phase 1 — the seam.

Proves the zero-behavior-change contract (an empty policy + no gates ⇒ ALLOW
everything), the uniform Decision combination (deny-wins + obligation union),
the most-restrictive-wins resolver monotonicity (a child layer can only
tighten), and the warn→enforce mode application + audit emission.
"""

from __future__ import annotations

import pytest

from movate.governance import (
    Decision,
    Effect,
    GateKind,
    GovernanceContext,
    GovernanceEngine,
    GovernancePolicy,
    Mode,
    combine,
    resolve,
)
from movate.governance.gate import Gate

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FixedGate:
    """A gate that always returns the same decision (for kind)."""

    def __init__(self, kind: GateKind, decision: Decision) -> None:
        self.kind = kind
        self._decision = decision

    def evaluate(self, ctx: GovernanceContext) -> Decision:
        return self._decision


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[Decision, GovernanceContext]] = []

    def emit(self, decision: Decision, ctx: GovernanceContext) -> None:
        self.events.append((decision, ctx))


_CTX = GovernanceContext(tenant_id="t1", actor="user@x", agent="rag-qa")


# ---------------------------------------------------------------------------
# Decision.combine — most-restrictive-wins + obligation union
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_combine_empty_is_allow() -> None:
    assert combine([]).effect is Effect.ALLOW


@pytest.mark.unit
def test_combine_deny_wins_over_warn_and_allow() -> None:
    d = combine(
        [
            Decision.allow(GateKind.MODEL),
            Decision(Effect.WARN, GateKind.COST, reason="near cap"),
            Decision.deny(GateKind.SKILL, "side-effect not allowed"),
        ]
    )
    assert d.effect is Effect.DENY
    assert d.gate_kind is GateKind.SKILL


@pytest.mark.unit
def test_combine_unions_obligations_order_preserving_dedup() -> None:
    d = combine(
        [
            Decision.allow(GateKind.DATA, obligations=["redact_pii", "tag_high_risk"]),
            Decision.allow(GateKind.APPROVAL, obligations=["require_hitl", "redact_pii"]),
        ]
    )
    assert d.effect is Effect.ALLOW
    assert d.obligations == ("redact_pii", "tag_high_risk", "require_hitl")


# ---------------------------------------------------------------------------
# resolve — most-restrictive-wins monotonicity (D1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_empty_is_empty() -> None:
    assert resolve().is_empty
    assert resolve(GovernancePolicy(), GovernancePolicy()).is_empty


@pytest.mark.unit
def test_resolve_allowed_providers_intersect() -> None:
    org = GovernancePolicy(allowed_providers=frozenset({"openai", "azure", "anthropic"}))
    tenant = GovernancePolicy(allowed_providers=frozenset({"azure", "gcp"}))
    assert resolve(org, tenant).allowed_providers == frozenset({"azure"})
    # A None (no allowlist) layer never widens an existing allowlist.
    assert resolve(org, GovernancePolicy()).allowed_providers == frozenset(
        {"openai", "azure", "anthropic"}
    )


@pytest.mark.unit
def test_resolve_denied_models_union() -> None:
    org = GovernancePolicy(denied_models=frozenset({"openai/gpt-3.5-turbo"}))
    tenant = GovernancePolicy(denied_models=frozenset({"openai/gpt-4-0314"}))
    assert resolve(org, tenant).denied_models == frozenset(
        {"openai/gpt-3.5-turbo", "openai/gpt-4-0314"}
    )


@pytest.mark.unit
def test_resolve_max_cost_is_minimum() -> None:
    org = GovernancePolicy(max_cost_usd=0.50)
    tenant = GovernancePolicy(max_cost_usd=1.00)  # tries to RAISE the cap
    assert resolve(org, tenant).max_cost_usd == 0.50  # cannot loosen
    assert resolve(org, GovernancePolicy(max_cost_usd=0.10)).max_cost_usd == 0.10  # can tighten


@pytest.mark.unit
def test_resolve_mode_enforce_cannot_be_downgraded() -> None:
    org = GovernancePolicy(modes={GateKind.COST: Mode.ENFORCE})
    tenant = GovernancePolicy(modes={GateKind.COST: Mode.WARN})  # tries to relax
    assert resolve(org, tenant).mode_for(GateKind.COST) is Mode.ENFORCE
    # A child CAN escalate warn → enforce.
    assert (
        resolve(
            GovernancePolicy(modes={GateKind.COST: Mode.WARN}),
            GovernancePolicy(modes={GateKind.COST: Mode.ENFORCE}),
        ).mode_for(GateKind.COST)
        is Mode.ENFORCE
    )


@pytest.mark.unit
def test_resolve_monotonicity_child_can_only_tighten() -> None:
    """The governance invariant: layering a child can never loosen the parent."""
    parent = GovernancePolicy(
        allowed_providers=frozenset({"openai", "azure"}),
        denied_models=frozenset({"x"}),
        max_cost_usd=1.0,
        modes={GateKind.COST: Mode.ENFORCE},
    )
    for child in (
        GovernancePolicy(),  # empty
        GovernancePolicy(allowed_providers=frozenset({"azure", "gcp"})),
        GovernancePolicy(max_cost_usd=5.0),
        GovernancePolicy(denied_models=frozenset({"y"})),
        GovernancePolicy(modes={GateKind.COST: Mode.WARN}),
    ):
        eff = resolve(parent, child)
        assert eff.allowed_providers is not None
        assert eff.allowed_providers <= (parent.allowed_providers or frozenset())  # narrower
        assert eff.denied_models >= parent.denied_models  # broader denials
        assert eff.max_cost_usd is not None and eff.max_cost_usd <= parent.max_cost_usd  # lower
        assert eff.mode_for(GateKind.COST) is Mode.ENFORCE  # not downgraded


# ---------------------------------------------------------------------------
# GovernanceEngine — no-op default, deny-wins, mode application, audit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_engine_no_gates_is_allow_noop() -> None:
    engine = GovernanceEngine()
    d = engine.check(GateKind.MODEL, _CTX)
    assert d.effect is Effect.ALLOW
    assert not d.blocked


@pytest.mark.unit
def test_engine_protocol_conformance() -> None:
    assert isinstance(_FixedGate(GateKind.MODEL, Decision.allow()), Gate)


@pytest.mark.unit
def test_engine_warn_mode_downgrades_deny() -> None:
    sink = _RecordingSink()
    deny_gate = _FixedGate(GateKind.COST, Decision.deny(GateKind.COST, "over budget"))
    # Default policy ⇒ COST gate is WARN ⇒ the deny is downgraded + recorded.
    engine = GovernanceEngine(gates=[deny_gate], audit_sink=sink)
    d = engine.check(GateKind.COST, _CTX)
    assert d.effect is Effect.WARN
    assert not d.blocked
    assert d.reason == "over budget"
    assert len(sink.events) == 1 and sink.events[0][0].effect is Effect.WARN


@pytest.mark.unit
def test_engine_enforce_mode_blocks_deny() -> None:
    sink = _RecordingSink()
    deny_gate = _FixedGate(GateKind.COST, Decision.deny(GateKind.COST, "over budget"))
    engine = GovernanceEngine(
        GovernancePolicy(modes={GateKind.COST: Mode.ENFORCE}),
        gates=[deny_gate],
        audit_sink=sink,
    )
    d = engine.check(GateKind.COST, _CTX)
    assert d.effect is Effect.DENY
    assert d.blocked
    assert len(sink.events) == 1 and sink.events[0][0].effect is Effect.DENY


@pytest.mark.unit
def test_engine_allow_not_audited_by_default() -> None:
    sink = _RecordingSink()
    allow_gate = _FixedGate(GateKind.MODEL, Decision.allow(GateKind.MODEL))
    engine = GovernanceEngine(gates=[allow_gate], audit_sink=sink)
    engine.check(GateKind.MODEL, _CTX)
    assert sink.events == []
    # …but opt-in audits allows too.
    engine_all = GovernanceEngine(gates=[allow_gate], audit_sink=sink, audit_allows=True)
    engine_all.check(GateKind.MODEL, _CTX)
    assert len(sink.events) == 1


@pytest.mark.unit
def test_engine_only_runs_matching_kind() -> None:
    cost_gate = _FixedGate(GateKind.COST, Decision.deny(GateKind.COST, "x"))
    engine = GovernanceEngine(
        GovernancePolicy(modes={GateKind.COST: Mode.ENFORCE}), gates=[cost_gate]
    )
    # A check for a DIFFERENT kind sees no gates ⇒ ALLOW.
    assert engine.check(GateKind.MODEL, _CTX).effect is Effect.ALLOW
