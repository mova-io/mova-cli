# Mova-iO Voice Playground (LiveKit)

A browser voice front-end for a **Lyzr hosted voice agent**, styled identically to
the Simple view of the sibling [`mova-voice-playground`](../mova-voice-playground)
— same Movate chrome, live-activity strip-chart, and event stream — but wired to
Lyzr's **LiveKit-native** voice service instead of running its own pipeline.

```
browser ──(LiveKit WebRTC)──▶ Lyzr hosted agent (LiveKit Cloud)
   │                                    │
   └──▶ this proxy ──(x-api-key)──▶ voice-livekit.studio.lyzr.ai
        /sessions/start                 /v1/sessions/start
        /sessions/end                   /v1/sessions/end
```

## How it differs from `mova-voice-playground`

| | `mova-voice-playground` | this app |
|---|---|---|
| Pipeline | STT→agent→TTS runs **in the demo's server** | runs on **Lyzr / LiveKit Cloud** |
| Transport | WebSocket PCM (LiveKit optional) | **LiveKit WebRTC** (always) |
| Interaction | push-to-talk per turn | **full-duplex call** (agent greets, you talk) |
| Backend role | full voice runtime + `movate.voice` | **thin proxy** — only hides the API key |
| Deps | `movate[voice]` + STT/TTS SDKs | `fastapi` + `uvicorn` + `httpx` |

## Flow

1. `POST /sessions/start {agentId, userIdentity}` → the proxy adds the Lyzr
   `x-api-key` and calls `voice-livekit.studio.lyzr.ai/v1/sessions/start`, which
   returns `{livekitUrl, userToken, roomName, sessionId, agentConfig}`.
2. The browser joins the room with the LiveKit JS SDK, publishes its mic; the
   auto-dispatched agent greets first and responds with audio + transcription.
3. `POST /sessions/end {roomName}` tears the room down when the user hangs up.

The Lyzr API key **never reaches the browser** — that's the only reason this app
has a backend at all.

## Run locally

```bash
# from the repo root (the project venv already has fastapi + httpx)
LYZR_API_KEY=sk-default-…  PORT=8766 \
  .venv/bin/uvicorn server:app --app-dir mova-voice-livekit-playground --host 0.0.0.0 --port 8766
# → http://localhost:8766
```

Environment variables:

- `LYZR_API_KEY` — Lyzr key used server-side. Falls back to the shared public
  **demo** key baked into `server.py`; set it for a real deploy.
- `LYZR_DEFAULT_AGENT_ID` — the preset agent shown in the picker
  (default `6a26e4fc6d80be4fdfe65fa1`). Users can paste any 24-char agent ID via
  the **Other…** option.
- `LYZR_VOICE_BASE` — Lyzr base URL (default `https://voice-livekit.studio.lyzr.ai`).
- `PORT` — listen port (default 8000; Azure Container Apps injects its own).

## Deploy (its own Azure Container App)

```bash
LYZR_API_KEY=sk-… mova-voice-livekit-playground/deploy.sh   # creates/rolls the app
```

The build context is **this directory** (no `movate.voice` install needed).

## Files

- `server.py` — FastAPI thin proxy: `GET /` (UI), `GET /config`, `GET /health`,
  `POST /sessions/start`, `POST /sessions/end`.
- `index.html` — the single-page voice UI (LiveKit JS SDK from CDN; Simple-view chrome).
- `Dockerfile`, `deploy.sh` — reproducible image build + ACA create/rollout.
