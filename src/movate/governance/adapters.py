"""Adapters that express mdk's *existing* controls as governance `Gate`s
(ADR 093 Phase 2).

Each adapter bridges a control that ships today (a `policy.yaml` block, an
executor cap, a quota) onto the uniform `Gate` seam, pinned to the legacy
behavior by a conformance test. This is **pure consolidation** — the same
decision, now via one `Decision` + audit shape — not new enforcement.

Boundary (CLAUDE.md §6): governance sits *above* core, so this module may import
`core`. The pure seam (`gate` / `policy` / `engine`) does NOT import `core`, so
`core` can call the engine in a later step without an import cycle. This module
is intentionally NOT re-exported from `movate.governance.__init__`, keeping the
seam importable without pulling in `core`.
"""

from __future__ import annotations

from movate.core.config import ModelPolicy, RuntimePolicy, SkillPolicy
from movate.core.models import AgentRuntime, SkillSideEffects
from movate.core.quotas import QuotaConfig
from movate.governance.gate import Decision, GateKind, GovernanceContext
from movate.governance.policy import GovernancePolicy


class ModelGate:
    """The MODEL gate (ADR 093 D6) — re-expresses `ModelPolicy`'s
    `deny_models` / `allowed_providers` check as a `Decision`.

    Reads the candidate LiteLLM model string from `ctx.attributes["model"]`.
    `deny_models` takes precedence over `allowed_providers` (legacy semantics).
    A missing/empty model is treated as allowed (the legacy `check_model`
    returns `None` for anything not explicitly denied).
    """

    kind = GateKind.MODEL

    def __init__(
        self,
        *,
        allowed_providers: frozenset[str] | None,
        denied_models: frozenset[str],
    ) -> None:
        self._allowed = allowed_providers
        self._denied = denied_models

    def evaluate(self, ctx: GovernanceContext) -> Decision:
        model = str(ctx.attributes.get("model", ""))
        if model in self._denied:
            return Decision.deny(
                GateKind.MODEL,
                f"model {model!r} is in deny_models",
                policy_id="model.deny_models",
            )
        if self._allowed:
            prefix = model.split("/", 1)[0] if "/" in model else model
            if prefix not in self._allowed:
                allowed = ", ".join(sorted(self._allowed))
                return Decision.deny(
                    GateKind.MODEL,
                    f"provider prefix {prefix!r} (from {model!r}) "
                    f"not in allowed_providers [{allowed}]",
                    policy_id="model.allowed_providers",
                )
        return Decision.allow(GateKind.MODEL)


def governance_policy_from_model_policy(mp: ModelPolicy) -> GovernancePolicy:
    """Map the existing `ModelPolicy` onto the unified `GovernancePolicy` (D1).

    The consolidation seam: today's `policy.yaml` `policy:` block becomes a
    sub-policy of the governance model with **no field renames**. An empty
    `ModelPolicy` maps to an empty (`is_empty`) `GovernancePolicy`.
    """
    return GovernancePolicy(
        # An empty allowlist means "no restriction" in both models → None here.
        allowed_providers=frozenset(mp.allowed_providers) or None,
        denied_models=frozenset(mp.deny_models),
        max_cost_usd=mp.max_cost_per_run_usd,
    )


def model_gate_from_policy(mp: ModelPolicy) -> ModelGate:
    """Build the MODEL `Gate` from a `ModelPolicy` (the legacy config object)."""
    return ModelGate(
        allowed_providers=frozenset(mp.allowed_providers) or None,
        denied_models=frozenset(mp.deny_models),
    )


class RuntimeGate:
    """The RUNTIME gate (ADR 093 D6) — re-expresses `RuntimePolicy`'s allowlist.

    Reads the agent's `AgentRuntime` from `ctx.attributes["runtime"]`. A `None`
    allowlist (the permissive default) allows everything.
    """

    kind = GateKind.RUNTIME

    def __init__(self, *, allowed: frozenset[AgentRuntime] | None) -> None:
        self._allowed = allowed

    def evaluate(self, ctx: GovernanceContext) -> Decision:
        if self._allowed is None:
            return Decision.allow(GateKind.RUNTIME)
        runtime = ctx.attributes.get("runtime")
        if runtime in self._allowed:
            return Decision.allow(GateKind.RUNTIME)
        allowed = ", ".join(sorted(r.value for r in self._allowed))
        shown = runtime.value if isinstance(runtime, AgentRuntime) else runtime
        return Decision.deny(
            GateKind.RUNTIME,
            f"runtime {shown!r} not in project allowlist [{allowed}]",
            policy_id="runtime.allowed",
        )


def runtime_gate_from_policy(rp: RuntimePolicy) -> RuntimeGate:
    """Build the RUNTIME `Gate` from a `RuntimePolicy`."""
    return RuntimeGate(allowed=frozenset(rp.allowed) if rp.allowed is not None else None)


class SkillGate:
    """The SKILL gate (ADR 093 D6) — re-expresses `SkillPolicy`'s
    `allowed_side_effects` allowlist.

    Reads `ctx.attributes["side_effects"]` (a `SkillSideEffects`) and, for the
    message, `ctx.attributes["skill_name"]`. A `None` allowlist allows every
    side-effect class; an **empty** allowlist denies every skill.
    """

    kind = GateKind.SKILL

    def __init__(self, *, allowed_side_effects: frozenset[SkillSideEffects] | None) -> None:
        self._allowed = allowed_side_effects

    def evaluate(self, ctx: GovernanceContext) -> Decision:
        if self._allowed is None:
            return Decision.allow(GateKind.SKILL)
        side_effects = ctx.attributes.get("side_effects")
        if side_effects in self._allowed:
            return Decision.allow(GateKind.SKILL)
        name = ctx.attributes.get("skill_name", "?")
        shown = side_effects.value if isinstance(side_effects, SkillSideEffects) else side_effects
        if not self._allowed:
            reason = (
                f"skill {name!r} has side_effects={shown!r} but project policy "
                f"allows no skill side-effects (empty allowlist)"
            )
        else:
            allowed = ", ".join(sorted(s.value for s in self._allowed))
            reason = (
                f"skill {name!r} has side_effects={shown!r} but project policy "
                f"only allows: {allowed}"
            )
        return Decision.deny(GateKind.SKILL, reason, policy_id="skill.allowed_side_effects")


def skill_gate_from_policy(sp: SkillPolicy) -> SkillGate:
    """Build the SKILL `Gate` from a `SkillPolicy`."""
    return SkillGate(
        allowed_side_effects=(
            frozenset(sp.allowed_side_effects) if sp.allowed_side_effects is not None else None
        )
    )


class CostGate:
    """The COST gate (ADR 093 D6) — re-expresses `ModelPolicy`'s per-run cost
    ceiling (`max_cost_per_run_usd`).

    Reads the agent's declared per-run budget from
    `ctx.attributes["max_cost_usd_per_run"]` (the same value `check_agent`
    reads from `spec.budget.max_cost_usd_per_run`). Denies when the declared
    budget is **strictly greater** than the policy ceiling — exactly the legacy
    boundary (`> max_cost_per_run_usd`, not `>=`). A `None` ceiling (the
    permissive default) or a missing declared budget allows.

    This is the *pre-call cap* (does the agent's declared budget fit under the
    policy ceiling?), distinct from the *execution-time* spend tracking the
    executor does against the resolved `effective_max_cost`.
    """

    kind = GateKind.COST

    def __init__(self, *, max_cost_usd_per_run: float | None) -> None:
        self._cap = max_cost_usd_per_run

    def evaluate(self, ctx: GovernanceContext) -> Decision:
        if self._cap is None:
            return Decision.allow(GateKind.COST)
        declared = ctx.attributes.get("max_cost_usd_per_run")
        if declared is None:
            return Decision.allow(GateKind.COST)
        if float(declared) > self._cap:
            return Decision.deny(
                GateKind.COST,
                f"budget.max_cost_usd_per_run={declared} exceeds policy ceiling {self._cap}",
                policy_id="cost.max_cost_per_run_usd",
            )
        return Decision.allow(GateKind.COST)


def cost_gate_from_model_policy(mp: ModelPolicy) -> CostGate:
    """Build the COST `Gate` from a `ModelPolicy` (the per-run cap lives there)."""
    return CostGate(max_cost_usd_per_run=mp.max_cost_per_run_usd)


class QuotaGate:
    """The QUOTA gate (ADR 093 D6) — re-expresses the per-tenant ceilings from
    `quotas.yaml` (ADR 036 D2).

    **The first stateful gate.** Where the model/runtime/skill/cost gates are
    pure functions of the request, the quota gate decides against *accumulated*
    tenant usage. Here that accumulated usage is passed in via `ctx.attributes`
    (raw numbers the caller already computed via `build_usage`); the D10
    `GovernanceState` seam (Plane 2) will later let the gate fetch it itself.

    Reads `ctx.tenant_id` plus the raw counters from `ctx.attributes`:
    `daily_tokens`, `daily_requests`, `monthly_cost_usd` (each defaulting to 0).
    A breach (any configured ceiling met-or-exceeded — the legacy `>=` boundary,
    "100% spent is spent") emits a raw `DENY`. Admin tenants and tenants with no
    configured row always allow — exactly `check_quota`'s admin / `quota=None`
    short-circuits.

    Note the legacy per-tenant `mode` (`warn`/`deny`) is **superseded** by the
    engine's universal rollout `Mode` (D4): the gate always reports the breach
    as a raw `DENY`, and the `GovernancePolicy.mode_for(QUOTA)` decides whether
    that blocks or is downgraded to a recorded warn. Conformance is therefore
    pinned against `check_quota` run in `deny` mode (where `allow` reflects the
    raw breach, not the rollout posture).
    """

    kind = GateKind.QUOTA

    def __init__(self, config: QuotaConfig) -> None:
        self._config = config

    def evaluate(self, ctx: GovernanceContext) -> Decision:
        tenant_id = ctx.tenant_id
        if self._config.is_admin(tenant_id):
            return Decision.allow(GateKind.QUOTA)
        row = self._config.get(tenant_id)
        if row is None:
            return Decision.allow(GateKind.QUOTA)

        daily_tokens = float(ctx.attributes.get("daily_tokens", 0))
        daily_requests = float(ctx.attributes.get("daily_requests", 0))
        monthly_cost = float(ctx.attributes.get("monthly_cost_usd", 0.0))

        over: list[str] = []
        if row.daily_token_limit is not None and daily_tokens >= row.daily_token_limit:
            over.append(f"daily_tokens {daily_tokens:.0f}/{row.daily_token_limit}")
        if row.daily_request_limit is not None and daily_requests >= row.daily_request_limit:
            over.append(f"daily_requests {daily_requests:.0f}/{row.daily_request_limit}")
        if row.monthly_cost_usd_limit is not None and monthly_cost >= row.monthly_cost_usd_limit:
            over.append(f"monthly_cost_usd {monthly_cost:.4f}/{row.monthly_cost_usd_limit}")

        if over:
            return Decision.deny(
                GateKind.QUOTA,
                f"tenant {tenant_id!r} quota exceeded: " + "; ".join(over),
                policy_id="quota.tenant",
            )
        return Decision.allow(GateKind.QUOTA)


def quota_gate_from_config(config: QuotaConfig) -> QuotaGate:
    """Build the QUOTA `Gate` from a loaded `QuotaConfig` (`quotas.yaml`)."""
    return QuotaGate(config)
