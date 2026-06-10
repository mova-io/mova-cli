"""Governance layer (ADR 093) — one policy model, one enforcement seam, one
audit spine.

Phase 1 ships the **seam only**, with **zero behavior change**: the uniform
:class:`Decision` shape + :class:`Gate` Protocol (D2), the layered
:class:`GovernancePolicy` + most-restrictive-wins :func:`resolve` (D1), the
:class:`GovernanceEngine` + :class:`AuditSink` (D3/D5), and the per-gate
``warn``/``enforce`` rollout :class:`Mode` (D4).

Nothing in the runtime imports this package yet. An engine with no registered
gates is a pure no-op (``check`` returns ``ALLOW``), and an empty
:class:`GovernancePolicy` carries zero constraints — so wiring it in (Phase 2)
is byte-for-byte safe until gates are registered and a policy is declared.
"""

from movate.governance.effects import (
    GovernanceEffectScope,
    consume_run_effect,
    governance_effect_scope,
    most_severe,
    peek_run_effect,
    record_run_effect,
    record_scope_effect,
)
from movate.governance.engine import AuditSink, GovernanceEngine, LoggingAuditSink
from movate.governance.gate import (
    Decision,
    Effect,
    Gate,
    GateKind,
    GovernanceContext,
    Mode,
    combine,
)
from movate.governance.policy import GovernancePolicy, resolve

__all__ = [
    "AuditSink",
    "Decision",
    "Effect",
    "Gate",
    "GateKind",
    "GovernanceContext",
    "GovernanceEffectScope",
    "GovernanceEngine",
    "GovernancePolicy",
    "LoggingAuditSink",
    "Mode",
    "combine",
    "consume_run_effect",
    "governance_effect_scope",
    "most_severe",
    "peek_run_effect",
    "record_run_effect",
    "record_scope_effect",
    "resolve",
]
