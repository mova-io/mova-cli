"""Voice — speech adapter seams + the pipeline transport (ADR 048, Phase 1).

Voice is **not a new kind of agent**. It is a transport + two adapter seams
that wrap the *unchanged* text Executor (ADR 048 D1): audio in → STT → the
existing agent → TTS → audio out. An agent shipped last month is
voice-capable with **zero edits** to its ``agent.yaml`` / ``prompt.md``.

Phase 1 scope (pipeline mode only):

* the two Protocols — :class:`SpeechToTextProvider` /
  :class:`TextToSpeechProvider` — and their chunk types
  (:class:`TranscriptChunk` / :class:`AudioChunk`), in :mod:`movate.voice.base`;
* OpenAI reference adapters (:class:`OpenAIWhisperSTT` / :class:`OpenAITTS`)
  in :mod:`movate.voice.openai_speech` (the T2 low-friction default), plus the
  T1 low-latency pair (:class:`DeepgramSTT` in :mod:`movate.voice.deepgram` /
  :class:`CartesiaTTS` in :mod:`movate.voice.cartesia`), the T2 premium-voice
  TTS (:class:`ElevenLabsTTS` in :mod:`movate.voice.elevenlabs`), and the T1
  enterprise/sovereignty Azure Speech pair (:class:`AzureSpeechSTT` /
  :class:`AzureNeuralTTS` in :mod:`movate.voice.azure_speech`, against the
  customer's own Azure subscription), each with its provider SDK imported
  lazily so a default install is unaffected (ADR 048 D9);
* test doubles (:class:`FakeSTT` / :class:`FakeTTS`) in
  :mod:`movate.voice.doubles`;
* the pipeline driver (:func:`run_voice_pipeline`) in
  :mod:`movate.voice.pipeline`, which the runtime's WS ``/voice`` route wraps.

Phase 2 (realtime / speech↔speech) adds the optional full-duplex
:class:`RealtimeVoiceProvider` seam + its chunk/event types
(:class:`RealtimeChunk` / :class:`RealtimeEventKind`) in
:mod:`movate.voice.base`, with two first impls — :class:`OpenAIRealtime`
(:mod:`movate.voice.realtime_openai`) and the sovereignty-preserving
:class:`AzureOpenAIRealtime` (:mod:`movate.voice.realtime_azure`, against the
customer's own Azure OpenAI resource) — plus the :class:`FakeRealtime` double.
Realtime is **voice-native**: it does NOT reuse the text Executor (ADR 048
D2b / Boundaries); it is routed by the transport's ``?mode=realtime`` mode
(ADR 050 D12), separate from the pipeline path.

Deferred (out of scope here): telephony (Phase 3) and the agility layer
(router / bench / drift — ADR 049).

The Protocols + chunk types + doubles + pipeline are import-cheap (no optional
deps). The OpenAI / Deepgram / Cartesia / ElevenLabs / Azure Speech / realtime
adapters import their provider SDK lazily; importing them by name here does **not**
trigger that import until the class is constructed.
"""

from __future__ import annotations

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
from movate.voice.cartesia import CartesiaTTS
from movate.voice.deepgram import DeepgramSTT
from movate.voice.doubles import FakeRealtime, FakeSTT, FakeTTS
from movate.voice.elevenlabs import ElevenLabsTTS
from movate.voice.openai_speech import OpenAITTS, OpenAIWhisperSTT
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

__all__ = [
    "AudioChunk",
    "AudioCodec",
    "AzureNeuralTTS",
    "AzureOpenAIRealtime",
    "AzureSpeechSTT",
    "CartesiaTTS",
    "DeepgramSTT",
    "ElevenLabsTTS",
    "FakeRealtime",
    "FakeSTT",
    "FakeTTS",
    "OpenAIRealtime",
    "OpenAITTS",
    "OpenAIWhisperSTT",
    "RealtimeChunk",
    "RealtimeEventKind",
    "RealtimeVoiceProvider",
    "SpeechToTextProvider",
    "TextToSpeechProvider",
    "TranscriptChunk",
    "VoiceEvent",
    "VoicePipelineResult",
    "VoiceTurnLatency",
    "compute_turn_latency",
    "format_latency_badge",
    "run_voice_pipeline",
]
