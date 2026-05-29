"""Voice adapter-seam conformance (ADR 048 D3, Phase 1).

The two Protocols — ``SpeechToTextProvider`` / ``TextToSpeechProvider`` — and
their chunk types are the seams the whole pipeline hangs off. These tests pin:

* the chunk dataclasses' shape (``TranscriptChunk.is_final`` endpointing;
  ``AudioChunk`` codec/sample-rate) and ``extra="forbid"`` strictness;
* runtime-checkable conformance of the fakes AND the OpenAI reference
  adapters against the Protocols (so a future provider can be checked the
  same way ``isinstance(p, BaseLLMProvider)`` works);
* the fakes' streaming behavior (partials then a final; text round-trips
  through TTS bytes);
* the OpenAI adapters' lazy SDK import (constructing them with an injected
  fake client does NOT require the ``openai`` package) + their buffered →
  single-final-chunk / chunked-audio behavior.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from movate.voice import (
    AudioChunk,
    FakeSTT,
    FakeTTS,
    OpenAITTS,
    OpenAIWhisperSTT,
    SpeechToTextProvider,
    TextToSpeechProvider,
    TranscriptChunk,
)


async def _audio_stream(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


async def _text_stream(*parts: str) -> AsyncIterator[str]:
    for p in parts:
        yield p


# ---------------------------------------------------------------------------
# Chunk types
# ---------------------------------------------------------------------------


def test_transcript_chunk_shape_and_strictness() -> None:
    c = TranscriptChunk(text="hi", is_final=True, confidence=0.9)
    assert c.text == "hi"
    assert c.is_final is True
    assert c.confidence == 0.9
    # Defaults: confidence optional.
    assert TranscriptChunk(text="x", is_final=False).confidence is None
    # extra="forbid" — a typo'd field is rejected, not silently dropped.
    with pytest.raises(Exception):
        TranscriptChunk(text="x", is_final=True, finall=True)  # type: ignore[call-arg]


def test_audio_chunk_defaults_and_codec() -> None:
    c = AudioChunk(data=b"\x00\x01")
    assert c.data == b"\x00\x01"
    assert c.codec == "pcm16"
    assert c.sample_rate == 24_000
    mulaw = AudioChunk(data=b"\x00", codec="mulaw", sample_rate=8000)
    assert mulaw.codec == "mulaw"
    assert mulaw.sample_rate == 8000


# ---------------------------------------------------------------------------
# Protocol conformance (runtime_checkable)
# ---------------------------------------------------------------------------


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeSTT(), SpeechToTextProvider)
    assert isinstance(FakeTTS(), TextToSpeechProvider)


def test_openai_adapters_satisfy_protocols() -> None:
    # Constructing with a None client must NOT import the openai SDK (lazy).
    assert isinstance(OpenAIWhisperSTT(), SpeechToTextProvider)
    assert isinstance(OpenAITTS(), TextToSpeechProvider)


# ---------------------------------------------------------------------------
# Fake doubles — streaming behavior
# ---------------------------------------------------------------------------


async def test_fake_stt_emits_partials_then_final_and_records_audio() -> None:
    stt = FakeSTT("the full utterance", partials=["the", "the full"])
    chunks = [
        c async for c in stt.transcribe(_audio_stream(b"aa", b"bb"), language="en-US", api_key="k")
    ]
    assert [c.text for c in chunks] == ["the", "the full", "the full utterance"]
    assert [c.is_final for c in chunks] == [False, False, True]
    # It drained + recorded every inbound audio frame.
    assert stt.received == [b"aa", b"bb"]
    assert stt.languages == ["en-US"]
    assert stt.api_keys == ["k"]


async def test_fake_tts_round_trips_text_to_audio_bytes() -> None:
    tts = FakeTTS()
    audio = [c async for c in tts.synthesize(_text_stream("hello ", "world"), voice_id="rachel")]
    joined = b"".join(c.data for c in audio)
    assert joined.decode("utf-8") == "hello world"
    assert tts.spoken == ["hello world"]
    assert tts.voice_ids == ["rachel"]


async def test_fake_tts_multi_frame_split() -> None:
    tts = FakeTTS(frames=3)
    audio = [c async for c in tts.synthesize(_text_stream("abcdef"))]
    assert len(audio) >= 2  # split into multiple frames
    assert b"".join(c.data for c in audio).decode() == "abcdef"


async def test_fake_tts_empty_text_yields_no_audio() -> None:
    tts = FakeTTS()
    audio = [c async for c in tts.synthesize(_text_stream("", ""))]
    assert audio == []


# ---------------------------------------------------------------------------
# OpenAI reference adapters — with an injected fake client (no SDK, no network)
# ---------------------------------------------------------------------------


class _FakeTranscriptions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, *, model: str, file, **kwargs):
        self.calls.append({"model": model, "name": file.name, **kwargs})

        class _R:
            text = "transcribed text"

        return _R()


class _FakeAudioNamespace:
    def __init__(self, transcriptions=None, speech=None) -> None:
        self.transcriptions = transcriptions
        self.speech = speech


class _FakeOpenAIClient:
    def __init__(self, *, transcriptions=None, speech=None) -> None:
        self.audio = _FakeAudioNamespace(transcriptions=transcriptions, speech=speech)


async def test_openai_whisper_buffers_and_yields_single_final() -> None:
    fake_tx = _FakeTranscriptions()
    stt = OpenAIWhisperSTT(client=_FakeOpenAIClient(transcriptions=fake_tx))
    audio = _audio_stream(b"aa", b"bb")
    chunks = [c async for c in stt.transcribe(audio, language="en-US", api_key="ignored")]
    assert len(chunks) == 1
    assert chunks[0].is_final is True
    assert chunks[0].text == "transcribed text"
    # The whole inbound stream became ONE buffered transcription call.
    assert len(fake_tx.calls) == 1
    # BCP-47 region form is reduced to the bare ISO-639-1 code OpenAI wants.
    assert fake_tx.calls[0]["language"] == "en"


async def test_openai_whisper_empty_audio_yields_empty_final() -> None:
    # No audio → no transcription call, one empty final chunk (so the
    # pipeline's "wait for is_final" loop unblocks rather than hanging).
    fake_tx = _FakeTranscriptions()
    stt = OpenAIWhisperSTT(client=_FakeOpenAIClient(transcriptions=fake_tx))
    chunks = [c async for c in stt.transcribe(_audio_stream())]
    assert chunks == [TranscriptChunk(text="", is_final=True)]
    assert fake_tx.calls == []


class _FakeSpeech:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.calls: list[dict] = []

    async def create(self, *, model: str, voice: str, input: str, **kwargs):
        self.calls.append({"model": model, "voice": voice, "input": input, **kwargs})

        class _Resp:
            def __init__(self, body: bytes) -> None:
                self.content = body

        return _Resp(self._body)


async def test_openai_tts_buffers_text_and_chunks_audio() -> None:
    body = b"\x00\x01" * 2048  # 4096 bytes → splits into multiple frames
    fake_speech = _FakeSpeech(body)
    tts = OpenAITTS(client=_FakeOpenAIClient(speech=fake_speech))
    audio = [c async for c in tts.synthesize(_text_stream("hello ", "there"), voice_id="alloy")]
    assert len(audio) >= 2
    assert b"".join(c.data for c in audio) == body
    assert all(c.codec == "pcm16" for c in audio)
    # The token stream was buffered into ONE synthesis call (raw pcm format).
    assert len(fake_speech.calls) == 1
    assert fake_speech.calls[0]["input"] == "hello there"
    assert fake_speech.calls[0]["response_format"] == "pcm"


async def test_openai_tts_blank_text_makes_no_call() -> None:
    fake_speech = _FakeSpeech(b"unused")
    tts = OpenAITTS(client=_FakeOpenAIClient(speech=fake_speech))
    audio = [c async for c in tts.synthesize(_text_stream("   "))]
    assert audio == []
    assert fake_speech.calls == []
