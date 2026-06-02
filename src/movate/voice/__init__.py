"""``movate.voice`` — re-export shim over the extracted ``mdk-voice`` SDK (ADR 067).

Voice was extracted into its own framework-neutral distribution, ``mdk-voice``,
so it can be used **separately from mdk** (e.g. on the Lyzr platform — ADR 069)
and embedded in deliverables that do not run the mdk runtime. This module keeps
the ``from movate.voice import ...`` surface working by re-exporting that package
verbatim, and adds the one mdk-specific piece that cannot live there:
:class:`~movate.voice.executor.ExecutorAgentTurn`, which runs the unchanged mdk
``Executor`` behind ``mdk-voice``'s ``AgentTurn`` seam (ADR 067 D2/D4).

The extracted package also ships the ADR-068 resilient router
(:class:`~mdk_voice.FailoverSTT` / :class:`~mdk_voice.FailoverTTS` /
:class:`~mdk_voice.FailoverRealtime`) and the ADR-069 Lyzr binding
(:class:`~mdk_voice.LyzrAgentTurn` / :func:`~mdk_voice.voice_agent`), all
re-exported here for convenience.
"""

from __future__ import annotations

from mdk_voice import (
    AgentTurn,
    AgentTurnError,
    AgentTurnResult,
    AudioChunk,
    AudioCodec,
    AzureNeuralTTS,
    AzureOpenAIRealtime,
    AzureSpeechSTT,
    CartesiaTTS,
    CircuitBreaker,
    DeepgramSTT,
    ElevenLabsTTS,
    FailoverRealtime,
    FailoverSTT,
    FailoverTTS,
    FakeAgentTurn,
    FakeRealtime,
    FakeSTT,
    FakeTTS,
    InMemoryVoiceCache,
    LyzrAgentTurn,
    NullObserver,
    OpenAIRealtime,
    OpenAITTS,
    OpenAIWhisperSTT,
    RealtimeChunk,
    RealtimeEventKind,
    RealtimeVoiceProvider,
    SpeechToTextProvider,
    StderrObserver,
    TextToSpeechProvider,
    TranscriptChunk,
    VoiceCache,
    VoiceEvent,
    VoiceFailureType,
    VoiceManifest,
    VoiceObserver,
    VoicePipelineResult,
    VoiceProviderError,
    VoiceTurnLatency,
    compute_turn_latency,
    format_latency_badge,
    manifest_for,
    run_voice_pipeline,
    voice_agent,
)

from movate.voice.executor import ExecutorAgentTurn

__all__ = [
    "AgentTurn",
    "AgentTurnError",
    "AgentTurnResult",
    "AudioChunk",
    "AudioCodec",
    "AzureNeuralTTS",
    "AzureOpenAIRealtime",
    "AzureSpeechSTT",
    "CartesiaTTS",
    "CircuitBreaker",
    "DeepgramSTT",
    "ElevenLabsTTS",
    "ExecutorAgentTurn",
    "FailoverRealtime",
    "FailoverSTT",
    "FailoverTTS",
    "FakeAgentTurn",
    "FakeRealtime",
    "FakeSTT",
    "FakeTTS",
    "InMemoryVoiceCache",
    "LyzrAgentTurn",
    "NullObserver",
    "OpenAIRealtime",
    "OpenAITTS",
    "OpenAIWhisperSTT",
    "RealtimeChunk",
    "RealtimeEventKind",
    "RealtimeVoiceProvider",
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
    "compute_turn_latency",
    "format_latency_badge",
    "manifest_for",
    "run_voice_pipeline",
    "voice_agent",
]
