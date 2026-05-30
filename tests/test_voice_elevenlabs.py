"""T2 premium-voice TTS — ElevenLabs (ADR 048/049).

Mirrors the Cartesia half of ``tests/test_voice_deepgram_cartesia.py`` for the
streaming-native premium-voice adapter behind the ADR 048 D3 seam
(``TextToSpeechProvider``). These tests pin:

* runtime-checkable conformance of the adapter against the Protocol (so a future
  provider can be checked the same way ``isinstance(p, BaseLLMProvider)`` works);
* the adapter's lazy SDK import — constructing it with an injected fake client
  does NOT require the ``elevenlabs`` package (so the whole suite runs without
  ``mdk[voice]`` installed);
* ElevenLabs' streaming behavior: the buffered text → one synthesis call with
  raw-PCM output format + the resolved voice id, frames streamed straight through
  as ``AudioChunk``s, blank text → no synthesis call, the default voice when
  unset, and the BYOK ``api_key=`` reaching the client constructor / SDK env.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from movate.voice import (
    ElevenLabsTTS,
    TextToSpeechProvider,
)


async def _text_stream(*parts: str) -> AsyncIterator[str]:
    for p in parts:
        yield p


# ---------------------------------------------------------------------------
# Protocol conformance (runtime_checkable) + lazy import
# ---------------------------------------------------------------------------


def test_elevenlabs_satisfies_protocol() -> None:
    # Constructing with a None client must NOT import the provider SDK (lazy) —
    # this whole test module runs without elevenlabs installed.
    assert isinstance(ElevenLabsTTS(), TextToSpeechProvider)


# ---------------------------------------------------------------------------
# ElevenLabs TTS — with an injected fake streaming client
# ---------------------------------------------------------------------------


class _FakeElevenLabsTextToSpeech:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.calls: list[dict] = []

    def stream(self, **kwargs: Any) -> AsyncIterator[bytes]:
        self.calls.append(kwargs)
        frames = self._frames

        async def _gen() -> AsyncIterator[bytes]:
            for f in frames:
                yield f

        return _gen()


class _FakeElevenLabsClient:
    def __init__(self, text_to_speech: _FakeElevenLabsTextToSpeech) -> None:
        self.text_to_speech = text_to_speech


async def test_elevenlabs_buffers_text_and_streams_frames() -> None:
    fake_tts = _FakeElevenLabsTextToSpeech([b"frame1", b"frame2", b"frame3"])
    tts = ElevenLabsTTS(client=_FakeElevenLabsClient(fake_tts))
    audio = [c async for c in tts.synthesize(_text_stream("hello ", "there"), voice_id="voice-xyz")]
    # Each emitted frame becomes one AudioChunk (streamed, not re-sliced).
    assert [c.data for c in audio] == [b"frame1", b"frame2", b"frame3"]
    assert all(c.codec == "pcm16" for c in audio)
    # 24 kHz raw PCM rides on the chunk so the edge can transcode.
    assert all(c.sample_rate == 24_000 for c in audio)
    # The token stream was buffered into ONE synthesis call.
    assert len(fake_tts.calls) == 1
    call = fake_tts.calls[0]
    assert call["text"] == "hello there"
    # Raw PCM output format so bytes map onto pcm16 with no container.
    assert call["output_format"] == "pcm_24000"
    # A model id was passed.
    assert call["model_id"]
    # The caller-supplied voice id was passed through.
    assert call["voice_id"] == "voice-xyz"


async def test_elevenlabs_default_voice_when_unset() -> None:
    fake_tts = _FakeElevenLabsTextToSpeech([b"frame"])
    tts = ElevenLabsTTS(client=_FakeElevenLabsClient(fake_tts))
    _ = [c async for c in tts.synthesize(_text_stream("hi"))]
    # voice_id="" → the adapter's configured default, not an empty id.
    assert fake_tts.calls[0]["voice_id"]


async def test_elevenlabs_blank_text_makes_no_call() -> None:
    fake_tts = _FakeElevenLabsTextToSpeech([b"unused"])
    tts = ElevenLabsTTS(client=_FakeElevenLabsClient(fake_tts))
    audio = [c async for c in tts.synthesize(_text_stream("   "))]
    assert audio == []
    assert fake_tts.calls == []


async def test_elevenlabs_byok_key_used_to_build_client(monkeypatch: Any) -> None:
    # With no injected client, a BYOK api_key= must reach the SDK's
    # AsyncElevenLabs(api_key=...) constructor — not a global env read. We patch
    # the lazy importer so the test runs without the real elevenlabs package.
    constructed: dict[str, Any] = {}

    class _FakeAsyncElevenLabs:
        def __init__(self, *, api_key: str) -> None:
            constructed["api_key"] = api_key
            self.text_to_speech = _FakeElevenLabsTextToSpeech([b"frame"])

    class _FakeModule:
        AsyncElevenLabs = _FakeAsyncElevenLabs

    import movate.voice.elevenlabs as el_mod  # noqa: PLC0415

    monkeypatch.setattr(el_mod, "_require_elevenlabs", lambda: _FakeModule)
    tts = ElevenLabsTTS()
    _ = [c async for c in tts.synthesize(_text_stream("hi"), api_key="byok-123")]
    assert constructed["api_key"] == "byok-123"
