"""``ExecutorAgentTurn`` — run the unchanged mdk Executor behind the voice seam.

ADR 067 (D4). The extracted ``mdk-voice`` package drives its pipeline through the
framework-neutral :class:`mdk_voice.AgentTurn` seam (transcript in → text out),
which knows nothing about mdk. This adapter is the one mdk-specific binding: it
maps that seam onto the existing :meth:`Executor.execute` call — the *same* call
the SSE streaming route makes — so an existing text agent is voice-capable with
**zero changes** and the Executor stays modality-blind (CLAUDE.md rule 6 / ADR
048 R2).

It is the only place ``AgentBundle`` / ``RunRequest`` / ``executor.execute`` live
in the voice path after the extraction; everything else imports from
``mdk_voice`` via the :mod:`movate.voice` re-export shim.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mdk_voice import AgentTurnError, AgentTurnResult


class ExecutorAgentTurn:
    """Adapt the mdk :class:`~movate.core.executor.Executor` to ``AgentTurn``.

    ``input_key`` is the agent-input field the transcript is bound to (the one
    zero-change knob the transport passes through, default ``"text"``);
    ``tenant_id`` is forwarded to the run so persistence/metering are identical to
    a non-voice run. A ``status == "error"`` run is returned as a typed
    :class:`~mdk_voice.AgentTurnResult` error (the pipeline surfaces a
    ``stage="agent"`` event); an unexpected exception is left to propagate and is
    caught by the pipeline's own agent-stage guard (same degrade either way).
    """

    name = "mdk-executor"
    version = "1"

    def __init__(
        self, executor: Any, bundle: Any, *, tenant_id: str, input_key: str = "text"
    ) -> None:
        self._executor = executor
        self._bundle = bundle
        self._tenant_id = tenant_id
        self._input_key = input_key

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurnResult:
        from movate.core.models import RunRequest  # noqa: PLC0415 - lazy: keep import light

        run_request = RunRequest(agent=self._bundle.spec.name, input={self._input_key: text})
        response = await self._executor.execute(
            self._bundle,
            run_request,
            on_token=on_token,
            tenant_id_override=self._tenant_id,
        )
        if response.status == "error":
            err = response.error
            return AgentTurnResult(
                run_id=response.run_id,
                status="error",
                error=AgentTurnError(
                    message=err.message if err is not None else "run failed",
                    code=err.type if err is not None else "agent_error",
                ),
            )
        return AgentTurnResult(
            answer_text=response.human_readable or "",
            run_id=response.run_id,
            status=response.status,
        )
