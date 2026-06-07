"""Cartesia reference TTS adapter — the T1 low-latency text-to-speech.

The streaming-native counterpart to the OpenAI TTS reference in
:mod:`movate.voice.openai_speech`, behind the same ADR 048 D3 seam
(:class:`movate.voice.base.TextToSpeechProvider`). Cartesia (Sonic) is ADR
048/049's **T1 "wow" tier** for synthesis: a streaming API that emits audio
frames as they're produced, so playback can start before the whole answer is
synthesized (the latency story, ADR 048 D7) — what OpenAI's buffered speech API
can't do.

The ``cartesia`` SDK import is **lazy + guarded** exactly like
:mod:`movate.providers.openai_native` / the OpenAI voice adapters: nothing here
imports ``cartesia`` at module scope, so a runtime/CLI installed without
``mdk[voice]`` is wholly unaffected (ADR 048 D9). The SDK client is constructed
on first use from the BYOK key; tests inject a fake via the ``client=`` kwarg.

Shape notes (re-confirmed at build time, per ADR 048's caveat that the provider
landscape moves fast):

* **Streaming synthesis** — Cartesia's ``tts.bytes(...)`` (and the SSE/WS
  variants) yields raw audio frames as they're generated. The adapter buffers
  the inbound text-delta stream into one utterance (the pipeline feeds the whole
  answer as one delta; a future per-token feed would pass deltas through), calls
  Cartesia once, and yields each emitted frame as an :class:`AudioChunk` — so
  the transport begins playback on the first frame rather than waiting for the
  full clip (unlike the OpenAI adapter, which gets one blob and re-slices it).
* **Raw PCM out** — we request raw ``pcm_s16le`` (header-less signed 16-bit LE)
  so the bytes map straight onto our ``pcm16`` ``AudioChunk`` codec with no
  container parsing at the edge, matching the OpenAI adapter's ``pcm`` choice.
* **Default voice** — ``voice_id=""`` selects the adapter's configured default
  Cartesia voice id; a caller-supplied id overrides it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from movate.voice.base import AudioChunk, AudioCodec

if TYPE_CHECKING:
    import cartesia

# Cartesia's streaming model + raw-PCM output format. ``sonic-2`` is the current
# low-latency model. ``pcm_s16le`` at 24 kHz maps directly onto our ``pcm16``
# ``AudioChunk`` (raw signed 16-bit LE), so there's no container to parse at the
# edge — the same posture as the OpenAI adapter's ``response_format="pcm"``.
_CARTESIA_MODEL = "sonic-2"
_CARTESIA_SAMPLE_RATE = 24_000
_CARTESIA_ENCODING = "pcm_s16le"
_CARTESIA_CONTAINER = "raw"

# A safe, broadly-available default Cartesia voice. Operators override per call
# via ``voice_id=`` (or per adapter via ``default_voice=``). Cartesia voice ids
# are uuids; this is Cartesia's documented sample voice.
_CARTESIA_DEFAULT_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"

# Re-slice size for a provider/fake that returns one big blob rather than a
# frame stream — ~40 ms of 24 kHz/16-bit mono (24000 * 2 * 0.04 ≈ 1920), the
# same tidy frame the OpenAI adapter uses so a slow consumer gets steady chunks.
_TTS_CHUNK_BYTES = 1920


def _require_cartesia() -> Any:
    """Import the ``cartesia`` SDK lazily, with a clear install hint.

    Mirrors :class:`movate.voice.openai_speech.OpenAITTS` — the import lives
    inside the call so importing this module (e.g. for the Protocol type) never
    requires the optional dep.
    """
    try:
        import cartesia as _cartesia  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'cartesia' package is required for the Cartesia voice adapter. "
            "Install with: pip install 'mdk-voice[cartesia]'"
        ) from exc
    return _cartesia


class CartesiaTTS:
    """Cartesia :class:`~movate.voice.base.TextToSpeechProvider` (T1 streaming).

    Buffers the inbound text-delta stream into one utterance, synthesizes it
    with Cartesia (Sonic), and yields each emitted audio frame as an
    :class:`~movate.voice.base.AudioChunk` so the transport can begin playback
    on the first frame.
    """

    name = "cartesia"
    version = "0.0.1"

    def __init__(
        self,
        *,
        model: str = _CARTESIA_MODEL,
        default_voice: str = _CARTESIA_DEFAULT_VOICE,
        client: cartesia.AsyncCartesia | None = None,
    ) -> None:
        """``client`` is for tests — pass a fake exposing the
        ``tts.bytes(...)`` shape (an async iterator of audio-frame bytes).
        Production leaves it ``None`` and the SDK client is constructed from the
        BYOK key on first use."""
        self._model = model
        self._default_voice = default_voice
        self._client = client

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        import os  # noqa: PLC0415

        cartesia_mod = _require_cartesia()
        # Per-call client when a tenant BYOK key is supplied (ADR 018); else fall
        # back to the SDK's own env (CARTESIA_API_KEY) for the local/dev path.
        key = api_key or os.environ.get("CARTESIA_API_KEY", "")
        return cartesia_mod.AsyncCartesia(api_key=key)

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

        # Guard: Cartesia voice IDs are UUIDs. If the failover chain passes a
        # foreign voice name (e.g. OpenAI's "alloy"), ignore it — use our
        # default rather than 400-ing (ADR 049 provider portability).
        import re  # noqa: PLC0415

        _is_uuid = bool(
            voice_id
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                voice_id.lower(),
            )
        )
        resolved_voice = voice_id if _is_uuid else self._default_voice

        client = self._resolve_client(api_key)
        # Use SSE (streaming) not bytes (buffered REST) — bytes waits for the
        # whole utterance server-side, killing Cartesia's first-byte advantage.
        # SSE yields typed chunks as audio is generated (~80ms first-byte vs
        # ~2.5s for bytes; verified live in examples/live_streaming_demo.py).
        # Fall back to bytes for fakes/older SDKs that don't expose sse.
        streamer = getattr(client.tts, "sse", None)
        if streamer is not None:
            stream = streamer(
                model_id=self._model,
                transcript=utterance,
                voice={"mode": "id", "id": resolved_voice},
                output_format={
                    "container": _CARTESIA_CONTAINER,
                    "encoding": _CARTESIA_ENCODING,
                    "sample_rate": _CARTESIA_SAMPLE_RATE,
                },
            )
        else:
            stream = client.tts.bytes(
                model_id=self._model,
                transcript=utterance,
                voice={"mode": "id", "id": resolved_voice},
                output_format={
                    "container": _CARTESIA_CONTAINER,
                    "encoding": _CARTESIA_ENCODING,
                    "sample_rate": _CARTESIA_SAMPLE_RATE,
                },
            )
        async for frame in _iter_audio_frames(stream):
            if not frame:
                continue
            yield AudioChunk(data=frame, codec=codec, sample_rate=_CARTESIA_SAMPLE_RATE)


async def _iter_audio_frames(stream: Any) -> AsyncIterator[bytes]:
    """Normalize Cartesia's streaming response into a stream of frame bytes.

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
    """Extract the raw audio bytes from one Cartesia frame.

    A frame is either raw ``bytes`` or an object/dict that nests them under
    ``audio`` (the SDK's field) or ``data``. The SSE path (``tts.sse``) returns
    ``WebSocketResponse_Chunk(type='chunk', data='<base64>')`` — the ``data``
    field is a **base64 str**, not bytes; decode it. Returns ``b""`` for a
    non-audio frame (e.g. timestamps / done / metadata), which the caller
    filters out.
    """
    import base64  # noqa: PLC0415 - stdlib, lazy-loaded to keep top of module clean

    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    for name in ("audio", "data"):
        value = frame.get(name) if isinstance(frame, dict) else getattr(frame, name, None)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str) and value:
            try:
                return base64.b64decode(value)
            except (ValueError, TypeError):
                continue
    return b""
