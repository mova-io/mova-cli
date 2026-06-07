# LiveKit Integration Roadmap — Full Parity with WebSocket Voice

**Status**: LiveKit room connects, browser mic publishes, but the bridge
has multiple issues preventing reliable end-to-end voice. This roadmap
takes LiveKit from "connects but broken" to "as good as or better than
WebSocket."

**Goal**: A user can click 📡 WebRTC, speak naturally, see transcripts
stream, hear the agent respond via WebRTC audio, and have the same UX
quality as the Talk-button WebSocket flow.

---

## Failure audit (what broke and why)

| # | Symptom | Root cause | Status |
|---|---------|-----------|--------|
| F1 | "LivekitClient is not defined" | cdn.livekit.io down | ✅ Fixed — switched to jsDelivr |
| F2 | "livekit SDK not installed" | Python `livekit` package missing from Docker image | ✅ Fixed — added to Dockerfile |
| F3 | 401 Unauthorized / "no permissions to access the room" | `token.video_grants = grant` silently does nothing in livekit-api v0.8.x; must use `token.with_grants(grant)` | ✅ Fixed |
| F4 | 401 Unauthorized (old key) | LIVEKIT_API_SECRET had literal `"` quotes in Azure env var | ✅ Fixed — code strips quotes + user re-created key |
| F5 | `UnboundLocalError: 'turn'` in error path | `turn` variable not initialized before `try` block | ✅ Fixed |
| F6 | `capture_frame` exception kills the bridge | TTS audio publishing crashes; entire session dies | ⚠️ Caught but audio still doesn't play (frames dropped) |
| F7 | VAD not detecting speech / "mic not working" | Bridge's `_audio_from_livekit` receives frames but VAD threshold may be too high, OR audio stream exhausted after pipeline error | 🔴 Not fixed |
| F8 | No agent audio heard in browser | `capture_frame` errors silently drop all TTS frames | 🔴 Not fixed |
| F9 | Wrong agent used in LiveKit session | Agent ID read from hidden text input instead of dropdown | ✅ Fixed |
| F10 | WebRTC chip disappears after BYOK apply | One-time async check, not re-asserted on reconnect | ✅ Fixed |

**Critical remaining**: F6 (TTS playback), F7 (VAD/mic), F8 (agent audio).
These three are the "it doesn't work" blockers.

---

## Phase 1: Make it work (critical path)

### 1.1 Fix `capture_frame` — TTS audio publishing
**LOE**: 2–4 hours | **Priority**: P0 | **Files**: `livekit_bridge.py`

**Problem**: `audio_source.capture_frame(frame)` throws an exception.
The `AudioSource` was created at `PIPELINE_RATE` (16 kHz), but TTS audio
from Cartesia/OpenAI comes at 24 kHz. The sample rate mismatch causes
the native FFI to reject the frame.

**Fix**:
1. Check `ev.audio.sample_rate` — if it doesn't match `audio_source`'s
   rate (16 kHz), resample before publishing.
2. OR: Create the `AudioSource` at the TTS output rate (24 kHz for
   Cartesia, 24 kHz for OpenAI TTS). Since we don't know the rate up
   front, create it lazily on the first TTS frame.
3. Add verbose error logging inside the `except` block to capture the
   actual error message (currently swallowed).

**Verification**: After fix, server logs should show TTS frames being
published without errors. Browser should hear agent audio via WebRTC
`<audio>` element.

### 1.2 Fix VAD / audio stream lifecycle
**LOE**: 2–3 hours | **Priority**: P0 | **Files**: `livekit_bridge.py`

**Problem**: `_audio_from_livekit()` is an async generator that iterates
over `audio_stream` (a `rtc.AudioStream`). After the pipeline runs once
(turn 1), the generator returns. On turn 2, the bridge creates a NEW
`_audio_from_livekit()` call with the SAME `audio_stream` — but the
stream may be exhausted or in a bad state after the first turn's
`capture_frame` error killed the pipeline.

**Bugs**:
1. `_audio_from_livekit` returns after silence detection, but the
   `audio_stream` async iterator is NOT rewindable. On turn 2, the bridge
   passes the same `audio_stream` object which may have pending frames
   from turn 1's silence tail.
2. The VAD threshold `SILENCE_RMS = 400.0` may be too high for Opus-
   decoded 16 kHz audio from WebRTC (vs raw PCM from the browser's
   ScriptProcessor). Need to log actual RMS values to calibrate.
3. After a pipeline error + `break`, the `while True` loop exits and the
   bridge disconnects. There's no recovery — the session is dead.

**Fix**:
1. Create a **shared audio frame buffer** (asyncio.Queue) that
   `_audio_from_livekit` reads from. The AudioStream event listener
   pushes frames into the queue continuously. This decouples the stream
   lifetime from the turn lifetime.
2. Add RMS debug logging: `log.debug("rms=%.1f speech_ms=%d silence_ms=%d")`
   so we can see if VAD is detecting speech or staying silent.
3. On pipeline error, don't `break` — log the error, send an error event
   to the browser, and continue the turn loop (skip to next turn).

### 1.3 Match the AudioSource sample rate to TTS output
**LOE**: 1 hour | **Priority**: P0 | **Files**: `livekit_bridge.py`

**Problem**: The `AudioSource` is created at `PIPELINE_RATE` (16 kHz) but
TTS providers output at 24 kHz (Cartesia) or 24 kHz (OpenAI TTS). The
native `capture_frame` expects frames at the source's declared rate.

**Fix**: Create the AudioSource at 24000 Hz (the TTS output rate), OR
resample TTS audio down to 16 kHz before publishing. The 24 kHz option
is better because it preserves audio quality.

```python
audio_source = rtc.AudioSource(sample_rate=24000, num_channels=1)
```

Then in the capture loop, resample if the TTS frame rate doesn't match:
```python
if ev.audio.sample_rate != 24000:
    audio_data = resample_pcm16(ev.audio.data, ev.audio.sample_rate, 24000)
else:
    audio_data = ev.audio.data
```

### 1.4 Local testing harness
**LOE**: 1 hour | **Priority**: P0

Before deploying, run the server locally and test:
```bash
cd examples/web_demo
LIVEKIT_URL=wss://mdk-voice-o5g0p61x.livekit.cloud \
LIVEKIT_API_KEY=APIoasSt3djZGd6 \
LIVEKIT_API_SECRET=XWal5fqIF7G5Pa4NeqH01uf5w5Zmnpvv4eR3jMCCMjPD \
uvicorn server:app --host 0.0.0.0 --port 8765
```
Then open `http://localhost:8765` and test WebRTC. Fix issues locally
before deploying — eliminates the deploy-and-pray cycle.

---

## Phase 2: Make it reliable

### 2.1 Pipeline error recovery (don't kill the session)
**LOE**: 1 hour | **Files**: `livekit_bridge.py`

Currently, any pipeline exception → `break` → bridge disconnects →
session over. Instead:
- Catch the exception
- Send `{"event": "error", "message": "..."}` over DataChannel
- Reset state and continue the turn loop
- Only `break` on `CancelledError` or participant disconnect

### 2.2 Graceful multi-turn
**LOE**: 2 hours | **Files**: `livekit_bridge.py`

The WS handler supports unlimited turns per session. The bridge should
too, but currently the AudioStream lifecycle is fragile:
- Refactor to use a persistent `asyncio.Queue` fed by the AudioStream
- Each turn reads from the queue until VAD signals end-of-speech
- Between turns, the queue buffers any stray frames (discarded)

### 2.3 Bridge diagnostic logging
**LOE**: 30 min | **Files**: `livekit_bridge.py`

Add structured logging at each critical point:
```
livekit_bridge: room connected (room=X, participants=N)
livekit_bridge: audio track received (sr=48000, channels=1)
livekit_bridge: VAD speech started (rms=523.4, after 320ms)
livekit_bridge: VAD silence end (1200ms quiet after 2.3s speech)
livekit_bridge: pipeline started (turn=1, agent=lyzr, agent_id=6a219...)
livekit_bridge: STT final: "Hi, my register is broken"
livekit_bridge: TTS frame published (sr=24000, bytes=4800)
livekit_bridge: turn complete (turn=1, events=12)
```

This makes remote debugging possible without guessing.

---

## Phase 3: Feature parity with WebSocket

### 3.1 Pass missing pipeline kwargs
**LOE**: 30 min | **Files**: `server.py`

The WS handler passes several kwargs that the LiveKit bridge doesn't:
- `language` — language hint for STT
- `speculative` — speculative kickoff
- `keyterms` — STT boosting
- `endpointing_ms` — silence hold
- `observer` — metrics/trail observer

Add these to `build_kwargs()` in `/livekit/join`.

### 3.2 Rich `done` event with latency + cost
**LOE**: 2 hours | **Files**: `livekit_bridge.py`, `server.py`

The WS handler sends a rich `done` event with latency breakdown, cost,
metrics, and badges. The LiveKit bridge sends `{"event": "done"}` only.

**Fix**: Instrument the bridge with timing (like the WS handler does):
- `stt_final_ms` — time from first audio to transcript.final
- `agent_first_token_ms` — time from transcript.final to first agent.token
- `tts_first_audio_ms` — time from first agent.token to first TTS frame
- `responded_in_ms` — total time from first audio to first TTS audio
- `cost_usd` — accumulate from session.metrics

Include all of these in the DataChannel `done` event.

### 3.3 Mid-session config changes (agent switch, language, voice)
**LOE**: 4 hours | **Files**: `livekit_bridge.py`, `server.py`, `index.html`

Currently, LiveKit sessions are "frozen" at connect time — changing the
agent dropdown doesn't affect the running bridge. The WS handler supports
live mid-session changes via `set_agent_tier`, `set_language`, etc.

**Options** (pick one):
- **A) Reconnect on change**: When user changes agent/language in LiveKit
  mode, disconnect + reconnect with new config. Simple, slight interruption.
- **B) Control channel**: Keep a parallel WS open for control messages.
  Complex but seamless.
- **C) DataChannel commands**: Browser sends `{"command": "set_agent", ...}`
  via LiveKit DataChannel. Bridge receives and updates session. Elegant
  but requires bidirectional DataChannel handling.

**Recommendation**: Start with (A), upgrade to (C) later.

### 3.4 Barge-in support
**LOE**: 2 hours | **Files**: `livekit_bridge.py`

The WS handler has barge-in: if the user starts speaking while the agent
is responding, the pipeline cancels and starts a new turn. The bridge
needs the same:
- While TTS audio is being published, keep monitoring the audio queue
- If VAD detects speech during TTS playback, set `cancel.set()` and
  break out of the pipeline loop
- Start a new turn immediately

### 3.5 Talk button support in LiveKit mode
**LOE**: 1 hour | **Files**: `index.html`, `livekit_bridge.py`

Currently the Talk button is disabled in LiveKit mode. Some users prefer
explicit push-to-talk over VAD. Add support:
- Browser sends `{"command": "start"}` / `{"command": "stop"}` via
  DataChannel when Talk is pressed/released
- Bridge listens for these commands and gates audio forwarding
- If no commands received, fall back to VAD (current behavior)

---

## Phase 4: Polish + production hardening

### 4.1 Recording integration
**LOE**: 1 hour | **Files**: `livekit_bridge.py`

Wire `session.recorder` into the bridge so LiveKit calls are recorded
to Azure blob storage (same as WS calls).

### 4.2 Session memory continuity
**LOE**: 1 hour | **Files**: `server.py`

When switching from WS to LiveKit, conversation memory is lost (new
Session). Option: pass the WS session_id to `/livekit/join` and reuse
the same Session object (store in a dict by session_id).

### 4.3 Concurrent call admission
**LOE**: 30 min | **Files**: `server.py`

The WS handler has `MAX_CONCURRENT_CALLS=3` admission gate. The
LiveKit `/livekit/join` handler doesn't check this. Add the same
gate to prevent resource exhaustion.

### 4.4 LiveKit SIP trunk (phone via WebRTC)
**LOE**: 2 hours (config only, no code) | **Files**: LiveKit Cloud dashboard

Configure LiveKit Cloud's SIP trunk to bridge PSTN calls into LiveKit
rooms. The existing bridge handles them identically to browser WebRTC.
Replaces Twilio for phone calls.

### 4.5 UI polish
**LOE**: 2 hours | **Files**: `index.html`

- Show "speaking…" indicator when bridge VAD detects speech
- Show latency badges after LiveKit turns (from rich `done` event)
- Mark WebRTC toggle as "beta" until Phase 2 is complete, then remove
- Auto-expand Event Stream when in LiveKit mode (for debugging visibility)

---

## Execution order + estimates

| Phase | Items | Total LOE | Depends on |
|-------|-------|-----------|------------|
| **Phase 1** | 1.1, 1.2, 1.3, 1.4 | **6–9 hours** | — |
| **Phase 2** | 2.1, 2.2, 2.3 | **3.5 hours** | Phase 1 |
| **Phase 3** | 3.1, 3.2, 3.3, 3.4, 3.5 | **9.5 hours** | Phase 2 |
| **Phase 4** | 4.1–4.5 | **6.5 hours** | Phase 3 |
| **Total** | | **~25–29 hours** | |

**Recommended approach**: Phase 1 as a focused local-dev session (no
deploy-and-pray). Phase 2 can be deployed incrementally. Phase 3 items
are independent — can be parallelized. Phase 4 is post-launch polish.

---

## Key architectural insight

The root cause of most issues is that `livekit_bridge.py` was written
as a thin prototype mirroring the Twilio bridge pattern, but the
**LiveKit SDK is fundamentally different** from the Twilio WS bridge:

| Aspect | Twilio bridge | LiveKit bridge |
|--------|--------------|----------------|
| Audio delivery | Raw µ-law bytes over WS | Opus-decoded PCM via native FFI |
| Audio publishing | WS binary frames | `AudioSource.capture_frame()` (native, rate-sensitive) |
| Lifecycle | WS open → close = session | Room connect → disconnect = session |
| Sample rates | 8 kHz (fixed) | 48 kHz input, 24 kHz TTS output (variable) |
| Error handling | WS disconnects clean | Native FFI errors crash the process |

The bridge needs to be more defensive and rate-aware than the WS/Twilio
paths because the native FFI layer is strict about frame formats.
