"""OpenAIWhisperSTT must hand Whisper a real container, not raw PCM."""

from __future__ import annotations

import struct
import types
from collections.abc import AsyncIterator

from movate.voice import AudioChunk, OpenAIWhisperSTT, pcm16_to_mulaw, pcm16_to_wav


class _FakeTranscriptions:
    def __init__(self) -> None:
        self.file_bytes: bytes | None = None
        self.filename: str | None = None

    async def create(self, *, model: str, file: object, **kwargs: object) -> object:
        self.filename = getattr(file, "name", None)
        self.file_bytes = file.read()  # type: ignore[attr-defined]
        return types.SimpleNamespace(text="transcribed")


class _FakeAudio:
    def __init__(self) -> None:
        self.transcriptions = _FakeTranscriptions()


class _FakeClient:
    def __init__(self) -> None:
        self.audio = _FakeAudio()


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


async def _audio(*chunks: AudioChunk) -> AsyncIterator[AudioChunk]:
    for c in chunks:
        yield c


async def _run(stt: OpenAIWhisperSTT, *chunks: AudioChunk) -> str:
    out = [t async for t in stt.transcribe(_audio(*chunks))]
    return next(t.text for t in out if t.is_final)


async def test_pcm_is_wrapped_in_wav_before_sending() -> None:
    client = _FakeClient()
    stt = OpenAIWhisperSTT(client=client)
    text = await _run(stt, AudioChunk(data=_pcm([1000] * 240), codec="pcm16", sample_rate=24_000))
    assert text == "transcribed"
    sent = client.audio.transcriptions.file_bytes
    assert sent is not None
    # A real RIFF/WAVE container — NOT raw PCM.
    assert sent[:4] == b"RIFF"
    assert sent[8:12] == b"WAVE"
    assert client.audio.transcriptions.filename == "audio.wav"


async def test_mulaw_is_decoded_then_wrapped_in_wav() -> None:
    client = _FakeClient()
    stt = OpenAIWhisperSTT(client=client)
    mulaw = pcm16_to_mulaw(_pcm([2000] * 160))
    await _run(stt, AudioChunk(data=mulaw, codec="mulaw", sample_rate=8_000))
    sent = client.audio.transcriptions.file_bytes
    assert sent is not None and sent[:4] == b"RIFF" and sent[8:12] == b"WAVE"


async def test_empty_audio_yields_empty_final() -> None:
    client = _FakeClient()
    stt = OpenAIWhisperSTT(client=client)
    out = [t async for t in stt.transcribe(_audio())]
    assert out == [out[0]] and out[0].text == "" and out[0].is_final
    assert client.audio.transcriptions.file_bytes is None  # never called


def test_pcm16_to_wav_is_valid_riff() -> None:
    wav = pcm16_to_wav(_pcm([0, 1, 2, 3]), 16_000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
