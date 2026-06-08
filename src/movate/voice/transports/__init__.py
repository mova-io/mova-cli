"""Telephony transports — ``TelephonyTransport`` Protocol + concrete adapters.

ADR 074 D1: a ``TelephonyTransport`` abstracts a bidirectional audio channel
(a LiveKit room, a Twilio Media Stream, etc.) behind the same
``AsyncIterator[AudioChunk]`` shape the pipeline already binds to. The pipeline
never learns which transport is connected (ADR 048 D8, CLAUDE.md rule 6). An
agent that works over the WebSocket works over the phone — zero changes.

Concrete adapters:

* :class:`~movate.voice.transports.twilio.TwilioTransport` — Twilio Media
  Streams (telephony PSTN).
* :class:`~movate.voice.transports.livekit.LiveKitTransport` — WebRTC-grade
  audio via LiveKit rooms (Phase 3a). Self-hostable, sovereignty-friendly,
  native SIP.

Each adapter lazily imports its provider SDK so importing this package never
pulls a heavy dependency at module scope (same posture as the speech adapters).
"""

from __future__ import annotations

import contextlib

from movate.voice.transports.base import AudioStream, TelephonyTransport

# Concrete transports are imported LAZILY so the base install doesn't pull in
# the twilio/livekit SDKs. Expose the names for convenience when the extras ARE
# installed — guarded so a missing extra doesn't break
# ``from movate.voice.transports import TelephonyTransport``.
with contextlib.suppress(ImportError):
    from movate.voice.transports.twilio import TwilioTransport  # noqa: F401

with contextlib.suppress(ImportError):
    from movate.voice.transports.livekit import LiveKitTransport  # noqa: F401

__all__ = ["AudioStream", "TelephonyTransport"]
