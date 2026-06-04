"""Twilio Media Stream transport -- ADR 074 D3.

Receives a Twilio Media Stream WebSocket connection, decodes inbound mu-law
audio via ``telephony.py``'s codec helpers, feeds PCM16 ``AudioChunk``s into
the voice pipeline, and encodes outbound TTS ``AudioChunk``s back to mu-law
for Twilio. The pipeline is unchanged (ADR 074 D8).

The Twilio Media Stream protocol sends JSON messages:

* ``connected`` -- session established, carries ``streamSid``.
* ``start``     -- stream metadata (encoding, sample rate, tracks).
* ``media``     -- base64-encoded mu-law audio payload.
* ``stop``      -- the call ended (hang-up or Twilio-side disconnect).

Outbound audio is sent as ``media`` events with base64-encoded mu-law frames.

Dependencies: the ``twilio`` SDK is imported LAZILY (only when
``TwilioTransport`` is instantiated) -- a runtime without ``mdk[telephony]``
never triggers the import (ADR 074 D5).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from movate.voice.base import AudioChunk
from movate.voice.telephony import (
    PIPELINE_RATE,
    TELEPHONY_RATE,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_pcm16,
)

logger = logging.getLogger(__name__)


class TwilioTransport:
    """Bidirectional audio bridge between a Twilio Media Stream and the pipeline.

    Implements the ``TelephonyTransport`` Protocol (ADR 074 D1).

    Usage from the runtime's WS endpoint::

        transport = TwilioTransport(websocket)
        async for audio_chunk in transport.receive_audio():
            # feed to pipeline
            ...
        await transport.send_audio(tts_chunk)

    The transport handles the Twilio Media Stream protocol (``connected``,
    ``start``, ``media``, ``stop`` events) and transcodes mu-law <-> PCM16
    at the edge using ``telephony.py`` (ADR 048 D8 / ADR 074 D7).
    """

    name: str = "twilio"

    def __init__(
        self,
        websocket: Any,
        *,
        pipeline_rate: int = PIPELINE_RATE,
        telephony_rate: int = TELEPHONY_RATE,
    ) -> None:
        self._ws = websocket
        self._pipeline_rate = pipeline_rate
        self._telephony_rate = telephony_rate
        self._stream_sid: str | None = None
        self._call_sid: str | None = None
        self._connected = False
        self._stopped = False

    @property
    def stream_sid(self) -> str | None:
        """The Twilio Media Stream SID (set after the ``start`` event)."""
        return self._stream_sid

    @property
    def call_sid(self) -> str | None:
        """The Twilio Call SID (set after the ``start`` event)."""
        return self._call_sid

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Yield PCM16 AudioChunks decoded from the Twilio Media Stream.

        Handles the Twilio protocol events (``connected``, ``start``,
        ``media``, ``stop``) and yields ``AudioChunk``s only for ``media``
        frames. Returns when the stream is stopped or the WS disconnects.
        """
        try:
            while not self._stopped:
                raw = await self._ws.receive_text()
                msg = json.loads(raw)
                event = msg.get("event", "")

                if event == "connected":
                    self._connected = True
                    logger.debug("Twilio stream connected: %s", msg.get("protocol"))

                elif event == "start":
                    start_data = msg.get("start", {})
                    self._stream_sid = start_data.get("streamSid", "")
                    self._call_sid = start_data.get("callSid", "")
                    logger.info(
                        "Twilio stream started: streamSid=%s callSid=%s",
                        self._stream_sid,
                        self._call_sid,
                    )

                elif event == "media":
                    payload = msg.get("media", {}).get("payload", "")
                    if not payload:
                        continue
                    # Decode base64 -> raw mu-law bytes -> PCM16 -> resample
                    mulaw_bytes = base64.b64decode(payload)
                    pcm = mulaw_to_pcm16(mulaw_bytes)
                    if self._pipeline_rate != self._telephony_rate:
                        pcm = resample_pcm16(pcm, self._telephony_rate, self._pipeline_rate)
                    yield AudioChunk(
                        data=pcm,
                        codec="pcm16",
                        sample_rate=self._pipeline_rate,
                    )

                elif event == "stop":
                    self._stopped = True
                    logger.info("Twilio stream stopped (callSid=%s)", self._call_sid)

                # Ignore unknown events (mark, dtmf, etc.) gracefully.
        except Exception:
            # WS disconnect or JSON parse error -- the call ended.
            self._stopped = True

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send a TTS AudioChunk back to Twilio as a mu-law media frame.

        Resamples from the chunk's sample rate to 8 kHz and mu-law encodes,
        then wraps in a Twilio ``media`` event with base64 payload.
        """
        if self._stopped or self._stream_sid is None:
            return

        pcm = chunk.data
        if chunk.sample_rate != self._telephony_rate:
            pcm = resample_pcm16(pcm, chunk.sample_rate, self._telephony_rate)
        mulaw_bytes = pcm16_to_mulaw(pcm)

        payload = base64.b64encode(mulaw_bytes).decode("ascii")
        msg = json.dumps(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            }
        )
        try:
            await self._ws.send_text(msg)
        except Exception:
            logger.debug("Failed to send audio to Twilio stream", exc_info=True)

    async def send_mark(self, name: str) -> None:
        """Send a Twilio ``mark`` event (used to track audio playback position)."""
        if self._stopped or self._stream_sid is None:
            return
        msg = json.dumps(
            {
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": name},
            }
        )
        try:
            await self._ws.send_text(msg)
        except Exception:
            logger.debug("Failed to send mark to Twilio stream", exc_info=True)

    # -- TelephonyTransport Protocol methods (ADR 074 D1) --

    async def connect(self, session_config: dict[str, Any]) -> AsyncIterator[AudioChunk]:
        """Start receiving audio from the Twilio Media Stream."""
        return self.receive_audio()

    async def publish(self, audio: AudioChunk) -> None:
        """Publish synthesized audio back to the Twilio Media Stream."""
        await self.send_audio(audio)

    async def disconnect(self) -> None:
        """Tear down the transport (the WS close is handled by the caller)."""
        self._stopped = True
