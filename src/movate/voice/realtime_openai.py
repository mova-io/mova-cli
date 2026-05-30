"""OpenAI Realtime adapter — full-duplex voice↔voice (ADR 048 D2b / Phase 2).

The first reference implementation behind the optional realtime seam
(:class:`movate.voice.base.RealtimeVoiceProvider`). Unlike the pipeline
adapters (Whisper STT + OpenAI TTS in :mod:`movate.voice.openai_speech`), this
is **voice-native**: audio goes in, audio comes out, the model does its own
speech recognition + synthesis, and there is **no intermediate text Executor**
(ADR 048 D2b / Boundaries). It is the premium, lowest-latency path selected via
the ``?mode=realtime`` transport mode (ADR 050 D12).

The ``openai`` SDK import is **lazy + guarded** exactly like
:mod:`movate.voice.openai_speech` / :mod:`movate.providers.openai_native`:
nothing here imports ``openai`` at module scope, so a runtime/CLI installed
without ``mdk[voice]`` is wholly unaffected (ADR 048 D9). The SDK connection is
opened on first use; tests inject a fake via the ``connect=`` kwarg (a callable
returning the realtime-connection async context manager).

BYOK (ADR 048 D6 / ADR 018): the tenant key is passed in via ``api_key=`` and
wins over the constructor default. With no per-call key the SDK reads its own
``OPENAI_API_KEY`` env — already in the credential autoload whitelist
(:data:`movate.credentials.loader.PROVIDER_KEY_ENV_VARS`), so realtime needs
**no new credential var**.

Shape notes (re-confirmed at build time, per ADR 048's caveat that the provider
landscape moves fast):

* **Session socket** — OpenAI Realtime is a bidirectional WebSocket reached via
  the SDK's ``client.beta.realtime.connect(model=...)`` async context manager.
  The adapter drives both halves concurrently: a sender task pumps the inbound
  :class:`~movate.voice.base.AudioChunk` stream into the socket as
  ``input_audio_buffer.append`` events (base64 PCM16), while the main coroutine
  consumes server events and yields :class:`~movate.voice.base.RealtimeChunk`
  slices. Server-side VAD (the default ``turn_detection``) decides turns, so the
  transport does **not** endpoint — it just forwards mic frames.
* **Event mapping** — ``response.audio.delta`` → an ``audio``
  :class:`RealtimeChunk` (the base64 PCM16 decoded to raw bytes);
  ``input_audio_buffer.speech_started`` / ``speech_stopped`` → the matching
  turn-boundary control chunks (``speech_started`` is the transport's barge-in
  cue); ``response.audio_transcript.delta`` → a ``transcript`` chunk;
  ``response.done`` → ``response_done``; ``error`` → an ``error`` chunk.
* **Audio format** — the session is configured for raw 24 kHz mono PCM16 in
  both directions (``pcm16``), matching our :class:`AudioChunk` default so the
  bytes map straight through with no container to parse at the edge (ADR 048
  D8). Telephony's μ-law transcode stays an edge concern (Phase 3).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from movate.voice.base import AudioChunk, AudioCodec, RealtimeChunk

# OpenAI Realtime's default audio I/O is raw 24 kHz mono PCM16 — header-less
# signed 16-bit LE, the same format our ``pcm16`` ``AudioChunk`` carries, so the
# decoded base64 bytes map straight through with no container parsing.
_REALTIME_SAMPLE_RATE = 24_000

# The current general-purpose realtime model. Pinned to a dated snapshot the
# same way the pipeline adapters pin ``whisper-1`` / ``tts-1`` — re-confirm at
# build time (ADR 048's fast-moving-landscape caveat).
_DEFAULT_REALTIME_MODEL = "gpt-4o-realtime-preview-2024-12-17"


def _require_openai() -> Any:
    """Import the ``openai`` SDK lazily, with a clear install hint.

    Mirrors :func:`movate.voice.openai_speech._require_openai` — the import
    lives inside the call so importing this module (e.g. for the Protocol type)
    never requires the optional dep.
    """
    try:
        import openai as _openai  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'openai' package is required for the OpenAI Realtime voice adapter. "
            "Install with: uv add 'movate-cli[voice]'"
        ) from exc
    return _openai


# A callable that opens a realtime connection given (api_key, model) and returns
# the SDK's async-context-manager connection object. The production default
# constructs an ``AsyncOpenAI`` client and calls ``beta.realtime.connect``; tests
# pass a fake exposing the same ``async with`` + ``send`` / ``recv`` shape.
RealtimeConnect = Callable[[str | None, str], Any]


class OpenAIRealtime:
    """OpenAI Realtime :class:`~movate.voice.base.RealtimeVoiceProvider`.

    Full-duplex voice↔voice over the OpenAI Realtime WebSocket. Constructed
    with a ``model`` and ``default_voice``; production opens the SDK connection
    on first use, tests inject ``connect=`` (a callable returning the realtime
    connection context manager) so no ``openai`` package / network / key is
    needed.
    """

    name = "openai_realtime"
    version = "0.0.1"

    def __init__(
        self,
        *,
        model: str = _DEFAULT_REALTIME_MODEL,
        default_voice: str = "alloy",
        connect: RealtimeConnect | None = None,
    ) -> None:
        self._model = model
        self._default_voice = default_voice
        self._connect = connect

    def _resolve_connect(self) -> RealtimeConnect:
        if self._connect is not None:
            return self._connect

        def _default(api_key: str | None, model: str) -> Any:
            openai_mod = _require_openai()
            # Per-call client when a tenant BYOK key is supplied (ADR 018); else
            # let the SDK read its own env (OPENAI_API_KEY) for the dev path.
            client = (
                openai_mod.AsyncOpenAI(api_key=api_key) if api_key else openai_mod.AsyncOpenAI()
            )
            return client.beta.realtime.connect(model=model)

        return _default

    async def session(
        self,
        audio_in: AsyncIterator[AudioChunk],
        *,
        voice_id: str = "",
        instructions: str = "",
        language: str | None = None,
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[RealtimeChunk]:
        # ``language`` is accepted for the Protocol; the realtime model
        # auto-detects from the audio, so there's no per-session knob for it.
        _ = language
        connect = self._resolve_connect()
        async for out in _stream_session(
            connect=connect,
            target=self._model,
            audio_in=audio_in,
            voice_id=voice_id or self._default_voice,
            instructions=instructions,
            codec=codec,
            api_key=api_key,
        ):
            yield out


async def _stream_session(
    *,
    connect: RealtimeConnect,
    target: str,
    audio_in: AsyncIterator[AudioChunk],
    voice_id: str,
    instructions: str,
    codec: AudioCodec,
    api_key: str | None,
) -> AsyncIterator[RealtimeChunk]:
    """Drive one realtime voice↔voice session over an opened connection.

    The shared driver for **both** the public-OpenAI and Azure-OpenAI realtime
    adapters: the two providers speak the **same** Realtime wire protocol and
    differ only in how the connection is opened (``connect`` / ``target`` — a
    model id for public OpenAI, a deployment name for Azure). Keeping the loop
    in one place avoids duplicating the concurrent-pump + event-mapping logic
    across the two adapters (CLAUDE.md rule 4).

    Configures the session (voice + persona + raw PCM16 both ways, server-side
    VAD for turn-taking), pumps the caller's mic into the socket on a sender
    task, and yields a :class:`RealtimeChunk` per surfaced server event.
    """
    async with connect(api_key, target) as conn:
        # Configure the session: voice + persona + raw PCM16 both ways, and let
        # the server's default VAD decide turns (so the transport just forwards
        # mic frames and never endpoints — D8).
        session_update: dict[str, Any] = {
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "voice": voice_id,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
            },
        }
        if instructions:
            session_update["session"]["instructions"] = instructions
        await conn.send(session_update)

        # Pump the caller's mic into the socket concurrently with consuming
        # server events — the same two-halves pattern the Deepgram STT adapter
        # uses for its live socket.
        async def _pump() -> None:
            try:
                async for chunk in audio_in:
                    await conn.send(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk.data).decode("ascii"),
                        }
                    )
            except Exception:  # pragma: no cover - mic stream errors end the pump
                return

        pump_task = asyncio.create_task(_pump())
        try:
            async for event in conn:
                out = _translate_event(event, codec)
                if out is not None:
                    yield out
                    if out.kind == "error":
                        return
        finally:
            if not pump_task.done():
                pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task


def _event_type(event: Any) -> str:
    """Pull the discriminator off a realtime server event.

    The SDK yields typed event objects (``event.type``); a fake or a raw dict
    may use ``event["type"]``. Accept both so the adapter is resilient and
    tests can hand back the simplest shape.
    """
    etype = getattr(event, "type", None)
    if etype is None and isinstance(event, dict):
        etype = event.get("type")
    return str(etype or "")


def _event_field(event: Any, name: str) -> Any:
    """Read ``name`` off a typed event object or a dict event."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def _translate_event(event: Any, codec: AudioCodec) -> RealtimeChunk | None:
    """Map one OpenAI Realtime server event to a :class:`RealtimeChunk`.

    Returns ``None`` for events the transport doesn't surface (session acks,
    buffer commits, etc.) so the caller simply skips them. Keeping the mapping
    in one pure function makes the event contract testable without a socket.
    """
    etype = _event_type(event)

    if etype == "response.audio.delta":
        delta = _event_field(event, "delta")
        if not delta:
            return None
        try:
            data = base64.b64decode(delta)
        except (ValueError, TypeError):  # malformed frame → skip, don't crash the session
            return None
        return RealtimeChunk(
            kind="audio",
            audio=AudioChunk(data=data, codec=codec, sample_rate=_REALTIME_SAMPLE_RATE),
        )

    if etype == "response.audio_transcript.delta":
        text = _event_field(event, "delta") or ""
        if not text:
            return None
        return RealtimeChunk(kind="transcript", text=str(text), is_final=False)

    if etype == "conversation.item.input_audio_transcription.completed":
        text = _event_field(event, "transcript") or ""
        return RealtimeChunk(kind="transcript", text=str(text), is_final=True)

    if etype == "input_audio_buffer.speech_started":
        return RealtimeChunk(kind="speech_started")

    if etype == "input_audio_buffer.speech_stopped":
        return RealtimeChunk(kind="speech_stopped")

    if etype == "response.done":
        return RealtimeChunk(kind="response_done")

    if etype == "error":
        err = _event_field(event, "error")
        message = ""
        code = "realtime_error"
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            code = str(err.get("code") or code)
        elif err is not None:
            message = str(getattr(err, "message", "") or err)
            code = str(getattr(err, "code", code) or code)
        return RealtimeChunk(kind="error", message=message or "realtime session error", code=code)

    return None
