"""Deepgram reference STT adapter — the T1 low-latency speech-to-text.

The streaming-native counterpart to the OpenAI Whisper reference in
:mod:`movate.voice.openai_speech`, behind the same ADR 048 D3 seam
(:class:`movate.voice.base.SpeechToTextProvider`). Deepgram is ADR 048/049's
**T1 "wow" tier**: a true streaming transcription socket that emits *partial*
hypotheses as the caller speaks and an *endpointed* final per utterance — the
latency story OpenAI's buffered transcription can't tell (see the shape note in
``openai_speech.py``).

The ``deepgram`` SDK import is **lazy + guarded** exactly like
:mod:`movate.providers.openai_native` / the OpenAI voice adapters: nothing here
imports ``deepgram`` at module scope, so a runtime/CLI installed without
``mdk[voice]`` is wholly unaffected (ADR 048 D9). The SDK client is constructed
on first use from the BYOK key; tests inject a fake via the ``client=`` kwarg.

Shape notes (re-confirmed at build time, per ADR 048's caveat that the provider
landscape moves fast):

* **Streaming socket** — Deepgram's live transcription is a bidirectional
  WebSocket: the caller ``send()``s audio frames and receives transcript
  events. The adapter drives both halves concurrently — a sender task pumps the
  inbound :class:`AudioChunk` stream into the socket, while the main coroutine
  consumes transcript events and yields :class:`TranscriptChunk` slices. An
  event with ``speech_final``/``is_final`` true (Deepgram's endpointing) yields
  a ``TranscriptChunk(is_final=True)``; interim results yield partials.
* **Endpointing** — Deepgram marks an endpointed utterance via ``speech_final``
  (VAD decided the turn ended) or, failing that, ``is_final`` (the final
  hypothesis for a segment). The adapter treats either as the Protocol's
  ``is_final`` so the pipeline knows when to run the agent. If the socket closes
  having only emitted interims, the adapter promotes the last interim to a final
  so the pipeline's "wait for is_final" loop unblocks rather than hangs (the
  same defensive guarantee the OpenAI adapter gives for empty audio).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from movate.voice.base import AudioChunk, TranscriptChunk

if TYPE_CHECKING:
    import deepgram

# Deepgram's live model + defaults. ``nova-2`` is the current low-latency
# general model; ``smart_format`` gives punctuated, readable transcripts. The
# encoding/sample-rate hint matches our ``pcm16`` ``AudioChunk`` default (raw
# signed 16-bit LE) so Deepgram decodes the raw frames the transport forwards
# without a container.
_DEEPGRAM_MODEL = "nova-2"
_DEEPGRAM_ENCODING = "linear16"
_DEEPGRAM_SAMPLE_RATE = 24_000


def _require_deepgram() -> Any:
    """Import the ``deepgram`` SDK lazily, with a clear install hint.

    Mirrors :class:`movate.voice.openai_speech.OpenAIWhisperSTT` — the import
    lives inside the call so importing this module (e.g. for the Protocol type)
    never requires the optional dep.
    """
    try:
        import deepgram as _deepgram  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'deepgram-sdk' package is required for the Deepgram voice adapter. "
            "Install with: uv add 'movate-cli[voice]'"
        ) from exc
    return _deepgram


def _read(payload: Any, name: str) -> Any:
    """Attribute-or-key read, so SDK objects and dict fakes both work."""
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def _chunk_is_final(payload: Any) -> bool:
    """True if a Deepgram transcript event marks an endpointed utterance.

    Deepgram surfaces endpointing two ways: ``speech_final`` (the VAD decided
    the speaker finished a turn) and ``is_final`` (the final hypothesis for the
    current interim segment). Either one is "this text is the contract the agent
    runs on" for our Protocol.
    """
    return bool(_read(payload, "speech_final") or _read(payload, "is_final"))


def _transcript_text(payload: Any) -> str:
    """Pull the best-alternative transcript text off a Deepgram event.

    Deepgram nests the transcript at ``channel.alternatives[0].transcript``.
    Walk it defensively (attr or key at each hop) and fall back to a top-level
    ``transcript`` field a simpler fake might expose. Returns ``""`` when the
    event carries no text (e.g. a keep-alive / metadata frame), which the caller
    filters out.
    """
    channel = _read(payload, "channel")
    if channel is not None:
        alternatives = _read(channel, "alternatives")
        if alternatives:
            text = _read(alternatives[0], "transcript")
            if isinstance(text, str):
                return text
    direct = _read(payload, "transcript")
    return direct if isinstance(direct, str) else ""


def _confidence(payload: Any) -> float | None:
    """Optional provider confidence in ``[0, 1]`` from the best alternative."""
    channel = _read(payload, "channel")
    if channel is not None:
        alternatives = _read(channel, "alternatives")
        if alternatives:
            conf = _read(alternatives[0], "confidence")
            if isinstance(conf, (int, float)):
                return float(conf)
    return None


class DeepgramSTT:
    """Deepgram :class:`~movate.voice.base.SpeechToTextProvider` (T1 streaming).

    Streams the inbound audio into Deepgram's live transcription socket and
    yields partial :class:`~movate.voice.base.TranscriptChunk` slices as the
    caller speaks, then an ``is_final=True`` chunk per endpointed utterance.
    """

    name = "deepgram"
    version = "0.0.1"

    def __init__(
        self,
        *,
        model: str = _DEEPGRAM_MODEL,
        client: deepgram.DeepgramClient | None = None,
    ) -> None:
        """``client`` is for tests — pass a fake exposing the live-transcription
        connection shape (``listen.asyncwebsocket.v("1")`` →
        ``start``/``send``/``finish`` + an ``on(...)`` event hook). Production
        leaves it ``None`` and the SDK client is constructed from the BYOK key
        on first use."""
        self._model = model
        self._client = client

    def _resolve_client(self, api_key: str | None) -> Any:
        if self._client is not None:
            return self._client
        import os  # noqa: PLC0415

        deepgram_mod = _require_deepgram()
        # Per-call client when a tenant BYOK key is supplied (ADR 018); else fall
        # back to the SDK's own env (DEEPGRAM_API_KEY) for the local/dev path.
        key = api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        return deepgram_mod.DeepgramClient(key)

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        import asyncio  # noqa: PLC0415
        import contextlib  # noqa: PLC0415

        client = self._resolve_client(api_key)
        # The live socket: client.listen.asyncwebsocket.v("1"). The SDK exposes
        # an event-callback API (``connection.on(event, handler)``); we bridge
        # those callbacks onto an asyncio.Queue so we can yield from a plain
        # async generator (the same decouple-callback-from-generator pattern the
        # voice pipeline uses for the executor's sync on_token).
        connection = client.listen.asyncwebsocket.v("1")
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _on_transcript(_self: Any = None, result: Any = None, **_: Any) -> None:
            queue.put_nowait(("transcript", result))

        async def _on_close(_self: Any = None, **_: Any) -> None:
            queue.put_nowait(("close", None))

        async def _on_error(_self: Any = None, error: Any = None, **_: Any) -> None:
            queue.put_nowait(("error", error))

        # Register handlers. Event names live on the SDK's LiveTranscriptionEvents
        # enum; fall back to the canonical string names if the enum isn't present
        # on a fake. Registration is best-effort — a minimal fake may no-op here.
        events = (
            getattr(_require_deepgram(), "LiveTranscriptionEvents", None)
            if (self._client is None)
            else None
        )
        with contextlib.suppress(Exception):
            connection.on(getattr(events, "Transcript", "Results"), _on_transcript)
            connection.on(getattr(events, "Close", "Close"), _on_close)
            connection.on(getattr(events, "Error", "Error"), _on_error)

        await connection.start(self._build_options(language))

        async def _pump() -> None:
            try:
                async for chunk in audio:
                    await connection.send(chunk.data)
            finally:
                # Tell Deepgram the stream is done so it flushes a final result,
                # then closes the socket. ``finish`` is the SDK's graceful close.
                with contextlib.suppress(Exception):
                    await connection.finish()

        sender = asyncio.get_event_loop().create_task(_pump())

        last_partial: str | None = None
        saw_final = False
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "error":
                    raise RuntimeError(str(payload) or "deepgram transcription error")
                if kind == "close":
                    break
                if kind != "transcript":
                    continue
                text = _transcript_text(payload)
                if not text:
                    continue  # keep-alive / empty interim — nothing to surface
                final = _chunk_is_final(payload)
                if final:
                    saw_final = True
                    last_partial = None
                else:
                    last_partial = text
                yield TranscriptChunk(text=text, is_final=final, confidence=_confidence(payload))
        finally:
            if not sender.done():
                sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

        # Defensive endpointing guarantee (mirrors the OpenAI adapter's empty-
        # audio final): if the socket closed having only emitted interims,
        # promote the last interim to a final so the pipeline doesn't hang
        # waiting for an is_final that never arrives.
        if not saw_final:
            yield TranscriptChunk(text=last_partial or "", is_final=True)

    def _build_options(self, language: str | None) -> Any:
        """Build Deepgram ``LiveOptions`` for the socket.

        Falls back to a plain dict when the SDK type isn't importable (the test
        path with an injected fake), so a fake never needs the real SDK.
        """
        opts: dict[str, Any] = {
            "model": self._model,
            "encoding": _DEEPGRAM_ENCODING,
            "sample_rate": _DEEPGRAM_SAMPLE_RATE,
            "smart_format": True,
            "interim_results": True,
        }
        if language:
            opts["language"] = language
        if self._client is not None:
            # Test path: hand the fake the raw option dict.
            return opts
        live_options = getattr(_require_deepgram(), "LiveOptions", None)
        if live_options is None:  # pragma: no cover - SDK always ships it
            return opts
        return live_options(**opts)
