# LiveKit browser voice — connecting a web client to mdk voice agents

ADR 074 Phase 3a delivers LiveKit as a WebRTC-grade transport for mdk voice
agents.  This document describes how to connect a **browser client** to a
LiveKit room hosting an mdk voice agent, replacing the raw WebSocket transport
with production-grade audio (adaptive bitrate, ICE/TURN, noise cancellation,
echo cancellation).

## Overview

The flow is:

1. **Provision a session** via `POST /api/v1/agents/{name}/call`.
2. **Join the LiveKit room** from the browser using `livekit-client` (the
   LiveKit JavaScript SDK) with the returned participant token.
3. **Audio flows via WebRTC** — the browser publishes mic audio as a track;
   the mdk agent (running as a LiveKit worker in the room) subscribes,
   processes it through the voice pipeline (STT -> agent -> TTS), and
   publishes the spoken answer back as an audio track.

The agent is unchanged — the same `agent.yaml`, `prompt.md`, and Executor
that work over the WebSocket work over LiveKit.

## Step 1 — Provision the session

```javascript
const response = await fetch(
  `${RUNTIME_URL}/api/v1/agents/${agentName}/call`,
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({ transport: "livekit" }),
  }
);

const { room_name, participant_token, livekit_url } = await response.json();
```

The response contains:

| Field               | Description                                        |
|---------------------|----------------------------------------------------|
| `room_name`         | The LiveKit room name (unique per session).         |
| `participant_token` | A JWT token for the caller to join the room.        |
| `livekit_url`       | The LiveKit server URL (`wss://...`).               |

## Step 2 — Join the room from the browser

Install the LiveKit client SDK:

```bash
npm install livekit-client
```

Connect and publish your microphone:

```javascript
import {
  Room,
  RoomEvent,
  Track,
  createLocalAudioTrack,
} from "livekit-client";

const room = new Room();

// Subscribe to the agent's audio track (TTS output).
room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
  if (track.kind === Track.Kind.Audio) {
    // Attach the audio track to an <audio> element for playback.
    const audioElement = track.attach();
    document.body.appendChild(audioElement);
  }
});

// Connect to the room with the token from step 1.
await room.connect(livekit_url, participant_token);

// Publish the local microphone as an audio track.
const micTrack = await createLocalAudioTrack();
await room.localParticipant.publishTrack(micTrack);
```

That is the entire integration.  The browser publishes mic audio; the mdk
agent subscribes, runs the voice pipeline, and publishes the spoken answer
back.  LiveKit handles codec negotiation (Opus/WebRTC), adaptive bitrate,
ICE/TURN traversal, and reconnection automatically.

## Step 3 — End the call

```javascript
// Disconnect from the room when done.
room.disconnect();
```

The mdk agent detects the participant leaving and tears down the pipeline
session.

## SIP inbound calls

For phone calls (PSTN/SIP), configure a LiveKit SIP trunk to route inbound
calls to the mdk agent's room.  The SIP trunk configuration is an
operator-run deployment step (see LiveKit's SIP documentation).  Once
configured, an inbound phone call joins the room as a participant, and the
same mdk agent worker handles it — no code changes.

## Advantages over the raw WebSocket transport

| Feature                    | Raw WS            | LiveKit (WebRTC)      |
|----------------------------|-------------------|-----------------------|
| Codec negotiation          | Manual (PCM only) | Automatic (Opus)      |
| Adaptive bitrate           | No                | Yes                   |
| ICE/TURN traversal         | No                | Yes                   |
| Noise cancellation         | No                | Browser + SDK         |
| Echo cancellation          | No                | Browser + SDK         |
| Reconnection               | Manual            | Automatic             |
| SIP (phone) support        | No                | Native                |
| Multiple participants      | No                | Yes                   |
| Self-hostable              | N/A               | Yes (Apache-2.0)      |

The raw WebSocket transport (`WS /api/v1/agents/{name}/voice`) remains
available for simple integrations and testing.  LiveKit is the
production-grade upgrade for browser and phone clients.

## Prerequisites

- The mdk runtime must be installed with the `[telephony]` extra:
  `pip install 'movate-cli[telephony]'`
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` must be
  configured (`mdk auth login livekit`).
- A LiveKit server must be running (self-hosted or LiveKit Cloud).
