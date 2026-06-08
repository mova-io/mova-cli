"""``TelephonyTransport`` Protocol — the adapter seam for telephony sessions.

ADR 074 D1.  A ``TelephonyTransport`` wraps a bidirectional audio channel (a
LiveKit room or a Twilio Media Stream) behind a three-method contract:

* ``connect`` — join the session and return an inbound audio stream.
* ``publish`` — send a synthesized ``AudioChunk`` back to the caller.
* ``disconnect`` — tear down the session.

The pipeline calls these identically regardless of which backing session is
connected — the same adapter-seam pattern as ``SpeechToTextProvider`` /
``TextToSpeechProvider`` (ADR 048 D3, CLAUDE.md rule 7).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from movate.voice.base import AudioChunk

# The inbound audio iterator returned by ``connect``.
AudioStream = AsyncIterator[AudioChunk]


@runtime_checkable
class TelephonyTransport(Protocol):
    """A bidirectional audio channel to a telephony session.

    Implementations wrap a provider-specific session (a LiveKit room, a Twilio
    Media Stream) and present the same ``AudioChunk`` iterators the pipeline
    already binds to.  The pipeline calls it identically to the WS transport.

    Attributes:
        name: Short identifier for the transport (``"livekit"``, ``"twilio"``).
    """

    name: str

    async def connect(self, session_config: dict[str, Any]) -> AudioStream:
        """Join or create the telephony session.

        ``session_config`` carries transport-specific parameters (room name,
        token, URL for LiveKit; stream URL / call SID for Twilio).  Returns an
        :data:`AudioStream` the pipeline consumes as ``audio_in``.
        """
        ...

    async def publish(self, audio: AudioChunk) -> None:
        """Send a synthesized ``AudioChunk`` back to the caller.

        Called by the voice pipeline (or a wrapping transport loop) for each
        TTS audio frame.  The transport encodes/publishes it to the session.
        """
        ...

    async def disconnect(self) -> None:
        """Tear down the telephony session.

        Leaves/closes the room or stream, releasing resources.  Safe to call
        more than once (idempotent) and safe to call if ``connect`` was never
        called (no-op).
        """
        ...
