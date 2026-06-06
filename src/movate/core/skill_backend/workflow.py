"""WorkflowDispatchSkillBackend — ``kind: workflow`` (ADR 077 D1/D2).

Lets a *thin* conversational agent HAND OFF a multi-step, branch-on-result
procedure (check → reboot → re-check → decide → resolve|escalate) to the
deterministic workflow engine, instead of orchestrating it itself — the
empirically-unreliable path the POS-reboot demo surfaced (a single agent
mis-passed a tool argument and reported a still-offline terminal as "resolved").

Mirrors :class:`~movate.core.skill_backend.agent.AgentSkillBackend`: each call
submits a ``JobKind.WORKFLOW`` run for the skill's ``target_workflow`` via
:class:`MovateClient`. Resolution flows through the runtime's existing dispatch
fork (native / Temporal, ADR 055) — the worker picks the backend; this skill
adds no selection logic.

Two modes (ADR 077 D2), read from the call ``input``:

* ``await`` (default) — block until the run reaches a terminal state, then
  return its ``status`` + ``final_state``. A HITL pause (``status == "paused"``)
  is a *successful* terminal outcome for the handoff: the workflow stopped at a
  HUMAN gate; the agent narrates "escalated, a human will follow up".
* ``detach`` — return the ``run_id`` immediately for long/multi-day flows.

Failure → :class:`SkillError`: missing connection config / client error →
``BACKEND_ERROR``; wait timeout → ``TIMEOUT``; non-success terminal job →
``BACKEND_ERROR``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from movate.core.models import JobKind, SkillImplementationKind
from movate.core.skill_backend.base import (
    SkillError,
    SkillErrorType,
    register_backend,
)

if TYPE_CHECKING:
    from movate.core.skill_backend.base import SkillExecutionContext
    from movate.core.skill_loader import SkillBundle


class WorkflowDispatchSkillBackend:
    """Dispatches ``kind: workflow`` skills by submitting a workflow run.

    One stateless instance handles every workflow-kind skill in the project.
    The :class:`MovateClient` is constructed lazily from runtime configuration
    (base URL + API key) on first use; tests pass ``ctx.mock=True`` for a
    deterministic pass-through.
    """

    kind = SkillImplementationKind.WORKFLOW

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        impl = skill.spec.implementation
        target_workflow = impl.target_workflow  # validated non-empty at parse time
        timeout_s: int = impl.timeout_s if impl.timeout_s is not None else 30
        mode = str(input.get("mode", "await")).strip().lower() or "await"
        # The initial workflow state is the input minus the control key.
        initial_state = {k: v for k, v in input.items() if k != "mode"}

        # ---- Mock short-circuit (eval --mock / tests) ----
        if ctx.mock:
            return {"_workflow_skill_mock": True, "mode": mode, **initial_state}

        # The SkillSpec validator guarantees target_workflow for kind=workflow;
        # guard here too so a malformed bundle fails loud (and narrows the type).
        if not target_workflow:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"workflow skill {skill.spec.name!r}: "
                    "implementation.target_workflow is empty"
                ),
            )

        # ---- Real dispatch via MovateClient (lazy import, mirrors agent.py) ----
        import asyncio  # noqa: PLC0415
        import os  # noqa: PLC0415

        from movate.core.client import MovateClient, MovateClientError  # noqa: PLC0415

        base_url = os.environ.get("MOVATE_RUNTIME_URL", "")
        api_key = os.environ.get("MOVATE_API_KEY", "")
        if not base_url or not api_key:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"workflow skill {skill.spec.name!r}: MOVATE_RUNTIME_URL and "
                    "MOVATE_API_KEY must be set to dispatch a workflow. "
                    "Use --mock / ctx.mock=True for local evaluation."
                ),
            )

        # ADR 024 — ``workflow.dispatch`` child span under the skill span.
        _span = None
        _t0 = 0.0
        if ctx.tracer is not None:
            _t0 = time.monotonic()
            _span = ctx.tracer.start_span(
                "workflow.dispatch",
                {"skill": skill.spec.name, "target_workflow": target_workflow, "mode": mode},
                parent=ctx.parent_span,
            )

        try:
            try:
                async with MovateClient(
                    base_url=base_url, api_key=api_key, timeout=float(timeout_s)
                ) as client:
                    accepted = await client.submit_job(
                        kind=JobKind.WORKFLOW, target=target_workflow, input=initial_state
                    )
                    if mode == "detach":
                        result = {
                            "run_id": accepted.job_id,
                            "status": "dispatched",
                            "summary": (
                                f"Workflow {target_workflow!r} started "
                                f"(run {accepted.job_id}); it's in progress."
                            ),
                        }
                    else:
                        job = await asyncio.wait_for(
                            client.wait_for_terminal(accepted.job_id), timeout=float(timeout_s)
                        )
                        if job.status.value != "success":
                            err_msg = f": {job.error.message}" if job.error is not None else ""
                            raise SkillError(
                                type=SkillErrorType.BACKEND_ERROR,
                                message=(
                                    f"workflow skill {skill.spec.name!r}: run of "
                                    f"{target_workflow!r} returned status "
                                    f"{job.status.value!r}{err_msg}"
                                ),
                            )
                        result = await self._fetch_outcome(
                            client, job.result_run_id, target_workflow, skill.spec.name
                        )
            except TimeoutError as exc:
                raise SkillError(
                    type=SkillErrorType.TIMEOUT,
                    message=(
                        f"workflow skill {skill.spec.name!r}: run of "
                        f"{target_workflow!r} timed out after {timeout_s}s"
                    ),
                ) from exc
            except MovateClientError as exc:
                raise SkillError(
                    type=SkillErrorType.BACKEND_ERROR,
                    message=(
                        f"workflow skill {skill.spec.name!r}: MovateClient error "
                        f"dispatching {target_workflow!r}: {exc}"
                    ),
                ) from exc

            if _span is not None and ctx.tracer is not None:
                ctx.tracer.set_attribute(
                    _span, "latency_ms", round((time.monotonic() - _t0) * 1000, 1)
                )
                ctx.tracer.end_span(_span, status="ok")
            return result
        except Exception:
            if _span is not None and ctx.tracer is not None:
                ctx.tracer.set_attribute(
                    _span, "latency_ms", round((time.monotonic() - _t0) * 1000, 1)
                )
                ctx.tracer.end_span(_span, status="error")
            raise

    async def _fetch_outcome(
        self, client: Any, run_id: str | None, target_workflow: str, skill_name: str
    ) -> dict[str, Any]:
        """Resolve the finished run's outcome (status + final_state) for the agent
        to narrate. Uses ``list_workflow_runs`` (the just-dispatched run is the
        most recent) — no single-run getter exists on the client yet."""
        if not run_id:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"workflow skill {skill_name!r}: {target_workflow!r} succeeded "
                    "but returned no workflow_run_id"
                ),
            )
        listing = await client.list_workflow_runs(limit=100)
        match = next((r for r in listing.workflow_runs if r.workflow_run_id == run_id), None)
        if match is None:
            # Run completed but rolled out of the recent window — report the handle.
            return {"run_id": run_id, "status": "completed", "state": {}}
        status = getattr(match.status, "value", str(match.status))
        out: dict[str, Any] = {
            "run_id": run_id,
            "status": status,
            "state": match.final_state or {},
        }
        if status == "paused" and match.human_task:
            out["human_task"] = match.human_task
            out["summary"] = "Escalated to a human — awaiting their response."
        return out


# Auto-register on import. The executor + skills_cmd import this module for its
# side-effect of registering with the dispatch table.
register_backend(WorkflowDispatchSkillBackend())
