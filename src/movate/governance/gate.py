"""The governance enforcement seam — uniform types + the ``Gate`` Protocol
(ADR 093 D2).

One :class:`Decision` shape for every control. Each existing check (model
allowlist, budget, quota, skill side-effect, guardrail, pattern caps, HITL,
eval gate) becomes a :class:`Gate` behind this Protocol in Phase 2; new
governance is a new ``Gate``, never a bespoke branch in execution logic.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Effect(StrEnum):
    """What a gate decided. Ordered by restrictiveness: ``DENY`` > ``WARN`` >
    ``ALLOW`` (see :data:`_EFFECT_RANK` / :func:`combine`)."""

    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


# Restrictiveness rank — higher is more restrictive (most-restrictive-wins).
_EFFECT_RANK: dict[Effect, int] = {Effect.ALLOW: 0, Effect.WARN: 1, Effect.DENY: 2}


class Mode(StrEnum):
    """Per-gate rollout posture (ADR 093 D4).

    * ``WARN`` — observe: a would-be ``DENY`` is recorded but downgraded to a
      ``WARN`` (the request proceeds). The safe rollout default.
    * ``ENFORCE`` — a ``DENY`` blocks.
    """

    WARN = "warn"
    ENFORCE = "enforce"


class GateKind(StrEnum):
    """The AI-specific gate taxonomy (ADR 093 D6) — the dimension a gate governs,
    and the edge it fires at."""

    MODEL = "model"  # pre-call: provider/model allowlist (residency + cost)
    RUNTIME = "runtime"  # pre-call: allowed execution backend
    SKILL = "skill"  # pre-call: skill side-effect class
    DATA = "data"  # pre-call: KB/context/tool read-scope
    COST = "cost"  # execution: budget/cost caps
    PATTERN = "pattern"  # execution: fan-out/supervisor/loop bounds (ADR 092)
    QUALITY = "quality"  # decision: eval-gate (ADR 056)
    APPROVAL = "approval"  # decision: HITL approval (ADR 062/083)
    QUOTA = "quota"  # admission: per-tenant ceilings (ADR 036)
    LIFECYCLE = "lifecycle"  # agent status / provenance (ADR 090)


@dataclass(frozen=True)
class GovernanceContext:
    """The inputs a gate evaluates against.

    All optional with an open ``attributes`` bag, so a gate reads exactly what
    it needs (a model string, a skill name, a cost-so-far, …) without a rigid
    cross-cutting schema. Frozen so a gate can't mutate the request context.
    """

    tenant_id: str = "local"
    actor: str = "system"
    project: str | None = None
    agent: str | None = None
    workflow: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def target(self) -> str:
        """A best-effort identifier of what is being governed (for audit)."""
        return self.workflow or self.agent or self.project or "-"


@dataclass(frozen=True)
class Decision:
    """A gate's verdict. ``obligations`` are conditional requirements a gate may
    attach to an ``ALLOW`` (e.g. ``"require_hitl"``, ``"redact_pii"``)."""

    effect: Effect
    gate_kind: GateKind | None = None
    reason: str = ""
    obligations: tuple[str, ...] = ()
    policy_id: str = ""

    @property
    def blocked(self) -> bool:
        """True iff this decision blocks the request (an *enforced* deny)."""
        return self.effect is Effect.DENY

    @classmethod
    def allow(
        cls,
        gate_kind: GateKind | None = None,
        *,
        reason: str = "",
        obligations: Iterable[str] = (),
    ) -> Decision:
        return cls(Effect.ALLOW, gate_kind, reason, tuple(obligations))

    @classmethod
    def deny(
        cls,
        gate_kind: GateKind,
        reason: str,
        *,
        obligations: Iterable[str] = (),
        policy_id: str = "",
    ) -> Decision:
        return cls(Effect.DENY, gate_kind, reason, tuple(obligations), policy_id)


def combine(decisions: Iterable[Decision]) -> Decision:
    """Combine gate decisions with **most-restrictive-wins** (``DENY`` > ``WARN``
    > ``ALLOW``), unioning every decision's obligations (order-preserving dedup).

    An empty input ⇒ ``ALLOW`` — the no-op default that makes an engine with no
    gates a pure pass-through.
    """
    items = list(decisions)
    if not items:
        return Decision(Effect.ALLOW)
    winner = max(items, key=lambda d: _EFFECT_RANK[d.effect])
    obligations = tuple(dict.fromkeys(o for d in items for o in d.obligations))
    return Decision(
        effect=winner.effect,
        gate_kind=winner.gate_kind,
        reason=winner.reason,
        obligations=obligations,
        policy_id=winner.policy_id,
    )


@runtime_checkable
class Gate(Protocol):
    """A single governance control. ``evaluate`` is pure (no side effects): the
    :class:`GovernanceEngine` owns audit + mode application."""

    kind: GateKind

    def evaluate(self, ctx: GovernanceContext) -> Decision: ...
