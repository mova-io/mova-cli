"""Cartesia STT adapter — Ink Whisper streaming speech-to-text.

The streaming-native sibling to :mod:`movate.voice.cartesia` (Cartesia's Sonic TTS)
behind the ADR 048 D3 :class:`~movate.voice.base.SpeechToTextProvider` seam.
Cartesia exposes its Ink Whisper model (``cartesia/ink-whisper:en`` on Lyzr's
menu) via a real-time WebSocket — the same low-latency posture as
:mod:`movate.voice.deepgram` (T1), so a router that prefers low-latency STT can
pick either without changing the pipeline contract.

The ``cartesia`` SDK import is **lazy + guarded** exactly like the Cartesia TTS
adapter: nothing here imports ``cartesia`` at module scope, so a runtime/CLI
installed without ``mdk[voice]`` (or without the ``cartesia`` extra in
particular) is wholly unaffected (ADR 048 D9). The SDK client is constructed on
first use from the BYOK key; tests inject a fake via the ``client=`` kwarg —
the same pattern as :mod:`movate.voice.deepgram` / :mod:`movate.voice.cartesia`.

Shape notes (re-confirmed at build time, per ADR 048's caveat that the provider
landscape moves fast — see the ``cartesia`` SDK's ``stt/_async_websocket.py``):

* **Streaming socket** — Cartesia's STT is a real-time WebSocket reached via
  ``client.stt.websocket(...)`` → an :class:`AsyncSttWebsocket` that exposes a
  ``transcribe(audio_chunks, ...)`` coroutine generator. It accepts an async
  iterator of raw audio bytes and yields dicts: ``{"type": "transcript",
  "text": ..., "is_final": ...}`` for transcript events, plus ``flush_done`` /
  ``done`` lifecycle events. The adapter forwards transcript text as
  :class:`TranscriptChunk`, marking ``is_final=True`` exactly where Cartesia
  does, so the pipeline knows when to run the agent.
* **Endpointing** — Cartesia's Ink Whisper sends one or more ``is_final=True``
  transcript events per utterance (after silence/endpointing). When the socket
  closes having only emitted interims (or having only emitted multiple finals
  for a long utterance), the adapter promotes / coalesces them into a terminal
  ``is_final=True`` chunk — the same defensive guarantee the Deepgram adapter
  gives so the pipeline's "wait for is_final" loop never hangs.
* **Why WebSocket, not the HTTP ``transcribe`` REST?** The buffered REST endpoint
  (``client.stt.transcribe``) requires a complete file upload and returns one
  blob — that defeats the streaming-first contract of
  :class:`SpeechToTextProvider` (ADR 048's latency story). The WebSocket is the
  only path that streams partials as the caller speaks. We document this so a
  future maintainer doesn't "simplify" us onto the REST endpoint.
* **Raw PCM in** — we declare ``pcm_s16le`` encoding to match the ``pcm16``
  :class:`AudioChunk` codec the transport emits (the same posture as the
  Cartesia TTS adapter's output format, mirror-image).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from movate.voice.base import AudioChunk, TranscriptChunk

if TYPE_CHECKING:
    import cartesia

# Cartesia's streaming STT model + raw-PCM input format. ``ink-whisper`` is the
# current Cartesia Whisper model (Lyzr lists it as ``cartesia/ink-whisper:en``).
# ``pcm_s16le`` at 16 kHz matches the Deepgram adapter's default sample-rate
# fallback and the browser-mic default — the actual rate is taken from the first
# audio chunk so a mismatched declaration can't produce empty transcripts
# (verified pattern from the Deepgram adapter).
_CARTESIA_STT_MODEL = "ink-whisper"
_CARTESIA_STT_ENCODING = "pcm_s16le"
_CARTESIA_STT_SAMPLE_RATE = 16_000


def _require_cartesia() -> Any:
    """Import the ``cartesia`` SDK lazily, with a clear install hint.

    Mirrors :func:`movate.voice.cartesia._require_cartesia` — the import lives
    inside the call so importing this module (e.g. for the Protocol type)
    never requires the optional dep.
    """
    try:
        import cartesia as _cartesia  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'cartesia' package is required for the Cartesia STT adapter. "
            "Install with: pip install 'mdk-voice[cartesia]'"
        ) from exc
    return _cartesia


def _read(payload: Any, name: str) -> Any:
    """Attribute-or-key read, so SDK objects and dict fakes both work."""
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


class CartesiaSTT:
    """Cartesia :class:`~movate.voice.base.SpeechToTextProvider` (T1 streaming).

    Streams the inbound audio into Cartesia's Ink Whisper WebSocket and yields
    partial :class:`~movate.voice.base.TranscriptChunk` slices as the caller speaks,
    then an ``is_final=True`` chunk per endpointed utterance.
    """

    name = "cartesia_stt"
    version = "1"

    def __init__(
        self,
        *,
        model: str = _CARTESIA_STT_MODEL,
        client: cartesia.AsyncCartesia | None = None,
    ) -> None:
        """``client`` is for tests — pass a fake exposing the STT WebSocket shape
        (``client.stt.websocket(...)`` → an object with an async
        ``transcribe(audio_chunks, **opts)`` generator yielding result dicts).
        Production leaves it ``None`` and the SDK client is constructed from the
        BYOK key on first use.
        """
        self._model = model
        self._client = client

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        import os  # noqa: PLC0415

        cartesia_mod = _require_cartesia()
        # Per-call client when a tenant BYOK key is supplied (ADR 018); else fall
        # back to the SDK's own env (CARTESIA_API_KEY) for the local/dev path.
        # Mirrors the TTS adapter exactly.
        key = api_key or os.environ.get("CARTESIA_API_KEY", "")
        return cartesia_mod.AsyncCartesia(api_key=key)

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
        keyterms: Sequence[str] | None = None,  # ADR 071 D4: accepted, not used by Cartesia STT
    ) -> AsyncIterator[TranscriptChunk]:
        # PEEK the first audio chunk so we can declare the correct sample rate
        # to Cartesia. The Deepgram adapter learned this the hard way — a
        # mismatched declared rate plays the audio at the wrong speed and the
        # transcript comes back empty.
        first_chunk: AudioChunk | None = None
        sample_rate = _CARTESIA_STT_SAMPLE_RATE
        audio_iter = audio.__aiter__()
        try:
            first_chunk = await audio_iter.__anext__()
            sample_rate = first_chunk.sample_rate or _CARTESIA_STT_SAMPLE_RATE
        except StopAsyncIteration:
            # No audio at all → emit empty final, same defensive guarantee as
            # the Deepgram + buffered OpenAI Whisper adapters.
            yield TranscriptChunk(text="", is_final=True)
            return

        async def _audio_with_replay() -> AsyncIterator[bytes]:
            if first_chunk is not None:
                yield first_chunk.data
            async for c in audio_iter:
                yield c.data

        client = self._resolve_client(api_key)
        # Open the STT WebSocket. ``client.stt.websocket(...)`` returns an
        # AsyncSttWebsocket whose ``transcribe(audio_chunks, ...)`` is the
        # streaming entry point — it consumes our audio iterator and yields
        # result dicts. ``language`` defaults to "en" inside the SDK if not
        # passed; we pass it through so the caller's BCP-47 hint reaches the
        # provider (Cartesia accepts ISO-639-1 — the leading 2-char part is the
        # same in either format, so "en-US" still works for "en").
        ws = await client.stt.websocket(
            model=self._model,
            language=(language or "en").split("-")[0],
            encoding=_CARTESIA_STT_ENCODING,
            sample_rate=sample_rate,
        )

        # Cartesia's WebSocket can emit several is_final transcripts for a long
        # utterance (silence-endpointed segments), plus a trailing interim if
        # the socket closes before another final. Coalesce per the same posture
        # as the Deepgram adapter so the single-turn pipeline runs the agent on
        # the full utterance, not a fragment.
        last_partial: str | None = None
        finals: list[str] = []
        try:
            async for result in ws.transcribe(
                _audio_with_replay(),
                model=self._model,
                language=(language or "en").split("-")[0],
                encoding=_CARTESIA_STT_ENCODING,
                sample_rate=sample_rate,
            ):
                # Only ``transcript`` events carry text; ``flush_done`` / ``done``
                # are lifecycle markers we ignore here. Read defensively so a
                # dict-shaped fake and a typed SDK object both work.
                if _read(result, "type") != "transcript":
                    continue
                text = _read(result, "text")
                if not isinstance(text, str) or not text:
                    continue  # keep-alive / empty interim — nothing to surface
                final = bool(_read(result, "is_final"))
                if final:
                    finals.append(text)
                    last_partial = None
                else:
                    last_partial = text
                yield TranscriptChunk(text=text, is_final=final)
        finally:
            # Best-effort close — the SDK's transcribe() closes for us, but a
            # fake or an early-exit may leave the socket open. Suppress because
            # the consumer's iteration may already have torn us down.
            close = getattr(ws, "close", None)
            if close is not None:
                import contextlib  # noqa: PLC0415

                with contextlib.suppress(Exception):
                    await close()

        # Promote the trailing unfinalized interim into a final so its words
        # aren't lost — mirrors the Deepgram adapter's tail-recovery posture.
        real_finals = len(finals)
        if last_partial:
            finals.append(last_partial)

        # Emit a terminal final when:
        # - nothing was ever transcribed → empty final (caller doesn't hang),
        # - we promoted a trailing interim → the promoted text as a final, or
        # - Cartesia emitted multiple segment-finals → coalesce into one so the
        #   single-turn pipeline runs the agent on the full utterance.
        # Skip only when there was exactly one real final and no trailing partial.
        if real_finals != 1 or last_partial:
            yield TranscriptChunk(text=" ".join(finals), is_final=True)
