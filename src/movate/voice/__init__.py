"""mdk-voice — a standalone, framework-neutral voice SDK.

Voice is **not a new kind of agent**. It is a transport + adapter seams that
wrap *any* text agent (ADR 048 D1 / ADR 067): audio in → STT → the agent → TTS →
audio out. The agent stage is the :class:`AgentTurn` seam (ADR 067 D2), so the
same pipeline voices an mdk agent, a Lyzr ADK agent (ADR 069), a LangGraph
graph, or a bare async function — with **zero dependency on mdk**.

This package contains:

* the **agent seam** — :class:`AgentTurn` / :class:`AgentTurnResult` /
  :class:`AgentTurnError` (:mod:`movate.voice.agent_turn`);
* the two **speech seams** — :class:`SpeechToTextProvider` /
  :class:`TextToSpeechProvider` — and the optional full-duplex
  :class:`RealtimeVoiceProvider`, plus their chunk types
  (:mod:`movate.voice.base`);
* reference **adapters**, each importing its provider SDK lazily so a base
  install (no extras) pulls only ``pydantic``: OpenAI Whisper/TTS
  (:mod:`movate.voice.openai_speech`), the low-latency Deepgram STT /
  Cartesia TTS pair (:mod:`movate.voice.deepgram` / :mod:`movate.voice.cartesia`),
  ElevenLabs TTS (:mod:`movate.voice.elevenlabs`), the Azure Speech sovereignty
  pair (:mod:`movate.voice.azure_speech`), and the realtime providers
  (:mod:`movate.voice.realtime_openai` / :mod:`movate.voice.realtime_azure`);
* the **pipeline driver** (:func:`run_voice_pipeline`) and its event/latency
  types (:mod:`movate.voice.pipeline`);
* **test doubles** (:class:`FakeSTT` / :class:`FakeTTS` / :class:`FakeAgentTurn`
  / :class:`FakeRealtime`) in :mod:`movate.voice.doubles`.

The seams + chunk types + doubles + pipeline are import-cheap (no optional
deps). Every provider adapter imports its SDK lazily; importing it by name here
does **not** trigger that import until the class is constructed.
"""

from __future__ import annotations

from movate.voice.agent_turn import AgentTurn, AgentTurnError, AgentTurnResult
from movate.voice.azure_speech import AzureNeuralTTS, AzureSpeechSTT
from movate.voice.base import (
    AudioChunk,
    AudioCodec,
    RealtimeChunk,
    RealtimeEventKind,
    RealtimeVoiceProvider,
    SpeechToTextProvider,
    TextToSpeechProvider,
    TranscriptChunk,
)
from movate.voice.bench import STTBenchItem, STTBenchReport, bench_stt, word_error_rate
from movate.voice.breaker import CircuitBreaker
from movate.voice.cache import InMemoryVoiceCache, VoiceCache, cache_key, warm_cache
from movate.voice.cartesia import CartesiaTTS
from movate.voice.cartesia_stt import CartesiaSTT
from movate.voice.chunking import SentenceChunker
from movate.voice.deepgram import DeepgramSTT
from movate.voice.deepgram_tts import DeepgramAuraTTS
from movate.voice.doubles import FakeAgentTurn, FakeRealtime, FakeSTT, FakeTTS
from movate.voice.elevenlabs import ElevenLabsTTS
from movate.voice.failover import FailoverRealtime, FailoverSTT, FailoverTTS
from movate.voice.failures import (
    DEFAULT_RETRY,
    RetryRule,
    VoiceFailureType,
    VoiceProviderError,
    classify,
)
from movate.voice.langgraph_adapter import LangGraphAgentTurn, voice_agent_langgraph
from movate.voice.lyzr import LyzrAgentTurn, voice_agent
from movate.voice.lyzr_parity import (
    LYZR_PROVIDER_MAP,
    LYZR_VOICE_BASE,
    LyzrProvider,
    ParityReport,
    check_lyzr_parity,
    check_parity,
    fetch_lyzr_voice_options,
    format_parity_report,
)
from movate.voice.manifest import DEFAULT_MANIFESTS, VoiceManifest, manifest_for
from movate.voice.observer import MetricsObserver, NullObserver, StderrObserver, VoiceObserver
from movate.voice.openai_speech import OpenAITTS, OpenAIWhisperSTT
from movate.voice.pii import redact_pii
from movate.voice.pipeline import (
    VoiceEvent,
    VoicePipelineResult,
    VoiceTurnLatency,
    compute_turn_latency,
    format_latency_badge,
    run_voice_pipeline,
)
from movate.voice.realtime_azure import AzureOpenAIRealtime
from movate.voice.realtime_openai import OpenAIRealtime
from movate.voice.speakify import speakify
from movate.voice.stt_wrappers import ConfidenceGatedSTT, SilenceGatedSTT
from movate.voice.telephony import (
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    pcm16_to_wav,
    resample_pcm16,
    telephony_inbound,
    telephony_outbound,
)
from movate.voice.vad import frame_rms, is_silent

__all__ = [
    "DEFAULT_MANIFESTS",
    "DEFAULT_RETRY",
    "LYZR_PROVIDER_MAP",
    "LYZR_VOICE_BASE",
    "AgentTurn",
    "AgentTurnError",
    "AgentTurnResult",
    "AudioChunk",
    "AudioCodec",
    "AzureNeuralTTS",
    "AzureOpenAIRealtime",
    "AzureSpeechSTT",
    "CartesiaSTT",
    "CartesiaTTS",
    "CircuitBreaker",
    "ConfidenceGatedSTT",
    "DeepgramAuraTTS",
    "DeepgramSTT",
    "ElevenLabsTTS",
    "FailoverRealtime",
    "FailoverSTT",
    "FailoverTTS",
    "FakeAgentTurn",
    "FakeRealtime",
    "FakeSTT",
    "FakeTTS",
    "InMemoryVoiceCache",
    "LangGraphAgentTurn",
    "LyzrAgentTurn",
    "LyzrProvider",
    "MetricsObserver",
    "NullObserver",
    "OpenAIRealtime",
    "OpenAITTS",
    "OpenAIWhisperSTT",
    "ParityReport",
    "RealtimeChunk",
    "RealtimeEventKind",
    "RealtimeVoiceProvider",
    "RetryRule",
    "STTBenchItem",
    "STTBenchReport",
    "SentenceChunker",
    "SilenceGatedSTT",
    "SpeechToTextProvider",
    "StderrObserver",
    "TextToSpeechProvider",
    "TranscriptChunk",
    "VoiceCache",
    "VoiceEvent",
    "VoiceFailureType",
    "VoiceManifest",
    "VoiceObserver",
    "VoicePipelineResult",
    "VoiceProviderError",
    "VoiceTurnLatency",
    "bench_stt",
    "cache_key",
    "check_lyzr_parity",
    "check_parity",
    "classify",
    "compute_turn_latency",
    "fetch_lyzr_voice_options",
    "format_latency_badge",
    "format_parity_report",
    "frame_rms",
    "is_silent",
    "manifest_for",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
    "pcm16_to_wav",
    "redact_pii",
    "resample_pcm16",
    "run_voice_pipeline",
    "speakify",
    "telephony_inbound",
    "telephony_outbound",
    "voice_agent",
    "voice_agent_langgraph",
    "warm_cache",
    "word_error_rate",
]
