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
  in :mod:`movate.voice.openai_speech`, with the ``openai`` SDK imported
  lazily so a default install is unaffected (ADR 048 D9);
* test doubles (:class:`FakeSTT` / :class:`FakeTTS`) in
  :mod:`movate.voice.doubles`;
* the pipeline driver (:func:`run_voice_pipeline`) in
  :mod:`movate.voice.pipeline`, which the runtime's WS ``/voice`` route wraps.

Deferred (out of scope here): the full-duplex :class:`RealtimeVoiceProvider`
seam + speech-to-speech mode (ADR 048 D2b / Phase 2), telephony (Phase 3), and
the agility layer (router / bench / drift — ADR 049).

The Protocols + chunk types + doubles + pipeline are import-cheap (no optional
deps). The OpenAI adapters import ``openai`` lazily; importing them by name
here does **not** trigger that import until the class is constructed.
"""

from __future__ import annotations

from movate.voice.base import (
    AudioChunk,
    AudioCodec,
    SpeechToTextProvider,
    TextToSpeechProvider,
    TranscriptChunk,
)
from movate.voice.doubles import FakeSTT, FakeTTS
from movate.voice.openai_speech import OpenAITTS, OpenAIWhisperSTT
from movate.voice.pipeline import VoicePipelineResult, run_voice_pipeline

__all__ = [
    "AudioChunk",
    "AudioCodec",
    "FakeSTT",
    "FakeTTS",
    "OpenAITTS",
    "OpenAIWhisperSTT",
    "SpeechToTextProvider",
    "TextToSpeechProvider",
    "TranscriptChunk",
    "VoicePipelineResult",
    "run_voice_pipeline",
]
