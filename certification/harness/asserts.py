"""Platform-capability assertions for the MDK certification harness.

A certification scenario runs a real workflow, then asserts what the *platform*
did — not what the LLM said. These helpers are the shared vocabulary for that:
each maps to one MDK capability the suite claims is production-ready. They read
three sources: the workflow result (status / per-node runs / cost), the
simulated-systems ledger (:mod:`sim_systems` — what the workflow did to the
world), and the governance audit stream (captured via :class:`GovernanceAudit`).

Assertions raise ``AssertionError`` with an actionable message on failure, so a
scenario reads as a checklist of platform guarantees.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from certification.harness import sim_systems

# ---------------------------------------------------------------------------
# Execution + cost
# ---------------------------------------------------------------------------


def assert_completed(result: Any) -> None:
    """The workflow reached a terminal SUCCESS (durably, if on Temporal)."""
    status = getattr(getattr(result, "status", None), "value", getattr(result, "status", None))
    assert str(status).lower() == "success", (
        f"workflow did not complete: status={status} error={getattr(result, 'error', None)}"
    )


def assert_cost_tracked(result: Any) -> None:
    """Per-node cost accounting is present (token/cost visibility).

    NOTE: on the Temporal backend ``result.runs`` is currently empty (a known
    gap — durable per-node RunRecords are not persisted), so this asserts the
    native-path contract today and will tighten once that gap closes."""
    runs = getattr(result, "runs", []) or []
    assert runs, "no per-node runs recorded — cannot verify cost tracking"
    assert any(getattr(getattr(r, "metrics", None), "cost_usd", None) is not None for r in runs), (
        "no node reported cost_usd — cost tracking not wired"
    )


# ---------------------------------------------------------------------------
# Simulated side-effects — what the workflow did to the (faked) outside world
# ---------------------------------------------------------------------------


def assert_side_effect(run_id: str, system: str, action: str, *, times: int | None = None) -> None:
    """A side-effect of ``(system, action)`` was recorded for this run.

    ``times`` pins an exact count (e.g. ERP submitted *exactly once*)."""
    hits = [e for e in sim_systems.read(run_id) if e["system"] == system and e["action"] == action]
    if times is None:
        assert hits, (
            f"expected at least one {system}.{action} side-effect for run {run_id!r}, got none"
        )
    else:
        assert len(hits) == times, (
            f"expected {times}x {system}.{action} for run {run_id!r}, got {len(hits)}"
        )


def assert_no_side_effect(run_id: str, system: str, action: str | None = None) -> None:
    """No side-effect on ``system`` (optionally a specific ``action``) — e.g.
    the ERP was NOT submitted because approval was denied."""
    hits = [
        e
        for e in sim_systems.read(run_id)
        if e["system"] == system and (action is None or e["action"] == action)
    ]
    label = f"{system}.{action}" if action else system
    assert not hits, f"expected no {label} side-effect for run {run_id!r}, but found {len(hits)}"


def side_effects(run_id: str) -> list[dict[str, Any]]:
    """The ordered side-effect ledger for a run (for ad-hoc payload assertions)."""
    return sim_systems.read(run_id)


# ---------------------------------------------------------------------------
# Governance — capture the audit stream + assert a decision fired
# ---------------------------------------------------------------------------


class GovernanceAudit:
    """Capture the ``governance.*`` audit events the GovernanceEngine emits.

    The engine's LoggingAuditSink routes every recorded decision to the
    ``movate.audit`` logger. This handler collects them so a scenario can assert
    "a COST/QUOTA/MODEL decision with effect=warn|deny fired during the run"."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _handle(self, record: logging.LogRecord) -> None:
        audit = getattr(record, "audit", None)
        if isinstance(audit, dict) and str(audit.get("action", "")).startswith("governance."):
            self.events.append(audit)

    def has(self, *, kind: str | None = None, effect: str | None = None) -> bool:
        for e in self.events:
            action = str(e.get("action", ""))  # e.g. "governance.cost"
            if kind is not None and action != f"governance.{kind}":
                continue
            if effect is not None and str(e.get("effect", "")) != effect:
                continue
            return True
        return False


@contextmanager
def capture_governance() -> Iterator[GovernanceAudit]:
    """Collect governance decisions emitted during the ``with`` block."""
    cap = GovernanceAudit()

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            cap._handle(record)

    handler = _H()
    logger = logging.getLogger("movate.audit")
    logger.addHandler(handler)
    prev_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield cap
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)


def assert_governance_fired(cap: GovernanceAudit, *, kind: str, effect: str | None = None) -> None:
    """A governance decision of ``kind`` (optionally a specific ``effect``) was
    recorded — proving the governance layer was actually active on this run."""
    where = f"kind={kind}" + (f", effect={effect}" if effect else "")
    assert cap.has(kind=kind, effect=effect), (
        f"no governance decision matching {where} was recorded — governance "
        f"did not fire (engine dormant, or no policy configured). "
        f"saw: {[e.get('action') + '/' + str(e.get('effect')) for e in cap.events] or '∅'}"
    )
