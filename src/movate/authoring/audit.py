"""Append-only authoring audit log + replay support (D7e, #136).

Every authoring **action** the :class:`~movate.authoring.driver.AuthoringDriver`
performs (apply / undo) is recorded to an append-only, per-project audit log so a
developer can answer "what did the copilot change, when, and at what cost?" and
**replay** a recorded sequence back through the same confirm-gated driver.

Design:

* **Append-only** — the driver only ever *appends* a record; it never rewrites
  history. The log is JSONL under the project state dir (``.mdk/``), one record
  per line, distinct from the driver's existing ``authoring_log.jsonl`` (which
  is the live undo stack). The audit log is the *immutable* record of intent +
  outcome; the action log is the *mutable* undo cursor.
* **Control-plane** (``cli`` ⊥ ``runtime``) — it lives next to the project, not
  in the runtime store.
* **Degrades gracefully** — a corrupt / unreadable line is skipped with a warning
  (never crashes ``mdk dev``); a missing log reads as empty. Writing is
  best-effort: an audit write failure is logged, never raised, so it can't break
  an apply that already landed.

Replay (:func:`replay_records`) re-drives a recorded *applied* sequence through
:meth:`AuthoringDriver.apply` — the SAME plan→confirm→apply→verify spine, never a
raw re-write — so every D2 confirmation gate and D3/D4 safety property holds on
replay exactly as on the original run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from movate.core.paths import project_state_dir

if TYPE_CHECKING:
    from movate.authoring.driver import ApplyOutcome, AuthoringDriver

log = logging.getLogger(__name__)

#: Filename of the append-only audit log under the project state dir (``.mdk/``).
AUDIT_LOG_NAME = "authoring_audit.jsonl"


class AuditOutcome(StrEnum):
    """The recorded outcome of one authoring action (D7e)."""

    APPLIED = "applied"
    """The action was applied and verify did not revert it."""

    SKIPPED = "skipped"
    """The action was previewed but not applied (declined / gate not granted)."""

    REVERTED = "reverted"
    """The action was applied then rolled back by the D3 verify loop."""

    UNDONE = "undone"
    """A previously-applied action was reverted via the driver's ``undo``."""


class AuditRecord(BaseModel):
    """One append-only entry in the authoring audit log (D7e).

    Captures the catalog action name, the validated args, the outcome, a
    timestamp, and — for cost-bearing actions — the cost incurred. Carries the
    pre-apply ``checkpoint_hash`` and ``changed_paths`` too, so the log doubles
    as a forensic record. JSON-serializable; one record per JSONL line.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="The catalog action name.")
    agent: str | None = Field(default=None, description="Agent in scope, if any.")
    args: dict[str, Any] = Field(
        default_factory=dict, description="The validated action args (post model_dump)."
    )
    outcome: AuditOutcome = Field(..., description="applied / skipped / reverted / undone.")
    summary: str = Field(default="", description="Human-readable summary of what happened.")
    cost_usd: float = Field(
        default=0.0, description="Monetary cost the action incurred (0.0 for free actions)."
    )
    checkpoint_hash: str = Field(
        default="", description="Pre-apply snapshot hash (the undo target), if taken."
    )
    changed_paths: list[str] = Field(
        default_factory=list, description="Project-relative paths the action changed."
    )
    created_at: str = Field(default="", description="ISO-8601 UTC timestamp of the record.")

    @classmethod
    def from_apply(cls, action: str, args: dict[str, Any], outcome: ApplyOutcome) -> AuditRecord:
        """Build a record from a driver :class:`~movate.authoring.driver.ApplyOutcome`.

        Classifies the outcome (applied / reverted) and pulls the cost (the
        action's ``ActionResult.cost_usd``, ADR 024), checkpoint, and changed
        paths off the outcome so the audit log captures the full picture without
        the driver having to assemble it by hand.
        """
        reverted = outcome.verify is not None and outcome.verify.reverted
        result = outcome.result
        entry = outcome.log_entry
        return cls(
            action=action,
            agent=getattr(entry, "agent", None) if entry else _agent_from_args(args),
            args=dict(args),
            outcome=AuditOutcome.REVERTED if reverted else AuditOutcome.APPLIED,
            summary=(result.summary if result else outcome.plan.summary),
            cost_usd=(result.cost_usd if result else 0.0),
            checkpoint_hash=(entry.checkpoint_hash if entry else ""),
            changed_paths=(list(result.changed_paths) if result else []),
            created_at=_now(),
        )


def _agent_from_args(args: dict[str, Any]) -> str | None:
    val = args.get("agent")
    return str(val) if isinstance(val, str) else None


class AuditLog:
    """Append-only reader/writer for the per-project authoring audit log (D7e).

    Records live at ``<project-state-dir>/authoring_audit.jsonl`` (one JSONL
    record per line). The driver constructs one per invocation and appends to it
    at apply / undo time. Reads tolerate a corrupt line (skip + warn) and a
    missing file (empty); appends are best-effort (a write failure is logged,
    never raised) so audit can never break an apply.
    """

    def __init__(self, project_root: Path) -> None:
        self._project = project_root.resolve()

    @property
    def path(self) -> Path:
        return project_state_dir(self._project) / AUDIT_LOG_NAME

    def append(self, record: AuditRecord) -> None:
        """Append one record (best-effort: a write error is logged, not raised)."""
        try:
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(record.model_dump_json() + "\n")
        except OSError as exc:
            # An audit write failure must never break an apply that already
            # landed — degrade to a warning (rule 10).
            log.warning("authoring audit: failed to append record to %s: %s", self.path, exc)

    def read(self) -> list[AuditRecord]:
        """Read all records oldest-first; skip corrupt lines, empty if missing.

        A malformed line is skipped with a warning rather than raising — a
        corrupt audit log degrades the *view*, it never crashes ``mdk dev``
        (rule 10).
        """
        path = self.path
        if not path.is_file():
            return []
        records: list[AuditRecord] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("authoring audit: cannot read %s: %s", path, exc)
            return []
        for n, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(AuditRecord.model_validate_json(line))
            except (ValueError, TypeError) as exc:
                log.warning("authoring audit: skipping corrupt line %d in %s: %s", n, path, exc)
        return records


# ---------------------------------------------------------------------------
# Replay — re-drive a recorded sequence through the SAME confirm-gated driver
# ---------------------------------------------------------------------------


class ReplayStep(BaseModel):
    """The result of replaying one recorded action through the driver (D7e)."""

    model_config = ConfigDict(extra="forbid")

    action: str
    applied: bool = False
    skipped: bool = False
    error: str | None = None
    summary: str = ""


def replayable(records: list[AuditRecord]) -> list[AuditRecord]:
    """The subset of records worth replaying — the actions that actually applied.

    Skipped / reverted / undone records describe things that did NOT end up in
    the project, so replaying them is meaningless; only ``applied`` records are
    a recorded *sequence of edits*. Order is preserved (oldest-first).
    """
    return [r for r in records if r.outcome == AuditOutcome.APPLIED]


def replay_records(
    driver: AuthoringDriver,
    records: list[AuditRecord],
    *,
    confirm: Any = None,
    fast_mode: bool = False,
) -> list[ReplayStep]:
    """Re-apply a recorded action sequence through the driver (confirm-gated).

    SAFETY: every step goes through :meth:`AuthoringDriver.apply` — the same
    plan → confirm → checkpoint → apply → verify spine as a live edit — never a
    raw re-write. So the D2 confirmation gate, D3 verify (revert-on-failure),
    and D4 checkpoint/undo all hold on replay exactly as on the first run.

    Parameters
    ----------
    confirm:
        Optional callable ``(record, plan) -> bool`` consulted per step before
        apply. When ``None``, a step auto-applies only when ``fast_mode`` is set
        AND the plan is not confirmation-gated — a gated action is never silently
        replayed.
    fast_mode:
        Auto-apply additive+reversible+free steps without a confirm callback.

    A step that fails to plan/apply is recorded (``error``) and the replay
    continues — one bad step doesn't abort the sequence. Replays only the
    ``applied`` records (see :func:`replayable`).
    """
    from movate.authoring.base import AuthoringActionError  # noqa: PLC0415
    from movate.authoring.catalog import UnknownActionError  # noqa: PLC0415
    from movate.authoring.driver import ConfirmationRequiredError  # noqa: PLC0415

    steps: list[ReplayStep] = []
    for record in replayable(records):
        step = ReplayStep(action=record.action)
        try:
            plan = driver.plan(record.action, record.args)
        except (UnknownActionError, AuthoringActionError, ValueError) as exc:
            step.error = f"plan failed: {exc}"
            steps.append(step)
            continue

        if confirm is not None:
            go = bool(confirm(record, plan))
        else:
            go = fast_mode and not plan.requires_confirmation
        if not go:
            step.skipped = True
            steps.append(step)
            continue

        try:
            verify = "network" not in [s.value for s in plan.side_effects]
            outcome = driver.apply(
                record.action,
                record.args,
                confirmed=True,
                fast_mode=fast_mode,
                verify=verify,
            )
        except ConfirmationRequiredError as exc:
            step.skipped = True
            step.error = str(exc)
            steps.append(step)
            continue
        except (AuthoringActionError, ValueError) as exc:
            step.error = f"apply failed: {exc}"
            steps.append(step)
            continue

        reverted = outcome.verify is not None and outcome.verify.reverted
        step.applied = not reverted
        step.skipped = reverted
        step.summary = outcome.result.summary if outcome.result else plan.summary
        if reverted and outcome.verify is not None:
            step.error = f"verify reverted: {outcome.verify.error}"
        steps.append(step)
    return steps


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "AUDIT_LOG_NAME",
    "AuditLog",
    "AuditOutcome",
    "AuditRecord",
    "ReplayStep",
    "replay_records",
    "replayable",
]
