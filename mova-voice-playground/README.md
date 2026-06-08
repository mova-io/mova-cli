# Mova-iO Voice Playground — generic standalone Lyzr voice adapter

A standalone, browser-based **voice front-end for any Lyzr / Mova-iO agent**. Hold
**Talk** (or **space**), speak, release — your words are transcribed, sent to the
agent, and the answer is spoken back, with a live transcript + per-turn latency +
a metrics panel. This is the **generic** adapter (plug in *any* agent) — distinct
from the POS-branded `mdk-pos-demo`.

```
mic → FailoverSTT(Deepgram → Whisper) → <your Lyzr agent> → FailoverTTS(Cartesia → OpenAI) → speaker
```

It was recovered from the old `examples/web_demo/` (removed by #714) into its own
source-controlled project so its Azure Container App is reproducible.

## Plug in your Lyzr / Mova-iO agent (BYOK)

By default a turn runs on the **server-side** agent (the operator's
`OPENAI_API_KEY`). Open the **BYOK panel** and paste your **Mova-iO (Lyzr) API
key** (+ pick an agent): each turn is then routed to *your* hosted agent via
`movate.voice.LyzrAgentTurn` → Lyzr `/v3/inference/chat/`, voiced through the same
STT/TTS pipeline. The key is **per-session and never persisted**. BYOK is scoped
to Lyzr/Mova-iO on purpose (`USER_KEY_PROVIDERS = ("lyzr",)` in `server.py`) —
that's the provider each customer brings.

A server-side default Lyzr agent can also be set via `LYZR_API_KEY` +
`LYZR_AGENT_ID` env (instead of OpenAI) — see `deploy.sh`.

## Run it locally

```bash
# from the repo root — needs the package's [voice] extra + the demo's HTTP deps
uv pip install -e '.[voice]' fastapi 'uvicorn[standard]' websockets pydub
export OPENAI_API_KEY=sk-...          # STT/TTS + the default server-side agent
PORT=8765 uvicorn web_demo.server:app --app-dir mova-voice-playground --host 0.0.0.0 --port 8765
# → http://localhost:8765   (then paste a Mova-iO key in the BYOK panel to use your agent)
```

## Deploy (its own Azure Container App)

```bash
mova-voice-playground/deploy.sh        # builds a git-sha-tagged image; CREATES the
                                       # `mova-voice-playground` app on first run, else rolls it
```

The build context is the **repo root** (so `movate.voice` installs); the
Dockerfile copies only the demo's runtime files into the image's `web_demo/`
package. The app listens on `$PORT` (8080) with external ingress.

## Files
- `server.py` — FastAPI app: `GET /` (the UI), `WS /ws/voice` (the turn loop),
  `/lyzr/agents` + `/parity` (Mova-iO catalog/parity), BYOK key handling.
- `index.html` — the single-page voice UI (Hold-to-Talk, live activity, BYOK panel).
- `agent.py` — the default server-side agent + the generic Lyzr agent binding.
- `livekit_bridge.py` / `twilio_bridge.py` — WebRTC / telephony transports (optional).
- `recording.py` — call recording to Azure Blob (lazy-imported; demo-level only).
- `doctor.py` — startup self-check (which providers are keyed).
- `Dockerfile`, `deploy.sh` — reproducible image build + ACA create/rollout.
