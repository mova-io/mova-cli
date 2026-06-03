"""The ``AgentTurn`` seam — the pipeline's agent stage, framework-neutral.

ADR 067 (D2). The voice pipeline (:func:`movate.voice.pipeline.run_voice_pipeline`)
is *audio → STT → **an agent** → TTS → audio*. The middle stage used to be the
mdk ``Executor``, hard-coded — which is what tied the whole package to mdk. This
module is the seam that removes that tie: the pipeline depends on the tiny
``AgentTurn`` Protocol, and *anything* that turns text into text satisfies it —
the mdk ``Executor`` (via an ``ExecutorAgentTurn`` adapter that lives in mdk), a
Lyzr ADK agent (:mod:`movate.voice.lyzr`, ADR 069), a LangGraph graph, or a bare
async function.

It is the same seam shape as the speech Protocols in :mod:`movate.voice.base`:

* a streaming-friendly callback (``on_token``) so the agent's output streams as
  it is produced — what makes the "agent starts speaking before the full answer
  exists" latency story possible (the pipeline forwards each token as an
  ``agent.token`` event);
* **no audio, no codecs, no mdk types** in the contract — an ``AgentTurn`` only
  ever sees the final transcript text and returns text. An audio concern
  reaching this seam would be a boundary violation (the agent never learns the
  text arrived as speech).

The result envelope (:class:`AgentTurnResult`) carries exactly what the
pipeline's terminal ``done`` / ``error`` events need — the final answer text, a
run id and status for the transport, and a typed error for the graceful-degrade
path (ADR 048 D8) — and nothing more.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentTurnError:
    """A failed agent turn, surfaced by the pipeline as a ``stage="agent"`` error.

    ``code`` mirrors the failure-type string an implementation has on hand (the
    mdk Executor passes its ``RunError.type``); it defaults to a generic
    ``"agent_error"`` for adapters that don't classify failures.
    """

    message: str
    code: str = "agent_error"


@dataclass(frozen=True)
class AgentTurnResult:
    """The outcome of one agent turn (what :func:`run_voice_pipeline` reads back).

    * ``answer_text`` — the human-readable answer to synthesize. May be empty
      when the agent only streamed tokens (the pipeline then falls back to the
      concatenated ``on_token`` deltas), or when ``error`` is set.
    * ``run_id`` / ``status`` — carried straight onto the terminal ``done``
      event so the transport can correlate the turn. ``status`` is the
      implementation's own status string (the mdk Executor uses
      ``"success"`` / ``"error"``); a minimal adapter may just use ``"ok"``.
    * ``error`` — set (and ``status`` non-success) when the turn failed; the
      pipeline emits a ``stage="agent"`` error and synthesizes no audio.
    """

    answer_text: str = ""
    run_id: str = ""
    status: str = "ok"
    error: AgentTurnError | None = None


@runtime_checkable
class AgentTurn(Protocol):
    """Run one text turn: transcript in → streamed text out. Framework-neutral.

    The pipeline's agent stage (ADR 067 D2). Implemented by ``ExecutorAgentTurn``
    (mdk, wrapping the unchanged ``Executor``), :class:`movate.voice.lyzr.LyzrAgentTurn`
    (ADR 069), or any text-in/text-out callable. The implementation owns *how*
    the transcript maps to its agent (mdk binds it to an ``agent.yaml`` input
    field; Lyzr passes it straight to ``agent.run``) — that binding is
    deliberately **not** the pipeline's concern.

    A new agent backend is a **new class implementing this Protocol** — the same
    extension story as adding a speech adapter (:mod:`movate.voice.base`).
    """

    name: str
    version: str

    def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> Awaitable[AgentTurnResult]:
        """Run the agent on ``text`` and return its :class:`AgentTurnResult`.

        ``on_token`` is the streaming hook: an implementation that can stream
        SHOULD call it with each output delta as it is produced (the pipeline
        forwards each as an ``agent.token`` event); a non-streaming agent simply
        omits the per-token calls (or emits the whole answer as one delta) and
        the buffered TTS path still works. ``language`` is an optional BCP-47
        hint; ``session_id`` lets an implementation thread multi-turn state
        (mdk/Lyzr keep their own session/memory — this is a pass-through).

        Implementations MUST NOT raise for an *expected* agent failure — return
        an :class:`AgentTurnResult` with ``error`` set so the pipeline can
        degrade gracefully (ADR 048 D8). An unexpected exception is also caught
        by the pipeline and surfaced as a ``stage="agent"`` error, but a typed
        result is the contract.
        """
        ...
