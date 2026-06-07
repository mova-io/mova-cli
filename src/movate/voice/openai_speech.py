"""OpenAI reference speech adapters — Whisper STT + OpenAI TTS.

The Phase-1 reference implementations behind the ADR 048 D3 seams
(:class:`movate.voice.base.SpeechToTextProvider` /
:class:`~movate.voice.base.TextToSpeechProvider`). OpenAI is the **T2
low-friction default** in ADR 048's provider tiering — most customers
already hold an OpenAI key, so it's the zero-procurement on-ramp (latency is
not best-in-class; Deepgram + Cartesia are the "wow" demo pair, deferred to a
fast-follow).

The ``openai`` SDK import is **lazy + guarded** exactly like
:mod:`movate.providers.openai_native`: nothing here imports ``openai`` at
module scope, so a runtime/CLI installed without ``mdk[voice]`` is wholly
unaffected (ADR 048 D9). The SDK is constructed on first use; tests inject a
fake client via the ``client=`` kwarg.

Shape notes (re-confirmed at build time, per ADR 048's caveat that the
provider landscape moves fast):

* **STT** — OpenAI's transcription API (``audio.transcriptions.create``) is
  **buffered**, not streaming: it takes a complete audio clip and returns the
  text. The adapter therefore drains the inbound :class:`AudioChunk` stream,
  concatenates the bytes, transcribes once, and yields a **single**
  ``is_final=True`` :class:`~movate.voice.base.TranscriptChunk`. This still
  satisfies the streaming Protocol (one final chunk) — a streaming-native
  provider (Deepgram) would yield partials too.
* **TTS** — OpenAI's speech API (``audio.speech.create``) returns the full
  audio for a piece of text. The adapter buffers the inbound text-delta
  stream into one utterance, synthesizes it, and yields the audio in
  fixed-size :class:`~movate.voice.base.AudioChunk` slices so the transport
  can begin playback before the whole buffer is drained.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from movate.voice.base import AudioChunk, AudioCodec, TranscriptChunk
from movate.voice.telephony import mulaw_to_pcm16, pcm16_to_wav

if TYPE_CHECKING:
    import openai

# OpenAI's TTS returns 24 kHz PCM for the ``pcm`` response format; ``wav`` and
# the compressed formats carry their own sample rate in a container. We request
# raw ``pcm`` (header-less signed 16-bit LE) so the bytes map straight onto our
# ``pcm16`` ``AudioChunk`` codec with no container parsing at the edge.
_OPENAI_TTS_SAMPLE_RATE = 24_000

# How many bytes of synthesized audio to emit per :class:`AudioChunk`. ~40 ms
# of 24 kHz/16-bit mono audio (24000 * 2 * 0.04 ≈ 1920); rounded to a tidy
# power-of-two-ish frame so a slow consumer still gets steady playback chunks.
_TTS_CHUNK_BYTES = 1920


def _require_openai() -> Any:
    """Import the ``openai`` SDK lazily, with a clear install hint.

    Mirrors :class:`movate.providers.openai_native.OpenAIProvider` — the
    import lives inside the call so importing this module (e.g. for the
    Protocol type) never requires the optional dep.
    """
    try:
        import openai as _openai  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'openai' package is required for OpenAI voice adapters. "
            "Install with: pip install 'mdk-voice[openai]'"
        ) from exc
    return _openai


class OpenAIWhisperSTT:
    """OpenAI Whisper :class:`~movate.voice.base.SpeechToTextProvider`.

    Buffers the inbound audio stream and transcribes it in one call —
    OpenAI's transcription API is not streaming. Yields a single
    ``is_final=True`` chunk carrying the full transcript.
    """

    name = "openai_whisper"
    version = "0.0.1"

    def __init__(
        self,
        *,
        model: str = "whisper-1",
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        """``client`` is for tests — pass a fake exposing the
        ``audio.transcriptions.create`` shape. Production leaves it ``None``
        and the SDK client is constructed from the BYOK key on first use."""
        self._model = model
        self._client = client

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        openai_mod = _require_openai()
        # Per-call client when a tenant BYOK key is supplied (ADR 018); else
        # let the SDK read its own env (OPENAI_API_KEY) for the local/dev path.
        return openai_mod.AsyncOpenAI(api_key=api_key) if api_key else openai_mod.AsyncOpenAI()

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
        keyterms: Sequence[str] | None = None,  # ADR 071 D4: accepted, not supported by Whisper
        endpointing_ms: int | None = None,  # ADR 073 D3: accepted; Whisper is buffered, no endpoint
    ) -> AsyncIterator[TranscriptChunk]:
        # Drain the inbound stream into one buffer, tracking the codec/sample
        # rate so we can build a real container (Whisper rejects header-less
        # PCM). The transport feeds endpointed audio (one utterance per call).
        buf = bytearray()
        codec: AudioCodec = "pcm16"
        sample_rate = 24_000
        async for chunk in audio:
            buf.extend(chunk.data)
            codec = chunk.codec
            sample_rate = chunk.sample_rate

        if not buf:
            # Empty utterance (caller sent end with no audio) — emit an empty
            # final chunk so the transport's "wait for is_final" loop unblocks
            # rather than hanging.
            yield TranscriptChunk(text="", is_final=True)
            return

        client = self._resolve_client(api_key)
        # OpenAI's SDK wants a file-like with a ``.name`` so it can infer the
        # format — and it must be a DECODABLE container, not raw PCM. Wrap PCM
        # (and μ-law, after decoding) in a WAV header; pass an already-containered
        # codec (opus) through with the right extension.
        import io  # noqa: PLC0415

        if codec == "pcm16":
            body, filename = pcm16_to_wav(bytes(buf), sample_rate), "audio.wav"
        elif codec == "mulaw":
            body, filename = pcm16_to_wav(mulaw_to_pcm16(bytes(buf)), sample_rate), "audio.wav"
        else:  # already a self-describing container (e.g. opus/ogg)
            body, filename = bytes(buf), "audio.ogg"

        file_obj = io.BytesIO(body)
        file_obj.name = filename

        extra: dict[str, Any] = {}
        if language:
            # OpenAI wants the bare ISO-639-1 code ("en"), not the BCP-47
            # region form ("en-US") — take the primary subtag.
            extra["language"] = language.split("-", 1)[0]

        resp = await client.audio.transcriptions.create(
            model=self._model,
            file=file_obj,
            **extra,
        )
        text = getattr(resp, "text", "") or ""
        yield TranscriptChunk(text=text, is_final=True)


# OpenAI's supported voice names (as of 2024-12). Any voice_id not in this
# set is silently mapped to the adapter's default — this prevents a Cartesia
# UUID or ElevenLabs voice_id from 400-ing on failover (ADR 049 portability).
_OPENAI_VOICES: frozenset[str] = frozenset(
    {"alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"}
)


class OpenAITTS:
    """OpenAI :class:`~movate.voice.base.TextToSpeechProvider`.

    Buffers the inbound text-delta stream into one utterance, synthesizes it
    with OpenAI's speech API, and yields the audio in fixed-size chunks so the
    transport can start playback promptly.
    """

    name = "openai_tts"
    version = "0.0.1"

    def __init__(
        self,
        *,
        model: str = "tts-1",
        default_voice: str = "alloy",
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        self._model = model
        self._default_voice = default_voice
        self._client = client

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        openai_mod = _require_openai()
        return openai_mod.AsyncOpenAI(api_key=api_key) if api_key else openai_mod.AsyncOpenAI()

    async def synthesize(
        self,
        text: AsyncIterator[str],
        *,
        voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        # Buffer the agent's streamed tokens into one utterance. OpenAI's
        # speech API takes whole text, not a token stream; a streaming-native
        # TTS (Cartesia) would synthesize incrementally here instead.
        parts: list[str] = []
        async for delta in text:
            if delta:
                parts.append(delta)
        utterance = "".join(parts)

        if not utterance.strip():
            return  # nothing to say → no audio frames

        # Guard: if the voice_id was set for a different provider (e.g. a
        # Cartesia UUID on failover), ignore it and use our default. Passing a
        # foreign voice_id to OpenAI yields a 400 — silent fallback is safer
        # than a hard error on a failover path (ADR 049 portability).
        resolved_voice = (
            voice_id if voice_id and voice_id.lower() in _OPENAI_VOICES else self._default_voice
        )

        client = self._resolve_client(api_key)
        # Request raw PCM (no container at the edge). Prefer the SDK's
        # streaming-response context manager so the FIRST audio bytes arrive
        # before the whole utterance is synthesized — drops perceived latency
        # from ~1.2s (buffered) to ~300-500ms. Fall back to a one-shot call for
        # older SDKs / fakes that don't expose ``with_streaming_response``.
        streamer = getattr(
            getattr(client.audio.speech, "with_streaming_response", None), "create", None
        )
        if streamer is not None:
            async with streamer(
                model=self._model,
                voice=resolved_voice,
                input=utterance,
                response_format="pcm",
            ) as resp:
                async for chunk in _iter_audio_bytes(resp):
                    yield AudioChunk(data=chunk, codec=codec, sample_rate=_OPENAI_TTS_SAMPLE_RATE)
            return

        resp = await client.audio.speech.create(
            model=self._model,
            voice=resolved_voice,
            input=utterance,
            response_format="pcm",
        )
        data = await _read_audio_bytes(resp)
        for start in range(0, len(data), _TTS_CHUNK_BYTES):
            yield AudioChunk(
                data=data[start : start + _TTS_CHUNK_BYTES],
                codec=codec,
                sample_rate=_OPENAI_TTS_SAMPLE_RATE,
            )


async def _iter_audio_bytes(resp: Any) -> AsyncIterator[bytes]:
    """Stream the audio body in chunks if the SDK supports it.

    OpenAI's StreamedBinaryAPIResponse exposes ``iter_bytes(chunk_size)`` — that's
    the path that drops first-byte latency. Anything older falls back to a single
    full-body read so the contract still holds.
    """
    iterator = getattr(resp, "iter_bytes", None)
    if iterator is not None:
        agen = iterator(chunk_size=_TTS_CHUNK_BYTES)
        async for piece in agen:
            if piece:
                yield bytes(piece)
        return
    data = await _read_audio_bytes(resp)
    for start in range(0, len(data), _TTS_CHUNK_BYTES):
        yield data[start : start + _TTS_CHUNK_BYTES]


async def _read_audio_bytes(resp: Any) -> bytes:
    """Pull the full audio body off an OpenAI speech response.

    The SDK has shifted shapes across versions; accept the common ones so the
    adapter is resilient (and test fakes can return the simplest):

    * ``resp.read()`` (async) — the streaming-response object.
    * ``resp.content`` — a bytes attribute on a buffered response.
    * raw ``bytes`` — a fake returning the body directly.
    """
    if isinstance(resp, bytes | bytearray):
        return bytes(resp)
    reader = getattr(resp, "read", None)
    if reader is not None:
        result = reader()
        if hasattr(result, "__await__"):
            result = await result
        return bytes(result)
    content = getattr(resp, "content", None)
    if content is not None:
        return bytes(content)
    return b""
