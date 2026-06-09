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


# ---------------------------------------------------------------------------
# COST gate — conformant with ModelPolicy's per-run budget ceiling.
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402

from movate.core.quotas import (  # noqa: E402
    QuotaConfig,
    QuotaMode,
    TenantQuota,
    check_quota,
)
from movate.core.reporting import Usage, UsageRollup  # noqa: E402
from movate.governance.adapters import (  # noqa: E402
    cost_gate_from_model_policy,
    quota_gate_from_config,
)


@dataclass
class _Budget:
    max_cost_usd_per_run: float


@dataclass
class _ModelRef:
    provider: str
    fallback: list[_ModelRef]


@dataclass
class _Spec:
    """Minimal stand-in for AgentSpec's budget/model surface (the only fields
    ModelPolicy.check_agent reads). Keeps the conformance test independent of
    the full AgentSpec constructor."""

    budget: _Budget
    model: _ModelRef


@pytest.mark.unit
@pytest.mark.parametrize(
    ("max_cost_per_run_usd", "declared", "expect_deny"),
    [
        (None, 5.00, False),  # no ceiling → permissive
        (0.50, 0.25, False),  # under cap
        (0.50, 0.50, False),  # exactly at cap (legacy boundary is strict >)
        (0.50, 0.51, True),  # over cap
        (1.00, 5.00, True),  # well over
    ],
)
def test_cost_gate_conforms_to_model_policy_budget(
    max_cost_per_run_usd: float | None, declared: float, expect_deny: bool
) -> None:
    policy = ModelPolicy(max_cost_per_run_usd=max_cost_per_run_usd)
    gate = cost_gate_from_model_policy(policy)
    decision = gate.evaluate(GovernanceContext(attributes={"max_cost_usd_per_run": declared}))
    # The legacy check_agent budget portion: a violation string mentioning the
    # budget iff the declared per-run cost exceeds the ceiling.
    spec = _Spec(budget=_Budget(max_cost_usd_per_run=declared), model=_ModelRef("openai", []))
    legacy_violations = policy.check_agent(spec)  # type: ignore[arg-type]
    legacy_budget_violation = any("budget.max_cost_usd_per_run" in v for v in legacy_violations)
    assert (decision.effect is Effect.DENY) == legacy_budget_violation == expect_deny


@pytest.mark.unit
def test_cost_gate_missing_declared_budget_allows() -> None:
    # No declared budget in context → nothing to compare → allow (the gate
    # only denies a concrete budget it's told about).
    gate = cost_gate_from_model_policy(ModelPolicy(max_cost_per_run_usd=0.10))
    assert gate.evaluate(GovernanceContext()).effect is Effect.ALLOW


# ---------------------------------------------------------------------------
# QUOTA gate — the first STATEFUL gate (Plane 2 preview, D10). Conformant with
# check_quota run in DENY mode (where `allow` reflects the raw breach).
# ---------------------------------------------------------------------------


def _usage(
    tenant_id: str, *, tokens_in: int, tokens_out: int, requests: int, cost_usd: float
) -> Usage:
    return Usage(
        tenant_id=tenant_id,
        window_days=1,
        agent_filter=None,
        totals=UsageRollup(
            key=tenant_id,
            requests=requests,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        ),
        by_agent=[],
        by_provider=[],
    )


# (limits, usage counters, is_admin, expect_deny). Limits: (tokens, requests, cost).
_QUOTA_CASES = [
    # no row for tenant → allow (handled separately; see no-row test)
    ((1000, 100, 5.0), (500, 50, 2.0), False, False),  # all under
    ((1000, 100, 5.0), (1000, 50, 2.0), False, True),  # tokens at ceiling (>=)
    ((1000, 100, 5.0), (1200, 50, 2.0), False, True),  # tokens over
    ((1000, 100, 5.0), (500, 100, 2.0), False, True),  # requests at ceiling
    ((1000, 100, 5.0), (500, 50, 5.0), False, True),  # cost at ceiling
    ((1000, 100, 5.0), (5000, 500, 50.0), True, False),  # admin bypass despite all over
    ((None, None, 5.0), (9999, 9999, 2.0), False, False),  # only cost configured, under
    ((None, None, 5.0), (9999, 9999, 6.0), False, True),  # only cost configured, over
]


@pytest.mark.unit
@pytest.mark.parametrize(("limits", "usage", "is_admin", "expect_deny"), _QUOTA_CASES)
def test_quota_gate_conforms_to_check_quota(
    limits: tuple[int | None, int | None, float | None],
    usage: tuple[int, int, float],
    is_admin: bool,
    expect_deny: bool,
) -> None:
    tokens_limit, requests_limit, cost_limit = limits
    tokens_used, requests_used, cost_used = usage
    tenant_id = "acme"

    row = TenantQuota(
        tenant_id=tenant_id,
        daily_token_limit=tokens_limit,
        daily_request_limit=requests_limit,
        monthly_cost_usd_limit=cost_limit,
        mode=QuotaMode.DENY,  # DENY mode ⇒ check_quota.allow reflects the raw breach
    )
    config = QuotaConfig(tenants=[row], admin_tenant_ids=[tenant_id] if is_admin else [])
    gate = quota_gate_from_config(config)

    # The gate reads raw accumulated counters from the context (Plane 2 preview).
    ctx = GovernanceContext(
        tenant_id=tenant_id,
        attributes={
            "daily_tokens": tokens_used,
            "daily_requests": requests_used,
            "monthly_cost_usd": cost_used,
        },
    )
    decision = gate.evaluate(ctx)

    # The legacy reducer over the same usage, split into daily + monthly windows.
    daily = _usage(
        tenant_id, tokens_in=tokens_used, tokens_out=0, requests=requests_used, cost_usd=0.0
    )
    monthly = _usage(tenant_id, tokens_in=0, tokens_out=0, requests=0, cost_usd=cost_used)
    legacy = check_quota(row, daily_usage=daily, monthly_usage=monthly, is_admin=is_admin)

    # Gate raw-DENY ⇔ legacy (deny-mode) blocks ⇔ expectation.
    assert decision.blocked == (not legacy.allow) == expect_deny


@pytest.mark.unit
def test_quota_gate_no_row_for_tenant_allows() -> None:
    # A tenant with no configured row is never blocked — exactly check_quota's
    # `quota=None` short-circuit.
    config = QuotaConfig(tenants=[TenantQuota(tenant_id="other", daily_token_limit=1)])
    gate = quota_gate_from_config(config)
    ctx = GovernanceContext(tenant_id="acme", attributes={"daily_tokens": 9999})
    assert gate.evaluate(ctx).effect is Effect.ALLOW
    assert check_quota(
        None,
        daily_usage=_usage("acme", tokens_in=9999, tokens_out=0, requests=0, cost_usd=0.0),
        monthly_usage=_usage("acme", tokens_in=0, tokens_out=0, requests=0, cost_usd=0.0),
    ).allow
