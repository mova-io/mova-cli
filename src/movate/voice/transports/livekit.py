"""LiveKit WebRTC transport -- ADR 074 D2.

Connects to a LiveKit room, subscribes to participant audio tracks, yields
PCM16 ``AudioChunk``s into the voice pipeline, and publishes outbound TTS
audio back to the room. The pipeline is unchanged (ADR 074 D8) -- same
``AudioChunk`` in/out as the Twilio and browser-WS transports.

Key advantages over Twilio:
* **16 kHz Opus** (vs 8 kHz mu-law) -- higher audio fidelity, better STT accuracy
* **WebRTC transport** -- sub-200ms transport latency (vs Twilio's ~100-400ms WS)
* **Built-in SIP gateway** -- LiveKit natively bridges SIP/PSTN calls into rooms
* **No codec transcoding** -- LiveKit SDK handles Opus->PCM; no mulaw_to_pcm16 needed

Dependencies: ``livekit`` and ``livekit-agents`` are imported LAZILY inside
``LiveKitTransport.__init__`` -- a runtime without ``mdk[telephony]`` never
triggers the import (ADR 074 D5).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import AsyncIterator
from typing import Any

from movate.voice.base import AudioChunk
from movate.voice.telephony import PIPELINE_RATE, resample_pcm16

logger = logging.getLogger(__name__)

# LiveKit's default audio output is 48 kHz stereo; the agents SDK
# provides 16 kHz mono PCM16 when using AudioStream. We match PIPELINE_RATE.
LIVEKIT_SAMPLE_RATE = 48_000


class LiveKitTransport:
    """Bidirectional audio bridge between a LiveKit room and the voice pipeline.

    Implements the ``TelephonyTransport`` Protocol (ADR 074 D1).

    The transport connects to a LiveKit room using the provided access token,
    subscribes to the first participant's audio track, and yields PCM16
    ``AudioChunk``s at ``PIPELINE_RATE`` (16 kHz). Outbound TTS audio is
    published back to the room as a local audio track.

    Usage::

        transport = LiveKitTransport()
        audio_in = await transport.connect({
            "livekit_url": "wss://my-livekit.example.com",
            "token": "<access-token>",
            "room_name": "call-123",
        })
        async for chunk in audio_in:
            # feed to pipeline
            ...
        await transport.publish(tts_chunk)
        await transport.disconnect()
    """

    name: str = "livekit"

    def __init__(self, *, pipeline_rate: int = PIPELINE_RATE) -> None:
        self._pipeline_rate = pipeline_rate
        self._room: Any = None
        self._audio_source: Any = None
        self._audio_stream: Any = None
        self._connected = False
        self._stopped = False
        self._room_name: str = ""
        self._participant_identity: str = ""

    @property
    def room_name(self) -> str:
        """The LiveKit room name (set after connect)."""
        return self._room_name

    @property
    def participant_identity(self) -> str:
        """The identity of the connected participant."""
        return self._participant_identity

    async def connect(self, session_config: dict[str, Any]) -> AsyncIterator[AudioChunk]:
        """Connect to a LiveKit room and start receiving audio.

        ``session_config`` keys:
        * ``livekit_url`` -- WebSocket URL of the LiveKit server (required)
        * ``token`` -- access token for the room (required)
        * ``room_name`` -- room name (informational, for logging)

        Returns an async iterator of PCM16 ``AudioChunk``s from the
        participant's audio track.
        """
        from livekit import rtc  # noqa: PLC0415

        livekit_url = session_config["livekit_url"]
        token = session_config["token"]
        self._room_name = session_config.get("room_name", "")

        # Create and connect to the room.
        self._room = rtc.Room()

        # Set up audio source for publishing TTS back to the room.
        self._audio_source = rtc.AudioSource(
            sample_rate=self._pipeline_rate,
            num_channels=1,
        )

        await self._room.connect(livekit_url, token)
        self._connected = True

        logger.info(
            "livekit: connected to room=%s url=%s",
            self._room_name,
            livekit_url[:50],
        )

        # Publish our audio track so the participant hears TTS output.
        local_track = rtc.LocalAudioTrack.create_audio_track("agent-voice", self._audio_source)
        publish_options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self._room.local_participant.publish_track(local_track, publish_options)

        logger.info("livekit: published local audio track 'agent-voice'")

        return self._receive_audio()

    async def _receive_audio(self) -> AsyncIterator[AudioChunk]:  # noqa: PLR0912
        """Subscribe to the first remote participant's audio and yield PCM16 chunks.

        LiveKit's ``AudioStream`` provides audio frames as ``AudioFrame``
        objects with PCM16 data. We resample to ``PIPELINE_RATE`` if needed.
        """
        from livekit import rtc  # noqa: PLC0415

        if self._room is None:
            return

        # Wait for a remote participant to join and publish audio.
        participant = None
        audio_track = None

        # Check existing participants first.
        for p in self._room.remote_participants.values():
            for track_pub in p.track_publications.values():
                if track_pub.kind == rtc.TrackKind.KIND_AUDIO and track_pub.track:
                    participant = p
                    audio_track = track_pub.track
                    break
            if audio_track:
                break

        # If no participant yet, wait for one to join.
        if audio_track is None:
            event = asyncio.Event()
            found: dict[str, Any] = {}

            @self._room.on("track_subscribed")  # type: ignore[untyped-decorator]
            def on_track_subscribed(track: Any, publication: Any, participant_obj: Any) -> None:
                if publication.kind == rtc.TrackKind.KIND_AUDIO:
                    found["track"] = track
                    found["participant"] = participant_obj
                    event.set()

            try:
                await asyncio.wait_for(event.wait(), timeout=30.0)
            except TimeoutError:
                logger.warning("livekit: no audio track received within 30s")
                return

            audio_track = found.get("track")
            participant = found.get("participant")

        if audio_track is None:
            logger.warning("livekit: no audio track found")
            return

        self._participant_identity = getattr(participant, "identity", "unknown")
        logger.info(
            "livekit: subscribed to audio from participant=%s",
            self._participant_identity,
        )

        # Create an AudioStream to receive frames.
        audio_stream = rtc.AudioStream(audio_track)
        self._audio_stream = audio_stream

        try:
            async for frame_event in audio_stream:
                if self._stopped:
                    break

                frame = frame_event.frame
                # LiveKit AudioFrame provides raw PCM data.
                pcm_data = bytes(frame.data)
                sample_rate = frame.sample_rate
                num_channels = frame.num_channels

                # Convert stereo to mono if needed.
                if num_channels > 1:
                    pcm_data = _stereo_to_mono(pcm_data, num_channels)

                # Resample to pipeline rate if needed.
                if sample_rate != self._pipeline_rate:
                    pcm_data = resample_pcm16(pcm_data, sample_rate, self._pipeline_rate)

                yield AudioChunk(
                    data=pcm_data,
                    codec="pcm16",
                    sample_rate=self._pipeline_rate,
                )
        except Exception:
            logger.debug("livekit: audio stream ended", exc_info=True)
        finally:
            self._stopped = True

    async def publish(self, audio: AudioChunk) -> None:
        """Publish a TTS AudioChunk to the LiveKit room.

        The audio is sent via the local ``AudioSource`` which LiveKit
        encodes to Opus and transmits to all participants.
        """
        if self._stopped or self._audio_source is None:
            return

        from livekit import rtc  # noqa: PLC0415

        pcm_data = audio.data
        sample_rate = audio.sample_rate

        # Resample to pipeline rate if the TTS provider used a different rate.
        if sample_rate != self._pipeline_rate:
            pcm_data = resample_pcm16(pcm_data, sample_rate, self._pipeline_rate)

        # Create an AudioFrame from the PCM data.
        num_samples = len(pcm_data) // 2  # 16-bit = 2 bytes per sample
        frame = rtc.AudioFrame(
            data=pcm_data,
            sample_rate=self._pipeline_rate,
            num_channels=1,
            samples_per_channel=num_samples,
        )

        try:
            await self._audio_source.capture_frame(frame)
        except Exception:
            logger.debug("livekit: failed to publish audio frame", exc_info=True)

    async def disconnect(self) -> None:
        """Tear down the LiveKit connection."""
        self._stopped = True

        if self._audio_stream is not None:
            with contextlib.suppress(Exception):
                await self._audio_stream.aclose()
            self._audio_stream = None

        if self._room is not None:
            with contextlib.suppress(Exception):
                await self._room.disconnect()
            self._room = None

        logger.info(
            "livekit: disconnected from room=%s participant=%s",
            self._room_name,
            self._participant_identity,
        )


def _stereo_to_mono(data: bytes, channels: int = 2) -> bytes:
    """Downmix interleaved PCM16 stereo to mono by averaging channels."""
    if channels <= 1:
        return data
    samples_per_channel = len(data) // (2 * channels)
    all_samples = struct.unpack(f"<{samples_per_channel * channels}h", data)
    mono = []
    for i in range(samples_per_channel):
        total = sum(all_samples[i * channels + c] for c in range(channels))
        mono.append(max(-32768, min(32767, total // channels)))
    return struct.pack(f"<{len(mono)}h", *mono)
