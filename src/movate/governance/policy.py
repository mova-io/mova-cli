"""``GovernancePolicy`` + the most-restrictive-wins resolver (ADR 093 D1).

Phase 1 carries a small set of representative, cross-cutting bounds plus the
per-gate rollout :class:`Mode`. Phase 2 maps the existing ``policy.yaml`` /
``quotas.yaml`` / ``agent.budget`` / ``workflow``-governance blocks onto
sub-policies here (no field renames). An empty policy carries zero constraints,
so default-empty ⇒ byte-for-byte current behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from movate.governance.gate import GateKind, Mode


@dataclass(frozen=True)
class GovernancePolicy:
    """The unified, layerable governance policy.

    Permissive defaults everywhere (``allowed_providers=None`` = all,
    ``denied_models`` empty, ``max_cost_usd=None`` = no cap, ``modes`` empty =
    every gate ``WARN``), so an empty policy resolves to zero constraints.
    """

    allowed_providers: frozenset[str] | None = None
    """``None`` ⇒ no allowlist (every provider allowed). A set restricts to those
    provider prefixes (intersected across layers)."""

    denied_models: frozenset[str] = frozenset()
    """Full model strings to reject outright (unioned across layers)."""

    max_cost_usd: float | None = None
    """``None`` ⇒ no aggregate cost ceiling (minimised across layers)."""

    modes: dict[GateKind, Mode] = field(default_factory=dict)
    """Per-gate rollout mode. An absent gate kind defaults to ``WARN``
    (observe-first, D4). ``ENFORCE`` cannot be downgraded by a lower layer."""

    def mode_for(self, kind: GateKind) -> Mode:
        return self.modes.get(kind, Mode.WARN)

    @property
    def is_empty(self) -> bool:
        """True iff this policy carries no constraints (a pure no-op)."""
        return (
            self.allowed_providers is None
            and not self.denied_models
            and self.max_cost_usd is None
            and not self.modes
        )


def resolve(*layers: GovernancePolicy) -> GovernancePolicy:
    """Combine policy layers (org → project → tenant → agent) with
    **most-restrictive-wins** (ADR 093 D1).

    The monotonicity contract — a lower (more specific) layer can only TIGHTEN,
    never loosen, a higher one:

    * ``allowed_providers`` — **intersection** (``None`` = the universe): a child
      can only narrow the org allowlist.
    * ``denied_models`` — **union**: a child can only add denials.
    * ``max_cost_usd`` — **minimum** (``None`` = ∞): a child can only lower the
      cap.
    * ``modes`` — **ENFORCE-wins**: a child can escalate ``WARN`` → ``ENFORCE``
      but can never downgrade an org-``ENFORCE``d gate.

    So a tenant override can never raise an org cost cap, widen an org provider
    allowlist, un-deny an org-denied model, or relax an org-enforced gate.
    """
    allowed: frozenset[str] | None = None  # the universe
    denied: set[str] = set()
    max_cost: float | None = None  # +infinity
    modes: dict[GateKind, Mode] = {}

    for p in layers:
        if p.allowed_providers is not None:
            allowed = p.allowed_providers if allowed is None else (allowed & p.allowed_providers)
        denied |= set(p.denied_models)
        if p.max_cost_usd is not None:
            max_cost = p.max_cost_usd if max_cost is None else min(max_cost, p.max_cost_usd)
        for kind, mode in p.modes.items():
            if modes.get(kind) is Mode.ENFORCE or mode is Mode.ENFORCE:
                modes[kind] = Mode.ENFORCE
            else:
                modes[kind] = mode

    return GovernancePolicy(
        allowed_providers=allowed,
        denied_models=frozenset(denied),
        max_cost_usd=max_cost,
        modes=modes,
    )
