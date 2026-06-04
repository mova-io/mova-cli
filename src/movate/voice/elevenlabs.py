"""ElevenLabs reference TTS adapter — the T2 premium-voice text-to-speech.

The streaming-native sibling of :mod:`movate.voice.cartesia` (Cartesia/Sonic,
the T1 low-latency tier), behind the same ADR 048 D3 seam
(:class:`movate.voice.base.TextToSpeechProvider`). ElevenLabs is ADR 048/049's
**T2 "premium voice" tier**: a streaming synthesis API whose differentiator is
voice *quality / naturalness* (the most lifelike voices in the lineup) rather
than the rock-bottom latency Cartesia optimizes for. Like Cartesia it streams
audio frames as they're produced, so playback can start before the whole answer
is synthesized (the latency story, ADR 048 D7) — what OpenAI's buffered speech
API can't do.

The ``elevenlabs`` SDK import is **lazy + guarded** exactly like
:mod:`movate.voice.cartesia`: nothing
here imports ``elevenlabs`` at module scope, so a runtime/CLI installed without
``mdk[voice]`` is wholly unaffected (ADR 048 D9). The SDK client is constructed
on first use from the BYOK key; tests inject a fake via the ``client=`` kwarg.

Shape notes (re-confirmed at build time against ``elevenlabs`` 2.x, per ADR
048's caveat that the provider landscape moves fast):

* **Streaming synthesis** — the async client's
  ``text_to_speech.stream(voice_id=..., text=..., model_id=..., output_format=...)``
  yields raw audio frames as ``bytes`` as they're generated. The adapter buffers
  the inbound text-delta stream into one utterance (the pipeline feeds the whole
  answer as one delta; a future per-token feed would pass deltas through), calls
  ElevenLabs once, and yields each emitted frame as an :class:`AudioChunk` — so
  the transport begins playback on the first frame, the same posture as the
  Cartesia adapter (unlike OpenAI's, which gets one blob and re-slices it).
* **Raw PCM out** — we request ``pcm_24000`` (header-less signed 16-bit LE at
  24 kHz) so the bytes map straight onto our ``pcm16`` ``AudioChunk`` codec with
  no container parsing at the edge, matching Cartesia's ``pcm_s16le`` choice.
* **Default voice** — ``voice_id=""`` selects the adapter's configured default
  ElevenLabs voice id; a caller-supplied id overrides it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from movate.voice.base import AudioChunk, AudioCodec

if TYPE_CHECKING:
    import elevenlabs.client

# ElevenLabs' streaming model + raw-PCM output format. ``eleven_turbo_v2_5`` is
# the current low-latency multilingual model — the streaming-friendly default
# (the higher-fidelity ``eleven_multilingual_v2`` is a per-call override via
# ``model=``). ``pcm_24000`` is raw signed 16-bit LE at 24 kHz, which maps
# directly onto our ``pcm16`` ``AudioChunk`` (no container to parse at the edge)
# — the same posture as the Cartesia adapter's ``pcm_s16le``.
_ELEVENLABS_MODEL = "eleven_turbo_v2_5"
_ELEVENLABS_SAMPLE_RATE = 24_000
_ELEVENLABS_OUTPUT_FORMAT = "pcm_24000"

# A safe, broadly-available default ElevenLabs voice ("Rachel" — ElevenLabs'
# documented sample/default voice). Operators override per call via ``voice_id=``
# (or per adapter via ``default_voice=``). ElevenLabs voice ids are opaque
# strings.
_ELEVENLABS_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"

# Re-slice size for a provider/fake that returns one big blob rather than a
# frame stream — ~40 ms of 24 kHz/16-bit mono (24000 * 2 * 0.04 ≈ 1920), the
# same tidy frame Cartesia/OpenAI use so a slow consumer gets steady chunks.
_TTS_CHUNK_BYTES = 1920


def _require_elevenlabs() -> Any:
    """Import the ``elevenlabs`` SDK lazily, with a clear install hint.

    Mirrors :class:`movate.voice.cartesia.CartesiaTTS` — the import lives inside
    the call so importing this module (e.g. for the Protocol type) never requires
    the optional dep.
    """
    try:
        import elevenlabs.client as _elevenlabs  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'elevenlabs' package is required for the ElevenLabs voice adapter. "
            "Install with: pip install 'mdk-voice[elevenlabs]'"
        ) from exc
    return _elevenlabs


class ElevenLabsTTS:
    """ElevenLabs :class:`~movate.voice.base.TextToSpeechProvider` (T2 premium).

    Buffers the inbound text-delta stream into one utterance, synthesizes it
    with ElevenLabs, and yields each emitted audio frame as an
    :class:`~movate.voice.base.AudioChunk` so the transport can begin playback on
    the first frame.
    """

    name = "elevenlabs"
    version = "0.0.1"

    def __init__(
        self,
        *,
        model: str = _ELEVENLABS_MODEL,
        default_voice: str = _ELEVENLABS_DEFAULT_VOICE,
        client: elevenlabs.client.AsyncElevenLabs | None = None,
    ) -> None:
        """``client`` is for tests — pass a fake exposing the
        ``text_to_speech.stream(...)`` shape (an async iterator of audio-frame
        bytes). Production leaves it ``None`` and the SDK client is constructed
        from the BYOK key on first use."""
        self._model = model
        self._default_voice = default_voice
        self._client = client

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        import os  # noqa: PLC0415

        elevenlabs_mod = _require_elevenlabs()
        # Per-call client when a tenant BYOK key is supplied (ADR 018); else fall
        # back to the SDK's own env (ELEVENLABS_API_KEY) for the local/dev path.
        key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        return elevenlabs_mod.AsyncElevenLabs(api_key=key)

    async def synthesize(
        self,
        text: AsyncIterator[str],
        *,
        voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        # Buffer the agent's streamed tokens into one utterance. The pipeline
        # feeds the whole answer as a single delta today; buffering keeps that
        # lossless and leaves room for a future per-token feed.
        parts: list[str] = []
        async for delta in text:
            if delta:
                parts.append(delta)
        utterance = "".join(parts)

        if not utterance.strip():
            return  # nothing to say → no audio frames

        # Guard: ElevenLabs voice IDs are alphanumeric ~20-char strings (no
        # dashes). If the failover chain passes a Cartesia UUID (has dashes),
        # fall back to our default rather than 400-ing (ADR 049 portability).
        import re  # noqa: PLC0415

        _is_uuid = bool(voice_id and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            voice_id.lower(),
        ))
        resolved_voice = self._default_voice if _is_uuid else (voice_id or self._default_voice)

        client = self._resolve_client(api_key)
        stream = client.text_to_speech.stream(
            voice_id=resolved_voice,
            text=utterance,
            model_id=self._model,
            output_format=_ELEVENLABS_OUTPUT_FORMAT,
        )
        async for frame in _iter_audio_frames(stream):
            if not frame:
                continue
            yield AudioChunk(data=frame, codec=codec, sample_rate=_ELEVENLABS_SAMPLE_RATE)


async def _iter_audio_frames(stream: Any) -> AsyncIterator[bytes]:
    """Normalize ElevenLabs' streaming response into a stream of frame bytes.

    The SDK has shifted shapes across versions; accept the common ones so the
    adapter is resilient (and test fakes can return the simplest):

    * an **async iterator** of frames — each frame either raw ``bytes`` or an
      object/dict carrying the bytes under ``audio``/``data`` (the streaming
      path; yielded straight through frame-by-frame);
    * a **sync iterable** of such frames;
    * an awaitable resolving to either of the above, or to a single ``bytes``
      blob — re-sliced into ``_TTS_CHUNK_BYTES`` frames so a buffered fallback
      still streams steadily.
    """
    if hasattr(stream, "__await__"):
        stream = await stream

    if isinstance(stream, (bytes, bytearray)):
        blob = bytes(stream)
        for start in range(0, len(blob), _TTS_CHUNK_BYTES):
            yield blob[start : start + _TTS_CHUNK_BYTES]
        return

    if hasattr(stream, "__aiter__"):
        async for frame in stream:
            yield _frame_bytes(frame)
        return

    if hasattr(stream, "__iter__"):
        for frame in stream:
            yield _frame_bytes(frame)
        return

    # Unknown shape — best-effort single blob.
    yield _frame_bytes(stream)


def _frame_bytes(frame: Any) -> bytes:
    """Extract the raw audio bytes from one ElevenLabs frame.

    A frame is either raw ``bytes`` (the SDK's streaming default) or an
    object/dict that nests them under ``audio`` or ``data``. Returns ``b""`` for
    a non-audio frame (e.g. a metadata event), which the caller filters out.
    """
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    for name in ("audio", "data"):
        value = frame.get(name) if isinstance(frame, dict) else getattr(frame, name, None)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    return b""
