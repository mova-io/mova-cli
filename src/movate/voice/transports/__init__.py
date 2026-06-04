"""Telephony transports — LiveKit + Twilio adapters for phone to mdk voice agents.

ADR 074: telephony is the last transport mile. The pipeline is unchanged; these
adapters decode phone audio into ``AudioChunk`` streams the pipeline consumes
and encode TTS ``AudioChunk`` output back to phone audio. An agent that works
over the WebSocket works over the phone -- zero changes.
"""

from __future__ import annotations

from movate.voice.transports.base import TelephonyTransport

__all__ = ["TelephonyTransport"]
