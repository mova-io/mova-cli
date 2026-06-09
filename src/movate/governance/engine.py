"""``GovernanceEngine`` + ``AuditSink`` (ADR 093 D3/D5).

The engine is the one place the runtime calls to evaluate a control. It runs the
registered gates for a kind, combines them deny-wins, applies the per-gate
rollout :class:`Mode` (a ``WARN``-mode deny is downgraded to a recorded warn),
emits audit, and returns the :class:`Decision`.

Default-empty: with no registered gates ``check`` returns ``ALLOW`` — a pure
no-op — so wiring the engine into the executor / runner / middleware (Phase 2)
is byte-for-byte safe until gates are registered and a policy is declared.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from movate.governance.gate import (
    Decision,
    Effect,
    Gate,
    GateKind,
    GovernanceContext,
    Mode,
    combine,
)
from movate.governance.policy import GovernancePolicy


@runtime_checkable
class AuditSink(Protocol):
    """Where governance decisions are recorded (ADR 093 D5). New backends slot in
    behind this Protocol; the default routes to the ``movate.audit`` logger."""

    def emit(self, decision: Decision, ctx: GovernanceContext) -> None: ...


class LoggingAuditSink:
    """Default sink: route every recorded decision to the existing
    ``record_audit_event`` (``movate.audit`` logger + active span). Never raises
    — audit must not break a request (ADR 093 D5)."""

    def emit(self, decision: Decision, ctx: GovernanceContext) -> None:
        try:
            from movate.tracing.audit import record_audit_event  # noqa: PLC0415

            record_audit_event(
                f"governance.{(decision.gate_kind.value if decision.gate_kind else 'engine')}",
                actor=ctx.actor,
                tenant_id=ctx.tenant_id,
                target=ctx.target(),
                effect=decision.effect.value,
                reason=decision.reason,
                obligations=list(decision.obligations),
                policy_id=decision.policy_id,
            )
        except Exception:
            pass


class GovernanceEngine:
    """Evaluate the registered gates for a :class:`GateKind` against the
    effective policy.

    Args:
        policy: the *resolved* effective policy (see
            :func:`movate.governance.policy.resolve`). Defaults to an empty
            policy (every gate ``WARN``, no constraints).
        gates: the registered gates. Defaults to none ⇒ ``check`` is a no-op.
        audit_sink: where decisions are recorded. Defaults to
            :class:`LoggingAuditSink`.
        audit_allows: whether ``ALLOW`` decisions are also recorded. Default
            ``False`` — only actionable (``WARN``/``DENY``) decisions are audited
            to keep the trail signal-dense (the engine still *returns* every
            decision; this only governs what is written).
    """

    def __init__(
        self,
        policy: GovernancePolicy | None = None,
        *,
        gates: Iterable[Gate] | None = None,
        audit_sink: AuditSink | None = None,
        audit_allows: bool = False,
    ) -> None:
        self._policy = policy or GovernancePolicy()
        self._gates: dict[GateKind, list[Gate]] = {}
        for gate in gates or []:
            self._gates.setdefault(gate.kind, []).append(gate)
        self._audit = audit_sink or LoggingAuditSink()
        self._audit_allows = audit_allows

    @property
    def policy(self) -> GovernancePolicy:
        return self._policy

    def check(self, kind: GateKind, ctx: GovernanceContext, *, audit: bool = True) -> Decision:
        """Evaluate ``kind`` against ``ctx`` and return the combined decision.

        Mode application (D4): a combined ``DENY`` only *blocks* when the gate is
        ``ENFORCE``; under ``WARN`` it is downgraded to a recorded ``WARN`` (the
        request proceeds). Obligations are preserved across the downgrade.
        """
        gates = self._gates.get(kind, ())
        decision = combine(gate.evaluate(ctx) for gate in gates)

        if decision.effect is Effect.DENY and self._policy.mode_for(kind) is Mode.WARN:
            decision = Decision(
                effect=Effect.WARN,
                gate_kind=decision.gate_kind,
                reason=decision.reason,
                obligations=decision.obligations,
                policy_id=decision.policy_id,
            )

        if audit and (decision.effect is not Effect.ALLOW or self._audit_allows):
            self._audit.emit(decision, ctx)

        return decision
