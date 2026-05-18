"""Agent skill backend — calls a deployed MDK agent as a tool.

Fourth backend per ADR 002. Enables "agent-as-tool" cross-agent
orchestration without the v1.1 LangGraph machinery. One agent (the
*orchestrator*) declares a ``kind: agent`` skill; when the LLM invokes
that skill, this backend submits a synchronous job to the *target* agent
and returns its output dict.

Shape, recapped from :class:`SkillImplementation`:

* ``target_agent`` — the agent name to call, as registered in the runtime.
* ``timeout_s`` — per-call timeout in seconds (default 30). The runtime
  honours the calling agent's ``timeouts.call_ms`` as an outer budget;
  ``timeout_s`` is an inner cap specifically for the HTTP round-trip to
  the sub-agent.

Mock mode:

When ``ctx.mock`` is ``True`` (set by ``mdk eval --mock`` or test harness)
the backend short-circuits without hitting a real endpoint. It returns::

    {"_agent_skill_mock": True, **input}

This keeps eval pipelines deterministic and avoids cross-agent
infrastructure dependencies during local development.

Failure → :class:`SkillError` mapping:

* ``target_agent`` missing → caught at SkillSpec parse time (not here)
* ``MovateClientError`` / non-success status → ``backend_error``
* Wall-clock timeout → ``timeout`` (raised by ``dispatch_skill``'s
  ``asyncio.wait_for`` wrapper or by ``MovateClient.wait_for_terminal``)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from movate.core.models import JobKind, SkillImplementationKind
from movate.core.skill_backend.base import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    register_backend,
)

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle


class AgentSkillBackend:
    """Dispatches ``kind: agent`` skills by calling a deployed MDK agent.

    One instance handles every agent-kind skill in the project.
    Stateless — each call submits a fresh job to the target agent via
    :class:`MovateClient` and blocks until it completes.

    The MovateClient is constructed lazily from runtime configuration
    (base URL + API key) on first use. Tests that need to intercept the
    call should swap out this backend via ``register_backend`` with a
    stub, or set ``ctx.mock=True`` to get the pass-through response.
    """

    kind = SkillImplementationKind.AGENT

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        impl = skill.spec.implementation
        target_agent = impl.target_agent  # validated non-empty at SkillSpec parse time
        timeout_s: int = impl.timeout_s if impl.timeout_s is not None else 30

        # ---- Mock short-circuit ----
        # When ctx.mock is True (eval --mock, test harness) we return a
        # deterministic stub without touching a real endpoint. The stub
        # includes the original input so callers can assert on what would
        # have been forwarded.
        if ctx.mock:
            return {"_agent_skill_mock": True, **input}

        # ---- Real dispatch via MovateClient ----
        # Import here (not at module level) so the backend registers at
        # import time without forcing MovateClient's httpx dependency into
        # every process that loads the skill registry — only processes that
        # actually execute an agent-kind skill pay the import cost.
        # Resolve connection details from the runtime configuration.
        # The base URL and API key must be set in the environment; in a
        # deployed movate worker they're injected via secrets. Local
        # development should use --mock to avoid needing a live runtime.
        import os  # noqa: PLC0415

        from movate.core.client import MovateClient, MovateClientError  # noqa: PLC0415

        base_url = os.environ.get("MOVATE_RUNTIME_URL", "")
        api_key = os.environ.get("MOVATE_API_KEY", "")

        if not base_url or not api_key:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"agent skill {skill.spec.name!r}: MOVATE_RUNTIME_URL and "
                    "MOVATE_API_KEY must be set to call a remote agent. "
                    "Use --mock / ctx.mock=True for local evaluation."
                ),
            )

        try:
            async with MovateClient(
                base_url=base_url,
                api_key=api_key,
                timeout=float(timeout_s),
            ) as client:
                # Submit the job and wait for it to reach a terminal state.
                accepted = await client.submit_job(
                    kind=JobKind.AGENT,
                    target=target_agent,
                    input=input,
                )
                job = await asyncio.wait_for(
                    client.wait_for_terminal(accepted.job_id),
                    timeout=float(timeout_s),
                )
        except TimeoutError as exc:
            raise SkillError(
                type=SkillErrorType.TIMEOUT,
                message=(
                    f"agent skill {skill.spec.name!r}: call to agent "
                    f"{target_agent!r} timed out after {timeout_s}s"
                ),
            ) from exc
        except MovateClientError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"agent skill {skill.spec.name!r}: MovateClient error "
                    f"calling agent {target_agent!r}: {exc}"
                ),
            ) from exc

        # Surface non-success terminal states as backend_error.
        if job.status.value != "success":
            err_msg = ""
            if job.error is not None:
                err_msg = f": {job.error.message}"
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"agent skill {skill.spec.name!r}: agent {target_agent!r} "
                    f"returned status {job.status.value!r}{err_msg}"
                ),
            )

        # Retrieve the run output. job.result_run_id is set on success.
        if not job.result_run_id:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"agent skill {skill.spec.name!r}: agent {target_agent!r} "
                    "succeeded but returned no result_run_id"
                ),
            )

        try:
            async with MovateClient(
                base_url=base_url,
                api_key=api_key,
                timeout=float(timeout_s),
            ) as client:
                run = await client.get_run(job.result_run_id)
        except MovateClientError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"agent skill {skill.spec.name!r}: failed to fetch run "
                    f"{job.result_run_id!r} from agent {target_agent!r}: {exc}"
                ),
            ) from exc

        return run.data


# Auto-register on import. The executor + skills_cmd import this module
# for its side-effect of registering with the dispatch table.
register_backend(AgentSkillBackend())
