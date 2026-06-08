"""Mova-iO Voice Playground (LiveKit) — thin proxy for the Lyzr hosted voice API.

This is the *LiveKit-native* sibling of ``mova-voice-playground``. Where that demo
runs its own STT→agent→TTS pipeline over a WebSocket, this one front-ends Lyzr's
hosted voice service (``voice-livekit.studio.lyzr.ai``): the agent lives on
LiveKit Cloud, and the browser connects to it directly with the LiveKit JS SDK.

The server's only job is to keep the Lyzr ``x-api-key`` off the client and to
forward two calls:

    POST /sessions/start  {agentId, userIdentity?}  → Lyzr /v1/sessions/start
    POST /sessions/end    {roomName}                → Lyzr /v1/sessions/end

``/sessions/start`` returns ``{livekitUrl, userToken, roomName, sessionId,
agentConfig}``; the browser uses ``livekitUrl`` + ``userToken`` to join the room,
publishes its mic, and the agent (auto-dispatched, greets first) responds with
audio + transcription. ``/sessions/end`` tears the room down.

Run locally:

    LYZR_API_KEY=sk-... PORT=8766 \
      uvicorn server:app --app-dir mova-voice-livekit-playground --host 0.0.0.0 --port 8766
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = pathlib.Path(__file__).resolve().parent

# Lyzr hosted-voice service. Override the base for staging; override the key per
# deploy. The fallback key is the shared public demo key — fine for a throwaway
# demo, but real deploys should pass LYZR_API_KEY via the environment (deploy.sh
# does). It is never sent to the browser — that's the whole point of this proxy.
LYZR_BASE = os.environ.get("LYZR_VOICE_BASE", "https://voice-livekit.studio.lyzr.ai").rstrip("/")
LYZR_API_KEY = os.environ.get("LYZR_API_KEY", "sk-default-HkKRBzwWISgFWvmlumOnMrvoaIwNdfY4")
DEFAULT_AGENT_ID = os.environ.get("LYZR_DEFAULT_AGENT_ID", "6a26e4fc6d80be4fdfe65fa1")

# A single pooled async client for the process lifetime. Lyzr can be slow to
# spin a room + dispatch an agent on a cold session, so allow a generous read
# timeout.
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One pooled async client for the process lifetime, kept on app.state.
    async with httpx.AsyncClient(base_url=LYZR_BASE, timeout=_TIMEOUT) as client:
        app.state.client = client
        yield


app = FastAPI(title="Mova-iO Voice Playground (LiveKit)", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "x-api-key": LYZR_API_KEY}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/config")
async def config() -> dict[str, object]:
    """Bootstrap values the UI needs at load: the preset agent + whether a key
    is configured. Never leaks the key value itself."""
    return {
        "defaultAgentId": DEFAULT_AGENT_ID,
        "voiceBase": LYZR_BASE,
        "keyed": bool(LYZR_API_KEY),
        "buildTag": os.environ.get("BUILD_TAG", "dev"),
    }


@app.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "keyed": bool(LYZR_API_KEY), "agent": DEFAULT_AGENT_ID}


async def _forward(request: Request, path: str, payload: dict[str, object]) -> JSONResponse:
    """POST `payload` to Lyzr `path` with the server-side key; pass the response
    (status + JSON) straight back to the browser so the UI can surface Lyzr's
    own validation errors verbatim."""
    client: httpx.AsyncClient = request.app.state.client
    try:
        r = await client.post(path, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"error": f"upstream request failed: {exc}"})
    # Lyzr's /sessions/end returns 204 No Content; a 204 (or any empty body) must
    # not carry a JSON payload, so normalize success-with-no-body to {"ok": true}.
    if not r.content:
        status = 200 if r.is_success else r.status_code
        return JSONResponse(status_code=status, content={"ok": r.is_success})
    try:
        body = r.json()
    except ValueError:
        body = (
            {"ok": True}
            if r.is_success
            else {"error": "non-JSON response from Lyzr", "raw": r.text[:500]}
        )
    return JSONResponse(status_code=r.status_code, content=body)


@app.post("/sessions/start")
async def start_session(request: Request) -> JSONResponse:
    body = await request.json()
    agent_id = (body.get("agentId") or DEFAULT_AGENT_ID).strip()
    # userIdentity is required by Lyzr; default to a per-tab id the browser sends.
    user_identity = (body.get("userIdentity") or "web-user").strip()
    payload: dict[str, object] = {"agentId": agent_id, "userIdentity": user_identity}
    # Let the caller pin a room name if they want (optional in the Lyzr API).
    if body.get("roomName"):
        payload["roomName"] = body["roomName"]
    return await _forward(request, "/v1/sessions/start", payload)


@app.post("/sessions/end")
async def end_session(request: Request) -> JSONResponse:
    body = await request.json()
    room_name = (body.get("roomName") or "").strip()
    if not room_name:
        return JSONResponse(status_code=400, content={"error": "roomName is required"})
    return await _forward(request, "/v1/sessions/end", {"roomName": room_name})
