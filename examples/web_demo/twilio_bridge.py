"""Twilio Media Streams WebSocket bridge — phone call → mdk-voice pipeline.

Twilio dials our public ``/twiml/voice`` endpoint when an inbound call lands,
gets back a TwiML ``<Stream>`` directive pointing at ``wss://.../ws/twilio``,
and pumps live caller audio at us as **base64-encoded μ-law 8 kHz 20 ms frames**
inside a JSON envelope. We decode → upsample → run the pipeline → downsample +
μ-law-encode the answer → base64 + envelope it back.

The pipeline itself is exactly the same one the browser demo uses — failover,
Lyzr/OpenAI agent toggle, PII redaction, streaming TTS, cache, metrics. Only
the transport changes (that's the architectural promise paying off).

Twilio's protocol (server-side message kinds):

* ``connected`` — first; we ignore (just a handshake).
* ``start`` — carries ``streamSid``, ``callSid``, the media format. We capture
  ``streamSid`` for outbound frames.
* ``media`` — caller audio: ``media.payload`` is base64-encoded μ-law.
* ``mark`` — Twilio echoes our outbound marks; we use them for "playback done".
* ``stop`` — call ended.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator

from fastapi import WebSocket, WebSocketDisconnect

from movate.voice import (
    AudioChunk,
    is_silent,
    mulaw_to_pcm16,
    resample_pcm16,
    run_voice_pipeline,
    telephony_outbound,
)

log = logging.getLogger("movate.voice.twilio")

# Twilio audio: G.711 μ-law, 8 kHz, mono, 160-byte (20 ms) frames.
TWILIO_RATE = 8_000
# We upsample to 16 kHz before feeding STT — Deepgram/Whisper both perform
# noticeably better on the wider band even when the source is 8 kHz.
PIPELINE_RATE = 16_000
# Auto-VAD endpointing for telephony — looser than the browser because phone
# audio has constant low-level noise that would otherwise never look silent.
_SILENCE_RMS = 600.0
_MIN_SPEECH_MS = 350
_SILENCE_END_MS = 900
# Barge-in during the agent answer: trigger after this much sustained speech.
_BARGE_IN_MS = 220


async def _twilio_inbound_audio(
    ws: WebSocket,
    end: asyncio.Event,
    *,
    on_endpoint,
    on_stop,
) -> AsyncIterator[AudioChunk]:
    """Yield PCM16 16 kHz audio frames from Twilio's WS until endpoint.

    Auto-endpoints on ``_SILENCE_END_MS`` of silence after speech started,
    fires ``on_endpoint`` so the caller can flip into the answer-monitor phase,
    and calls ``on_stop`` if Twilio sends a stop frame (call ended).
    """
    speech_ms = 0.0
    silence_ms = 0.0
    started_speaking = False
    while not end.is_set():
        try:
            msg = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
        except TimeoutError:
            continue
        except WebSocketDisconnect:
            on_stop()
            end.set()
            return
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            continue
        ev = data.get("event")
        if ev == "stop":
            on_stop()
            return
        if ev != "media":
            continue
        # Decode base64 μ-law, convert to PCM, upsample to pipeline rate.
        payload = data.get("media", {}).get("payload", "")
        if not payload:
            continue
        mulaw = base64.b64decode(payload)
        pcm = mulaw_to_pcm16(mulaw)
        pcm = resample_pcm16(pcm, TWILIO_RATE, PIPELINE_RATE)
        chunk = AudioChunk(data=pcm, codec="pcm16", sample_rate=PIPELINE_RATE)
        yield chunk

        # Energy-based endpointing (same shape as the browser path).
        frame_ms = (len(chunk.data) / 2) / (PIPELINE_RATE / 1000.0)
        if is_silent(chunk, _SILENCE_RMS):
            if started_speaking:
                silence_ms += frame_ms
                if silence_ms >= _SILENCE_END_MS:
                    log.info("twilio: VAD endpoint after %.1fs speech", speech_ms / 1000)
                    on_endpoint()
                    return
        else:
            started_speaking = True
            speech_ms += frame_ms
            silence_ms = 0
        if started_speaking and speech_ms < _MIN_SPEECH_MS:
            silence_ms = 0


async def _send_tts_to_twilio(
    ws: WebSocket, stream_sid: str, audio_iter: AsyncIterator[AudioChunk]
) -> None:
    """Resample + μ-law-encode each TTS chunk and stream it back to Twilio.

    ``telephony_outbound`` already rechunks to 160-byte (20 ms) frames, which is
    exactly what Twilio expects — we just base64 + wrap in the JSON envelope.
    """
    async for mu_frame in telephony_outbound(audio_iter, dst_rate=TWILIO_RATE):
        if not mu_frame:
            continue
        await ws.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(mu_frame).decode("ascii")},
                }
            )
        )


async def _send_clear(ws: WebSocket, stream_sid: str) -> None:
    """Tell Twilio to drop everything it's buffered (barge-in)."""
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))


async def _watch_for_barge_in(
    ws: WebSocket, cancel: asyncio.Event, done_listening: asyncio.Event
) -> None:
    """During the agent answer, watch caller mic for sustained speech → barge-in.

    Sets ``cancel`` so the pipeline cancels the agent + TTS, and sends a
    ``clear`` to Twilio so it drops any buffered playback. Exits once the turn
    completes (cancel set elsewhere) OR the caller hangs up.
    """
    await done_listening.wait()
    speech_ms = 0.0
    while not cancel.is_set():
        try:
            msg = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
        except TimeoutError:
            continue
        except WebSocketDisconnect:
            cancel.set()
            return
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            continue
        ev = data.get("event")
        if ev == "stop":
            cancel.set()
            return
        if ev != "media":
            continue
        payload = data.get("media", {}).get("payload", "")
        if not payload:
            continue
        mulaw = base64.b64decode(payload)
        pcm = mulaw_to_pcm16(mulaw)
        # Don't upsample — VAD only needs the energy.
        chunk = AudioChunk(data=pcm, codec="pcm16", sample_rate=TWILIO_RATE)
        frame_ms = (len(chunk.data) / 2) / (TWILIO_RATE / 1000.0)
        if not is_silent(chunk, _SILENCE_RMS):
            speech_ms += frame_ms
            if speech_ms >= _BARGE_IN_MS:
                log.info("twilio: barge-in (server VAD)")
                cancel.set()
                return
        else:
            speech_ms = max(0.0, speech_ms - frame_ms * 0.5)


async def handle_twilio_call(
    ws: WebSocket,
    *,
    build_pipeline_kwargs,  # callable: (turn) -> dict[str, Any] for run_voice_pipeline
    on_turn_done=None,  # callable: (events: list) -> None for metrics/logging
    on_call_sid=None,  # callable: (call_sid: str | None) -> None, fires once on `start`
    on_event=None,  # callable: (ev_kind: str, **fields) -> awaitable, per pipeline event
    on_call_start=None,  # callable: (call_sid, stream_sid) -> awaitable, on accept
    on_call_end=None,  # callable: (turns: int) -> awaitable, on hangup
) -> None:
    """Drive one phone call: a loop of (listen → run pipeline → stream answer).

    The caller (``server.py``) supplies a factory that returns fresh
    pipeline kwargs (STT/TTS/agent/observers/etc.) per turn — same chain the
    browser demo uses, just with phone-shaped audio at the edges.

    ``on_call_sid`` (B5) fires synchronously with the parsed Twilio ``callSid``
    the moment the ``start`` frame arrives, so the caller can swap in a
    resumed :class:`Session` *before* ``build_pipeline_kwargs`` runs for turn 1.
    Errors raised by the hook are logged and swallowed so a UI hiccup never
    drops a call.
    """
    await ws.accept()
    log.info("twilio: WS accepted")

    # First two frames are always "connected" then "start" — parse to capture
    # the streamSid we need to address outbound media.
    stream_sid: str | None = None
    call_sid: str | None = None
    while stream_sid is None:
        try:
            msg = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except TimeoutError:
            log.warning("twilio: timeout waiting for start frame")
            return
        except WebSocketDisconnect:
            log.info("twilio: caller hung up before start")
            return
        data = json.loads(msg)
        if data.get("event") == "start":
            stream_sid = data.get("streamSid") or data.get("start", {}).get("streamSid")
            call_sid = data.get("start", {}).get("callSid")
            log.info("twilio: stream %s call %s", stream_sid, call_sid)
            break

    if stream_sid is None:
        return

    # B5: fire the call-sid hook so the caller can swap in a resumed session
    # before the first turn. Synchronous; errors swallowed (a session-resume
    # bug should not drop a phone call).
    if on_call_sid is not None:
        try:
            on_call_sid(call_sid)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning("twilio: on_call_sid hook raised: %s", exc)

    # Live-mirror hook: broadcast call-start to any browser observers so a
    # Detailed-view event stream can show the phone conversation as it happens.
    if on_call_start is not None:
        with contextlib.suppress(Exception):
            await on_call_start(call_sid, stream_sid)

    call_ended = asyncio.Event()

    def _stop() -> None:
        log.info("twilio: caller hung up")
        call_ended.set()

    try:
        turn = 0
        while not call_ended.is_set():
            turn += 1
            end = asyncio.Event()
            cancel = asyncio.Event()
            done_listening = asyncio.Event()
            events: list[object] = []

            def _endpointed(_done: asyncio.Event = done_listening) -> None:
                _done.set()

            barge_in_task = asyncio.create_task(_watch_for_barge_in(ws, cancel, done_listening))

            audio_in = _twilio_inbound_audio(ws, end, on_endpoint=_endpointed, on_stop=_stop)

            # Wrap the pipeline's TTS output so it goes both to events (for
            # latency/cost telemetry) AND to Twilio (the actual audio bytes).
            tts_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue()

            async def _audio_tap(
                _q: asyncio.Queue[AudioChunk | None] = tts_queue,
            ) -> AsyncIterator[AudioChunk]:
                while True:
                    item = await _q.get()
                    if item is None:
                        return
                    yield item

            tts_send_task = asyncio.create_task(_send_tts_to_twilio(ws, stream_sid, _audio_tap()))

            kwargs = build_pipeline_kwargs(turn)
            try:
                async for ev in run_voice_pipeline(
                    audio_in=audio_in,
                    cancel=cancel,
                    **kwargs,
                ):
                    events.append(ev)
                    if ev.kind == "tts.audio" and ev.audio:
                        await tts_queue.put(ev.audio)
                    elif ev.kind == "transcript.final":
                        log.info("twilio turn %d heard: %r", turn, ev.text)
                    elif ev.kind == "error":
                        log.warning("twilio turn %d %s: %s", turn, ev.stage, ev.message)
                    # Live mirror — forward each event to whoever's watching
                    # (browser observers via WS). Drop audio bytes (huge,
                    # binary, not useful in an event stream).
                    if on_event is not None:
                        try:
                            if ev.kind == "transcript.partial":
                                await on_event(ev.kind, turn=turn, text=ev.text)
                            elif ev.kind == "transcript.final":
                                await on_event(ev.kind, turn=turn, text=ev.text)
                            elif ev.kind == "agent.token":
                                await on_event(ev.kind, turn=turn, text=ev.text)
                            elif ev.kind == "tts.audio":
                                # Skip the audio payload; just signal a frame.
                                pass
                            elif ev.kind == "error":
                                await on_event(
                                    ev.kind, turn=turn, stage=ev.stage, message=ev.message
                                )
                        except Exception:  # noqa: BLE001 - never let mirroring drop a call
                            pass
            except WebSocketDisconnect:
                call_ended.set()
                break
            finally:
                done_listening.set()
                await tts_queue.put(None)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await tts_send_task
                if cancel.is_set():
                    await _send_clear(ws, stream_sid)
                barge_in_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await barge_in_task

            if on_turn_done:
                on_turn_done(events)

    finally:
        final_turn = turn if "turn" in dir() else 0
        log.info("twilio: call ended after %d turn(s)", final_turn)
        if on_call_end is not None:
            with contextlib.suppress(Exception):
                await on_call_end(final_turn)


def twiml_for_stream(public_wss_url: str) -> str:
    """Return the TwiML response that hands the call off to our WS.

    ``public_wss_url`` is the *full* public WSS URL Twilio should dial — e.g.
    ``wss://your-tunnel.ngrok-free.dev/ws/twilio``. Use HTTPS for HTTP, WSS for
    WebSocket — Twilio requires TLS for both.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f'    <Stream url="{public_wss_url}"/>\n'
        "  </Connect>\n"
        "</Response>\n"
    )
