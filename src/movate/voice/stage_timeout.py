"""Per-stage voice timeouts + partial-failure degradation (#210).

Hard per-stage timeouts for the voice pipeline — STT, LLM/Executor, TTS —
with graceful degradation so the user NEVER hears dead silence.  Every failure
path produces a user-visible response (text or audio).

Timeouts are configurable via environment variables:

* ``VOICE_STT_TIMEOUT_S`` — STT stage (default 30 s)
* ``VOICE_LLM_TIMEOUT_S`` — LLM/Executor stage (default 60 s)
* ``VOICE_TTS_TIMEOUT_S`` — TTS stage (default 30 s)
* ``VOICE_HEARTBEAT_INTERVAL_S`` — "still working" cue interval (default 5 s)

Design (CLAUDE.md rule 6 — extend, don't hardcode):

* The timeout values live here as named config; the pipeline's existing
  ``agent_timeout`` already uses ``asyncio.wait_for``.  This module provides
  the wrappers + degradation logic the transport wires in.
* The heartbeat is a transport-layer concern: it sends a control frame while
  any stage is in progress, so the client knows the server is alive.
* Degradation messages follow the "never dead silence" principle:
    - STT timeout  → ``"transcription timed out"`` error frame, turn closed.
    - LLM timeout  → ``"I'm taking longer than expected — please try again"``
      as a text response (synthesized if TTS is up, text-only if not).
    - TTS timeout  → text-only response with a note.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

# ── Configurable defaults ────────────────────────────────────────────────
_DEFAULT_STT_TIMEOUT: float = float(os.environ.get("VOICE_STT_TIMEOUT_S", "30"))
_DEFAULT_LLM_TIMEOUT: float = float(os.environ.get("VOICE_LLM_TIMEOUT_S", "60"))
_DEFAULT_TTS_TIMEOUT: float = float(os.environ.get("VOICE_TTS_TIMEOUT_S", "30"))
_DEFAULT_HEARTBEAT_INTERVAL: float = float(os.environ.get("VOICE_HEARTBEAT_INTERVAL_S", "5"))

# Degradation messages — the user-facing strings for each failure path.
STT_TIMEOUT_MSG = "Transcription timed out — please try again."
LLM_TIMEOUT_MSG = "I'm taking longer than expected — please try again."
TTS_TIMEOUT_MSG = "Audio synthesis unavailable — here is the text response."


@dataclass(frozen=True)
class StageTimeouts:
    """Per-stage timeout configuration for one voice session.

    All values are in seconds.  ``0`` or negative disables the timeout for
    that stage (the stage runs unbounded — not recommended in production).
    """

    stt: float = _DEFAULT_STT_TIMEOUT
    llm: float = _DEFAULT_LLM_TIMEOUT
    tts: float = _DEFAULT_TTS_TIMEOUT
    heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL

    @classmethod
    def from_env(cls) -> StageTimeouts:
        """Build from environment variables, falling back to defaults."""
        return cls(
            stt=float(os.environ.get("VOICE_STT_TIMEOUT_S", str(_DEFAULT_STT_TIMEOUT))),
            llm=float(os.environ.get("VOICE_LLM_TIMEOUT_S", str(_DEFAULT_LLM_TIMEOUT))),
            tts=float(os.environ.get("VOICE_TTS_TIMEOUT_S", str(_DEFAULT_TTS_TIMEOUT))),
            heartbeat_interval=float(
                os.environ.get("VOICE_HEARTBEAT_INTERVAL_S", str(_DEFAULT_HEARTBEAT_INTERVAL))
            ),
        )

    def effective_stt(self) -> float | None:
        """Return the STT timeout or None if disabled."""
        return self.stt if self.stt > 0 else None

    def effective_llm(self) -> float | None:
        """Return the LLM timeout or None if disabled."""
        return self.llm if self.llm > 0 else None

    def effective_tts(self) -> float | None:
        """Return the TTS timeout or None if disabled."""
        return self.tts if self.tts > 0 else None


@dataclass(frozen=True)
class StageTimeoutResult:
    """The outcome of a stage that may have timed out.

    ``timed_out`` is True when the stage was cancelled via ``asyncio.wait_for``.
    ``stage`` identifies which stage timed out (``"stt"`` / ``"llm"`` / ``"tts"``).
    ``degradation_message`` is the user-facing fallback text.
    """

    timed_out: bool
    stage: str = ""
    degradation_message: str = ""


class Heartbeat:
    """A "still working" heartbeat that fires periodically during a stage.

    The transport creates one per turn and starts it before each stage.  It
    calls ``send_fn`` every ``interval_s`` seconds until stopped.  The send
    function is expected to push a small control frame onto the WS (e.g.
    ``{"type": "heartbeat", "stage": "stt"}``).

    Safe to stop multiple times or to never start (no-op).
    """

    def __init__(
        self,
        send_fn: Any,
        interval_s: float = _DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._send_fn = send_fn
        self._interval_s = max(0.01, interval_s)
        self._task: asyncio.Task[None] | None = None
        self._stage: str = ""

    def start(self, stage: str) -> None:
        """Begin heartbeating for ``stage``."""
        self.stop()
        self._stage = stage
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        """Stop the heartbeat (idempotent)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                try:
                    await self._send_fn(self._stage)
                except Exception:
                    # The WS may have closed — stop silently.
                    return
        except asyncio.CancelledError:
            return


def degradation_for_stage(stage: str) -> str:
    """Return the user-facing degradation message for a timed-out stage."""
    return {
        "stt": STT_TIMEOUT_MSG,
        "llm": LLM_TIMEOUT_MSG,
        "tts": TTS_TIMEOUT_MSG,
    }.get(stage, f"Stage '{stage}' timed out.")
