"""Voice cost computation — STT + TTS cost per turn (ADR 050 D7).

Computes the voice-specific cost components that flow into the metering
envelope alongside the LLM ``metrics.cost_usd``:

* **STT cost** = ``audio_duration_s * rate_per_second``
* **TTS cost** = ``answer_chars * rate_per_char``

Provider rates are env-configurable so operators can match their actual
provider contract:

    VOICE_COST_PER_STT_SECOND=0.006   # Deepgram Nova-2 pay-as-you-go
    VOICE_COST_PER_TTS_CHAR=0.000015  # ElevenLabs Turbo v2.5

When not set, sensible defaults are used (Deepgram Nova-2 / ElevenLabs
Turbo rates as of 2025). The cost is an *estimate* — same caveat as the
LLM cost from ``pricing.yaml`` (see ``build_usage`` docstring in
``core/reporting.py``).

Pure: no I/O, no provider SDK imports. Tests can assert exact numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Default provider rates (USD). Operators override via env vars to match
# their actual contract. These are representative 2025 rates:
#   STT: Deepgram Nova-2 pay-as-you-go = $0.0043/s ≈ $0.006/s with overhead
#   TTS: ElevenLabs Turbo v2.5 ~$0.000015/char (scale tier)
_DEFAULT_STT_RATE_PER_SECOND = 0.006
_DEFAULT_TTS_RATE_PER_CHAR = 0.000015


def _stt_rate() -> float:
    """STT cost per second of audio (USD). Env-configurable."""
    raw = os.environ.get("VOICE_COST_PER_STT_SECOND")
    if raw is not None:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return _DEFAULT_STT_RATE_PER_SECOND


def _tts_rate() -> float:
    """TTS cost per character synthesized (USD). Env-configurable."""
    raw = os.environ.get("VOICE_COST_PER_TTS_CHAR")
    if raw is not None:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return _DEFAULT_TTS_RATE_PER_CHAR


@dataclass(frozen=True)
class VoiceTurnCost:
    """Itemized voice cost for one turn (ADR 050 D7 three-stage cost)."""

    stt_cost_usd: float
    tts_cost_usd: float
    llm_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        """STT + TTS + LLM = total voice turn cost."""
        return self.stt_cost_usd + self.tts_cost_usd + self.llm_cost_usd


def compute_voice_turn_cost(
    *,
    audio_duration_s: float,
    answer_chars: int,
    llm_cost_usd: float,
) -> VoiceTurnCost:
    """Compute the three-stage cost for one voice turn.

    Pure and side-effect-free. ``audio_duration_s`` and ``answer_chars``
    come from the pipeline result; ``llm_cost_usd`` from the Executor's
    ``RunResponse.metrics.cost_usd``.
    """
    stt = round(audio_duration_s * _stt_rate(), 8)
    tts = round(answer_chars * _tts_rate(), 8)
    return VoiceTurnCost(
        stt_cost_usd=stt,
        tts_cost_usd=tts,
        llm_cost_usd=llm_cost_usd,
    )


__all__ = [
    "VoiceTurnCost",
    "compute_voice_turn_cost",
]
