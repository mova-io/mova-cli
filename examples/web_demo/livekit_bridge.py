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
        payload = json.dumps(event).encode()
        await room.local_participant.publish_data(
            payload,
            reliable=True,
        )
    except Exception:
        log.warning(
            "livekit_bridge: failed to publish data event %s",
            event.get("event", ""),
            exc_info=True,
        )

# LiveKit audio constants
LIVEKIT_FRAME_DURATION_MS = 20  # Standard WebRTC frame
SILENCE_RMS = 300.0     # lowered from 400 — Opus-decoded audio has lower RMS floor
MIN_SPEECH_MS = 400     # require 400ms of speech before endpointing activates
SILENCE_END_MS = 1800   # 1.8s silence to endpoint (was 1.2s — too aggressive for natural pauses)
TTS_OUTPUT_RATE = 24_000  # Cartesia + OpenAI TTS both output 24 kHz


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
    # Must match the TTS output rate (24 kHz), NOT the pipeline/STT rate
    # (16 kHz). The native FFI rejects frames whose sample rate doesn't
    # match the source's declared rate.
    audio_source = rtc.AudioSource(
        sample_rate=TTS_OUTPUT_RATE,
        num_channels=1,
    )

    turn = 0  # init before try so finally can reference it on connect failure
    try:
        log.warning("livekit_bridge: connecting to %s room=%s", livekit_url, room_name)
        await room.connect(livekit_url, token)
        log.warning("livekit_bridge: connected to room=%s participants=%d",
                     room_name, len(room.remote_participants))

        # Publish our audio track.
        local_track = rtc.LocalAudioTrack.create_audio_track(
            "agent-voice", audio_source
        )
        publish_options = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_MICROPHONE
        )
        await room.local_participant.publish_track(local_track, publish_options)
        log.warning("livekit_bridge: agent audio track published")

        if on_call_start:
            await on_call_start(room_name, None)

        # Wait for a participant to join with audio.
        log.warning("livekit_bridge: waiting for participant audio track...")
        audio_track = await _wait_for_audio_track(room)
        if audio_track is None:
            log.warning("livekit_bridge: no participant audio within 60s")
            return

        participant_id = getattr(audio_track, "sid", "unknown")
        sr = getattr(audio_track, "sample_rate", "?")
        log.warning("livekit_bridge: got audio from participant=%s sr=%s", participant_id, sr)

        # Run turns.
        turn = 0
        audio_stream = rtc.AudioStream(audio_track)
        log.warning("livekit_bridge: AudioStream created, entering turn loop")

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
                        try:
                            audio_data = bytes(ev.audio.data)
                            sr = ev.audio.sample_rate
                            # Resample to the AudioSource rate if needed.
                            if sr != TTS_OUTPUT_RATE:
                                audio_data = resample_pcm16(
                                    audio_data, sr, TTS_OUTPUT_RATE
                                )
                                sr = TTS_OUTPUT_RATE
                            frame = rtc.AudioFrame(
                                data=audio_data,
                                sample_rate=sr,
                                num_channels=1,
                                samples_per_channel=len(audio_data) // 2,
                            )
                            await audio_source.capture_frame(frame)
                        except Exception:
                            log.warning(
                                "livekit_bridge: capture_frame error "
                                "(sr=%s len=%d), skipping frame",
                                ev.audio.sample_rate,
                                len(ev.audio.data),
                                exc_info=True,
                            )

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
    frame_count = 0
    # Buffer tiny WebRTC frames (~3ms each at 48kHz) into larger chunks
    # for stable VAD and proper pipeline feeding (~20ms chunks).
    pcm_buffer = bytearray()
    CHUNK_SAMPLES = PIPELINE_RATE // 50  # 320 samples = 20ms at 16kHz
    CHUNK_BYTES = CHUNK_SAMPLES * 2

    async for frame_event in audio_stream:
        frame_count += 1
        if frame_count == 1:
            log.warning("livekit_bridge: first audio frame received from participant")
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

        # Buffer small frames into 20ms chunks for stable VAD.
        pcm_buffer.extend(pcm_data)
        if len(pcm_buffer) < CHUNK_BYTES:
            continue  # accumulate more before processing

        # Process all complete chunks in the buffer.
        while len(pcm_buffer) >= CHUNK_BYTES:
            chunk_data = bytes(pcm_buffer[:CHUNK_BYTES])
            del pcm_buffer[:CHUNK_BYTES]

            samples = struct.unpack(f"<{CHUNK_SAMPLES}h", chunk_data)
            rms = (sum(s * s for s in samples) / CHUNK_SAMPLES) ** 0.5
            frame_ms = 20  # fixed: 320 samples at 16kHz = 20ms

            if frame_count <= 5 or frame_count % 200 == 0:
                log.warning(
                    "livekit_bridge: chunk rms=%.1f speech=%s "
                    "speech_ms=%d silence_ms=%d",
                    rms, speech_started, speech_ms, silence_ms,
                )

            if rms > SILENCE_RMS:
                silence_ms = 0
                speech_ms += frame_ms
                if speech_ms >= MIN_SPEECH_MS:
                    if not speech_started:
                        log.warning(
                            "livekit_bridge: VAD speech started "
                            "(rms=%.1f after %dms)",
                            rms, speech_ms,
                        )
                    speech_started = True
            else:
                if speech_started:
                    silence_ms += frame_ms
                    if silence_ms >= SILENCE_END_MS:
                        log.warning(
                            "livekit_bridge: VAD silence endpoint "
                            "(%dms quiet after speech)",
                            silence_ms,
                        )
                        yield AudioChunk(
                            data=chunk_data,
                            codec="pcm16",
                            sample_rate=PIPELINE_RATE,
                        )
                        return

            yield AudioChunk(
                data=chunk_data,
                codec="pcm16",
                sample_rate=PIPELINE_RATE,
            )

        # Safety: if no participants remain, stop.
        if not room.remote_participants:
            return
