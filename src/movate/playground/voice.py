"""Voice-mode WS client for the playground (pure logic + a thin WS wrapper).

Voice mode is **opt-in** (``mdk playground serve --voice`` / the per-session
``MDK_PLAYGROUND_VOICE`` env flag) and **additive**: with it OFF the text
playground is byte-for-byte unchanged. When ON, the Chainlit app captures mic
audio (``@cl.on_audio_chunk``), streams the frames to the runtime's
``WS /api/v1/agents/{name}/voice`` route (ADR 048 D4 / voice Phase 1), renders
the partial/final transcripts + the agent's streamed answer text as they
arrive, and plays the returned TTS audio back via ``cl.Audio``.

This module is the transport half — it mirrors the runtime voice route's wire
protocol (see the "Voice WS message protocol" block in ``runtime/app.py``):

    client → server
      {"type": "config", ...}      optional, first — input_key / language /
                                   voice_id / mock for THIS turn
      <binary frame>               an inbound audio chunk (pcm16 by default)
      {"type": "end"}              the caller finished the utterance → run it
      {"type": "interrupt"}        barge-in — cancel the in-flight answer
      {"type": "close"}            end the session

    server → client
      {"type": "transcript.partial", "text": ...}   streaming partial (STT)
      {"type": "transcript.final",   "text": ...}   endpointed utterance
      {"type": "agent.token",        "text": ...}   streamed agent token
      {"type": "tts.audio", "codec","sample_rate","bytes"}  audio header, then
      <binary frame>                                the raw synthesized audio
      {"type": "latency", "badge", "responded_in_ms", ...}  per-stage timings
      {"type": "usage", ...}                        end-of-turn meter
      {"type": "error", "message","code","stage"}   a stage failure + degrade
      {"type": "done", "run_id","status"}           terminal for the turn

Kept Chainlit-free and import-light (only ``websockets`` + stdlib) so the
URL/event/capability logic is unit-testable on a no-extras install, exactly
like :mod:`movate.playground.sse`. The Chainlit app (:mod:`movate.playground.app`)
binds these to the audio callbacks; tests drive the parser + URL builder
directly and the client against a fake socket.

``websockets`` is declared in the ``[playground]`` extra (BSD-3-Clause,
permissive — same posture as the other playground deps). It is imported
lazily inside :class:`VoiceWSClient` so importing this module on a no-extras
install (for the pure helpers) never requires it.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------
#
# The runtime's GET /api/v1/capabilities endpoint detects features by probing
# the FastAPI route table — and that probe intentionally SKIPS WebSocket routes
# (see ``runtime/capabilities._registered_paths``), so today's runtime does NOT
# advertise a "voice" feature even though the WS /voice route exists. We still
# look for a voice flag here (so a future runtime that adds one auto-enables the
# UI), but the playground does NOT hard-gate on it: voice mode is offered
# whenever the operator asked for it, and a runtime without the route degrades
# to a friendly "voice not enabled on this runtime" message rather than a crash.

# Capability slugs a runtime might use to advertise the voice transport.
_VOICE_FEATURE_NAMES: tuple[str, ...] = ("voice", "voice_ws", "voice_pipeline")


def _coerce_bool(value: Any) -> bool:
    """Best-effort truthiness for a capability flag (mirrors capabilities.py)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on", "enabled"}
    return False


def runtime_advertises_voice(capabilities_raw: dict[str, Any] | None) -> bool:
    """True only when the runtime's capabilities payload advertises voice.

    Reads the same ``features`` block :func:`~movate.playground.capabilities.
    parse_capabilities` reads (a dict ``{"voice": true}`` OR a list of enabled
    slugs ``["voice", ...]``). Returns ``False`` for a ``None`` payload (the
    endpoint was absent / 404) or any shape that doesn't name voice.

    This is a *positive* signal, never a gate: a ``False`` here does NOT mean
    voice is unavailable (the route is a WebSocket the capabilities probe can't
    see). The app uses it only to tailor the banner; the real test is attempting
    the connection and degrading gracefully on failure.
    """
    if not isinstance(capabilities_raw, dict):
        return False
    features = capabilities_raw.get("features")
    if isinstance(features, dict):
        return any(_coerce_bool(features.get(n)) for n in _VOICE_FEATURE_NAMES)
    if isinstance(features, (list, tuple, set)):
        present = {str(item).strip().lower() for item in features}
        return any(n in present for n in _VOICE_FEATURE_NAMES)
    return False


# ---------------------------------------------------------------------------
# WS URL building
# ---------------------------------------------------------------------------


def build_voice_ws_url(
    runtime_url: str,
    agent: str,
    *,
    token: str | None = None,
) -> str:
    """Build the ``ws(s)://.../api/v1/agents/{agent}/voice`` URL.

    Mirrors the runtime route: a WebSocket handshake can't carry an
    ``Authorization`` header from a browser, so the bearer token rides on the
    ``?token=`` query param (the route accepts that, ADR 048 D4). The scheme is
    flipped ``http→ws`` / ``https→wss`` from the configured runtime base URL so
    a single ``--runtime-url`` drives both the HTTP client and the voice socket.

    ``agent`` and ``token`` are URL-encoded so an agent name with reserved chars
    or a token with ``+`` / ``=`` can't corrupt the URL.
    """
    parts = urlsplit(runtime_url.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = f"{parts.path}/api/v1/agents/{quote(agent, safe='')}/voice"
    query = f"token={quote(token, safe='')}" if token else parts.query
    return urlunsplit((scheme, parts.netloc, path, query, ""))


# ---------------------------------------------------------------------------
# Typed server→client events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceFrame:
    """One parsed server→client frame from the voice WS.

    A control frame carries its ``type`` discriminator + payload in
    :attr:`data`; an audio frame carries the raw synthesized bytes in
    :attr:`audio` (with the preceding header's ``codec`` / ``sample_rate``
    folded onto :attr:`data` so the player has the format).
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    audio: bytes | None = None

    @property
    def text(self) -> str:
        """The ``text`` field of a transcript / agent.token frame."""
        value = self.data.get("text")
        return value if isinstance(value, str) else ""

    @property
    def is_partial(self) -> bool:
        return self.type == "transcript.partial"

    @property
    def is_final_transcript(self) -> bool:
        return self.type == "transcript.final"

    @property
    def is_agent_token(self) -> bool:
        return self.type == "agent.token"

    @property
    def is_audio(self) -> bool:
        return self.type == "tts.audio" and self.audio is not None

    @property
    def is_error(self) -> bool:
        return self.type == "error"

    @property
    def is_done(self) -> bool:
        return self.type == "done"

    @property
    def is_latency(self) -> bool:
        """A ``latency`` frame carrying the turn's per-stage badge (demo polish)."""
        return self.type == "latency"

    @property
    def is_speech_started(self) -> bool:
        """The realtime barge-in cue — the caller started speaking; stop playback."""
        return self.type == "speech_started"

    @property
    def latency_badge(self) -> str:
        """The ready-to-render "responded in {X}ms" string off a ``latency`` frame."""
        value = self.data.get("badge")
        return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Control-frame builders (client → server)
# ---------------------------------------------------------------------------


def config_frame(
    *,
    input_key: str = "text",
    language: str | None = None,
    voice_id: str = "",
    mock: bool = False,
) -> str:
    """JSON ``config`` control frame for the start of a turn.

    Only non-default fields are sent so the runtime keeps its own defaults for
    anything we don't override (the route reads each field defensively).
    """
    payload: dict[str, Any] = {"type": "config"}
    if input_key and input_key != "text":
        payload["input_key"] = input_key
    if language:
        payload["language"] = language
    if voice_id:
        payload["voice_id"] = voice_id
    if mock:
        payload["mock"] = True
    return json.dumps(payload, separators=(",", ":"))


def end_frame() -> str:
    """JSON ``end`` control frame — the caller finished the utterance."""
    return json.dumps({"type": "end"}, separators=(",", ":"))


def close_frame() -> str:
    """JSON ``close`` control frame — end the voice session."""
    return json.dumps({"type": "close"}, separators=(",", ":"))


def interrupt_frame() -> str:
    """JSON ``interrupt`` control frame — barge-in: cancel the in-flight answer.

    Sent when the user starts speaking while the agent is still talking; the
    runtime's pipeline path honors it by stopping the in-flight TTS so the agent
    isn't talking over the user (the realtime path uses the provider's own VAD
    instead). Additive — an older runtime ignores an unknown control frame.
    """
    return json.dumps({"type": "interrupt"}, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Frame parsing (the byte-mirror of the runtime's _send_voice_event)
# ---------------------------------------------------------------------------


def parse_control_frame(text: str) -> VoiceFrame | None:
    """Parse one JSON control frame text into a :class:`VoiceFrame`.

    Returns ``None`` for a non-JSON / non-object / type-less frame so a stray
    keep-alive can't crash the consumer (defense in depth — the runtime only
    writes well-formed control frames).

    A ``tts.audio`` control frame is the *header* announcing the binary frame
    that follows; we return it with ``audio=None`` and the caller attaches the
    next binary frame's bytes (see :meth:`VoiceWSClient.iter_turn`).
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    type_ = parsed.get("type")
    if not isinstance(type_, str):
        return None
    return VoiceFrame(type=type_, data=parsed)


def collect_audio(frames: Iterable[VoiceFrame]) -> bytes:
    """Concatenate every audio frame's bytes into one buffer.

    The runtime may emit several ``tts.audio`` chunks per turn (streaming
    synthesis); the player wants one clip, so we join them in arrival order.
    """
    return b"".join(f.audio for f in frames if f.audio is not None)


# ---------------------------------------------------------------------------
# The WS client
# ---------------------------------------------------------------------------


class VoiceNotEnabledError(RuntimeError):
    """Raised when the voice WS can't be reached on the target runtime.

    The route is absent (an old runtime), voice deps aren't installed, or the
    handshake was rejected (e.g. a 1008 policy close). The app catches this and
    shows a friendly "voice not enabled on this runtime" message rather than a
    stack trace — voice is opt-in and best-effort, never a hard requirement.
    """


@dataclass
class VoiceWSClient:
    """Thin async wrapper over a single voice WebSocket connection.

    One client serves one session (which may run multiple turns). The bearer
    token rides in the ``?token=`` query param (set at :meth:`connect`); each
    turn is: send a ``config`` frame, stream binary audio frames via
    :meth:`send_audio`, send ``end``, then consume :meth:`iter_turn` until the
    terminal ``done``/``error``. :meth:`aclose` sends ``close`` and shuts the
    socket.

    ``websockets`` is imported lazily in :meth:`connect` so this module's pure
    helpers import without the extra installed.
    """

    runtime_url: str
    agent: str
    token: str | None = None
    open_timeout_s: float = 10.0
    _ws: Any = field(default=None, init=False, repr=False)

    @property
    def url(self) -> str:
        """The resolved ``ws(s)://`` URL this client connects to."""
        return build_voice_ws_url(self.runtime_url, self.agent, token=self.token)

    async def connect(self) -> None:
        """Open the WebSocket. Raises :class:`VoiceNotEnabledError` on failure.

        Any connect-time failure (missing ``websockets`` dep, route absent /
        404, handshake rejected, connection refused) is normalized to
        :class:`VoiceNotEnabledError` so the app shows one friendly message for
        every "this runtime can't do voice" case.
        """
        try:
            from websockets.asyncio.client import connect as ws_connect  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - dep declared in the extra
            raise VoiceNotEnabledError(
                "the 'websockets' dependency is not installed — reinstall the "
                "playground extra: uv pip install 'movate-cli[playground]'"
            ) from exc
        try:
            self._ws = await ws_connect(self.url, open_timeout=self.open_timeout_s)
        except Exception as exc:  # connection refused / handshake rejected / timeout
            raise VoiceNotEnabledError(
                f"could not open a voice connection to {self.runtime_url!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def send_config(
        self,
        *,
        input_key: str = "text",
        language: str | None = None,
        voice_id: str = "",
        mock: bool = False,
    ) -> None:
        """Send the per-turn ``config`` control frame (call before audio)."""
        await self._ws.send(
            config_frame(input_key=input_key, language=language, voice_id=voice_id, mock=mock)
        )

    async def send_audio(self, chunk: bytes) -> None:
        """Send one binary audio frame (an inbound mic chunk)."""
        if chunk:
            await self._ws.send(chunk)

    async def end_turn(self) -> None:
        """Signal end-of-utterance — the runtime runs the turn."""
        await self._ws.send(end_frame())

    async def send_interrupt(self) -> None:
        """Barge-in: ask the runtime to cancel the in-flight answer (best-effort).

        Never raises — a barge-in racing a socket teardown must not crash the
        UI; the turn ends on its own if the signal doesn't land.
        """
        with contextlib.suppress(Exception):
            await self._ws.send(interrupt_frame())

    async def iter_turn(self) -> AsyncIterator[VoiceFrame]:
        """Yield server frames for the current turn until ``done``/``error``.

        Binary frames arrive *after* their ``tts.audio`` JSON header; we pair
        them so the consumer gets one :class:`VoiceFrame` carrying both the
        format (from the header) and the bytes. A stray binary frame with no
        preceding header is still surfaced as a ``tts.audio`` frame so audio is
        never silently dropped.
        """
        pending_audio_header: VoiceFrame | None = None
        async for message in self._ws:
            if isinstance(message, (bytes, bytearray)):
                header = pending_audio_header
                pending_audio_header = None
                data = header.data if header is not None else {"type": "tts.audio"}
                yield VoiceFrame(type="tts.audio", data=data, audio=bytes(message))
                continue
            frame = parse_control_frame(message)
            if frame is None:
                continue
            if frame.type == "tts.audio":
                # Header — hold it; the binary frame that follows carries bytes.
                pending_audio_header = frame
                continue
            yield frame
            if frame.is_done or frame.is_error:
                return

    async def aclose(self) -> None:
        """Send ``close`` (best-effort) and shut the socket.

        Never raises — a disconnect mid-session is normal (the user navigated
        away); cleanup must not surface an error into the UI.
        """
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.send(close_frame())
        with contextlib.suppress(Exception):
            await ws.close()


__all__ = [
    "VoiceFrame",
    "VoiceNotEnabledError",
    "VoiceWSClient",
    "build_voice_ws_url",
    "close_frame",
    "collect_audio",
    "config_frame",
    "end_frame",
    "interrupt_frame",
    "parse_control_frame",
    "runtime_advertises_voice",
]
