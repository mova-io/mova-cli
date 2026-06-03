"""Capability manifests for voice providers (ADR 068 D2 / ADR 049 D1).

A manifest is the declarative data the router reads to **order** providers
(latency-first) and to **bound cost** (D4): latency tier, price, languages,
streaming, region-sovereignty. It is keyed by a provider's ``name`` (the same
string the adapters expose), so attaching a manifest to a bundled adapter needs
no edit to the adapter itself.

The figures here are **indicative defaults**, not a price sheet: ADR 068 D2/D4
notes vendor pricing/latency drift, and the observer hook (D7) feeds real metered
cost back under mdk. A caller can always pass an explicit manifest to override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VoiceKind = Literal["stt", "tts", "realtime"]


@dataclass(frozen=True)
class VoiceManifest:
    """What a provider can do, and roughly what it costs.

    * ``latency_tier`` — 1 = lowest latency; higher = slower. The router orders
      ascending (latency-first, ADR 068 D2).
    * ``cost_per_min`` — STT price ($/audio-minute), if known.
    * ``cost_per_char`` — TTS price ($/character), if known.
    * ``languages`` — BCP-47 tags the provider supports; empty = many/unknown.
    * ``streaming`` — emits partials/early audio (vs. buffered whole-utterance).
    * ``sovereign`` — runs in the customer's region / on-prem (data residency).
    """

    provider: str
    kind: VoiceKind
    latency_tier: int = 2
    cost_per_min: float | None = None
    cost_per_char: float | None = None
    languages: tuple[str, ...] = field(default_factory=tuple)
    streaming: bool = True
    sovereign: bool = False


# Indicative manifests for the bundled adapters, keyed by adapter ``.name``.
# Latency tiers encode the ADR 068 D2 default ordering: T1 low-latency
# (Deepgram/Cartesia/Azure) ahead of T2 (OpenAI/ElevenLabs).
DEFAULT_MANIFESTS: dict[str, VoiceManifest] = {
    # ── STT ──
    "deepgram": VoiceManifest("deepgram", "stt", latency_tier=1, cost_per_min=0.0043),
    "cartesia_stt": VoiceManifest(
        "cartesia_stt",
        "stt",
        latency_tier=1,
        cost_per_min=0.013,
        languages=("en", "es", "fr", "de", "zh", "ja", "pt", "it", "hi", "ko"),
    ),
    "azure_speech_stt": VoiceManifest(
        "azure_speech_stt", "stt", latency_tier=1, cost_per_min=0.0167, sovereign=True
    ),
    "openai_whisper": VoiceManifest(
        "openai_whisper", "stt", latency_tier=2, cost_per_min=0.006, streaming=False
    ),
    # ── TTS ──
    "cartesia": VoiceManifest("cartesia", "tts", latency_tier=1, cost_per_char=0.000035),
    # Deepgram Aura 2 — ~$0.030 / 1k chars (= 0.00003 / char), streaming-native,
    # T1 alongside Cartesia. Pricing per Deepgram's published Aura 2 pay-as-you-go rate.
    "deepgram_aura": VoiceManifest("deepgram_aura", "tts", latency_tier=1, cost_per_char=0.00003),
    "azure_neural_tts": VoiceManifest(
        "azure_neural_tts", "tts", latency_tier=1, cost_per_char=0.000016, sovereign=True
    ),
    "openai_tts": VoiceManifest("openai_tts", "tts", latency_tier=2, cost_per_char=0.000015),
    "elevenlabs": VoiceManifest("elevenlabs", "tts", latency_tier=2, cost_per_char=0.00018),
}


def manifest_for(provider: str) -> VoiceManifest | None:
    """Look up a bundled adapter's default manifest by its ``name`` (or ``None``)."""
    return DEFAULT_MANIFESTS.get(provider)
