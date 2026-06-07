"""Telephony transports — LiveKit + Twilio adapters for phone to mdk voice agents.

ADR 074: telephony is the last transport mile. The pipeline is unchanged; these
adapters decode phone audio into ``AudioChunk`` streams the pipeline consumes
and encode TTS ``AudioChunk`` output back to phone audio. An agent that works
over the WebSocket works over the phone -- zero changes.
"""

from __future__ import annotations

import contextlib

from movate.voice.transports.base import TelephonyTransport

# Concrete transports are imported LAZILY by consumers (not here) so the
# base install doesn't pull in twilio/livekit SDKs. Expose the names for
# convenience when the extras ARE installed — guarded so a missing extra
# doesn't break ``from movate.voice.transports import TelephonyTransport``.
with contextlib.suppress(ImportError):
    from movate.voice.transports.twilio import TwilioTransport  # noqa: F401

with contextlib.suppress(ImportError):
    from movate.voice.transports.livekit import LiveKitTransport  # noqa: F401

__all__ = ["TelephonyTransport"]
