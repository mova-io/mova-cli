"""LiveKit agent bridge — joins a room and runs the mdk voice pipeline.

Mirrors ``twilio_bridge.py`` but for LiveKit rooms. When a participant
joins a LiveKit room, this bridge:

1. Subscribes to their audio track (16 kHz PCM via WebRTC)
2. Runs the mdk voice pipeline (STT → Agent → TTS)
3. Publishes TTS audio back to the room

No mu-law transcoding needed (unlike Twilio) — LiveKit handles Opus→PCM
at the SDK level. The pipeline gets 16 kHz PCM directly.

Usage from the demo server::

    from livekit_bridge import handle_livekit_room

    await handle_livekit_room(
        livekit_url="wss://...",
        token="<agent-token>",
        room_name="call-123",
        build_pipeline_kwargs=lambda turn: {...},
        on_turn_done=callback,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from movate.voice.base import AudioChunk
from movate.voice.pipeline import run_voice_pipeline
from movate.voice.telephony import PIPELINE_RATE, resample_pcm16

log = logging.getLogger(__name__)


async def _publish_data(room: Any, event: dict[str, Any]) -> None:
    """Publish a JSON event over the LiveKit DataChannel (reliable)."""
    try:
        from livekit import rtc  # noqa: PLC0415

        payload = json.dumps(event).encode()
        await room.local_participant.publish_data(
            payload,
            kind=rtc.DataPacketKind.KIND_RELIABLE,
        )
    except Exception:
        log.debug("livekit_bridge: failed to publish data event %s", event.get("event", ""))

# LiveKit audio constants
LIVEKIT_FRAME_DURATION_MS = 20  # Standard WebRTC frame
SILENCE_RMS = 400.0
MIN_SPEECH_MS = 300
SILENCE_END_MS = 1200


async def handle_livekit_room(
    *,
    livekit_url: str,
    token: str,
    room_name: str,
    build_pipeline_kwargs: Callable[[int], dict[str, Any]],
    on_turn_done: Callable[[list[object]], None] | None = None,
    on_event: Callable[..., Any] | None = None,
    on_call_start: Callable[..., Any] | None = None,
    on_call_end: Callable[..., Any] | None = None,
    publish_events: bool = False,
) -> None:
    """Join a LiveKit room as an agent and run the voice pipeline.

    This is the LiveKit equivalent of ``handle_twilio_call`` — same
    callback signatures, same pipeline, different transport.
    """
    from livekit import rtc  # noqa: PLC0415

    room = rtc.Room()

    # Audio source for publishing TTS back to the room.
    audio_source = rtc.AudioSource(
        sample_rate=PIPELINE_RATE,
        num_channels=1,
    )

    try:
        await room.connect(livekit_url, token)
        log.info("livekit_bridge: connected to room=%s", room_name)

        # Publish our audio track.
        local_track = rtc.LocalAudioTrack.create_audio_track(
            "agent-voice", audio_source
        )
        publish_options = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_MICROPHONE
        )
        await room.local_participant.publish_track(local_track, publish_options)

        if on_call_start:
            await on_call_start(room_name, None)

        # Wait for a participant to join with audio.
        audio_track = await _wait_for_audio_track(room)
        if audio_track is None:
            log.warning("livekit_bridge: no participant audio within 60s")
            return

        participant_id = getattr(audio_track, "sid", "unknown")
        log.info("livekit_bridge: got audio from participant=%s", participant_id)

        # Run turns.
        turn = 0
        audio_stream = rtc.AudioStream(audio_track)

        while True:
            turn += 1
            kwargs = build_pipeline_kwargs(turn)

            # Collect audio chunks from the participant.
            audio_in = _audio_from_livekit(audio_stream, room)

            events: list[object] = []
            cancel = asyncio.Event()

            try:
                async for ev in run_voice_pipeline(
                    audio_in=audio_in,
                    cancel=cancel,
                    **kwargs,
                ):
                    events.append(ev)

                    # Publish TTS audio back to the room.
                    if ev.kind == "tts.audio" and hasattr(ev, "audio"):
                        frame = rtc.AudioFrame(
                            data=ev.audio.data,
                            sample_rate=ev.audio.sample_rate,
                            num_channels=1,
                            samples_per_channel=len(ev.audio.data) // 2,
                        )
                        await audio_source.capture_frame(frame)

                    # Forward events to the caller.
                    if on_event and hasattr(ev, "kind"):
                        try:
                            await on_event(ev.kind)
                        except Exception:  # noqa: BLE001
                            pass

                    # Publish pipeline events over DataChannel so the
                    # browser UI can display transcripts, tokens, etc.
                    if publish_events and hasattr(ev, "kind"):
                        evt = ev.kind
                        if evt in (
                            "transcript.partial",
                            "transcript.final",
                            "agent.token",
                        ):
                            await _publish_data(
                                room, {"event": evt, "text": ev.text}
                            )
                        elif evt == "error":
                            await _publish_data(room, {
                                "event": "error",
                                "stage": getattr(ev, "stage", ""),
                                "code": getattr(ev, "code", ""),
                                "message": getattr(ev, "message", ""),
                            })

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("livekit_bridge: pipeline error on turn %d", turn)
                if publish_events:
                    await _publish_data(room, {"event": "error", "message": "pipeline error"})
                break

            # Signal turn completion over DataChannel.
            if publish_events:
                await _publish_data(room, {"event": "done"})

            if on_turn_done:
                on_turn_done(events)

            # Check if the participant disconnected.
            if not room.remote_participants:
                log.info("livekit_bridge: participant left, ending session")
                break

    except Exception:
        log.exception("livekit_bridge: room connection error")
    finally:
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        log.info("livekit_bridge: disconnected from room=%s turns=%d", room_name, turn)
        if on_call_end:
            await on_call_end(turn)


async def _wait_for_audio_track(room: Any, timeout: float = 60.0) -> Any:
    """Wait for a remote participant to publish an audio track."""
    from livekit import rtc  # noqa: PLC0415

    # Check existing participants first.
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.track:
                return pub.track

    # Wait for a new track.
    event = asyncio.Event()
    result: dict[str, Any] = {}

    @room.on("track_subscribed")  # type: ignore[misc]
    def on_track(track: Any, publication: Any, participant: Any) -> None:
        if publication.kind == rtc.TrackKind.KIND_AUDIO:
            result["track"] = track
            event.set()

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        return None

    return result.get("track")


async def _audio_from_livekit(
    audio_stream: Any,
    room: Any,
) -> AsyncIterator[AudioChunk]:
    """Yield PCM16 AudioChunks from a LiveKit AudioStream.

    Includes basic endpointing: yields audio while speech is detected,
    stops when silence exceeds SILENCE_END_MS after speech started.
    """
    import struct  # noqa: PLC0415

    speech_started = False
    speech_ms = 0
    silence_ms = 0

    async for frame_event in audio_stream:
        frame = frame_event.frame
        pcm_data = bytes(frame.data)
        sample_rate = frame.sample_rate
        num_channels = frame.num_channels

        # Stereo to mono if needed.
        if num_channels > 1:
            samples_per_ch = len(pcm_data) // (2 * num_channels)
            all_samples = struct.unpack(f"<{samples_per_ch * num_channels}h", pcm_data)
            mono = []
            for i in range(samples_per_ch):
                total = sum(all_samples[i * num_channels + c] for c in range(num_channels))
                mono.append(max(-32768, min(32767, total // num_channels)))
            pcm_data = struct.pack(f"<{len(mono)}h", *mono)

        # Resample to pipeline rate if needed.
        if sample_rate != PIPELINE_RATE:
            pcm_data = resample_pcm16(pcm_data, sample_rate, PIPELINE_RATE)

        # Simple RMS-based VAD for endpointing.
        samples = struct.unpack(f"<{len(pcm_data) // 2}h", pcm_data)
        rms = (sum(s * s for s in samples) / max(len(samples), 1)) ** 0.5
        frame_ms = (len(samples) * 1000) // PIPELINE_RATE

        if rms > SILENCE_RMS:
            silence_ms = 0
            speech_ms += frame_ms
            if speech_ms >= MIN_SPEECH_MS:
                speech_started = True
        else:
            if speech_started:
                silence_ms += frame_ms
                if silence_ms >= SILENCE_END_MS:
                    # End of speech — stop yielding.
                    yield AudioChunk(
                        data=pcm_data,
                        codec="pcm16",
                        sample_rate=PIPELINE_RATE,
                    )
                    return

        yield AudioChunk(
            data=pcm_data,
            codec="pcm16",
            sample_rate=PIPELINE_RATE,
        )

        # Safety: if no participants remain, stop.
        if not room.remote_participants:
            return
