"""ADR 093 Phase 2 — the MODEL gate is conformant with the legacy `ModelPolicy`.

The consolidation contract: re-expressing an existing control as a `Gate` must
reproduce the *exact* allow/deny verdict the shipped check produces. This is the
before/after equivalence anchor that makes Phase 2 a pure refactor (no new
enforcement).
"""

from __future__ import annotations

import pytest

from movate.core.config import ModelPolicy
from movate.governance.adapters import (
    ModelGate,
    governance_policy_from_model_policy,
    model_gate_from_policy,
)
from movate.governance.gate import Effect, GovernanceContext


def _ctx(model: str) -> GovernanceContext:
    return GovernanceContext(attributes={"model": model})


# Matrix of (policy, model) cases spanning the legacy semantics:
# permissive, deny-list, allowlist, allowlist-miss, deny-precedence.
_CASES = [
    (ModelPolicy(), "openai/gpt-4o-mini", False),
    (ModelPolicy(deny_models=["openai/gpt-3.5-turbo"]), "openai/gpt-3.5-turbo", True),
    (ModelPolicy(deny_models=["openai/gpt-3.5-turbo"]), "openai/gpt-4o", False),
    (ModelPolicy(allowed_providers=["azure"]), "openai/gpt-4o", True),
    (ModelPolicy(allowed_providers=["azure", "openai"]), "openai/gpt-4o", False),
    (ModelPolicy(allowed_providers=["azure"]), "azure/gpt-4.1", False),
    # deny_models takes precedence even within an allowed provider.
    (ModelPolicy(allowed_providers=["azure"], deny_models=["azure/gpt-4"]), "azure/gpt-4", True),
    # bare provider (no '/') is matched as its own prefix.
    (ModelPolicy(allowed_providers=["ollama"]), "ollama", False),
]


@pytest.mark.unit
@pytest.mark.parametrize(("policy", "model", "expect_deny"), _CASES)
def test_model_gate_conforms_to_model_policy(
    policy: ModelPolicy, model: str, expect_deny: bool
) -> None:
    gate = model_gate_from_policy(policy)
    decision = gate.evaluate(_ctx(model))
    legacy = policy.check_model(model)  # None ⇒ allowed; str ⇒ denied
    # The gate's deny ⇔ the legacy check's violation ⇔ the expected outcome.
    assert (decision.effect is Effect.DENY) == (legacy is not None) == expect_deny


@pytest.mark.unit
def test_governance_policy_from_model_policy_maps_fields() -> None:
    mp = ModelPolicy(
        allowed_providers=["openai", "azure"],
        deny_models=["openai/gpt-3.5-turbo"],
        max_cost_per_run_usd=0.50,
    )
    gp = governance_policy_from_model_policy(mp)
    assert gp.allowed_providers == frozenset({"openai", "azure"})
    assert gp.denied_models == frozenset({"openai/gpt-3.5-turbo"})
    assert gp.max_cost_usd == 0.50


@pytest.mark.unit
def test_empty_model_policy_maps_to_empty_governance_policy() -> None:
    # The permissive default must remain a no-op (the rule-5 compat contract).
    assert governance_policy_from_model_policy(ModelPolicy()).is_empty


@pytest.mark.unit
def test_model_gate_missing_model_matches_legacy() -> None:
    # With no allowlist (deny-only / permissive), a missing model is allowed —
    # exactly as the legacy check (it only denies a model it's told about).
    deny_only = ModelGate(allowed_providers=None, denied_models=frozenset({"x"}))
    assert deny_only.evaluate(GovernanceContext()).effect is Effect.ALLOW
    assert ModelPolicy(deny_models=["x"]).check_model("") is None  # legacy parity
    # …but under an allowlist an empty model fails the prefix check — and the
    # legacy check denies it too, so the gate is still conformant.
    with_allowlist = ModelGate(allowed_providers=frozenset({"azure"}), denied_models=frozenset())
    assert with_allowlist.evaluate(GovernanceContext()).effect is Effect.DENY
    assert ModelPolicy(allowed_providers=["azure"]).check_model("") is not None


# ---------------------------------------------------------------------------
# RUNTIME + SKILL gates — same conformance contract vs the legacy policies.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from movate.core.config import RuntimePolicy, SkillPolicy  # noqa: E402
from movate.core.models import AgentRuntime, SkillSideEffects  # noqa: E402
from movate.governance.adapters import (  # noqa: E402
    runtime_gate_from_policy,
    skill_gate_from_policy,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("policy", "runtime", "expect_deny"),
    [
        (RuntimePolicy(), AgentRuntime.LANGCHAIN, False),  # permissive
        (RuntimePolicy(allowed=[AgentRuntime.LITELLM]), AgentRuntime.LITELLM, False),
        (RuntimePolicy(allowed=[AgentRuntime.LITELLM]), AgentRuntime.NATIVE_ANTHROPIC, True),
        (
            RuntimePolicy(allowed=[AgentRuntime.LITELLM, AgentRuntime.LYZR]),
            AgentRuntime.LYZR,
            False,
        ),
    ],
)
def test_runtime_gate_conforms_to_runtime_policy(
    policy: RuntimePolicy, runtime: AgentRuntime, expect_deny: bool
) -> None:
    gate = runtime_gate_from_policy(policy)
    decision = gate.evaluate(GovernanceContext(attributes={"runtime": runtime}))
    legacy = policy.check_agent(SimpleNamespace(runtime=runtime))  # type: ignore[arg-type]
    assert (decision.effect is Effect.DENY) == (legacy is not None) == expect_deny


@pytest.mark.unit
@pytest.mark.parametrize(
    ("policy", "side_effects", "expect_deny"),
    [
        (SkillPolicy(), SkillSideEffects.MUTATES_STATE, False),  # permissive
        (
            SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY]),
            SkillSideEffects.READ_ONLY,
            False,
        ),
        (
            SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY]),
            SkillSideEffects.NETWORK,
            True,
        ),
        (
            SkillPolicy(allowed_side_effects=[]),
            SkillSideEffects.READ_ONLY,
            True,
        ),  # empty = deny all
    ],
)
def test_skill_gate_conforms_to_skill_policy(
    policy: SkillPolicy, side_effects: SkillSideEffects, expect_deny: bool
) -> None:
    gate = skill_gate_from_policy(policy)
    decision = gate.evaluate(
        GovernanceContext(attributes={"side_effects": side_effects, "skill_name": "lookup"})
    )
    legacy = policy.check_skill("lookup", side_effects)
    assert (decision.effect is Effect.DENY) == (legacy is not None) == expect_deny
