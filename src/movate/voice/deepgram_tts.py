"""Deepgram Aura reference TTS adapter — T1 low-latency text-to-speech.

The TTS-side sibling of :mod:`movate.voice.deepgram` (Deepgram's STT), behind the
ADR 048 D3 :class:`movate.voice.base.TextToSpeechProvider` seam — and the parity
fix for Lyzr's TTS menu, which lists Deepgram alongside Cartesia/ElevenLabs but
which we'd only adapted for STT until now. Aura is Deepgram's streaming
synthesis API; like Cartesia it emits audio frames as they're produced, so
playback can start before the whole answer is synthesized (the latency story,
ADR 048 D7) — the streaming counterpart to OpenAI's buffered speech API.

The ``deepgram`` SDK import is **lazy + guarded** exactly like
:mod:`movate.voice.deepgram`: nothing here imports ``deepgram`` at module scope,
so a runtime/CLI installed without ``mdk[voice]`` is wholly unaffected (ADR
048 D9). The SDK client is constructed on first use from the BYOK key; tests
inject a fake via the ``client=`` kwarg.

Shape notes (re-confirmed at build time against ``deepgram-sdk`` 3.x, per ADR
048's caveat that the provider landscape moves fast):

* **Streaming synthesis** — we use the async REST endpoint
  (``client.speak.asyncrest.v("1").stream_raw(source, options=...)``), which
  returns an ``httpx.Response`` whose ``aiter_bytes()`` yields raw audio frames
  as Aura generates them. (The SDK also exposes a WebSocket variant under
  ``speak.asyncwebsocket`` — its differentiator is *input* text streaming for
  long-running narration, which we don't need: the pipeline already buffers the
  agent's tokens into one utterance before calling synthesize. REST stream_raw
  gives the same output-streaming first-byte latency without the WS event-loop
  bridging.) The adapter buffers the inbound text-delta stream into one
  utterance, calls Aura once, and yields each emitted frame as an
  :class:`AudioChunk` — same posture as Cartesia/ElevenLabs.
* **Raw PCM out** — we request ``linear16`` at 24 kHz in a ``none`` container
  (header-less signed 16-bit LE) so the bytes map straight onto our ``pcm16``
  :class:`AudioChunk` codec with no container parsing at the edge, matching
  Cartesia's ``pcm_s16le`` choice.
* **Default voice** — ``voice_id=""`` selects ``aura-2-thalia-en`` (Deepgram's
  current default Aura 2 voice — English-language, broadly available);
  a caller-supplied id overrides it. Aura's "voice" is selected via the same
  ``model=`` parameter (the model id IS the voice id, e.g. ``aura-2-thalia-en``),
  so we forward ``voice_id`` onto ``SpeakOptions(model=...)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from movate.voice.base import AudioChunk, AudioCodec

if TYPE_CHECKING:
    import deepgram

# Aura's defaults. ``aura-2-thalia-en`` is the current default Aura 2 English
# voice (Deepgram's published "default" sample in the Aura 2 lineup). The
# encoding/sample-rate hint matches our ``pcm16`` ``AudioChunk`` default (raw
# signed 16-bit LE at 24 kHz) so the bytes Aura streams back map directly onto
# our codec with no container parsing at the edge.
_AURA_DEFAULT_VOICE = "aura-2-thalia-en"
_AURA_ENCODING = "linear16"
_AURA_SAMPLE_RATE = 24_000
_AURA_CONTAINER = "none"

# Re-slice size for a provider/fake that returns one big blob rather than a
# frame stream — ~40 ms of 24 kHz/16-bit mono (24000 * 2 * 0.04 ≈ 1920), the
# same tidy frame Cartesia/ElevenLabs use so a slow consumer gets steady chunks.
_TTS_CHUNK_BYTES = 1920


def _require_deepgram() -> Any:
    """Import the ``deepgram`` SDK lazily, with a clear install hint.

    Mirrors :func:`movate.voice.deepgram._require_deepgram` — the import lives
    inside the call so importing this module (e.g. for the Protocol type) never
    requires the optional dep.
    """
    try:
        import deepgram as _deepgram  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'deepgram-sdk' package is required for the Deepgram voice adapter. "
            "Install with: pip install 'mdk-voice[deepgram]'"
        ) from exc
    return _deepgram


class DeepgramAuraTTS:
    """Deepgram Aura :class:`~movate.voice.base.TextToSpeechProvider` (T1 streaming).

    Buffers the inbound text-delta stream into one utterance, synthesizes it
    with Aura via the streaming REST endpoint (``speak.asyncrest.stream_raw``),
    and yields each emitted audio frame as an :class:`~movate.voice.base.AudioChunk`
    so the transport can begin playback on the first frame.
    """

    name = "deepgram_aura"
    version = "1"

    def __init__(
        self,
        *,
        default_voice: str = _AURA_DEFAULT_VOICE,
        client: deepgram.DeepgramClient | None = None,
    ) -> None:
        """``client`` is for tests — pass a fake exposing the
        ``speak.asyncrest.v("1").stream_raw(source, options=...)`` shape (an
        awaitable returning an object with ``aiter_bytes()`` / ``aclose()``).
        Production leaves it ``None`` and the SDK client is constructed from the
        BYOK key on first use."""
        self._default_voice = default_voice
        self._client = client

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        import os  # noqa: PLC0415

        deepgram_mod = _require_deepgram()
        # Per-call client when a tenant BYOK key is supplied (ADR 018); else fall
        # back to the SDK's own env (DEEPGRAM_API_KEY) for the local/dev path —
        # same posture as the STT adapter.
        key = api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        return deepgram_mod.DeepgramClient(key)

    def _build_options(self, voice_id: str) -> Any:
        """Build Deepgram ``SpeakOptions`` for the synthesis call.

        Aura selects voice via the ``model`` parameter (the model id IS the
        voice id, e.g. ``aura-2-thalia-en``). Falls back to a plain dict when
        the SDK type isn't importable (the test path with an injected fake), so
        a fake never needs the real SDK.
        """
        opts: dict[str, Any] = {
            "model": voice_id or self._default_voice,
            "encoding": _AURA_ENCODING,
            "sample_rate": _AURA_SAMPLE_RATE,
            "container": _AURA_CONTAINER,
        }
        if self._client is not None:
            # Test path: hand the fake the raw option dict.
            return opts
        speak_options = getattr(_require_deepgram(), "SpeakOptions", None)
        if speak_options is None:  # pragma: no cover - SDK always ships it
            return opts
        return speak_options(**opts)

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

        client = self._resolve_client(api_key)
        # asyncrest.v("1") → AsyncSpeakRESTClient. stream_raw(source, options)
        # returns an httpx.Response whose aiter_bytes() yields the audio as it
        # streams off the wire — the first-byte advantage over a buffered POST.
        speak = client.speak.asyncrest.v("1")
        options = self._build_options(voice_id)
        response = await speak.stream_raw(
            source={"text": utterance},
            options=options,
        )
        async for frame in _iter_audio_frames(response):
            if not frame:
                continue
            yield AudioChunk(data=frame, codec=codec, sample_rate=_AURA_SAMPLE_RATE)


async def _iter_audio_frames(response: Any) -> AsyncIterator[bytes]:  # noqa: PLR0912
    """Normalize Aura's streaming response into a stream of frame bytes.

    The real call returns an ``httpx.Response`` exposing ``aiter_bytes()``
    (which the adapter prefers — chunked transfer means each yielded slice is
    one tidy audio frame). Test fakes can return any of the shapes below so the
    adapter is resilient and so a minimal fake doesn't need httpx:

    * an **object with ``aiter_bytes()``** — the real httpx shape; iterated
      straight through frame-by-frame, then ``aclose()``'d to release the
      connection (closing it is important on httpx — without it a streamed
      response leaks the underlying connection);
    * an **async iterator** of frames (each raw ``bytes``);
    * a **sync iterable** of such frames;
    * a single ``bytes`` blob — re-sliced into ``_TTS_CHUNK_BYTES`` frames so a
      buffered fallback still streams steadily.
    """
    import contextlib  # noqa: PLC0415

    aiter_bytes = getattr(response, "aiter_bytes", None)
    if callable(aiter_bytes):
        try:
            async for frame in aiter_bytes():
                if isinstance(frame, (bytes, bytearray)):
                    yield bytes(frame)
        finally:
            aclose = getattr(response, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()
        return

    if isinstance(response, (bytes, bytearray)):
        blob = bytes(response)
        for start in range(0, len(blob), _TTS_CHUNK_BYTES):
            yield blob[start : start + _TTS_CHUNK_BYTES]
        return

    if hasattr(response, "__aiter__"):
        async for frame in response:
            if isinstance(frame, (bytes, bytearray)):
                yield bytes(frame)
        return

    if hasattr(response, "__iter__"):
        for frame in response:
            if isinstance(frame, (bytes, bytearray)):
                yield bytes(frame)
        return
