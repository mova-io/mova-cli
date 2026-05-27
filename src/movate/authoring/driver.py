"""The plan -> preview -> apply -> verify spine + checkpoint/undo/history (ADR 025 D2-D4).

:class:`AuthoringDriver` is the safe execution path the thin CLI (and PR3's
planner, PR4's MCP server) drive the catalog through. It owns:

* **D2 — plan→apply with a confirmation gate.** :meth:`plan` returns a
  :class:`~movate.authoring.models.ActionPlan`; :meth:`apply` refuses to run a
  plan whose ``requires_confirmation`` is set unless the caller passes
  ``confirmed=True`` (or opts into ``fast_mode`` for additive+reversible+free
  plans). The LLM proposes; a human / policy gates.
* **D3 — verify-and-self-correct.** After apply, run ``validate`` → ``run
  --mock``; on a ``validate`` failure, REVERT to the pre-apply checkpoint (D4)
  and return the structured error so a caller can re-plan.
* **D4 — checkpoint + undo + history.** Each apply first takes a snapshot
  (reusing ``mdk snapshot`` / content-addressed versioning, ADR 021). The
  action log persists under the project state dir; :meth:`undo` reverts the
  last applied action; :meth:`history` lists the log.

Everything composes shipped primitives — no raw filesystem writes outside the
catalog, no shell, no LLM (D8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from movate.authoring.base import AuthoringAction, AuthoringActionError, AuthoringContext
from movate.authoring.catalog import get_action
from movate.authoring.models import (
    ActionLogEntry,
    ActionPlan,
    ActionResult,
    VerifyReport,
)
from movate.authoring.verify import AgentLoadError, mock_run, validate_agent
from movate.core.paths import project_state_dir
from movate.snapshot import create_snapshot, rollback_to

_LOG_NAME = "authoring_log.jsonl"


class ConfirmationRequiredError(AuthoringActionError):
    """Raised when an apply needs confirmation but none was given (D2)."""


@dataclass
class ApplyOutcome:
    """The full result of a driven apply: plan + result + verify report.

    Returned by :meth:`AuthoringDriver.apply` so a caller sees what was
    planned, what landed, and how verify went (including whether it reverted).
    """

    plan: ActionPlan
    result: ActionResult | None
    verify: VerifyReport | None
    log_entry: ActionLogEntry | None


class AuthoringDriver:
    """Drives the catalog through the safe plan→apply→verify spine.

    Stateless except for the project root + the on-disk action log; safe to
    construct per CLI invocation.
    """

    def __init__(self, ctx: AuthoringContext) -> None:
        self._ctx = ctx
        self._project = ctx.project.resolve()

    # -- D2: plan -------------------------------------------------------------

    def plan(self, action_name: str, args: dict[str, Any]) -> ActionPlan:
        """Validate ``args`` against the action's schema + return its dry-run plan."""
        action = get_action(action_name)
        model = action.args_model.model_validate(args)
        return action.plan(self._ctx, model)

    # -- D2/D3/D4: apply ------------------------------------------------------

    def apply(
        self,
        action_name: str,
        args: dict[str, Any],
        *,
        confirmed: bool = False,
        fast_mode: bool = False,
        verify: bool = True,
    ) -> ApplyOutcome:
        """Plan → gate → checkpoint → apply → verify (revert on failure).

        Parameters
        ----------
        confirmed:
            Caller-supplied explicit yes. Required to apply a plan whose
            ``requires_confirmation`` is set (cost/networked/destructive).
        fast_mode:
            Opt-in auto-apply for additive+reversible+free plans (D2). Has no
            effect on a plan that requires confirmation.
        verify:
            Run the D3 verify loop after apply (default True). Disable for the
            ingest-kb/networked actions where a mock run is meaningless.
        """
        action = get_action(action_name)
        model = action.args_model.model_validate(args)
        plan = action.plan(self._ctx, model)

        if plan.requires_confirmation and not confirmed:
            raise ConfirmationRequiredError(
                f"action {action_name!r} requires confirmation "
                f"(side effects: {[s.value for s in plan.side_effects]}, "
                f"reversible={plan.reversible}); pass confirmed=True"
            )
        # fast_mode only auto-applies additive+reversible+free plans; a plan
        # that requires confirmation is already gated above.
        if not fast_mode and not plan.requires_confirmation and not confirmed:
            # A non-confirmation plan still needs an explicit go from the
            # caller unless they opted into fast mode — the library never
            # silently applies. (The CLI prompts; PR3 decides.)
            raise ConfirmationRequiredError(
                f"action {action_name!r} not confirmed; pass confirmed=True or fast_mode=True"
            )

        # D4 — checkpoint BEFORE the apply (the undo target). Snapshot the
        # current project state via content-addressed versioning (ADR 021).
        checkpoint = create_snapshot(
            project_root=self._project,
            description=f"authoring checkpoint before {action_name}",
            extras={"authoring_action": action_name},
        )

        result = action.apply(self._ctx, model)

        log_entry = ActionLogEntry(
            action=action_name,
            agent=getattr(model, "agent", None),
            args=model.model_dump(mode="json"),
            checkpoint_hash=checkpoint.hash,
            summary=result.summary,
            changed_paths=result.changed_paths,
            created_at=_now(),
        )

        verify_report: VerifyReport | None = None
        if verify:
            verify_report = self._verify(action, log_entry)
            if not verify_report.ok and verify_report.reverted:
                # Reverted — do NOT record this as a successful applied entry.
                return ApplyOutcome(plan=plan, result=result, verify=verify_report, log_entry=None)

        self._append_log(log_entry)
        return ApplyOutcome(plan=plan, result=result, verify=verify_report, log_entry=log_entry)

    # -- D3: verify -----------------------------------------------------------

    def _verify(self, action: AuthoringAction, entry: ActionLogEntry) -> VerifyReport:
        """Run validate → run --mock; revert to the checkpoint on validate failure."""
        steps: list[str] = []
        agent = entry.agent
        if agent is None:
            # No agent in scope (e.g. compose-workflow) — nothing to validate
            # against an agent dir. Treat as a pass (the action's own schema
            # validation already ran at plan time).
            return VerifyReport(ok=True, steps=["no-agent-scope: skipped agent validate"])

        agent_dir = self._ctx.agent_dir(agent)
        steps.append("validate")
        try:
            bundle = validate_agent(agent_dir)
        except AgentLoadError as exc:
            # D4 — validate failed → revert to the pre-apply checkpoint.
            reverted = self._revert_to(entry)
            return VerifyReport(
                ok=False,
                validated=False,
                reverted=reverted,
                error=str(exc),
                steps=steps,
            )

        steps.append("run-mock")
        try:
            mock_ok = mock_run(bundle)
        except Exception as exc:
            # A mock-run failure is a soft signal (the MockProvider's generic
            # output can legitimately miss a strict output schema). We do NOT
            # revert on it — validate (the structural sensor) passed. Report it.
            return VerifyReport(
                ok=False,
                validated=True,
                mock_ran=True,
                mock_ok=False,
                error=f"mock run raised: {exc}",
                steps=steps,
            )

        return VerifyReport(
            ok=mock_ok,
            validated=True,
            mock_ran=True,
            mock_ok=mock_ok,
            error=None if mock_ok else "mock run did not return success",
            steps=steps,
        )

    # -- D4: undo + history ---------------------------------------------------

    def undo(self) -> ActionLogEntry | None:
        """Revert the most recent (not-yet-undone) applied action.

        Restores the pre-action checkpoint via :func:`rollback_to` and deletes
        any files the action *created* that weren't in the checkpoint (rollback
        only restores captured files; it doesn't un-create new ones). Marks the
        log entry ``undone``. Returns the entry undone, or ``None`` if the log
        is empty.
        """
        log = self._read_log()
        target_idx = next((i for i in range(len(log) - 1, -1, -1) if not log[i].undone), None)
        if target_idx is None:
            return None
        entry = log[target_idx]
        self._revert_to(entry)
        entry.undone = True
        log[target_idx] = entry
        self._write_log(log)
        return entry

    def history(self) -> list[ActionLogEntry]:
        """Return the full action log, oldest-first."""
        return self._read_log()

    # -- internals ------------------------------------------------------------

    def _revert_to(self, entry: ActionLogEntry) -> bool:
        """Roll the project back to ``entry``'s checkpoint + drop newly-created files.

        Returns True on success. ``rollback_to`` restores every file captured
        in the checkpoint; files the action created (present in
        ``changed_paths`` but absent from the checkpoint) are removed so the
        revert is exact.
        """
        # Files captured by the checkpoint snapshot — anything the action
        # created that's NOT in this set must be deleted to make the revert
        # exact (rollback only restores, it never un-creates).
        from movate.snapshot import resolve_snapshot  # noqa: PLC0415

        checkpoint = resolve_snapshot(self._project, entry.checkpoint_hash)
        captured = {f.path for f in checkpoint.files}
        rollback_to(project_root=self._project, target_hash=entry.checkpoint_hash)
        for rel in entry.changed_paths:
            if rel not in captured:
                created = self._project / rel
                if created.is_file():
                    created.unlink()
        return True

    def _log_path(self) -> Path:
        return project_state_dir(self._project) / _LOG_NAME

    def _read_log(self) -> list[ActionLogEntry]:
        path = self._log_path()
        if not path.is_file():
            return []
        entries: list[ActionLogEntry] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line:
                entries.append(ActionLogEntry.model_validate_json(line))
        return entries

    def _write_log(self, entries: list[ActionLogEntry]) -> None:
        path = self._log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(e.model_dump_json() for e in entries) + ("\n" if entries else ""),
            encoding="utf-8",
        )

    def _append_log(self, entry: ActionLogEntry) -> None:
        log = self._read_log()
        log.append(entry)
        self._write_log(log)


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ApplyOutcome",
    "AuthoringDriver",
    "ConfirmationRequiredError",
]
