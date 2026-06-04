"""TelephonyTransport Protocol -- ADR 074 D1.

A bidirectional audio channel to a telephony session (a LiveKit room or a
Twilio Media Stream). The pipeline calls it identically to the WS transport --
same ``AudioChunk`` in/out. The Protocol is the adapter-seam pattern from
ADR 048 D3 / ADR 007 / CLAUDE.md rule 7.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from movate.voice.base import AudioChunk


@runtime_checkable
class TelephonyTransport(Protocol):
    """A bidirectional audio channel to a telephony session.

    ``connect`` returns an ``AsyncIterator[AudioChunk]`` the pipeline
    consumes as ``audio_in``. ``publish`` sends a synthesized
    ``AudioChunk`` back to the caller. ``disconnect`` tears down the
    session. The pipeline calls these identically regardless of whether
    the backing session is a LiveKit room or a Twilio Media Stream.
    """

    name: str

    async def connect(self, session_config: dict[str, Any]) -> AsyncIterator[AudioChunk]: ...

    async def publish(self, audio: AudioChunk) -> None: ...

    async def disconnect(self) -> None: ...
