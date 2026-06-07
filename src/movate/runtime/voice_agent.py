"""``ExecutorAgentTurn`` — the mdk-side adapter that wraps the unchanged
:class:`~movate.core.executor.Executor` as a
:class:`~movate.voice.agent_turn.AgentTurn` (ADR 067 D4).

This is **the only place** in the runtime that imports the agent-runtime types
(:class:`~movate.core.models.RunRequest`, :class:`~movate.core.loader.AgentBundle`)
on the voice path. Everything below it in
:func:`movate.voice.pipeline.run_voice_pipeline` is framework-neutral: the
pipeline only sees the ``AgentTurn`` Protocol, never the executor.

Why an adapter (and not a thicker pipeline) — three boundary rules:

* **CLAUDE.md rule 6 / ADR 048 R2** — the agent stage must not know that the
  text arrived as speech. The adapter passes only the transcript through; the
  executor never learns the modality.
* **ADR 067 D3** — ``run_voice_pipeline`` takes an ``AgentTurn``, not an
  executor. The mapping from transcript → ``RunRequest`` (which agent input
  field to bind to, which tenant to charge) is mdk's concern and lives here.
* **CLAUDE.md rule 5** — the WS contract is unchanged. ``input_key``,
  ``tenant_id``, ``stt/tts_api_key`` all keep their existing meaning; only the
  internal stitching moves.

The adapter is constructed per WS turn (it closes over the bundle, tenant, and
input-key for that session). A non-streaming or error-returning executor is
handled the same way :func:`run_voice_pipeline` used to handle it inline —
errors become :class:`~movate.voice.agent_turn.AgentTurnError` rather than
exceptions, so the pipeline can emit a ``stage="agent"`` degrade event without
catching us.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from movate.voice.agent_turn import AgentTurn, AgentTurnError, AgentTurnResult


class ExecutorAgentTurn(AgentTurn):
    """An :class:`~movate.voice.agent_turn.AgentTurn` over the mdk ``Executor``.

    Owns the transcript→``RunRequest`` binding (``input_key`` selects the agent
    input field) and the per-tenant routing (``tenant_id_override``). The
    executor itself is untouched — it sees the same ``execute(bundle, request,
    on_token=..., tenant_id_override=...)`` call it always did.
    """

    name = "mdk-executor"
    version = "1"
    # ADR 070 D3: a speculative run is cancel-safe here — the Executor's per-turn
    # work (LLM call + token stream) has no irreversible side effect before the
    # first token, and mdk session/memory state is committed out-of-band, not
    # inside this turn. So a discarded speculation leaves nothing behind.
    speculatable = True

    def __init__(
        self,
        *,
        executor: Any,
        bundle: Any,
        tenant_id: str | None = None,
        input_key: str = "text",
        voice_hint: str = "",
    ) -> None:
        self._executor = executor
        self._bundle = bundle
        self._tenant_id = tenant_id
        self._input_key = input_key
        self._voice_hint = voice_hint

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,  # pass-through; executor is language-agnostic
        session_id: str | None = None,  # pass-through; mdk keeps session state out-of-band
    ) -> AgentTurnResult:
        # Lazy imports keep this module loadable in voice-only test contexts
        # where ``movate.core`` may not be fully wired (mirrors ADR 067 D1's
        # lazy-import posture in the voice package).
        from movate.core.models import RunRequest  # noqa: PLC0415

        # Append a voice-context cue so the agent responds conversationally
        # (short sentences, no markdown) instead of its default chat format.
        effective_text = text
        if self._voice_hint:
            effective_text = f"{text}\n\n[Voice channel — {self._voice_hint}]"
        request = RunRequest(
            agent=self._bundle.spec.name,
            input={self._input_key: effective_text},
        )
        try:
            response = await self._executor.execute(
                self._bundle,
                request,
                on_token=on_token,
                tenant_id_override=self._tenant_id,
            )
        except Exception as exc:  # surface as a typed result, not an exception
            return AgentTurnResult(
                status="error",
                error=AgentTurnError(
                    message=str(exc) or exc.__class__.__name__,
                    code="agent_error",
                ),
            )

        if response.status == "error":
            err = response.error
            return AgentTurnResult(
                run_id=response.run_id,
                status=response.status,
                error=AgentTurnError(
                    message=(err.message if err is not None else "run failed"),
                    code=(err.type if err is not None else "agent_error"),
                ),
            )
        return AgentTurnResult(
            answer_text=response.human_readable or "",
            run_id=response.run_id,
            status=response.status,
            # Carry the Executor's trace id (the one persisted on the
            # RunRecord's ``metrics.trace_id``) onto the turn so the voice
            # transport can deep-link to the Langfuse / OTel trace. Empty when
            # tracing is off (SilentTracer) — the surfacing degrades to no link.
            trace_id=response.metrics.trace_id,
        )
