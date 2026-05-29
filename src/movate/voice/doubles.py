"""In-memory / fake voice adapters for tests.

Mirror the ``testing/doubles.py`` philosophy: these satisfy the ADR 048 D3
Protocols (:class:`movate.voice.base.SpeechToTextProvider` /
:class:`~movate.voice.base.TextToSpeechProvider`) closely enough to type-check
against them, and capture what they were fed in plain lists so a test can
assert directly (``assert stt.received == [...]``). No network, no SDK, no
audio libs — usable from a default install with no ``mdk[voice]`` extra.

These live in ``movate.voice`` (not ``tests/``) so an agent author writing
voice tests for their own deployment can import them too, same as
:class:`movate.testing.InMemoryStorage`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from movate.voice.base import AudioChunk, AudioCodec, TranscriptChunk


class FakeSTT:
    """Scripted :class:`~movate.voice.base.SpeechToTextProvider`.

    Drains the inbound audio (recording every chunk's bytes in
    :attr:`received` for assertion) and yields a configured transcript. By
    default it returns one ``is_final=True`` chunk with :attr:`transcript`;
    pass ``partials=`` to also emit non-final partial chunks first, exercising
    the streaming-endpointing path.
    """

    name = "fake_stt"
    version = "0.0.1"

    def __init__(self, transcript: str = "hello", *, partials: list[str] | None = None) -> None:
        self.transcript = transcript
        self._partials = partials or []
        self.received: list[bytes] = []
        self.languages: list[str | None] = []
        self.api_keys: list[str | None] = []

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        self.languages.append(language)
        self.api_keys.append(api_key)
        async for chunk in audio:
            self.received.append(chunk.data)
        for partial in self._partials:
            yield TranscriptChunk(text=partial, is_final=False)
        yield TranscriptChunk(text=self.transcript, is_final=True, confidence=1.0)


class FakeTTS:
    """Scripted :class:`~movate.voice.base.TextToSpeechProvider`.

    Buffers the inbound text deltas (recording the joined utterance in
    :attr:`spoken` for assertion) and yields the utterance encoded as audio
    bytes — by default ``utf-8`` of the text wrapped in a single
    :class:`~movate.voice.base.AudioChunk`, so a test can decode the audio
    back to text and assert the agent's answer round-tripped through TTS.
    Set ``frames`` to split the bytes into that many chunks to exercise the
    transport's multi-frame audio-out path.
    """

    name = "fake_tts"
    version = "0.0.1"

    def __init__(self, *, frames: int = 1) -> None:
        self._frames = max(1, frames)
        self.spoken: list[str] = []
        self.voice_ids: list[str] = []
        self.api_keys: list[str | None] = []

    async def synthesize(
        self,
        text: AsyncIterator[str],
        *,
        voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        self.voice_ids.append(voice_id)
        self.api_keys.append(api_key)
        parts: list[str] = []
        async for delta in text:
            if delta:
                parts.append(delta)
        utterance = "".join(parts)
        self.spoken.append(utterance)
        if not utterance:
            return
        data = utterance.encode("utf-8")
        # Split into ``frames`` roughly-equal slices.
        step = max(1, len(data) // self._frames)
        for start in range(0, len(data), step):
            yield AudioChunk(data=data[start : start + step], codec=codec, sample_rate=24_000)
