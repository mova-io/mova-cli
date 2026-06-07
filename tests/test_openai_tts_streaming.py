"""OpenAITTS should use the SDK's streaming-bytes API when available."""

from __future__ import annotations

from collections.abc import AsyncIterator

from movate.voice import AudioChunk, OpenAITTS


class _StreamedResp:
    """Fake StreamedBinaryAPIResponse: yields a few PCM chunks via iter_bytes."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        for c in self._chunks:
            yield c


class _StreamingCtx:
    def __init__(self, resp: _StreamedResp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _StreamedResp:
        return self._resp

    async def __aexit__(self, *exc: object) -> None:
        return None


class _WithStreaming:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.called = False

    def create(self, **kwargs: object) -> _StreamingCtx:
        self.called = True
        return _StreamingCtx(_StreamedResp(self._chunks))


class _Speech:
    def __init__(self, chunks: list[bytes]) -> None:
        self.with_streaming_response = _WithStreaming(chunks)

    async def create(self, **kwargs: object) -> bytes:  # pragma: no cover - shouldn't be called
        return b"".join(self._fallback) if hasattr(self, "_fallback") else b""


class _BufferedSpeech:
    """Older SDK shape: no with_streaming_response."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.called = False

    async def create(self, **kwargs: object) -> bytes:
        self.called = True
        return self._body


class _Audio:
    def __init__(self, speech: object) -> None:
        self.speech = speech


class _Client:
    def __init__(self, speech: object) -> None:
        self.audio = _Audio(speech)


async def _text(s: str) -> AsyncIterator[str]:
    yield s


async def test_tts_uses_streaming_path_when_available() -> None:
    speech = _Speech([b"a" * 1920, b"b" * 1920, b"c" * 100])
    tts = OpenAITTS(client=_Client(speech))
    out = [c async for c in tts.synthesize(_text("hi"))]
    # Streaming path was used (with_streaming_response called) — first audio
    # arrives before all bytes are buffered into one blob.
    assert speech.with_streaming_response.called
    assert len(out) == 3
    assert isinstance(out[0], AudioChunk)
    assert b"".join(c.data for c in out) == b"a" * 1920 + b"b" * 1920 + b"c" * 100


async def test_tts_falls_back_to_buffered_path() -> None:
    speech = _BufferedSpeech(b"x" * 4096)
    tts = OpenAITTS(client=_Client(speech))
    out = [c async for c in tts.synthesize(_text("hi"))]
    assert speech.called  # the buffered API was hit
    assert b"".join(c.data for c in out) == b"x" * 4096
