"""mdk-voice browser demo — FastAPI WebSocket server.

    python examples/web_demo/server.py
    # then open http://localhost:8765 in a browser

Real audio in/out via the browser microphone:

    browser mic (PCM16 16kHz)
        ─▶ WebSocket /ws/voice
            ─▶ FailoverSTT(Deepgram → OpenAI Whisper)
                ─▶ OpenAIChatAgent (streaming Chat Completions)
                    ─▶ FailoverTTS(Cartesia → OpenAI), streaming sentence-by-sentence
                        ─▶ tts.audio frames back to browser

Plus: PII redaction on the emitted transcript, MetricsObserver snapshot per
turn, latency badge from the event stream. Keys auto-load from
~/.mdk_{openai,cartesia,deepgram}_key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from movate.voice import (
    AdaptiveEndpointing,
    AgentTurnError,
    AgentTurnResult,
    AudioChunk,
    CartesiaTTS,
    DeepgramSTT,
    FailoverSTT,
    FailoverTTS,
    HeuristicTurnDetector,
    InMemoryVoiceCache,
    MetricsObserver,
    OpenAIRealtime,
    OpenAITTS,
    OpenAIWhisperSTT,
    SilenceGatedSTT,
    SpeculationGuard,
    check_lyzr_parity,
    compute_turn_latency,
    format_latency_badge,
    is_silent,
    redact_pii,
    resample_pcm16,
    run_voice_pipeline,
    speakify,
    warm_stt,
)

# Make the demo dir importable so this script runs the same whether you launch
# it via `python examples/web_demo/server.py` or `python -m examples.web_demo.server`.
sys.path.insert(0, str(Path(__file__).parent))
from agent import OpenAIChatAgent  # noqa: E402 - relative import after path tweak
from recording import (  # noqa: E402 - relative import after path tweak
    CallRecorder,
    is_recording_enabled,
    upload_recording,
)

log = logging.getLogger("movate.voice.demo")
DEMO_DIR = Path(__file__).parent

# Deepgram keyterm boosting (nova-3): domain vocabulary the general model often
# mis-hears in enterprise IT-support calls. Boosting these at recognition time is
# the cheapest accuracy win — "VPN" / "VIP" / "Okta" / "Mova-iO" come back right
# instead of "BPM" / "VP" / "October" / "movie I/O". Tune per deployment; an
# operator can override the whole list via the DEEPGRAM_KEYTERMS env var
# (comma-separated). Empty list / unset = no boosting (the prior behavior).
_DEFAULT_KEYTERMS = [
    "VPN",
    "VIP",
    "Okta",
    "Mova-iO",
    "Movate",
    "Lyzr",
    "SSO",
    "MFA",
    "Active Directory",
    "Outlook",
    "SharePoint",
    "Azure",
]


def _demo_keyterms() -> list[str]:
    """Curated keyterm list, overridable via DEEPGRAM_KEYTERMS (comma-separated)."""
    override = os.environ.get("DEEPGRAM_KEYTERMS")
    if override is not None:
        return [t.strip() for t in override.split(",") if t.strip()]
    return list(_DEFAULT_KEYTERMS)


# ── key loading ───────────────────────────────────────────────────────────────


def load_keys() -> dict[str, bool]:
    """Auto-load API keys from ~/.mdk_*_key files; never logs the value."""
    home = Path.home()
    present: dict[str, bool] = {}
    for env, name in (
        ("OPENAI_API_KEY", "openai"),
        ("CARTESIA_API_KEY", "cartesia"),
        ("DEEPGRAM_API_KEY", "deepgram"),
        ("LYZR_API_KEY", "lyzr"),
    ):
        if os.environ.get(env):
            present[name] = True
            continue
        path = home / f".mdk_{name}_key"
        if path.is_file():
            os.environ[env] = path.read_text().strip()
            present[name] = True
        else:
            present[name] = False
    return present


# ── per-session state (one agent + one metrics observer per connection) ──────


class _FaultSTT:
    """Inject a one-shot STT failure for the next turn (demo: trigger failover).

    Sits in front of the real provider in the failover chain. When ``arm()`` is
    called, the next ``transcribe()`` raises — exactly once — so the audience
    sees the router pick the fallback live.
    """

    def __init__(self, inner: object, name: str) -> None:
        self._inner = inner
        self.name = name
        self.version = "0"
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    async def transcribe(  # noqa: ANN001,ANN201
        self, audio, *, language=None, api_key=None, keyterms=None, endpointing_ms=None
    ):
        if self._armed:
            self._armed = False
            # Drain the buffer so the failover composite can replay it cleanly.
            async for _ in audio:
                pass
            raise RuntimeError("fault injected (demo)")
        async for c in self._inner.transcribe(  # type: ignore[attr-defined]
            audio,
            language=language,
            api_key=api_key,
            keyterms=keyterms,
            endpointing_ms=endpointing_ms,
        ):
            yield c

    async def warm(self, api_key: str | None = None) -> bool:  # ADR 073 Phase 5
        return await warm_stt(self._inner, api_key)


class _FaultTTS:
    """One-shot TTS failure for the next turn (demo: failover on synthesis)."""

    def __init__(self, inner: object, name: str) -> None:
        self._inner = inner
        self.name = name
        self.version = "0"
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    async def synthesize(self, text, *, voice_id="", codec="pcm16", api_key=None):  # noqa: ANN001,ANN201
        if self._armed:
            self._armed = False
            async for _ in text:
                pass
            raise RuntimeError("fault injected (demo)")
        async for c in self._inner.synthesize(  # type: ignore[attr-defined]
            text, voice_id=voice_id, codec=codec, api_key=api_key
        ):
            yield c


TTS_TIERS = {
    "cartesia": "Cartesia (streaming, fast)",
    "openai": "OpenAI (buffered, slower)",
}


# Curated voice catalog per provider. Each entry: id (the value the SDK takes),
# label (UI display), description (voice quality), use_case (when to pick it).
# UI shows: "<label> — <description> (<use_case>)" so the user picks by intent.
#
# OpenAI: 6 official voices, well-documented characteristics.
# Cartesia: a small curated subset of their Sonic-2 voices (their library has
#   dozens — these are picked for clear use-case differentiation in a demo).
#
# Voice IDs reference the actual SDK identifiers. Cartesia IDs are UUIDs from
# their public voice catalog; if Cartesia rotates them, the request falls back
# to the provider default (the adapter handles voice_id="" as default).
TTS_VOICES: dict[str, list[dict[str, str]]] = {
    "openai": [
        {
            "id": "alloy",
            "label": "Alloy",
            "description": "neutral, balanced",
            "use_case": "general support, default",
        },
        {
            "id": "echo",
            "label": "Echo",
            "description": "calm, measured male",
            "use_case": "customer service, IVR",
        },
        {
            "id": "fable",
            "label": "Fable",
            "description": "warm British accent",
            "use_case": "storytelling, training",
        },
        {
            "id": "onyx",
            "label": "Onyx",
            "description": "deep, authoritative male",
            "use_case": "announcements, alerts",
        },
        {
            "id": "nova",
            "label": "Nova",
            "description": "bright, friendly female",
            "use_case": "help desk, assistant",
        },
        {
            "id": "shimmer",
            "label": "Shimmer",
            "description": "soft, expressive female",
            "use_case": "wellness, calm settings",
        },
    ],
    # Cartesia entries are *fallback only* — populated live from Cartesia's
    # /voices API at request time (see `_fetch_cartesia_voices`), so names +
    # gender + descriptions come from the source of truth (Cartesia rotates
    # voice IDs / renames characters). This list is only used when the API is
    # unreachable AND we haven't cached a fresh result. Kept as the well-
    # documented Cartesia default voice + an empty placeholder so the UI
    # still has something selectable.
    "cartesia": [
        {
            "id": "a0e99841-438c-4a64-b679-ae501e7d6091",
            "label": "Cartesia default",
            "description": "F · adapter default voice",
            "use_case": "general use (fallback when /voices unreachable)",
        },
    ],
}


# ── Multi-language voice mapping ─────────────────────────────────────────
# Maps ISO 639-1 language codes to recommended TTS voices per provider.
# Cartesia has multilingual voices (Sonic Multilingual model); OpenAI TTS
# auto-adapts to the text language (alloy/nova/etc. work for any language).
# When the user selects a language, we auto-select the matching Cartesia
# voice so pronunciation is native. Deepgram STT just needs the language=
# hint (e.g. "es", "fr") which is passed separately.
SUPPORTED_LANGUAGES: list[dict[str, str]] = [
    {"code": "", "label": "English (default)", "flag": "🇺🇸"},
    {"code": "es", "label": "Spanish", "flag": "🇪🇸"},
    {"code": "fr", "label": "French", "flag": "🇫🇷"},
    {"code": "fr-CA", "label": "French (Canadian)", "flag": "🇨🇦"},
    {"code": "de", "label": "German", "flag": "🇩🇪"},
    {"code": "pt", "label": "Portuguese", "flag": "🇧🇷"},
    {"code": "hi", "label": "Hindi", "flag": "🇮🇳"},
    {"code": "ja", "label": "Japanese", "flag": "🇯🇵"},
    {"code": "zh", "label": "Chinese (Mandarin)", "flag": "🇨🇳"},
    {"code": "ko", "label": "Korean", "flag": "🇰🇷"},
    {"code": "auto", "label": "Auto-detect", "flag": "🌐"},
]
LANGUAGE_VOICES: dict[str, dict[str, str]] = {
    # Cartesia Sonic Multilingual voices — curated for natural accent.
    # OpenAI TTS voices are language-agnostic (auto-adapt), so no mapping needed.
    # These Cartesia voice IDs may change — the TTS failover guard in
    # openai_speech.py / cartesia.py handles unrecognized IDs gracefully.
    "es": {"cartesia": "846d6cb0-2301-48b6-9683-48f5618ea2f6", "label": "Spanish (Sophia)"},
    "fr": {"cartesia": "a8a1eb38-5f15-4c1d-8722-7ac0f329f8f3", "label": "French (Marie)"},
    "fr-CA": {"cartesia": "a8a1eb38-5f15-4c1d-8722-7ac0f329f8f3", "label": "French-Canadian (Marie)"},
    "de": {"cartesia": "3f6e78a8-5283-42aa-b236-e00b39dbb3d3", "label": "German (Hans)"},
    "pt": {"cartesia": "700d1ee3-a641-4018-ba6e-899dcadc9e2b", "label": "Portuguese (Ana)"},
    "hi": {"cartesia": "95856005-0332-41b0-935f-352e296aa0df", "label": "Hindi (Priya)"},
    "ja": {"cartesia": "2b568345-1d48-4047-b25f-7baccf842eb0", "label": "Japanese (Yuki)"},
    "zh": {"cartesia": "e90c6678-f0d3-4767-9883-5d0ecf5894a8", "label": "Chinese (Li Wei)"},
    "ko": {"cartesia": "663afeec-d082-4ab5-827e-2e41bf73fa9c", "label": "Korean (Soo-Jin)"},
}


# Cartesia voice catalog cache. Voices rarely change but the catalog is 700+
# entries so we don't want to re-fetch + filter on every page load. Per-key
# (so a BYOK user's catalog and the server's stay isolated, even though they
# usually return identical results for the public catalog).
_CARTESIA_VOICES_TTL_S = 3600.0
_cartesia_voices_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _classify_use_case(description: str) -> str:
    """Heuristic best-fit label from Cartesia's free-text description.

    Cartesia voices ship with a one-line description like "Calm voice with
    soothing balance" or "Authortative, mature female for giving
    instructions". Bucket those into a handful of use-case labels so the
    dropdown reads "Anna (F) — professional voice (enterprise voice)" rather
    than just the raw description.
    """
    d = (description or "").lower()
    rules: tuple[tuple[tuple[str, ...], str], ...] = (
        (("meditat", "calm", "asmr", "soothing", "relax"), "wellness, calm settings"),
        (("support", "service", "customer", "help"), "customer service, help desk"),
        (
            ("instruct", "training", "guide", "explainer", "walkthrough", "task"),
            "training, walkthroughs",
        ),
        (
            ("conversational", "friendly", "warm", "approach", "natural", "sister", "pal"),
            "general support, conversation",
        ),
        (
            ("authoritative", "command", "professional", "polished", "business", "executive"),
            "enterprise voice, announcements",
        ),
        (("british", "refined", "narration", "storytell"), "storytelling, narration"),
        (("upbeat", "energetic", "cheerful", "bright"), "consumer apps, retail"),
        (("news", "broadcast"), "broadcast, news"),
        (("character", "gaming", "fairy", "animated", "performer"), "entertainment, games"),
    )
    for keywords, label in rules:
        if any(k in d for k in keywords):
            return label
    return "general use"


def _fetch_cartesia_voices_sync(api_key: str) -> list[dict[str, str]]:
    """Synchronous Cartesia voice catalog fetch. Wrapped via to_thread."""
    if not api_key:
        return []
    try:
        import httpx  # noqa: PLC0415

        r = httpx.get(
            "https://api.cartesia.ai/voices/",
            headers={"X-API-Key": api_key, "Cartesia-Version": "2024-06-10"},
            timeout=8.0,
        )
        r.raise_for_status()
        voices = r.json() or []
    except Exception as exc:  # noqa: BLE001
        log.warning("cartesia voices fetch failed: %s", exc)
        return []
    if not isinstance(voices, list):
        return []
    # Filter to English, public voices; skip clearly-character voices that
    # would distract from a business demo.
    SKIP = ("fairy", "gaming", "pikachu", "robot", "elf", "demon", "ghost", "alien")
    en = [
        v
        for v in voices
        if isinstance(v, dict)
        and v.get("language") == "en"
        and v.get("is_public")
        and v.get("id")
        and v.get("name")
        and not any(s in (v.get("description") or "").lower() for s in SKIP)
        and not any(s in (v.get("name") or "").lower() for s in SKIP)
    ]
    # Score each voice toward business-friendliness so the top-N is curated,
    # not random. Voices with support/service/professional keywords float up.
    PREFERRED = (
        "support",
        "professional",
        "natural",
        "conversational",
        "assistant",
        "guide",
        "explainer",
        "customer",
        "warm",
        "friendly",
    )

    def score(v: dict[str, object]) -> int:
        d = str(v.get("description") or "").lower()
        return sum(1 for k in PREFERRED if k in d)

    en.sort(key=lambda v: (-score(v), str(v.get("name") or "")))
    # Cap to a balanced ~16; ensure both genders represented.
    by_gender: dict[str, list[dict[str, object]]] = {"feminine": [], "masculine": []}
    seen_names: set[str] = set()  # dedup by name (Cartesia has multiple "Carson" voices, etc.)
    for v in en:
        # Use the trimmed name (before " - Subtitle") as the dedup key.
        nm_full = str(v.get("name") or "")
        nm_key = nm_full.split(" - ")[0].strip().lower()
        if not nm_key or nm_key in seen_names:
            continue
        seen_names.add(nm_key)
        g = str(v.get("gender") or "").lower()
        if g.startswith("fem"):
            key = "feminine"
        elif g.startswith("masc"):
            key = "masculine"
        else:
            key = "feminine"
        if len(by_gender[key]) < 8:
            by_gender[key].append(v)
    picked = by_gender["feminine"] + by_gender["masculine"]

    def _normalize(v: dict[str, object]) -> dict[str, str]:
        # Strip the "- Subtitle" suffix Cartesia uses ("Anna - Methodical Guide" → "Anna")
        name = str(v.get("name") or "").split(" - ")[0].strip()
        gender = str(v.get("gender") or "").lower()
        if gender.startswith("fem"):
            gender_short = "F"
        elif gender.startswith("masc"):
            gender_short = "M"
        else:
            gender_short = "?"
        desc = str(v.get("description") or "").strip().rstrip(".")
        # Cap description for UI sanity.
        if len(desc) > 80:
            desc = desc[:77] + "…"
        return {
            "id": str(v.get("id") or ""),
            "label": f"{name} ({gender_short})",
            "description": desc or f"{gender_short} voice",
            "use_case": _classify_use_case(desc),
        }

    return [_normalize(v) for v in picked]


async def _fetch_cartesia_voices_cached(api_key: str) -> list[dict[str, str]]:
    """Cache wrapper over the synchronous fetch (1 hour TTL, per-key)."""
    import time as _t  # noqa: PLC0415

    if not api_key:
        return []
    now = _t.monotonic()
    cached = _cartesia_voices_cache.get(api_key)
    if cached and now - cached[0] < _CARTESIA_VOICES_TTL_S:
        return cached[1]
    voices = await asyncio.to_thread(_fetch_cartesia_voices_sync, api_key)
    if voices:  # don't cache an empty/failed result — retry on next request
        _cartesia_voices_cache[api_key] = (now, voices)
    return voices


# Provider IDs the BYOK UI accepts. Scoped to JUST Lyzr because that's the
# one provider where each demo user genuinely brings their own account +
# agents — STT (Deepgram), TTS (Cartesia), and OpenAI Chat are infrastructure
# the demo provides and pays for. Keeping the user-facing surface narrow makes
# the privacy story crisp ("the only credential you ever paste here is yours
# to begin with — your Lyzr key").
#
# Server-side the _KeyedSTT/_KeyedTTS wrappers stay general (they're cheap,
# and ``user_keys.get("openai")`` simply returns ``None`` so they fall through
# to call-time api_key). That keeps the door open if a future round wants to
# extend BYOK to more providers without re-architecting the chain.
USER_KEY_PROVIDERS = ("lyzr",)


class _KeyedSTT:
    """Force a fixed BYOK api_key on one provider inside a failover chain.

    FailoverSTT passes one api_key down the chain (per-call from
    ``run_voice_pipeline(stt_api_key=...)``). That works when every provider
    in the chain authenticates the same way — but our chains mix Deepgram +
    OpenAI Whisper, which need different keys. This wrapper pins the right
    key on each member so a BYOK user can supply per-provider tokens.

    Falls back to the chain's call-time ``api_key`` when no fixed key is set
    — so wrapping a provider in this is safe even without BYOK active.
    """

    def __init__(self, inner: Any, *, api_key: str | None = None) -> None:
        self._inner = inner
        self._fixed_key = api_key or None
        self.name = inner.name
        self.version = getattr(inner, "version", "0")

    async def transcribe(  # noqa: ANN001,ANN201
        self, audio, *, language=None, api_key=None, keyterms=None, endpointing_ms=None
    ):
        async for c in self._inner.transcribe(
            audio,
            language=language,
            api_key=self._fixed_key or api_key,
            keyterms=keyterms,
            endpointing_ms=endpointing_ms,
        ):
            yield c

    async def warm(self, api_key: str | None = None) -> bool:  # ADR 073 Phase 5
        return await warm_stt(self._inner, self._fixed_key or api_key)


class _KeyedTTS:
    """TTS counterpart of :class:`_KeyedSTT` — same per-provider-key pinning."""

    def __init__(self, inner: Any, *, api_key: str | None = None) -> None:
        self._inner = inner
        self._fixed_key = api_key or None
        self.name = inner.name
        self.version = getattr(inner, "version", "0")

    async def synthesize(self, text, *, voice_id="", codec="pcm16", api_key=None):  # noqa: ANN001,ANN201
        async for c in self._inner.synthesize(
            text, voice_id=voice_id, codec=codec, api_key=self._fixed_key or api_key
        ):
            yield c


AGENT_TIERS = {
    "openai": "OpenAI Chat (GPT-4o-mini)",
    "lyzr": "Mova-iO Streaming (/v3 native)",
    # L6: alternative Lyzr integration path — wraps Lyzr's /v3/inference/chat/
    # (the SDK's historical endpoint) inside movate.voice.LyzrAgentTurn. Slower
    # than streaming (buffered, no token streaming) but demonstrates the
    # SDK-wrapping binding shipped in mdk-voice. The architectural point: the
    # same voice pipeline handles BOTH a streaming agent AND a non-streaming
    # SDK-shaped agent (via LyzrAgentTurn) without changing.
    "lyzr_sdk": "Mova-iO SDK (buffered)",
    "deep_agent": "Deep Agent (LangChain planning + subagents)",
}


class _LyzrV3HTTPAgent:
    """Duck-typed agent for :class:`LyzrAgentTurn` — calls /v3/inference/chat/.

    The mdk_voice :class:`LyzrAgentTurn` binding is duck-typed: it expects
    *any* object with ``.run(text)`` returning something with ``.response``.
    This is the smallest possible client — no ``lyzr`` SDK install needed —
    that satisfies that contract. Demonstrates the binding's reach without
    forcing the lazy-import SDK dependency into the deployed demo image.
    """

    def __init__(self, agent_id: str, api_key: str) -> None:
        self._agent_id = agent_id
        self._api_key = api_key

    def run(self, message: str) -> object:  # sync — matches LyzrAgentTurn's threading model
        import httpx  # noqa: PLC0415

        r = httpx.post(
            "https://agent-prod.studio.lyzr.ai/v3/inference/chat/",
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            json={
                "user_id": "mdk-voice-demo",
                "agent_id": self._agent_id,
                "session_id": "demo",
                "message": message,
            },
            timeout=30,
        )
        r.raise_for_status()
        body = r.json() or {}
        # LyzrAgentTurn extracts .response (or dict["response"], or bare str);
        # return a dict for the simplest happy path.
        return {"response": body.get("response", "")}


class LyzrV3StreamAgent:
    """Streaming agent using Lyzr's native ``/v3/inference/stream/`` endpoint.

    Uses ``x-api-key`` header auth (the standard Lyzr auth) instead of
    ``Authorization: Bearer`` (the OpenAI-compat ``/v4`` path). This matters
    because some agents are only accessible via the ``/v3`` auth — the ``/v4``
    OpenAI-compat shim can return 403 for the same key + agent_id that works
    fine on ``/v3``.

    Streams the response using ``httpx.AsyncClient.stream()`` and fires
    ``on_token`` per chunk so the voice pipeline can overlap TTS synthesis
    with agent generation — same latency benefit as the OpenAI SSE path.

    Implements the AgentTurn protocol: ``async run(text, *, on_token, ...)``
    returning an ``AgentTurnResult``.
    """

    name = "lyzr-v3-stream"
    version = "1"
    speculatable = True  # stateless per-call; safe to discard

    def __init__(
        self,
        *,
        agent_id: str,
        api_key: str,
        voice_hint: str | None = None,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
        on_extras: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        import httpx  # noqa: PLC0415

        self._agent_id = agent_id
        self._api_key = api_key
        self._voice_hint = (voice_hint or "").strip() or None
        self.on_tool_call = on_tool_call
        self.on_extras = on_extras
        self._session_id = f"mdk-voice-{id(self):x}"
        # Reuse a single httpx client across turns — avoids a TCP+TLS
        # handshake (~100–300 ms) on every call. The client's connection
        # pool keeps the HTTPS connection warm between turns.
        self._http = httpx.AsyncClient(
            timeout=30,
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/x-ndjson, application/json",
            },
        )

    def reset(self) -> None:
        """Clear server-side session by rotating the session_id."""
        self._session_id = f"mdk-voice-{id(self):x}-{__import__('time').monotonic_ns()}"

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurnResult:
        user_content = text
        if self._voice_hint:
            user_content = f"{text}\n\n[Voice channel — {self._voice_hint}]"

        payload = {
            "user_id": "mdk-voice-demo",
            "agent_id": self._agent_id,
            "session_id": session_id or self._session_id,
            "message": user_content,
        }

        try:
            async with self._http.stream(
                "POST",
                "https://agent-prod.studio.lyzr.ai/v3/inference/stream/",
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    return AgentTurnResult(
                        status="error",
                        error=AgentTurnError(message=f"{resp.status_code}: {body[:500]}"),
                    )

                collected: list[str] = []
                # Auto-detect the streaming format by inspecting chunks.
                # Lyzr may use SSE ("data: ..."), newline-delimited JSON,
                # or plain text streaming. We handle all three.
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue

                    text_chunk = ""

                    # SSE format: "data: ..." lines
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            # OpenAI-compat SSE shape
                            if "choices" in obj:
                                delta = obj["choices"][0].get("delta", {})
                                text_chunk = delta.get("content", "")
                            # Lyzr native shape
                            elif "response" in obj:
                                text_chunk = obj["response"]
                            elif "content" in obj:
                                text_chunk = obj["content"]
                            elif "chunk" in obj:
                                text_chunk = obj["chunk"]
                            # Surface extras if present
                            if self.on_extras is not None:
                                extras: dict[str, Any] = {}
                                for f in ("citations", "sources", "metadata"):
                                    if f in obj and obj[f]:
                                        extras[f] = obj[f]
                                if extras:
                                    try:
                                        self.on_extras(extras)
                                    except Exception:  # noqa: BLE001
                                        pass
                        except json.JSONDecodeError:
                            # Plain text after "data: " prefix
                            text_chunk = data

                    # Newline-delimited JSON (no "data:" prefix)
                    elif line.startswith("{"):
                        try:
                            obj = json.loads(line)
                            text_chunk = (
                                obj.get("response", "")
                                or obj.get("content", "")
                                or obj.get("chunk", "")
                                or obj.get("text", "")
                            )
                            # Check for done signal
                            if obj.get("done") is True and not text_chunk:
                                break
                        except json.JSONDecodeError:
                            text_chunk = line

                    # Plain text streaming — each line IS a chunk
                    else:
                        text_chunk = line

                    if text_chunk:
                        collected.append(text_chunk)
                        if on_token is not None:
                            on_token(text_chunk)

                answer = "".join(collected).strip()

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return AgentTurnResult(
                status="error",
                error=AgentTurnError(message=str(exc) or exc.__class__.__name__),
            )

        if not answer:
            return AgentTurnResult(
                status="error",
                error=AgentTurnError(message="Empty response from Lyzr streaming endpoint"),
            )

        return AgentTurnResult(status="ok", answer_text=answer)


def _lyzr_agent_id() -> str | None:
    """Read the Lyzr agent_id from env or ~/.mdk_lyzr_agent_id."""
    aid = os.environ.get("LYZR_AGENT_ID")
    if aid:
        return aid.strip()
    path = Path.home() / ".mdk_lyzr_agent_id"
    if path.is_file():
        return path.read_text().strip()
    return None


def _build_agent(
    tier: str,
    *,
    lyzr_agent_id: str | None = None,
    on_tool_call: Callable[[dict[str, Any]], None] | None = None,
    on_extras: Callable[[dict[str, Any]], None] | None = None,
    openai_api_key: str | None = None,
    lyzr_api_key: str | None = None,
) -> object:
    """Build an AgentTurn for the chosen tier.

    The ``lyzr`` tier uses Lyzr's native ``/v3/inference/stream/`` endpoint
    with ``x-api-key`` header auth. This is the officially documented Lyzr
    streaming API and avoids the 403 permission issues that the OpenAI-compat
    ``/v4/chat/completions`` shim (Bearer auth) sometimes returns for BYOK
    agents. Token streaming feeds the sentence-by-sentence TTS pipeline just
    like the OpenAI Chat path — same latency benefit.

    The agent's own system prompt is configured in Lyzr Studio, so we append
    a voice-context cue to the user message instead of overriding it.
    """
    # Lyzr agents in Studio are typically configured to produce structured,
    # multi-section chat output (headers, bullet lists, citations). That reads
    # terribly aloud. We don't touch the operator's system prompt — we just
    # append a voice-context cue to the user turn so the model knows to
    # reshape the answer for spoken delivery. Same hint used for streaming and
    # the SDK paths. Adjust to taste; leave empty to disable.
    _LYZR_VOICE_HINT = (
        "please respond conversationally in 1-3 short sentences suitable for "
        "text-to-speech playback. Do not use markdown, headers, bullet lists, "
        "numbered steps, or section labels. Summarize key info inline. If a "
        "long procedure is needed, offer to send detailed steps separately."
    )
    if tier == "lyzr":
        # BYOK: prefer the user-supplied lyzr_api_key; else env.
        key = lyzr_api_key or os.environ.get("LYZR_API_KEY")
        agent_id = (lyzr_agent_id or _lyzr_agent_id() or "").strip()
        log.info(
            "build_agent(lyzr): agent_id=%s key_source=%s key_len=%d",
            (agent_id or "—")[:8],
            "byok" if lyzr_api_key else ("env" if key else "none"),
            len(key) if key else 0,
        )
        if key and agent_id:
            try:
                return LyzrV3StreamAgent(
                    agent_id=agent_id,
                    api_key=key,
                    voice_hint=_LYZR_VOICE_HINT,
                    on_tool_call=on_tool_call,
                    on_extras=on_extras,
                )
            except Exception as exc:  # noqa: BLE001 - any Lyzr setup failure → fallback
                log.warning("Lyzr streaming unavailable, falling back to OpenAI Chat: %s", exc)
        else:
            log.warning(
                "Lyzr selected but %s missing — falling back to OpenAI Chat",
                "key" if not key else "agent_id",
            )
    if tier == "lyzr_sdk":
        # L6: the same Lyzr agent, voiced through movate.voice.LyzrAgentTurn (the
        # SDK-wrapping binding) instead of OpenAIChatAgent + /v4. Demonstrates
        # that the voice pipeline doesn't care WHICH AgentTurn implementation
        # runs the turn — pluralism within one provider (ADR 067).
        from movate.voice import LyzrAgentTurn  # noqa: PLC0415

        key = lyzr_api_key or os.environ.get("LYZR_API_KEY")
        agent_id = (lyzr_agent_id or _lyzr_agent_id() or "").strip()
        if key and agent_id:
            try:
                return LyzrAgentTurn(_LyzrV3HTTPAgent(agent_id, key))
            except Exception as exc:  # noqa: BLE001
                log.warning("Lyzr SDK path unavailable, falling back: %s", exc)
        else:
            log.warning("Lyzr SDK selected but key/agent_id missing — falling back")
    if tier == "deep_agent":
        try:
            from movate.integrations.deep_agents import DeepAgentTurn  # noqa: PLC0415

            return DeepAgentTurn(
                model="openai:gpt-4o-mini",
                system_prompt=(
                    "You are Deva, a helpful voice assistant at Movate. "
                    "Reply concisely in 1-3 sentences. You have planning "
                    "capabilities — for complex questions, break them into "
                    "steps. You are being read aloud via text-to-speech."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Deep Agent unavailable, falling back to OpenAI Chat: %s", exc)
    return OpenAIChatAgent(
        on_tool_call=on_tool_call,
        on_extras=on_extras,
        api_key=openai_api_key,  # BYOK — None = SDK reads OPENAI_API_KEY env
    )


# Indicative per-call prices. Real production reads these from the manifest +
# observer; for the demo we keep a tiny table close to the call sites so the
# "$0.00X" badge is concrete + auditable.
_TTS_PRICE_PER_CHAR = {"cartesia": 0.000035, "openai": 0.000015, "elevenlabs": 0.00018}
_STT_PRICE_PER_MIN = {"deepgram": 0.0043, "openai_whisper": 0.006}
# GPT-4o-mini @ $0.150 / $0.600 per 1M tokens (input/output). Tokens estimated
# as len/4 (a common rule of thumb good enough for an on-stage cents display).
_AGENT_IN_PER_TOKEN = 0.150 / 1_000_000
_AGENT_OUT_PER_TOKEN = 0.600 / 1_000_000


def _estimate_cost(*, transcript: str, answer: str, audio_seconds: float, tts_tier: str) -> float:
    """Cents-precision estimate of one turn's cost (STT + LLM + TTS)."""
    stt = (audio_seconds / 60.0) * _STT_PRICE_PER_MIN.get("deepgram", 0)
    in_tokens = len(transcript) / 4
    out_tokens = len(answer) / 4
    llm = in_tokens * _AGENT_IN_PER_TOKEN + out_tokens * _AGENT_OUT_PER_TOKEN
    tts = len(answer) * _TTS_PRICE_PER_CHAR.get(tts_tier, 0)
    return round(stt + llm + tts, 5)


class _TrailObserver:
    """Compose :class:`MetricsObserver` with a per-event callback for the UI.

    Failover/breaker/cache events flow through both sinks: the metrics observer
    counts them (powering the dashboard's session totals), and the callback
    forwards a structured ``{"kind", "fields"}`` to a per-session WebSocket so
    the browser can render a live failover trail in its event stream.

    The callback is optional — when ``None`` (e.g. ``mdk voice bench`` mode or
    a unit test) the observer behaves exactly like a bare :class:`MetricsObserver`.
    Only ADR-068 router events (provider_selected, failover, circuit_*,
    exhausted, cache_hit, hedge) are surfaced; the firehose stays on the metrics
    side. This keeps A1 demo-clean: each line in the event-stream means something
    to a viewer.
    """

    def __init__(
        self,
        metrics: MetricsObserver,
        on_trail: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._metrics = metrics
        self._on_trail = on_trail

    # Events we surface to the UI — kept tight so the event stream stays readable.
    _UI_EVENTS = frozenset(
        {
            "provider_selected",
            "failover",
            "circuit_open",
            "circuit_close",
            "exhausted",
            "cache_hit",
            "hedge",
            "hedge_won",
            # ADR 070 — speculative kickoff outcomes (live commit/cancel trail).
            "speculation_started",
            "speculation_committed",
            "speculation_cancelled",
        }
    )

    def on_event(self, event: str, /, **fields: Any) -> None:  # noqa: D102 - Protocol
        self._metrics.on_event(event, **fields)
        if self._on_trail is not None and event in self._UI_EVENTS:
            # Catch any callback-side error so a UI hiccup never poisons the
            # router's accounting path.
            try:
                self._on_trail(event, fields)
            except Exception:  # noqa: BLE001 - defensive: UI failures must not affect routing
                pass


class Session:
    """One browser tab — its own agent memory + accumulating metrics.

    The TTS primary tier is swappable per turn (the A/B latency-comparison
    toggle in the UI), and faults can be injected to trigger failover on stage.
    """

    def __init__(
        self,
        on_trail: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
        on_extras: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        # BYOK — per-session user-supplied API keys (override server's env keys).
        # All entries default to None = "use server's env key". Never logged
        # in full, never persisted anywhere.
        self.user_keys: dict[str, str | None] = {p: None for p in USER_KEY_PROVIDERS}
        # Stash so set_agent_tier can rebuild with the same callbacks (otherwise
        # swapping tier mid-session would silently drop tool visibility / extras).
        self._on_tool_call = on_tool_call
        self._on_extras = on_extras
        # Stable per-session id used as the recording blob name + the
        # /recordings/{session_id} key. uuid4 keeps it unguessable so a
        # second tab can't drive-by-fetch the first tab's audio.
        import uuid  # noqa: PLC0415

        self.session_id = uuid.uuid4().hex
        # Recorder is None when RECORD_CALLS=1 + AZURE_STORAGE_CONNECTION_STRING
        # aren't both set — zero per-frame overhead in the default deploy.
        self.recorder: CallRecorder | None = CallRecorder() if is_recording_enabled() else None
        self.metrics = MetricsObserver()
        # Composite observer = metrics + optional UI trail callback (A1).
        # FailoverSTT/TTS see one Protocol-shaped observer; the UI plumbing is
        # invisible from the router's point of view (ADR 068 D7).
        self.observer = _TrailObserver(self.metrics, on_trail=on_trail)
        self.cache = InMemoryVoiceCache()
        self.agent_tier = "openai"
        # The Lyzr agent_id is tracked separately so the user can change it from
        # the UI without losing context, and so the dashboard can show which
        # agent is in use. Defaults to the file/env-loaded id.
        self.lyzr_agent_id = (_lyzr_agent_id() or "").strip()
        self.agent: object = _build_agent(
            self.agent_tier,
            lyzr_agent_id=self.lyzr_agent_id,
            on_tool_call=self._on_tool_call,
            on_extras=self._on_extras,
            openai_api_key=self.user_keys.get("openai"),
            lyzr_api_key=self.user_keys.get("lyzr"),
        )
        # Fault wrappers sit in front of the primaries so a one-shot inject
        # forces the failover composite to pick the fallback.
        self._fault_stt = _FaultSTT(
            DeepgramSTT(keyterms=_demo_keyterms()), name="deepgram_with_fault"
        )
        self._fault_tts_cartesia = _FaultTTS(CartesiaTTS(), name="cartesia_with_fault")
        self._fault_tts_openai = _FaultTTS(OpenAITTS(), name="openai_with_fault")
        # B2: latency-hedging toggles (default off — doubles cost when on, so
        # explicit per-session). When enabled, FailoverSTT/TTS race the top two
        # providers in parallel and take the first to commit — the trail UI
        # shows `hedge` + `hedge_won` events as proof.
        self.hedge_stt: bool = False
        self.hedge_tts: bool = False
        # ADR 070: speculative agent kickoff — start the agent on a stable interim
        # to recover the ~1.5s endpointing wait. ON by default — the pipeline
        # auto-guards on agent.speculatable so non-streaming agents (LyzrAgentTurn
        # SDK) are skipped. Streaming agents (OpenAIChatAgent, LyzrV3StreamAgent)
        # benefit from ~500–1000 ms latency savings. Toggled live via the
        # set_speculative WS event so the demo can A/B it and the dashboard can
        # watch the real commit/cancel ratio (speculation_* events → metrics).
        self.speculative: bool = True
        # ADR 071 D4 / ADR 073 D3 — per-session voice tuning, editable live from
        # the UI so the audience can A/B it. Keyterms default to the curated demo
        # list (merged, de-duped, with the DeepgramSTT constructor list);
        # endpointing_ms None keeps the adapter default (1500 ms). Both passed
        # per-turn to run_voice_pipeline; set via set_keyterms / set_endpointing.
        self.keyterms: list[str] = list(_demo_keyterms())
        self.endpointing_ms: int | None = None
        # ADR 072 / ADR 073 Phase 3 — the two endpointing levers, opt-in + live:
        # * turn_detection: fire speculation on semantic completeness (a
        #   HeuristicTurnDetector), not just the quiet-gap → higher commit rate;
        # * adaptive_endpointing: move the silence-hold within the session from
        #   the commit-ratio (clean ends → shorten; runs-long → lengthen).
        # Plus the ADR 073 cost-guard that auto-disables speculation when its
        # commit-ratio proves too low to repay the cancelled-run cost.
        self.turn_detection: bool = False
        self.adaptive_endpointing: bool = False
        self._turn_detector = HeuristicTurnDetector()
        self._spec_guard = SpeculationGuard()
        self._adaptive = AdaptiveEndpointing(base_ms=1500)
        # Wrap the failover chain in SilenceGatedSTT so we don't pay per-minute
        # to transcribe dead air — and so the silence-trimmed metric shows up in
        # the dashboard. The observer captures both gate + failover events.
        self.stt = SilenceGatedSTT(self._build_stt(), observer=self.observer)
        self.tts_tier = "cartesia"  # default primary
        # Voice selection — empty string means "use the adapter's default
        # voice for this tier" (alloy for OpenAI, the Cartesia adapter's
        # default UUID for Cartesia). Updated via the set_voice_id WS message.
        self.voice_id: str = ""
        # Multi-language support — STT language hint + agent prompt language.
        # Default "" = English (auto-detect). Set via set_language WS event.
        self.language: str = ""
        self.tts = self._build_tts()
        self.turns = 0
        # A4: cost-bounded routing. Per-session budget; once cumulative spent
        # crosses the ceiling the router auto-demotes TTS (cartesia → openai,
        # ~7x cheaper per character) and emits a `budget_demote` event for the
        # UI's failover trail. Default $5.00 — generous enough that a normal
        # demo session keeps the faster Cartesia TTS the whole time (a turn
        # costs ~$0.0003-$0.002, so $5 = thousands of turns), but bounded so
        # a runaway can't drain the account. Set BUDGET_USD=0 for truly
        # unbounded; smaller values (e.g. 0.05) demonstrate the demote behavior.
        try:
            self.budget_usd: float | None = float(os.environ.get("BUDGET_USD", "5.00"))
        except ValueError:
            self.budget_usd = 5.00
        if self.budget_usd is not None and self.budget_usd <= 0:
            self.budget_usd = None  # 0 / negative → unbounded
        self.spent_usd: float = 0.0
        self.budget_demoted: bool = False  # one-shot — don't repeat the event

    def _build_stt(self) -> FailoverSTT:
        """Build the STT chain honoring the current ``hedge_stt`` flag (B2)
        and any BYOK user-supplied keys (each chain member gets its right key).
        """
        return FailoverSTT(
            [
                _KeyedSTT(self._fault_stt, api_key=self.user_keys.get("deepgram")),
                _KeyedSTT(OpenAIWhisperSTT(), api_key=self.user_keys.get("openai")),
            ],
            observer=self.observer,
            call_timeout=15.0,
            connect_timeout=8.0,
            hedge=self.hedge_stt,
        )

    def _build_tts(self) -> FailoverTTS:
        """Build a fresh TTS chain putting the chosen tier first (B2-aware,
        BYOK-aware — each member is wrapped with its specific user key)."""
        cart = _KeyedTTS(self._fault_tts_cartesia, api_key=self.user_keys.get("cartesia"))
        oa = _KeyedTTS(self._fault_tts_openai, api_key=self.user_keys.get("openai"))
        chain = [cart, oa] if self.tts_tier == "cartesia" else [oa, cart]
        return FailoverTTS(chain, observer=self.observer, cache=self.cache, hedge=self.hedge_tts)

    def set_voice_id(self, voice_id: str) -> str:
        """Pick a specific TTS voice for the next turn ("" = adapter default)."""
        self.voice_id = (voice_id or "").strip()
        return self.voice_id

    def set_language(self, language: str) -> dict[str, str]:
        """Set the session language for STT + agent prompt.

        Returns ``{language, voice_id}`` — when switching languages, the
        TTS voice is auto-selected from LANGUAGE_VOICES so the accent
        matches. Pass "" to revert to English/auto-detect.
        """
        self.language = (language or "").strip().lower()
        # Auto-select a matching TTS voice when switching languages.
        voice = LANGUAGE_VOICES.get(self.language, {}).get("cartesia", "")
        if voice:
            self.voice_id = voice
        elif not self.language or self.language == "en":
            # Revert to default English voice.
            self.voice_id = ""
        return {"language": self.language, "voice_id": self.voice_id}

    def set_hedge(self, stt: bool | None = None, tts: bool | None = None) -> dict[str, bool]:
        """B2: toggle latency hedging for STT and/or TTS; rebuild affected chains.

        Returns ``{stt, tts}`` so the WS handler can echo the applied state.
        """
        if stt is not None and stt != self.hedge_stt:
            self.hedge_stt = stt
            self.stt = SilenceGatedSTT(self._build_stt(), observer=self.observer)
        if tts is not None and tts != self.hedge_tts:
            self.hedge_tts = tts
            self.tts = self._build_tts()
        return {"stt": self.hedge_stt, "tts": self.hedge_tts}

    def set_speculative(self, enabled: bool) -> dict[str, bool]:
        """ADR 070: toggle speculative agent kickoff for this session.

        Returns ``{enabled, effective}`` — ``effective`` is whether it will
        actually fire, i.e. ``enabled`` AND the active agent is cancel-safe
        (``speculatable``). The UI shows ``effective=false`` so a viewer knows a
        non-speculatable tier (e.g. the Lyzr SDK agent) silently no-ops.
        """
        self.speculative = bool(enabled)
        effective = self.speculative and bool(getattr(self.agent, "speculatable", False))
        return {"enabled": self.speculative, "effective": effective}

    def set_keyterms(self, raw: object) -> dict[str, list[str]]:
        """ADR 071 D4: replace this session's STT keyterm-boost vocabulary.

        Accepts a list or a comma/newline-separated string. Empty resets to the
        curated demo list. Returns ``{keyterms}`` so the UI can echo the
        effective set. Applied per-turn (no adapter rebuild).
        """
        if isinstance(raw, str):
            terms = [t.strip() for t in raw.replace("\n", ",").split(",")]
        elif isinstance(raw, list):
            terms = [str(t).strip() for t in raw]
        else:
            terms = []
        cleaned = [t for t in terms if t]
        self.keyterms = cleaned or list(_demo_keyterms())
        return {"keyterms": self.keyterms}

    def set_endpointing(self, raw: object) -> dict[str, int | None]:
        """ADR 073 D3: override the STT silence-hold (ms) for this session.

        ``None``/empty resets to the adapter default (1500 ms). Clamped to
        [0, 10000]. Returns ``{endpointing_ms}`` for the UI to echo. Applied
        per-turn (no adapter rebuild) — the audience can A/B the latency floor.
        """
        if raw is None or raw == "":
            self.endpointing_ms = None
        else:
            try:
                self.endpointing_ms = max(0, min(10_000, int(raw)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                self.endpointing_ms = None
        return {"endpointing_ms": self.endpointing_ms}

    def set_turn_detection(self, enabled: bool) -> dict[str, bool]:
        """ADR 072: toggle semantic turn-detection (speculation trigger)."""
        self.turn_detection = bool(enabled)
        return {"enabled": self.turn_detection}

    def set_adaptive_endpointing(self, enabled: bool) -> dict[str, object]:
        """ADR 073 Phase 3: toggle adaptive endpointing. Reseeds the controller
        from the current static hold so the band brackets the right base."""
        self.adaptive_endpointing = bool(enabled)
        if self.adaptive_endpointing:
            self._adaptive = AdaptiveEndpointing(base_ms=self.endpointing_ms or 1500)
        return {"enabled": self.adaptive_endpointing, "endpointing_ms": self._adaptive.current_ms}

    def set_user_keys(self, keys: dict[str, str | None]) -> dict[str, bool]:
        """BYOK — per-session API key overrides. Rebuilds adapter chains.

        ``keys`` is ``{provider: api_key | None | ""}``. Empty string / None
        clears that provider's BYOK key (falls back to server's env key).
        Returns ``{provider: bool}`` indicating which providers have a user
        key set after the update — used by the UI status badge. **Never logs
        the key values** — only which providers got something.
        """
        for p in USER_KEY_PROVIDERS:
            if p in keys:
                v = keys[p]
                self.user_keys[p] = v.strip() if isinstance(v, str) and v.strip() else None
        # Rebuild every adapter whose key might have changed.
        self.stt = SilenceGatedSTT(self._build_stt(), observer=self.observer)
        self.tts = self._build_tts()
        # Rebuild agent so it picks up new openai/lyzr keys.
        self.agent = _build_agent(
            self.agent_tier,
            lyzr_agent_id=self.lyzr_agent_id,
            on_tool_call=self._on_tool_call,
            on_extras=self._on_extras,
            openai_api_key=self.user_keys.get("openai"),
            lyzr_api_key=self.user_keys.get("lyzr"),
        )
        which = {p: bool(self.user_keys.get(p)) for p in USER_KEY_PROVIDERS}
        active = [p for p, on in which.items() if on]
        log.info("byok: user keys active for: %s", ", ".join(active) or "(none)")
        return which

    def set_tts_tier(self, tier: str) -> str:
        """Swap the primary TTS for the next turn; returns the tier in effect."""
        if tier in TTS_TIERS and tier != self.tts_tier:
            self.tts_tier = tier
            self.tts = self._build_tts()
        return self.tts_tier

    def set_budget(self, usd: float | None) -> float | None:
        """Update the per-session budget ceiling (A4); None / 0 = unbounded."""
        self.budget_usd = usd if (usd is not None and usd > 0) else None
        # Reset demoted flag so a higher budget can re-arm a future demotion.
        self.budget_demoted = False
        return self.budget_usd

    def record_turn_cost(self, cost_usd: float) -> bool:
        """Accumulate one turn's cost; return True iff this turn tripped a demote.

        Demotion policy (A4 D4): if the per-session budget is set AND cumulative
        spent crosses it AND we're still on the expensive primary (cartesia),
        flip to the cheaper tier (openai TTS, ~7x lower $/char) for subsequent
        turns. Single-shot — the UI gets exactly one `budget_demote` event per
        session crossing.
        """
        self.spent_usd += max(0.0, cost_usd)
        if (
            self.budget_usd is not None
            and self.spent_usd > self.budget_usd
            and self.tts_tier == "cartesia"
            and not self.budget_demoted
        ):
            self.tts_tier = "openai"
            self.tts = self._build_tts()
            self.budget_demoted = True
            return True
        return False

    def set_agent_tier(self, tier: str) -> str:
        """Swap the agent backend (OpenAI Chat ⇄ Lyzr ADK) for the next turn."""
        if tier in AGENT_TIERS and tier != self.agent_tier:
            self.agent_tier = tier
            self.agent = _build_agent(
                tier,
                lyzr_agent_id=self.lyzr_agent_id,
                on_tool_call=self._on_tool_call,
                on_extras=self._on_extras,
                openai_api_key=self.user_keys.get("openai"),
                lyzr_api_key=self.user_keys.get("lyzr"),
            )
        return self.agent_tier

    def set_lyzr_agent_id(self, agent_id: str) -> str:
        """Switch which Lyzr agent serves the next turn (UI input field).

        Validated only loosely (24-char hex is the Lyzr convention, but other
        ids may exist). Rebuilds the agent immediately if we're on the Lyzr
        tier so the next turn talks to the new agent.
        """
        agent_id = agent_id.strip()
        if agent_id and agent_id != self.lyzr_agent_id:
            self.lyzr_agent_id = agent_id
            if self.agent_tier == "lyzr":
                self.agent = _build_agent(
                    "lyzr",
                    lyzr_agent_id=agent_id,
                    on_tool_call=self._on_tool_call,
                    on_extras=self._on_extras,
                    openai_api_key=self.user_keys.get("openai"),
                    lyzr_api_key=self.user_keys.get("lyzr"),
                )
        return self.lyzr_agent_id

    def inject_fault(self, stage: str) -> str:
        """Arm a one-shot failure on the named stage. Returns what got armed."""
        if stage == "stt":
            self._fault_stt.arm()
            return "stt:primary"
        if stage == "tts":
            if self.tts_tier == "cartesia":
                self._fault_tts_cartesia.arm()
            else:
                self._fault_tts_openai.arm()
            return f"tts:{self.tts_tier}"
        return ""


# ── WS protocol ───────────────────────────────────────────────────────────────


# VAD endpointing constants — at 16kHz PCM16 mono, ~4096 samples per browser
# ScriptProcessorNode buffer = ~256ms. We endpoint after `silence_ms` of quiet
# audio, provided we've seen at least `min_speech_ms` of speech first.
_SILENCE_RMS = 350.0  # speech-conservative threshold (caller voice usually >800)
_MIN_SPEECH_MS = 400
# 1500 ms — matches the DeepgramSTT default endpointing_ms (the two VADs
# agree). We chased this value up to 4000 ms when testers reported turns
# ending mid-sentence — but with the underlying is_final-vs-speech_final
# bug fixed (Deepgram no longer ends the turn on a mid-stream commit),
# 1500 ms is plenty: it tolerates natural inter-sentence pauses without
# adding 2-3 sec of wait on every turn. Raise to 2500+ for deliberate
# speakers or IVR; drop to 800 for snappy chatbot call-and-response.
_SILENCE_END_MS = 1500
# Auto barge-in: cancel agent/TTS after this much detected speech during the
# answer. Bumped to 450ms sustained (was 180) so a brief speaker-bleed burst
# doesn't trip it — only deliberate interruption does.
_BARGE_IN_SPEECH_MS = 450
# A *separate*, much higher RMS threshold for barge-in detection than for
# end-of-turn VAD. Speaker bleed (the caller's own playback re-entering the
# mic) routinely crosses _SILENCE_RMS=350 but rarely 1500 unless the caller
# really speaks up. Keeping these decoupled is the principled fix:
# endpointing wants sensitivity, barge-in wants insensitivity.
_BARGE_IN_RMS = 1500.0


async def _audio_from_ws(
    ws: WebSocket,
    end: asyncio.Event,
    *,
    vad: bool = True,
    sample_rate: int = 16_000,
    on_endpoint: Callable[[], None] | None = None,
    recorder: CallRecorder | None = None,
) -> AsyncIterator[AudioChunk]:
    """Yield browser audio frames until the utterance ends.

    Two ways to end:
    * **explicit stop** — JSON ``{"event": "stop"}`` (push-to-talk path);
    * **server-side VAD** — when ``vad`` is on (default), end after ~900ms of
      silence following at least ~400ms of speech (talk-normally path).

    The talk-normally path is the real-voice-agent feel — caller doesn't need
    to hold a button. ``on_endpoint`` (if given) fires once when VAD ends the
    turn so the transport can tell the browser to stop sending frames.
    """
    speech_ms = 0.0
    silence_ms = 0.0
    started_speaking = False
    first_audio_logged = False
    total_audio_bytes = 0
    while not end.is_set():
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
        except TimeoutError:
            continue
        if msg.get("type") == "websocket.disconnect":
            end.set()
            return
        if "bytes" in msg and msg["bytes"]:
            chunk = AudioChunk(data=msg["bytes"], codec="pcm16", sample_rate=sample_rate)
            if recorder is not None:
                # B4: tap raw mic frames into the call recorder. The frames
                # are already PCM16 at the browser's reported rate; the WAV
                # stereo mix expects 16 kHz, so any non-16k session would
                # need resampling here — for now the browser is constrained
                # to 16 k by the demo's AudioContext settings.
                recorder.add_caller(chunk.data)
            total_audio_bytes += len(chunk.data)
            if not first_audio_logged:
                # First frame: log size + the actual rate we'll declare to STT.
                # Helps diagnose "transcript empty" issues — if the rate here
                # doesn't match the browser's real capture rate, STT is garbled.
                import struct  # noqa: PLC0415

                count = len(chunk.data) // 2
                if count > 0:
                    samples = struct.unpack(f"<{count}h", chunk.data[: count * 2])
                    peak = max(abs(s) for s in samples)
                    log.info(
                        "first audio frame: %dB @%dHz, peak=%d/32768 (%.1f%% full-scale)",
                        len(chunk.data),
                        sample_rate,
                        peak,
                        100 * peak / 32768,
                    )
                first_audio_logged = True
            yield chunk
            if not vad:
                continue
            # Frame ms = (bytes / 2 bytes per sample) / (sample_rate / 1000)
            frame_ms = (len(chunk.data) / 2) / (sample_rate / 1000.0)
            if is_silent(chunk, _SILENCE_RMS):
                if started_speaking:
                    silence_ms += frame_ms
                    if silence_ms >= _SILENCE_END_MS:
                        log.info(
                            "VAD endpoint: %dB ≈ %.2fs @%dHz",
                            total_audio_bytes,
                            (total_audio_bytes / 2) / sample_rate if sample_rate else 0,
                            sample_rate,
                        )
                        if on_endpoint:
                            on_endpoint()
                        return
            else:
                started_speaking = True
                speech_ms += frame_ms
                silence_ms = 0
            # Floor: keep listening until min speech is heard (avoids endpointing
            # on a hot-mic burst).
            if started_speaking and speech_ms < _MIN_SPEECH_MS:
                silence_ms = 0
            continue
        text = msg.get("text")
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if data.get("event") == "stop":
            log.info(
                "audio captured this turn: %dB ≈ %.2fs @%dHz",
                total_audio_bytes,
                (total_audio_bytes / 2) / sample_rate if sample_rate else 0,
                sample_rate,
            )
            return


async def _send_event(ws: WebSocket, **fields: object) -> None:
    with __import__("contextlib").suppress(Exception):
        await ws.send_text(json.dumps(fields))


async def _send_audio(ws: WebSocket, chunk: AudioChunk) -> None:
    # Header announces the audio chunk's shape, then the raw bytes — keeps the
    # binary frame self-describing on the browser side.
    await _send_event(
        ws,
        event="tts.audio",
        bytes=len(chunk.data),
        codec=chunk.codec,
        sample_rate=chunk.sample_rate,
    )
    with __import__("contextlib").suppress(Exception):
        await ws.send_bytes(chunk.data)


# ── FastAPI app ───────────────────────────────────────────────────────────────


app = FastAPI(title="mdk-voice browser demo")

# Serve /static/* from examples/web_demo/static — Movate logos + any other
# brand/marketing assets the demo references. Mounted at /static instead of
# / so the SPA's catch-all routes (none today, but future-friendly) can't
# accidentally shadow asset paths.
_STATIC_DIR = DEMO_DIR / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(DEMO_DIR / "index.html")


@app.get("/livekit")
async def livekit_page() -> FileResponse:
    """Standalone LiveKit browser demo — WebRTC voice to the mdk agent."""
    return FileResponse(str(Path(__file__).parent / "static" / "livekit.html"))


# Service-wide telemetry the /health endpoint exposes (C3). Bumped from
# `_active_sessions` whenever a /ws/voice or /ws/voice/realtime handler enters
# and decremented in its finally. Lets ops see "is anyone using the demo right
# now?" without scraping logs.
_active_sessions: int = 0
_started_at_monotonic: float = 0.0  # set in main(); 0 means "not yet started"

# Phone-call concurrency. `_active_phone_calls` is bumped on WS accept and
# decremented in the finally of the /ws/twilio handler. The TwiML endpoint
# checks it against `MAX_CONCURRENT_CALLS` and returns a polite <Say> rejection
# when full — callers hear "all agents are busy" instead of degrading active
# calls. Configurable via env so ops can tune without a redeploy.
MAX_CONCURRENT_CALLS: int = int(os.environ.get("MAX_CONCURRENT_CALLS", "3"))
_active_phone_calls: int = 0


def _phone_call_entered() -> None:
    """Bump the active-phone-call count."""
    globals()["_active_phone_calls"] = globals()["_active_phone_calls"] + 1


def _phone_call_exited() -> None:
    """Decrement the active-phone-call count, floor at zero."""
    globals()["_active_phone_calls"] = max(0, globals()["_active_phone_calls"] - 1)


def _session_entered() -> None:
    """Bump the live-session count. Module-level helper to avoid the multiple-
    ``global`` SyntaxError that bites when both a ``finally:`` block and the
    function body want to touch the same module var (a single function can have
    at most one ``global X`` declaration, but several handlers want the bump)."""
    globals()["_active_sessions"] = globals()["_active_sessions"] + 1


def _session_exited() -> None:
    """Decrement the live-session count, floor at zero (so a double-decrement
    from an exception path can't go negative)."""
    globals()["_active_sessions"] = max(0, globals()["_active_sessions"] - 1)


import time as _time  # noqa: E402, PLC0415 - module-level monotonic for uptime

_started_at_wall_iso: str = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def _adapter_status() -> dict[str, dict[str, bool]]:
    """Per-adapter readiness map — key present + SDK importable.

    Honest: an adapter is "ready" only if BOTH its API key is set AND its SDK
    is importable (extras installed). The demo's container image installs the
    [openai,deepgram,cartesia,elevenlabs] extras, so SDKs are present; the
    field still flags any future missing-extra footgun explicitly.
    """
    keys = load_keys()  # idempotent — also seeds env from ~/.mdk_*_key files
    out: dict[str, dict[str, bool]] = {}

    def _importable(modname: str) -> bool:
        try:
            __import__(modname)
            return True
        except ImportError:
            return False

    out["openai_chat"] = {"key": bool(keys.get("openai")), "sdk": _importable("openai")}
    out["openai_whisper"] = out["openai_chat"]
    out["openai_tts"] = out["openai_chat"]
    out["openai_realtime"] = out["openai_chat"]
    out["deepgram_stt"] = {"key": bool(keys.get("deepgram")), "sdk": _importable("deepgram")}
    out["deepgram_aura_tts"] = out["deepgram_stt"]
    out["cartesia_stt"] = {"key": bool(keys.get("cartesia")), "sdk": _importable("cartesia")}
    out["cartesia_tts"] = out["cartesia_stt"]
    out["elevenlabs_tts"] = {
        "key": bool(os.environ.get("ELEVENLABS_API_KEY")),
        "sdk": _importable("elevenlabs"),
    }
    out["lyzr"] = {
        "key": bool(keys.get("lyzr")),
        "sdk": True,  # duck-typed binding — no SDK import
        "agent_id": bool(_lyzr_agent_id()),
    }
    # Annotate readiness as the AND of key+sdk (for the convenience of dashboards).
    for status in out.values():
        status["ready"] = bool(status.get("key") and status.get("sdk", True))
    return out


@app.get("/health")
async def health() -> dict[str, object]:
    """Service health + adapter readiness map + endpoint catalog (C3).

    Returns enough for a monitoring system to alert on:
    * ``ok``: overall liveness (true if at least one STT/TTS pair is ready).
    * ``active_sessions``: how many browsers/phones are connected right now.
    * ``adapters``: per-adapter ``{key, sdk, ready, ...}`` so a missing extra
      or rotated key is visible from the URL, not just the logs.
    * ``endpoints``: catalog of the WS/REST paths for service discovery.
    * ``uptime_s`` + ``started_at``: when the container booted (handy for
      "did my rolling update actually replace the replica?").
    """
    adapters = _adapter_status()
    # Liveness: have at least one workable STT AND TTS combo.
    has_stt = any(adapters.get(k, {}).get("ready") for k in ("deepgram_stt", "openai_whisper"))
    has_tts = any(
        adapters.get(k, {}).get("ready")
        for k in ("cartesia_tts", "openai_tts", "deepgram_aura_tts")
    )
    return {
        "ok": has_stt and has_tts,
        "service": "mdk-voice-demo",
        "version": getattr(
            __import__("movate.voice", fromlist=["__version__"]), "__version__", "unknown"
        ),
        "uptime_s": (
            round(_time.monotonic() - _started_at_monotonic, 1) if _started_at_monotonic else None
        ),
        "started_at": _started_at_wall_iso,
        "active_sessions": _active_sessions,
        "active_phone_calls": _active_phone_calls,
        "max_phone_calls": MAX_CONCURRENT_CALLS,
        "adapters": adapters,
        "livekit": {
            "configured": _livekit_config() is not None,
            "url": (_livekit_config() or {}).get("url", ""),
        },
        "endpoints": {
            "browser_ws": "/ws/voice",
            "realtime_ws": "/ws/voice/realtime",
            "twilio_ws": "/ws/twilio",
            "twilio_twiml": "/twiml/voice",
            "livekit_token": "/livekit/token",
            "livekit_status": "/livekit/status",
            "parity": "/parity",
        },
        "tip": "drop missing keys at ~/.mdk_<name>_key (chmod 600) or set them via env",
    }


# Parity-report cache. Lyzr's discovery menu rarely changes, so we cache a
# successful report for `_PARITY_TTL_S` seconds to avoid hammering the endpoint
# on every browser load. The cache key is the LYZR_API_KEY (so a different
# tenant gets its own slot if we ever want one).
_PARITY_TTL_S = 300.0
_parity_cache: dict[str, tuple[float, dict[str, object]]] = {}

# Per-session recording URLs, populated on WS disconnect once the background
# upload finishes. Keyed by ``Session.session_id``. Bounded in practice by
# the demo's lifetime; for a long-running deploy you'd want an LRU here.
_recordings: dict[str, str] = {}
# Blob container — overridable for tenant isolation across customer demos.
_BLOB_CONTAINER = os.environ.get("BLOB_CONTAINER", "mdk-voice-recordings").strip()

# Lyzr agent-list cache for the agent-picker dropdown (L1). Cached per
# api-key for _LYZR_AGENTS_TTL_S — the user's BYOK Lyzr key and the demo's
# default key get separate cache slots so flipping BYOK on doesn't return
# stale results.
_LYZR_AGENTS_TTL_S = 60.0
_lyzr_agents_cache: dict[str, tuple[float, dict[str, object]]] = {}


def _resolve_lyzr_key(request: Request) -> str:
    """BYOK precedence: ``X-Lyzr-Api-Key`` header wins over the env key.

    The header arrives from the UI when a user has pasted their own Lyzr
    key in the BYOK panel (it's the same key the WS session uses). Keeps
    the agent-picker dropdown and validation endpoint showing THEIR agents
    instead of the demo's.
    """
    header_key = (request.headers.get("x-lyzr-api-key") or "").strip()
    return header_key or os.environ.get("LYZR_API_KEY", "").strip()


@app.get("/lyzr/agents")
async def lyzr_agents(request: Request) -> dict[str, object]:
    """List the caller's Lyzr agents — drives the agent-picker dropdown (L1).

    Calls Lyzr's ``GET /v3/agents/`` and returns a slim, UI-friendly shape:
    ``{ok, agents: [{id, name, description, role, instructions_snippet,
    tools, features}], count}``. Cached briefly to avoid hammering Lyzr's API
    on every page load.

    Honors BYOK — if the caller sets ``X-Lyzr-Api-Key`` (the UI does this
    when the user has pasted their own key), THAT key's agents are returned
    instead of the demo's. Cache is keyed on the api_key value so different
    callers get isolated slots. Falls back gracefully when no key is set or
    Lyzr is unreachable — returns ``ok=False`` with a hint, never 500s.
    """
    import time as _t  # noqa: PLC0415

    key = _resolve_lyzr_key(request)
    if not key:
        return {
            "ok": False,
            "error": "LYZR_API_KEY not set",
            "tip": "Paste a Lyzr API key in the 🔑 panel, or set LYZR_API_KEY env",
        }
    now = _t.monotonic()
    cached = _lyzr_agents_cache.get(key)
    if cached and now - cached[0] < _LYZR_AGENTS_TTL_S:
        return cached[1]
    try:
        import httpx  # noqa: PLC0415

        r = httpx.get(
            "https://agent-prod.studio.lyzr.ai/v3/agents/",
            headers={"x-api-key": key},
            timeout=5.0,
        )
        r.raise_for_status()
        raw = r.json() or []
    except Exception as exc:  # noqa: BLE001 - network failure shouldn't 500 the demo
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _agent_summary(a: dict[str, object]) -> dict[str, object]:
        tools = a.get("tools") or []
        tool_names = [t.get("name") for t in tools if isinstance(t, dict)]
        instructions = str(a.get("agent_instructions") or "")
        return {
            "id": a.get("_id"),
            "name": a.get("name") or "(unnamed)",
            "description": (a.get("description") or "")[:200],
            "role": a.get("agent_role") or "",
            "instructions_snippet": instructions[:280] + ("…" if len(instructions) > 280 else ""),
            "tools": tool_names,
            "features": a.get("features") or [],
            "created_at": a.get("created_at"),
        }

    agents = sorted(
        (_agent_summary(a) for a in raw if isinstance(a, dict) and a.get("_id")),
        key=lambda x: str(x.get("name") or "").lower(),
    )
    payload: dict[str, object] = {"ok": True, "agents": agents, "count": len(agents)}
    _lyzr_agents_cache[key] = (now, payload)
    return payload


@app.get("/lyzr/agents/{agent_id}")
async def lyzr_agent_detail(agent_id: str, request: Request) -> dict[str, object]:
    """Validate one agent_id and return its full metadata (L5 + L2).

    Tries to find ``agent_id`` in the cached list first (one fewer Lyzr API
    call); on cache miss, hits ``/v3/agents/{id}`` directly. Returns
    ``{ok: True, agent: {...}}`` on hit, ``{ok: False, error}`` on miss or
    network failure. Honors BYOK via ``X-Lyzr-Api-Key`` header — same as the
    list endpoint, so a user's agent_id resolves against THEIR account.
    """
    key = _resolve_lyzr_key(request)
    if not key:
        return {"ok": False, "error": "LYZR_API_KEY not set"}
    # Cache hit fast path — but only if it's THIS user's cache entry.
    cached = _lyzr_agents_cache.get(key)
    if cached:
        for a in cached[1].get("agents", []):  # type: ignore[union-attr]
            if a.get("id") == agent_id:  # type: ignore[union-attr]
                return {"ok": True, "agent": a, "source": "cache"}
    # Direct fetch.
    try:
        import httpx  # noqa: PLC0415

        r = httpx.get(
            f"https://agent-prod.studio.lyzr.ai/v3/agents/{agent_id}",
            headers={"x-api-key": key},
            timeout=5.0,
        )
        if r.status_code == 404:
            return {"ok": False, "error": "agent_id not found", "agent_id": agent_id}
        r.raise_for_status()
        raw = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    tools = raw.get("tools") or []
    instructions = str(raw.get("agent_instructions") or "")
    return {
        "ok": True,
        "source": "live",
        "agent": {
            "id": raw.get("_id"),
            "name": raw.get("name") or "(unnamed)",
            "description": (raw.get("description") or "")[:400],
            "role": raw.get("agent_role") or "",
            "instructions_snippet": instructions[:600] + ("…" if len(instructions) > 600 else ""),
            "tools": [t.get("name") for t in tools if isinstance(t, dict)],
            "features": raw.get("features") or [],
            "created_at": raw.get("created_at"),
        },
    }


@app.get("/tts/voices")
async def tts_voices() -> dict[str, object]:
    """Curated voice catalog per provider for the UI's voice picker dropdown.

    OpenAI's six voices are documented + stable, so we hardcode them.
    Cartesia's catalog is 700+ entries and IDs/names change over time, so
    we fetch live from Cartesia's ``/voices`` API (cached 1 hour per key),
    score for business-friendliness, and pick a balanced 16-voice subset.
    Falls back to the (single) hardcoded entry only if the API is
    unreachable AND the cache is cold.
    """
    out: dict[str, list[dict[str, str]]] = {"openai": TTS_VOICES["openai"]}
    cart_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    cart_voices = await _fetch_cartesia_voices_cached(cart_key) if cart_key else []
    out["cartesia"] = cart_voices or TTS_VOICES["cartesia"]
    return {"ok": True, "voices": out, "cartesia_live": bool(cart_voices)}


@app.get("/languages")
async def languages() -> dict[str, object]:
    """Supported languages for multi-language voice."""
    return {
        "ok": True,
        "languages": SUPPORTED_LANGUAGES,
        "voices": LANGUAGE_VOICES,
    }


# ── LiveKit endpoints ────────────────────────────────────────────────────

@app.get("/livekit/status")
async def livekit_status() -> dict[str, object]:
    """Check if LiveKit is configured and reachable."""
    cfg = _livekit_config()
    return {
        "ok": cfg is not None,
        "configured": cfg is not None,
        "url": cfg["url"] if cfg else None,
    }


@app.post("/livekit/token")
async def livekit_token(request: Request) -> dict[str, object]:
    """Generate a LiveKit access token for a browser participant.

    The browser calls this to get a token, then connects directly to the
    LiveKit room via WebRTC. The mdk agent joins the same room via the
    LiveKitTransport and runs the voice pipeline.

    Body: {"room_name": "call-123", "participant_name": "browser-user"}
    """
    cfg = _livekit_config()
    if not cfg:
        return {"ok": False, "error": "LiveKit not configured"}

    try:
        from livekit.api import AccessToken, VideoGrants  # noqa: PLC0415
    except ImportError:
        return {"ok": False, "error": "livekit SDK not installed"}

    body = await request.json()
    room_name = body.get("room_name", f"mdk-voice-{__import__('time').monotonic_ns()}")
    participant_name = body.get("participant_name", "browser-user")

    token = AccessToken(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
    )
    token.identity = participant_name
    token.name = participant_name
    grant = VideoGrants(
        room_create=True,
        room_join=True,
        room=room_name,
    )
    token.with_grants(grant)

    jwt_token = token.to_jwt()

    log.info(
        "livekit: issued token for room=%s participant=%s",
        room_name,
        participant_name,
    )

    return {
        "ok": True,
        "token": jwt_token,
        "url": cfg["url"],
        "room_name": room_name,
    }


@app.get("/parity")
async def parity() -> dict[str, object]:
    """Live Lyzr provider-parity report — "we cover N/M of your voice menu".

    Demo proof: hits Lyzr's two discovery endpoints
    (``/v1/config/{pipeline,realtime}-options``) and maps each entry against the
    mdk-voice adapter table (:data:`movate.voice.LYZR_PROVIDER_MAP`). Returns the
    structured ``{covered, gaps, coverage_pct}`` so the UI can render a badge
    and an expandable gap list. Falls back gracefully when ``LYZR_API_KEY`` is
    not set — the offline case returns a clear ``ok=False`` with a hint, not a
    500 (the demo still works without Lyzr connectivity).
    """
    import time  # noqa: PLC0415 - lazy

    # ``load_keys()`` (at startup) populates LYZR_API_KEY from ~/.mdk_lyzr_key
    # if present; container deploys set it via secret env var. Either way: env.
    key = os.environ.get("LYZR_API_KEY", "").strip()
    if not key:
        return {
            "ok": False,
            "error": "LYZR_API_KEY not set",
            "tip": "Run with LYZR_API_KEY=$(cat ~/.mdk_lyzr_key) to enable the live parity check.",
        }
    now = time.monotonic()
    cached = _parity_cache.get(key)
    if cached and now - cached[0] < _PARITY_TTL_S:
        return cached[1]
    try:
        report = await check_lyzr_parity(api_key=key)
    except Exception as exc:  # noqa: BLE001 - network failure shouldn't 500 the demo
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    payload: dict[str, object] = {
        "ok": True,
        "coverage_pct": round(report.coverage_pct, 1),
        "covered_count": len(report.covered),
        "gap_count": len(report.gaps),
        "total": report.total,
        "is_parity": report.is_parity,
        "covered": [
            {
                "kind": p.kind,
                "provider_id": p.provider_id,
                "display_name": p.display_name,
                "model_count": p.model_count,
            }
            for p in report.covered
        ],
        "gaps": [
            {
                "kind": p.kind,
                "provider_id": p.provider_id,
                "display_name": p.display_name,
                "model_count": p.model_count,
            }
            for p in report.gaps
        ],
    }
    _parity_cache[key] = (now, payload)
    return payload


@app.get("/recordings/{session_id}")
async def recording_url(session_id: str) -> dict[str, object]:
    """Return the SAS download URL for a finished session's recording.

    The browser receives the URL via the WS ``recording_ready`` event when
    its own session ends; this endpoint exists for the operator (or a CLI
    test) who wants to look up a recording by id after the fact.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    url = _recordings.get(session_id)
    if not url:
        raise HTTPException(status_code=404, detail="no recording for session")
    return {"session_id": session_id, "url": url}


# ── Twilio telephony bridge ──────────────────────────────────────────────────
# Live phone calls into the same pipeline the browser demo uses. Twilio's WS
# protocol differs (μ-law 8kHz, base64, JSON envelope) but everything past the
# transport — STT, agent, TTS, failover, PII, metrics, cost — is identical.


def _twilio_creds() -> tuple[str, str, str] | None:
    """Load Twilio (SID, auth_token, phone_number).

    Resolution order:
    1. ``~/.mdk_twilio_{sid,token,number}`` files (laptop dev).
    2. ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` / ``TWILIO_NUMBER`` env
       vars (Azure Container Apps injects these as container secrets).
    """
    home = Path.home()
    try:
        return (
            (home / ".mdk_twilio_sid").read_text().strip(),
            (home / ".mdk_twilio_token").read_text().strip(),
            (home / ".mdk_twilio_number").read_text().strip(),
        )
    except OSError:
        pass
    # Fallback: env vars (ACA container secrets path).
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    number = os.environ.get("TWILIO_NUMBER", "").strip()
    if sid and token and number:
        return (sid, token, number)
    return None


# ── LiveKit credentials ──────────────────────────────────────────────────
# Loaded from env vars (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
# or ~/.mdk_livekit_* files (same pattern as Twilio).

def _livekit_config() -> dict[str, str] | None:
    """Load LiveKit credentials. Returns {url, api_key, api_secret} or None."""
    home = Path.home()
    try:
        return {
            "url": (home / ".mdk_livekit_url").read_text().strip(),
            "api_key": (home / ".mdk_livekit_key").read_text().strip(),
            "api_secret": (home / ".mdk_livekit_secret").read_text().strip(),
        }
    except OSError:
        pass
    url = os.environ.get("LIVEKIT_URL", "").strip().strip('"').strip("'")
    key = os.environ.get("LIVEKIT_API_KEY", "").strip().strip('"').strip("'")
    secret = os.environ.get("LIVEKIT_API_SECRET", "").strip().strip('"').strip("'")
    if url and key and secret:
        return {"url": url, "api_key": key, "api_secret": secret}
    return None


def _twilio_number() -> str:
    """Just the public phone number — what callers dial to reach the agent.

    Env var ``TWILIO_NUMBER`` wins (the way the Azure deploy injects it as a
    container secret); falls back to ``~/.mdk_twilio_number`` for local runs.
    Returns ``""`` when neither is configured (UI hides the dial chip then).
    """
    env = os.environ.get("TWILIO_NUMBER", "").strip()
    if env:
        return env
    try:
        return (Path.home() / ".mdk_twilio_number").read_text().strip()
    except OSError:
        return ""


@app.get("/twilio/number")
async def twilio_number() -> dict[str, object]:
    """Public phone number for the agent — empty string if not configured."""
    return {"ok": True, "number": _twilio_number()}


def _ngrok_url() -> str | None:
    """Best-effort: ask the local ngrok agent for its current public HTTPS URL.

    Free-tier ngrok URLs change on restart; this saves the operator from
    pasting them around. Fall back to the ``NGROK_URL`` env var.
    """
    if env := os.environ.get("NGROK_URL"):
        return env.rstrip("/")
    try:
        import httpx  # noqa: PLC0415

        r = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
        if r.status_code == 200:
            for t in r.json().get("tunnels", []):
                if t.get("public_url", "").startswith("https://"):
                    return t["public_url"].rstrip("/")
    except Exception:  # noqa: BLE001 - ngrok may not be running
        return None
    return None


def _public_base_url(request: Any) -> str | None:
    """Resolve the public HTTPS base URL the caller should hand to Twilio.

    Resolution order:
    1. ``NGROK_URL`` env var (explicit override — laptop dev with custom tunnel).
    2. Live local ngrok agent (laptop dev with default agent running).
    3. The request's own scheme+host — works when the server itself is
       directly internet-reachable (e.g. Azure Container Apps, Render, Fly).
       This is the *normal* production path; ngrok is only the laptop case.

    Returns ``None`` if all three fail (e.g. fully offline test).
    """
    if (ng := _ngrok_url()) is not None:
        return ng
    # FastAPI's Request exposes the public URL Twilio just dialed; reuse it so
    # the deployment never has to hard-code its own hostname.
    base = getattr(getattr(request, "url", None), "scheme", "") and str(request.url)
    if base:
        # Strip path + query — we only want scheme://host[:port].
        from urllib.parse import urlparse  # noqa: PLC0415

        u = urlparse(str(request.url))
        if u.scheme and u.netloc:
            # Honor X-Forwarded-Proto: behind a TLS-terminating ingress (ACA,
            # CloudFront, nginx with proxy_pass) the request the app sees is
            # plain http even though the public URL is https. Twilio requires
            # wss://, not ws://, so we MUST emit the original-edge scheme.
            fwd_proto = ""
            if hasattr(request, "headers"):
                fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
            scheme = fwd_proto or u.scheme
            host = (
                request.headers.get("x-forwarded-host") if hasattr(request, "headers") else None
            ) or u.netloc
            return f"{scheme}://{host}"
    return None


@app.get("/twiml/voice")
@app.post("/twiml/voice")
async def twiml_voice(request: Request) -> Any:
    """Return a TwiML <Connect><Stream/> that points Twilio at our /ws/twilio.

    Admission gate: if ``_active_phone_calls >= MAX_CONCURRENT_CALLS``, return
    a polite TwiML ``<Say>`` rejection instead of handing off the stream. The
    caller hears "all agents are busy" and hangs up — active calls are not
    degraded. The browser event stream gets a ``call_rejected`` notification.
    """
    from fastapi import Response  # noqa: PLC0415
    from twilio_bridge import twiml_for_stream  # noqa: PLC0415

    public = _public_base_url(request)
    if not public:
        return Response(
            status_code=503,
            media_type="application/xml",
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response><Say>No public URL detected — set NGROK_URL "
                "or deploy behind a public ingress.</Say></Response>"
            ),
        )

    # Admission gate — reject before opening the WS so active calls keep
    # their full CPU/bandwidth budget.
    if _active_phone_calls >= MAX_CONCURRENT_CALLS:
        log.warning(
            "twilio: rejecting call — at capacity (%d/%d)",
            _active_phone_calls,
            MAX_CONCURRENT_CALLS,
        )
        # Best-effort broadcast to browsers watching the event stream.
        asyncio.ensure_future(
            _broadcast_twilio(
                kind="call_rejected",
                reason="at_capacity",
                active=_active_phone_calls,
                max=MAX_CONCURRENT_CALLS,
            )
        )
        return Response(
            media_type="application/xml",
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response><Say voice=\"Polly.Joanna\">"
                "All agents are currently busy. Please try again shortly."
                "</Say></Response>"
            ),
        )

    wss = public.replace("https://", "wss://").replace("http://", "ws://") + "/ws/twilio"
    return Response(media_type="application/xml", content=twiml_for_stream(wss))


@app.get("/twilio/setup")
async def twilio_setup(request: Request) -> dict[str, object]:
    """One-shot: point the Twilio number's Voice URL at our /twiml/voice.

    Open in a browser after starting the server (and ngrok, or after deploying
    behind a public ingress) to wire the call path. Returns the current Voice
    URL so you can verify. Idempotent.
    """
    import httpx  # noqa: PLC0415

    creds = _twilio_creds()
    if not creds:
        return {"ok": False, "error": "missing ~/.mdk_twilio_{sid,token,number}"}
    sid, token, number = creds
    public = _public_base_url(request)
    if not public:
        return {"ok": False, "error": "no public URL (set NGROK_URL or run ngrok)"}
    voice_url = f"{public}/twiml/voice"

    # Look up the IncomingPhoneNumber sid for the number.
    list_r = httpx.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
        auth=(sid, token),
        timeout=10,
    )
    if list_r.status_code != 200:
        return {"ok": False, "error": f"twilio list {list_r.status_code}: {list_r.text[:200]}"}
    pn = next(
        (n for n in list_r.json().get("incoming_phone_numbers", []) if n["phone_number"] == number),
        None,
    )
    if pn is None:
        return {"ok": False, "error": f"number {number} not found on account"}

    # Update the Voice URL + method.
    upd = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/{pn['sid']}.json",
        auth=(sid, token),
        data={"VoiceUrl": voice_url, "VoiceMethod": "POST"},
        timeout=10,
    )
    if upd.status_code != 200:
        return {"ok": False, "error": f"twilio update {upd.status_code}: {upd.text[:200]}"}
    return {
        "ok": True,
        "number": number,
        "voice_url": upd.json().get("voice_url"),
        "ngrok": public,
        "tip": f"now call {number}",
    }


# Live Twilio → browser mirror. Browser /ws/voice connections subscribe by
# adding themselves to _twilio_observers; the /ws/twilio handler broadcasts
# call events to all of them. Lets the Detailed-view event stream show
# whatever's happening on a phone call in real time (useful for a demo where
# you want to dial in and have the audience watch the transcript flow).
_twilio_observers: set[WebSocket] = set()


async def _broadcast_twilio(**fields: object) -> None:
    """Send a ``twilio_event`` JSON message to every subscribed browser.

    Quietly drops observers whose send fails (closed sockets, etc.) so a
    stale subscription can't poison the broadcast. Safe to call from any
    async context.
    """
    if not _twilio_observers:
        return
    payload = dict(fields)
    payload["event"] = "twilio_event"
    dead: list[WebSocket] = []
    for obs in list(_twilio_observers):
        try:
            await obs.send_text(json.dumps(payload))
        except Exception:  # noqa: BLE001 - prune on any send error
            dead.append(obs)
    for d in dead:
        _twilio_observers.discard(d)


# Playground → phone config bridge. The browser demo can push its current
# settings here so the NEXT inbound Twilio call uses the same agent /
# voice / hedge / budget. Per-call, not per-call_sid (a resumed call keeps
# its prior session's config via B5). Mutated by `set_twilio_config` WS
# events and read once per /ws/twilio handler entry.
_twilio_config: dict[str, Any] = {
    "agent_tier": "openai",
    "lyzr_agent_id": "",
    "tts_tier": "cartesia",
    "voice_id": "",
    "hedge_stt": False,
    "hedge_tts": False,
    "speculative": True,
    "budget_usd": None,
    "lyzr_api_key": None,  # BYOK key from the browser session (if any)
    "updated_by": None,  # browser session_id that last pushed, for telemetry
}


def _apply_twilio_config(session: Session) -> Session:
    """Apply the playground's last-pushed Twilio config to a fresh Session."""
    cfg = _twilio_config
    # BYOK: if the browser pushed a Lyzr key, inject it so the phone call
    # can access the same agents the browser user selected.
    if cfg.get("lyzr_api_key"):
        session.set_user_keys({"lyzr": cfg["lyzr_api_key"]})
    if cfg.get("lyzr_agent_id"):
        session.set_lyzr_agent_id(cfg["lyzr_agent_id"])
    if cfg.get("agent_tier") and cfg["agent_tier"] != session.agent_tier:
        session.set_agent_tier(cfg["agent_tier"])
    if cfg.get("tts_tier") and cfg["tts_tier"] != session.tts_tier:
        session.set_tts_tier(cfg["tts_tier"])
    if cfg.get("voice_id"):
        session.set_voice_id(cfg["voice_id"])
    session.set_hedge(stt=bool(cfg.get("hedge_stt")), tts=bool(cfg.get("hedge_tts")))
    session.set_speculative(bool(cfg.get("speculative")))
    if cfg.get("budget_usd") is not None:
        session.set_budget(float(cfg["budget_usd"]))
    return session


@app.get("/twilio/config")
async def twilio_config_get() -> dict[str, object]:
    """Inspect the current Twilio default config (what the next call will use)."""
    return {"ok": True, "config": dict(_twilio_config)}


# B5: Twilio session resumption table. When the same Twilio call_sid reconnects
# within _TWILIO_SESSION_TTL_S, we restore the prior Session — preserving the
# agent's memory, the cumulative cost budget, the metrics counters. Without
# this, a transient WS blip would reset the conversation mid-call.
_twilio_sessions: dict[str, tuple[Session, float]] = {}
_TWILIO_SESSION_TTL_S = 300.0  # 5 min — generous for a mid-call reconnect


def _prune_stale_twilio_sessions() -> None:
    """Drop entries older than the TTL. Called opportunistically on each connect."""
    cutoff = _time.monotonic() - _TWILIO_SESSION_TTL_S
    stale = [cs for cs, (_, ts) in _twilio_sessions.items() if ts < cutoff]
    for cs in stale:
        del _twilio_sessions[cs]


@app.websocket("/ws/twilio")
async def twilio_voice(ws: WebSocket) -> None:
    """Twilio Media Streams endpoint — one call per WebSocket.

    B5: ``call_sid``-keyed session resumption. If Twilio reconnects the same
    call within ``_TWILIO_SESSION_TTL_S``, we restore the prior session's
    agent memory, cost-budget state, and metrics counters. Otherwise a fresh
    Session is created (the default path for a new call).
    """
    from twilio_bridge import handle_twilio_call  # noqa: PLC0415

    keys = load_keys()
    if not keys.get("openai") or not keys.get("deepgram"):
        log.warning("twilio: missing required keys; rejecting call")
        await ws.close()
        return

    # Track the active call count for the admission gate + /health.
    _phone_call_entered()
    log.info(
        "twilio: call accepted (%d/%d active)",
        _active_phone_calls,
        MAX_CONCURRENT_CALLS,
    )
    # Notify browsers so the event stream can show "📞 2/3 lines active".
    await _broadcast_twilio(
        kind="call_count",
        active=_active_phone_calls,
        max=MAX_CONCURRENT_CALLS,
    )

    _prune_stale_twilio_sessions()
    # B5: session_holder is a 1-slot mutable container so on_call_sid can swap
    # in a resumed Session BEFORE build_kwargs is read for turn 1. The eager
    # Session() is the fallback if call_sid never arrives or isn't in the dict.
    # Apply the playground's last-pushed config so the phone uses the same
    # agent / voice / hedge / budget the browser demo is currently set to.
    session_holder: list[Session] = [_apply_twilio_config(Session())]
    _s = session_holder[0]
    log.info(
        "twilio: new session agent=%s/%s tts=%s voice=%s byok_lyzr=%s",
        _s.agent_tier,
        (_s.lyzr_agent_id or "—")[:8],
        _s.tts_tier,
        (_s.voice_id or "default")[:24],
        bool(_s.user_keys.get("lyzr")),
    )
    resumed_call_sid: list[str | None] = [None]

    def _on_call_sid(call_sid: str | None) -> None:
        if not call_sid:
            return
        cached = _twilio_sessions.get(call_sid)
        if cached is not None:
            prev_session, _ts = cached
            log.info("twilio: RESUMING session for callSid=%s (B5)", call_sid)
            session_holder[0] = prev_session
        # Always remember the call_sid so we can store/refresh on disconnect.
        resumed_call_sid[0] = call_sid

    def build_kwargs(turn: int) -> dict[str, Any]:
        # Same pipeline the browser uses; phone-shaped audio is already adapted
        # by the bridge before this point. Reads session via the holder so the
        # B5 swap (if any) is reflected.
        s = session_holder[0]
        return {
            "stt": s.stt,
            "agent": s.agent,
            "tts": s.tts,
            "tts_streaming": True,
            "text_filter": speakify,
            "pii_redactor": redact_pii,
            "agent_timeout": 15.0,
            "voice_id": s.voice_id,
        }

    def on_done(events: list[object]) -> None:
        lat = compute_turn_latency(events)  # type: ignore[arg-type]
        log.info("twilio turn done: %s", format_latency_badge(lat) or "(no audio)")
        # Accumulate the full agent answer from token events so the browser
        # mirror can display the complete response (not just latency badges).
        answer = "".join(
            getattr(e, "text", "") for e in events
            if getattr(e, "kind", "") == "agent.token"
        )
        # Also tell the live mirror so browser shows per-turn summary.
        asyncio.create_task(
            _broadcast_twilio(
                kind="done",
                badge=format_latency_badge(lat) or "",
                answer=answer,
                stt_final_ms=getattr(lat, "stt_final_ms", None),
                agent_first_token_ms=getattr(lat, "agent_first_token_ms", None),
                tts_first_audio_ms=getattr(lat, "tts_first_audio_ms", None),
                responded_in_ms=getattr(lat, "responded_in_ms", None),
            )
        )

    async def _mirror_event(ev_kind: str, **fields: object) -> None:
        await _broadcast_twilio(kind=ev_kind, **fields)

    async def _mirror_call_start(call_sid: str | None, stream_sid: str | None) -> None:
        await _broadcast_twilio(kind="call_start", call_sid=call_sid, stream_sid=stream_sid)

    async def _mirror_call_end(turns: int) -> None:
        await _broadcast_twilio(kind="call_end", turns=turns)

    try:
        await handle_twilio_call(
            ws,
            build_pipeline_kwargs=build_kwargs,
            on_turn_done=on_done,
            on_call_sid=_on_call_sid,
            on_event=_mirror_event,
            on_call_start=_mirror_call_start,
            on_call_end=_mirror_call_end,
        )
    finally:
        # B5: store/refresh the session keyed by call_sid so the NEXT
        # reconnect within TTL can resume it. (No-op if Twilio never sent us
        # a call_sid — e.g. malformed start frame.)
        cs = resumed_call_sid[0]
        if cs:
            _twilio_sessions[cs] = (session_holder[0], _time.monotonic())
        # Release the call slot and notify browsers.
        _phone_call_exited()
        log.info(
            "twilio: call ended (%d/%d active)",
            _active_phone_calls,
            MAX_CONCURRENT_CALLS,
        )
        asyncio.ensure_future(
            _broadcast_twilio(
                kind="call_count",
                active=_active_phone_calls,
                max=MAX_CONCURRENT_CALLS,
            )
        )


# ── End Twilio bridge ─────────────────────────────────────────────────────────


# ── LiveKit room-based voice ─────────────────────────────────────────────────

@app.post("/livekit/join")
async def livekit_join(request: Request) -> dict[str, object]:
    """Start a LiveKit voice session.

    The browser:
    1. Calls POST /livekit/token to get a participant token + room name
    2. Connects to the LiveKit room via the JS SDK (WebRTC)
    3. Calls POST /livekit/join with the room_name to tell the server
       to join as the agent

    The server joins the same room via the LiveKit bridge, subscribes to
    the participant's audio, runs the voice pipeline, and publishes TTS
    audio back. The browser hears the agent via WebRTC.
    """
    cfg = _livekit_config()
    if not cfg:
        return {"ok": False, "error": "LiveKit not configured"}

    body = await request.json()
    room_name = body.get("room_name", "")
    if not room_name:
        return {"ok": False, "error": "room_name required"}

    # Generate an agent token for this room.
    try:
        from livekit.api import AccessToken, VideoGrants  # noqa: PLC0415
    except ImportError:
        return {"ok": False, "error": "livekit SDK not installed"}

    log.info(
        "livekit: generating agent token with key=%s secret_len=%d url=%s room=%s",
        cfg["api_key"],
        len(cfg["api_secret"]),
        cfg["url"],
        room_name,
    )
    agent_token = AccessToken(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
    )
    agent_token.identity = "mdk-agent"
    agent_token.name = "Deva (AI Agent)"
    agent_token.with_grants(VideoGrants(room_create=True, room_join=True, room=room_name))
    agent_jwt = agent_token.to_jwt()

    # Launch the bridge in the background.
    from livekit_bridge import handle_livekit_room  # noqa: PLC0415

    session = Session()

    # Accept optional session config from the main playground.
    # BYOK: if the user pasted their own Lyzr key in the browser, apply it
    # to this session so the LiveKit bridge authenticates with their account.
    user_lyzr_key = body.get("user_lyzr_key", "")
    if user_lyzr_key:
        session.user_keys["lyzr"] = user_lyzr_key
    # Set lyzr_agent_id BEFORE agent_tier — set_agent_tier rebuilds the
    # agent object using self.lyzr_agent_id, so it needs to be current.
    lyzr_agent_id = body.get("lyzr_agent_id", "")
    if lyzr_agent_id:
        session.lyzr_agent_id = lyzr_agent_id
    agent_tier = body.get("agent_tier", "")
    if agent_tier and agent_tier != session.agent_tier:
        session.set_agent_tier(agent_tier)
    # If agent_tier was already correct but agent_id changed, rebuild.
    elif lyzr_agent_id:
        session.set_lyzr_agent_id(lyzr_agent_id)
    language = body.get("language", "")
    if language:
        session.set_language(language)
    voice_id = body.get("voice_id", "")
    if voice_id:
        session.voice_id = voice_id
    tts_tier = body.get("tts_tier", "")
    if tts_tier:
        session.set_tts_tier(tts_tier)

    def build_kwargs(turn: int) -> dict[str, Any]:
        return {
            "stt": session.stt,
            "agent": session.agent,
            "tts": session.tts,
            "tts_streaming": True,
            "text_filter": speakify,
            "pii_redactor": redact_pii,
            "agent_timeout": 15.0,
            "voice_id": session.voice_id,
        }

    asyncio.create_task(
        handle_livekit_room(
            livekit_url=cfg["url"],
            token=agent_jwt,
            room_name=room_name,
            build_pipeline_kwargs=build_kwargs,
            publish_events=True,
        )
    )

    log.info("livekit: agent joining room=%s", room_name)
    return {"ok": True, "room_name": room_name, "agent_identity": "mdk-agent"}


# ── End LiveKit bridge ────────────────────────────────────────────────────────


@app.websocket("/ws/voice")
async def voice(ws: WebSocket) -> None:
    await ws.accept()
    # C3: track active sessions for /health (decremented in the finally below).
    _session_entered()
    # Live Twilio mirror — subscribe this browser to phone-call events.
    # Removed in the finally below.
    _twilio_observers.add(ws)
    keys = load_keys()
    if not keys.get("openai"):
        await _send_event(
            ws,
            event="error",
            message="OPENAI_API_KEY missing — drop your key at ~/.mdk_openai_key",
        )
        await ws.close()
        return

    # Trail queue: failover/breaker/cache events flow through this asyncio
    # queue from the (sync-callback) observer to a forwarder task that ships
    # them as `event: trail` WS messages — the A1 "see failover happening live"
    # demo wire. Bounded so a slow browser can't unbound-grow the queue.
    trail_q: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=256)
    # L3/L4: tool_call + extras queues. Same pattern as trail: agent emits via
    # sync callback → queued → async forwarder ships to WS. Bounded so a slow
    # browser can't grow them unboundedly.
    tool_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    extras_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)

    def _on_trail(event: str, fields: dict[str, Any]) -> None:
        # Called synchronously from inside FailoverSTT/TTS on the event loop —
        # put_nowait is safe. Drop silently on overflow rather than block the
        # router (failover signals are informational; correctness doesn't need
        # them to be acked).
        try:
            trail_q.put_nowait((event, fields))
        except asyncio.QueueFull:
            pass

    def _on_tool_call(tc: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            tool_q.put_nowait(tc)

    def _on_extras(ex: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            extras_q.put_nowait(ex)

    session = Session(on_trail=_on_trail, on_tool_call=_on_tool_call, on_extras=_on_extras)
    # ADR 073 Phase 5 — warm the STT client now (while the page is still wiring
    # up) so the caller's FIRST turn skips Deepgram client cold-start. Best-effort.
    with contextlib.suppress(Exception):
        await warm_stt(session.stt)

    async def _forward_trail() -> None:
        """Drain the trail queue onto the WS until the session ends."""
        while True:
            ev, fields = await trail_q.get()
            await _send_event(ws, event="trail", kind=ev, fields=fields)

    async def _forward_tool_calls() -> None:
        """L3: ship Lyzr/OpenAI tool_call deltas to the UI's event stream."""
        while True:
            tc = await tool_q.get()
            await _send_event(ws, event="tool_call", **tc)

    async def _forward_extras() -> None:
        """L4: ship provider extras (RAG citations, sources, etc.) to the UI."""
        while True:
            ex = await extras_q.get()
            await _send_event(ws, event="extras", **ex)

    trail_task = asyncio.create_task(_forward_trail())
    tool_task = asyncio.create_task(_forward_tool_calls())
    extras_task = asyncio.create_task(_forward_extras())

    await _send_event(
        ws,
        event="ready",
        keys=keys,
        tts_tiers=TTS_TIERS,
        tts_tier=session.tts_tier,
        agent_tiers=AGENT_TIERS,
        agent_tier=session.agent_tier,
        lyzr_available=bool(os.environ.get("LYZR_API_KEY") and session.lyzr_agent_id),
        lyzr_agent_id=session.lyzr_agent_id,
        session_id=session.session_id,
        # B4: tell the browser whether call recording is on so it can render
        # a recording-pending hint (the actual URL arrives later via
        # ``recording_ready`` when the session closes).
        recording=session.recorder is not None,
        # A4: surface the per-session budget so the UI can render the slider
        # in the right starting position and the spent / remaining counters.
        budget_usd=session.budget_usd,
        spent_usd=session.spent_usd,
        # B2: initial hedge state — both off by default (doubles cost when on).
        hedge_stt=session.hedge_stt,
        hedge_tts=session.hedge_tts,
        # BYOK: initial state of user-supplied keys (all None at session start).
        user_keys_active={p: bool(session.user_keys.get(p)) for p in USER_KEY_PROVIDERS},
        # Voice picker — initial empty = adapter default voice.
        voice_id=session.voice_id,
        # The public phone number callers dial to reach the agent. Empty
        # string when Twilio isn't configured (UI hides the chip then).
        twilio_number=_twilio_number(),
    )
    log.info("session opened; keys=%s", keys)

    vad_enabled = True  # default = talk-normally; toggleable per session

    try:
        while True:
            # Wait for a control message. Two kinds start a turn: "start" (begin
            # listening) or it can be preceded by config messages (set_tts_tier,
            # inject_fault, set_vad) that adjust state for the NEXT turn.
            # receive_text() raises KeyError if Starlette delivers a non-text
            # frame (e.g. an abrupt browser tab close); treat that as a clean
            # disconnect rather than a 500.
            try:
                msg = await ws.receive_text()
            except (WebSocketDisconnect, KeyError, RuntimeError):
                log.info("/ws/voice: client disconnected")
                return
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            ev_name = data.get("event")

            if ev_name == "set_tts_tier":
                tier = session.set_tts_tier(str(data.get("tier", "")))
                await _send_event(ws, event="tts_tier", tier=tier)
                continue
            if ev_name == "set_agent_tier":
                tier = session.set_agent_tier(str(data.get("tier", "")))
                await _send_event(ws, event="agent_tier", tier=tier)
                continue
            if ev_name == "set_lyzr_agent_id":
                aid = session.set_lyzr_agent_id(str(data.get("agent_id", "")))
                await _send_event(ws, event="lyzr_agent_id", agent_id=aid)
                continue
            if ev_name == "inject_fault":
                armed = session.inject_fault(str(data.get("stage", "")))
                await _send_event(ws, event="fault_armed", stage=armed)
                continue
            if ev_name == "set_vad":
                vad_enabled = bool(data.get("enabled", True))
                await _send_event(ws, event="vad", enabled=vad_enabled)
                continue
            if ev_name == "reset_memory":
                if hasattr(session.agent, "reset"):
                    session.agent.reset()
                await _send_event(ws, event="memory_reset")
                continue
            if ev_name == "reset_metrics":
                session.metrics.reset()
                session.turns = 0
                # A4: also reset cumulative spend + re-arm demote trigger.
                session.spent_usd = 0.0
                session.budget_demoted = False
                await _send_event(ws, event="metrics_reset")
                continue
            if ev_name == "set_user_keys":
                # BYOK — per-session API key overrides. Accepts a dict of
                # {provider: api_key | "" | None}; "" / None clears that
                # provider's BYOK and falls back to the server's env key.
                raw = data.get("keys") or {}
                if not isinstance(raw, dict):
                    raw = {}
                # Sanitize: only the providers we know about; only strings.
                sanitized: dict[str, str | None] = {}
                for p in USER_KEY_PROVIDERS:
                    if p in raw:
                        v = raw[p]
                        sanitized[p] = v if isinstance(v, str) else None
                applied = session.set_user_keys(sanitized)
                await _send_event(ws, event="user_keys", active=applied)
                continue
            if ev_name == "push_to_twilio":
                # Snapshot the browser session's current settings into the
                # module-level Twilio default. The NEXT inbound phone call
                # will use these — including the BYOK Lyzr key so the phone
                # can access the same agents the browser user selected.
                _twilio_config["agent_tier"] = session.agent_tier
                _twilio_config["lyzr_agent_id"] = session.lyzr_agent_id
                _twilio_config["tts_tier"] = session.tts_tier
                _twilio_config["voice_id"] = session.voice_id
                _twilio_config["hedge_stt"] = session.hedge_stt
                _twilio_config["hedge_tts"] = session.hedge_tts
                _twilio_config["speculative"] = session.speculative
                _twilio_config["budget_usd"] = session.budget_usd
                _twilio_config["lyzr_api_key"] = session.user_keys.get("lyzr")
                _twilio_config["updated_by"] = session.session_id
                log.info(
                    "twilio config pushed: tier=%s tts=%s voice=%s lyzr=%s",
                    session.agent_tier,
                    session.tts_tier,
                    (session.voice_id or "default")[:24],
                    (session.lyzr_agent_id or "—")[:8],
                )
                await _send_event(ws, event="twilio_config", config=dict(_twilio_config))
                continue
            if ev_name == "set_voice_id":
                applied = session.set_voice_id(str(data.get("voice_id", "")))
                await _send_event(ws, event="voice_id", voice_id=applied)
                continue
            if ev_name == "set_language":
                result = session.set_language(str(data.get("language", "")))
                await _send_event(
                    ws, event="language", language=result["language"],
                    voice_id=result["voice_id"],
                )
                continue
            if ev_name == "set_hedge":
                # B2: latency hedging — fire 2 STTs/TTSs in parallel, take first.
                applied = session.set_hedge(
                    stt=data.get("stt"),
                    tts=data.get("tts"),
                )
                await _send_event(ws, event="hedge", **applied)
                continue
            if ev_name == "set_speculative":
                # ADR 070: speculative agent kickoff (start on a stable interim).
                spec = session.set_speculative(bool(data.get("enabled")))
                await _send_event(ws, event="speculative", **spec)
                continue
            if ev_name == "set_keyterms":
                # ADR 071 D4: live STT keyterm-boost vocabulary (accuracy A/B).
                applied = session.set_keyterms(data.get("keyterms"))
                await _send_event(ws, event="keyterms", **applied)
                continue
            if ev_name == "set_endpointing":
                # ADR 073 D3: live STT silence-hold override (latency-floor A/B).
                applied = session.set_endpointing(data.get("endpointing_ms"))
                await _send_event(ws, event="endpointing", **applied)
                continue
            if ev_name == "set_turn_detection":
                # ADR 072: semantic turn-detection as the speculation trigger.
                applied = session.set_turn_detection(bool(data.get("enabled")))
                await _send_event(ws, event="turn_detection", **applied)
                continue
            if ev_name == "set_adaptive_endpointing":
                # ADR 073 Phase 3: adaptively move the silence-hold from cadence.
                applied = session.set_adaptive_endpointing(bool(data.get("enabled")))
                await _send_event(ws, event="adaptive_endpointing", **applied)
                continue
            if ev_name == "set_budget":
                # A4: per-session cost ceiling. None / 0 / negative → unbounded.
                raw = data.get("usd")
                budget = float(raw) if raw not in (None, "") else None
                applied = session.set_budget(budget)
                await _send_event(
                    ws, event="budget", budget_usd=applied, spent_usd=session.spent_usd
                )
                continue
            if ev_name != "start":
                continue

            # The browser reports its actual AudioContext.sampleRate — many
            # browsers ignore the 16kHz constraint and deliver 48kHz, which
            # silently breaks STT if the server assumes 16kHz. Default to 16k
            # only if the browser didn't tell us.
            client_sample_rate = int(data.get("sample_rate") or 16_000)
            session.turns += 1
            end = asyncio.Event()
            cancel = asyncio.Event()  # ← barge-in: caller interrupts agent/TTS
            audio_done = asyncio.Event()  # set when audio iterator returns
            events: list[object] = []

            # Bind the per-turn events as defaults so ruff B023 is happy and so
            # the closures can't accidentally reference a later iteration's set.
            def _endpointed(_done: asyncio.Event = audio_done) -> None:
                # VAD ended the turn — tell the browser to stop sending mic frames.
                asyncio.create_task(_send_event(ws, event="endpoint"))
                _done.set()

            async def _watch_for_barge_in(
                _done: asyncio.Event = audio_done,
                _cancel: asyncio.Event = cancel,
                _rate: int = client_sample_rate,
            ) -> None:
                """Once the user's turn ends, listen for barge-in two ways:

                1. **Manual** — browser fires ``{"event": "barge_in"}``;
                2. **Automatic VAD** — browser keeps streaming PCM frames during
                   the answer; we run energy-RMS on each one and fire the cancel
                   after ~150ms of detected speech (real-voice-agent feel —
                   caller doesn't need to click anything to take the floor).
                """
                await _done.wait()
                speech_ms = 0.0  # rolling speech-during-speak accumulator
                while not _cancel.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                    except TimeoutError:
                        continue
                    except (RuntimeError, WebSocketDisconnect):
                        return
                    if msg.get("type") == "websocket.disconnect":
                        _cancel.set()
                        return
                    # Auto-VAD: each binary frame is PCM16 16kHz from the mic.
                    if msg.get("bytes"):
                        if session.recorder is not None:
                            # B4: caller is still talking during the agent's
                            # response — capture those frames too so the
                            # recording's caller channel doesn't go silent
                            # while the agent is being interrupted.
                            session.recorder.add_caller(msg["bytes"])
                        chunk = AudioChunk(data=msg["bytes"], codec="pcm16", sample_rate=_rate)
                        frame_ms = (len(chunk.data) / 2) / (_rate / 1000.0)
                        # Use _BARGE_IN_RMS (≫ _SILENCE_RMS) so speaker bleed
                        # from the agent's own audio doesn't trigger false
                        # barge-in cancellation mid-TTS.
                        if not is_silent(chunk, _BARGE_IN_RMS):
                            speech_ms += frame_ms
                            if speech_ms >= _BARGE_IN_SPEECH_MS:
                                await _send_event(ws, event="barged_in", auto=True)
                                _cancel.set()
                                return
                        else:
                            # Slow decay so a single isolated noisy frame doesn't
                            # trip; sustained speech still wins.
                            speech_ms = max(0.0, speech_ms - frame_ms * 0.5)
                        continue
                    text = msg.get("text")
                    if not text:
                        continue
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if data.get("event") == "barge_in":
                        await _send_event(ws, event="barged_in")
                        _cancel.set()
                        return

            barge_in_task = asyncio.create_task(_watch_for_barge_in())

            # ADR 072 / ADR 073 — resolve this turn's effective levers:
            # * speculation fires only if requested AND the cost-guard hasn't
            #   tripped (low commit-ratio auto-disables it mid-session);
            # * the turn-detector is supplied only when enabled (fires the
            #   speculation on semantic completeness, not just the quiet-gap);
            # * adaptive endpointing, when on, supplies the moved hold.
            eff_speculative = session.speculative and session._spec_guard.should_speculate()
            eff_turn_detector = session._turn_detector if session.turn_detection else None
            eff_endpointing = session.endpointing_ms
            if session.adaptive_endpointing:
                eff_endpointing = session._adaptive.current_ms
            # The session observer is cumulative; snapshot before the turn so we
            # can feed the guard/adaptive controller this turn's delta after it.
            _spec_before = session.metrics.speculation_snapshot()

            try:
                async for ev in run_voice_pipeline(
                    audio_in=_audio_from_ws(
                        ws,
                        end,
                        vad=vad_enabled,
                        sample_rate=client_sample_rate,
                        on_endpoint=_endpointed,
                        recorder=session.recorder,
                    ),
                    stt=session.stt,
                    agent=session.agent,
                    tts=session.tts,
                    tts_streaming=True,
                    text_filter=speakify,
                    pii_redactor=redact_pii,
                    cancel=cancel,
                    agent_timeout=15.0,
                    voice_id=session.voice_id,
                    language=session.language or None,
                    # ADR 070: opt-in speculative kickoff + observer so the
                    # speculation_started/committed/cancelled events flow to the
                    # metrics + trail (the live cancel-ratio the dashboard shows).
                    speculative=eff_speculative,
                    # ADR 072 — semantic turn-detection as the speculation trigger
                    # (None when the toggle is off → quiet-gap debounce alone).
                    turn_detector=eff_turn_detector,
                    # ADR 071 D4 / ADR 073 D3 — per-session keyterm boosting +
                    # endpointing override, both editable live from the UI so the
                    # audience can A/B accuracy (keyterms) and the silence-wait
                    # latency floor (endpointing) without a redeploy.
                    keyterms=session.keyterms or None,
                    endpointing_ms=eff_endpointing,
                    observer=session.observer,
                ):
                    events.append(ev)
                    if ev.kind == "transcript.partial":
                        await _send_event(ws, event="transcript.partial", text=ev.text)
                    elif ev.kind == "transcript.final":
                        await _send_event(ws, event="transcript.final", text=ev.text)
                    elif ev.kind == "agent.token":
                        await _send_event(ws, event="agent.token", text=ev.text)
                    elif ev.kind == "tts.audio" and ev.audio:
                        if session.recorder is not None:
                            # B4: tap the outgoing TTS frame into the agent
                            # leg of the recording. Frames are PCM16 at the
                            # browser-playback rate (24 kHz for Cartesia /
                            # OpenAI TTS); the recorder's stereo mix uses
                            # its own sample_rate, so a rate mismatch here
                            # would play back slow/fast. For now we accept
                            # the v1 limitation: agent leg may sound
                            # speed-shifted unless rates match — the
                            # transcript is the load-bearing artifact.
                            session.recorder.add_agent(ev.audio.data)
                        await _send_audio(ws, ev.audio)
                    elif ev.kind == "error":
                        await _send_event(
                            ws,
                            event="error",
                            stage=ev.stage,
                            code=ev.code,
                            message=ev.message,
                        )
            except WebSocketDisconnect:
                log.info("client disconnected mid-turn")
                barge_in_task.cancel()
                return
            finally:
                audio_done.set()  # wake the monitor if VAD didn't fire
                barge_in_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await barge_in_task

            # ADR 073 — feed this turn's speculation delta to the cost-guard +
            # adaptive controller; surface a trip / an adapted hold to the UI.
            _after = session.metrics.speculation_snapshot()
            _delta = {
                "started": _after["started"] - _spec_before["started"],
                "committed": _after["committed"] - _spec_before["committed"],
                "cancelled": _after["cancelled"] - _spec_before["cancelled"],
            }
            if eff_speculative and session._spec_guard.record(_delta):
                await _send_event(
                    ws,
                    event="speculation_disabled",
                    reason="low_commit_ratio",
                    commit_ratio=round(session._spec_guard.commit_ratio, 3),
                )
            if session.adaptive_endpointing:
                _moved = session._adaptive.record(_delta)
                if _moved is not None:
                    await _send_event(ws, event="endpointing_adapted", endpointing_ms=_moved)

            # Per-turn summary the browser dashboard renders.
            latency = compute_turn_latency(events)  # type: ignore[arg-type]
            # Cost: scrape the turn's text + audio bytes from the event stream
            # so the dashboard shows real dollars (not a manifest guess).
            transcript_text = ""
            answer_text = ""
            audio_seconds = 0.0
            for ev in events:
                if ev.kind == "transcript.final":  # type: ignore[attr-defined]
                    transcript_text = ev.text  # type: ignore[attr-defined]
                elif ev.kind == "agent.token":  # type: ignore[attr-defined]
                    answer_text += ev.text  # type: ignore[attr-defined]
                elif ev.kind == "tts.audio" and ev.audio:  # type: ignore[attr-defined]
                    # Out-audio duration: approximates STT-audio for this loop;
                    # close enough for a per-turn cost display.
                    a = ev.audio  # type: ignore[attr-defined]
                    audio_seconds += (len(a.data) / 2) / max(1, a.sample_rate)
            cost_usd = _estimate_cost(
                transcript=transcript_text,
                answer=answer_text,
                audio_seconds=audio_seconds,
                tts_tier=session.tts_tier,
            )
            # A4: thread per-turn cost through the budget tracker. If this turn
            # tripped the ceiling, the demote already happened (TTS swapped to
            # cheaper for the next turn) — surface a trail event AND a
            # `budget_demote` UI badge so Deva sees the policy fire live.
            demoted = session.record_turn_cost(cost_usd)
            if demoted:
                log.info(
                    "budget tripped: spent $%.5f > $%.5f → demoted tts → %s",
                    session.spent_usd,
                    session.budget_usd,
                    session.tts_tier,
                )
                await _send_event(
                    ws,
                    event="trail",
                    kind="budget_demote",
                    fields={
                        "spent_usd": round(session.spent_usd, 5),
                        "budget_usd": session.budget_usd,
                        "to_tier": session.tts_tier,
                    },
                )
            await _send_event(
                ws,
                event="done",
                turn=session.turns,
                tts_tier=session.tts_tier,
                agent_tier=session.agent_tier,
                badge=format_latency_badge(latency),
                cost_usd=cost_usd,
                spent_usd=round(session.spent_usd, 5),
                budget_usd=session.budget_usd,
                latency={
                    "stt_final_ms": latency.stt_final_ms,
                    "agent_first_token_ms": latency.agent_first_token_ms,
                    "tts_first_audio_ms": latency.tts_first_audio_ms,
                    "responded_in_ms": latency.responded_in_ms,
                },
                metrics=session.metrics.snapshot(),
                # TTS phrase-cache effectiveness (ADR 068 D6): every hit is one
                # synthesis NOT paid for, so a rising hit_rate is a falling
                # $/turn — surfaced so the dashboard can show the cost win live.
                cache=session.cache.stats(),
            )
    except WebSocketDisconnect:
        log.info("session closed")
    finally:
        # Stop the trail / tool / extras forwarders so they don't leak past the session.
        for _t in (trail_task, tool_task, extras_task):
            _t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _t
        # B4: finalize + upload the call recording on the way out, fire-and-
        # forget so a slow upload (or a missing SDK) never blocks the WS
        # close path. The browser already saw its 'done' for the last turn;
        # 'recording_ready' shows up later — or never, if uploads are off.
        if session.recorder is not None:
            asyncio.create_task(_finalize_recording(ws, session))
        # C3: decrement the live session count for /health.
        _session_exited()
        # Unsubscribe from the Twilio live mirror.
        _twilio_observers.discard(ws)


async def _finalize_recording(ws: WebSocket, session: Session) -> None:
    """Background task: encode WAV, upload, push ``recording_ready`` to UI.

    Runs after the ``/ws/voice`` handler returns. WAV encoding is sync but
    O(seconds) for a 10-min call; upload is network-bound. Both happen off
    the request path so the WS close isn't held open. The ``recording_ready``
    WS event is best-effort — by the time we get here the browser may already
    be disconnected, in which case the URL is still recoverable via the REST
    ``/recordings/{session_id}`` endpoint.
    """
    recorder = session.recorder
    if recorder is None:
        return
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        # is_recording_enabled() should have prevented this, but a missing
        # conn-string mid-session shouldn't crash — just no-op.
        return
    try:
        wav_bytes = recorder.to_wav_stereo()
    except Exception:  # noqa: BLE001 - encoding failure shouldn't crash
        log.exception("recording: WAV encode failed for session %s", session.session_id)
        return
    url = await upload_recording(
        wav_bytes,
        session_id=session.session_id,
        container=_BLOB_CONTAINER,
        conn_str=conn_str,
    )
    if url is None:
        return
    _recordings[session.session_id] = url
    # Best-effort UI notify — caller may already be gone.
    with contextlib.suppress(Exception):
        await _send_event(
            ws,
            event="recording_ready",
            session_id=session.session_id,
            url=url,
        )


# Browser PCM is 16 kHz; OpenAI Realtime expects 24 kHz. We resample at the
# transport edge so the adapter stays framework-neutral (CLAUDE.md rule 6).
_BROWSER_RATE = 16_000
_REALTIME_RATE = 24_000


@app.websocket("/ws/voice/realtime")
async def voice_realtime(ws: WebSocket) -> None:
    """Full-duplex voice↔voice via the OpenAI Realtime API (B1).

    A different transport path from ``/ws/voice``: there is no STT, no
    intermediate text agent, no TTS — the realtime model owns all three
    stages. The browser ships continuous 16 kHz PCM frames; we upsample to
    24 kHz, feed them to the :class:`~movate.voice.OpenAIRealtime` session, and
    downsample the assistant's audio back to 16 kHz for the browser to play.
    Sub-second turn time is the demo win this enables.

    The same UI handlers as ``/ws/voice`` work because we emit the same
    ``transcript.*`` / ``tts.audio`` / ``done`` / ``error`` event envelopes —
    the only difference the browser notices is the latency.
    """
    await ws.accept()
    # C3: track active sessions for /health.
    _session_entered()
    keys = load_keys()
    if not keys.get("openai"):
        await _send_event(
            ws,
            event="error",
            message="OPENAI_API_KEY missing — realtime requires it",
        )
        await ws.close()
        _session_exited()
        return

    # Announce realtime-specific state — UI uses ``mode`` to gate which controls
    # apply (TTS A/B + agent toggle are owned by the model in realtime, so the
    # UI greys them out).
    await _send_event(
        ws,
        event="ready",
        mode="realtime",
        keys=keys,
        agent_tier="openai_realtime",
        tts_tier="openai_realtime",
        agent_tiers=["openai_realtime"],
        tts_tiers=["openai_realtime"],
        lyzr_available=False,
    )
    log.info("realtime session opened; keys=%s", keys)

    realtime = OpenAIRealtime()
    end_evt = asyncio.Event()
    audio_in_q: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=64)

    async def _receive_mic() -> None:
        """Pump browser mic frames into the realtime adapter's input queue.

        Browser sends raw PCM16 16 kHz; we upsample to 24 kHz at the edge.
        Stop signals (``stop`` JSON event, ``stop`` lifecycle) cleanly close
        the input iterator so the model knows the caller is done.
        """
        try:
            while not end_evt.is_set():
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                except TimeoutError:
                    continue
                except (RuntimeError, WebSocketDisconnect):
                    return
                if msg.get("type") == "websocket.disconnect":
                    return
                if msg.get("bytes"):
                    upsampled = resample_pcm16(msg["bytes"], _BROWSER_RATE, _REALTIME_RATE)
                    chunk = AudioChunk(data=upsampled, codec="pcm16", sample_rate=_REALTIME_RATE)
                    try:
                        audio_in_q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        # Drop the oldest frame to keep audio fresh — better
                        # to skip 20 ms than to accumulate latency.
                        with contextlib.suppress(asyncio.QueueEmpty):
                            audio_in_q.get_nowait()
                        audio_in_q.put_nowait(chunk)
                    continue
                text = msg.get("text")
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if data.get("event") == "stop":
                    return
                # In realtime mode, barge-in is handled server-side by the
                # model's own VAD — we don't need explicit cancel.
        finally:
            await audio_in_q.put(None)  # signal end of input to the session

    async def _audio_iter() -> AsyncIterator[AudioChunk]:
        while True:
            chunk = await audio_in_q.get()
            if chunk is None:
                return
            yield chunk

    recv_task = asyncio.create_task(_receive_mic())

    # System prompt — voice-shaped, no markdown. The realtime model honors it
    # as the assistant's persona for the session.
    instructions = (
        "You are a concise customer-support voice agent. Reply in one or two "
        "short sentences. Speak naturally — no markdown, no lists."
    )

    transcript_user_acc = ""
    transcript_agent_acc = ""
    audio_bytes_out = 0

    try:
        async for chunk in realtime.session(
            _audio_iter(),
            codec="pcm16",
            instructions=instructions,
        ):
            if chunk.kind == "audio" and chunk.audio:
                # Downsample 24 kHz → 16 kHz for the browser's playback.
                pcm16 = resample_pcm16(chunk.audio.data, chunk.audio.sample_rate, _BROWSER_RATE)
                out_chunk = AudioChunk(data=pcm16, codec="pcm16", sample_rate=_BROWSER_RATE)
                audio_bytes_out += len(pcm16)
                await _send_audio(ws, out_chunk)
            elif chunk.kind == "transcript":
                # The realtime API emits BOTH input (caller) and output (agent)
                # transcripts as ``transcript`` chunks. We can't always tell
                # which from the chunk alone, so route both to a single
                # rolling line; the final one wins for display.
                if chunk.is_final:
                    # Final agent transcript usually arrives after audio →
                    # treat as the "answer" line.
                    transcript_agent_acc = chunk.text
                    await _send_event(ws, event="transcript.final", text=chunk.text)
                else:
                    transcript_user_acc = chunk.text
                    await _send_event(ws, event="transcript.partial", text=chunk.text)
            elif chunk.kind == "speech_started":
                await _send_event(ws, event="speech_started")
            elif chunk.kind == "speech_stopped":
                await _send_event(ws, event="endpoint")
            elif chunk.kind == "response_done":
                # Approximate cost: OpenAI Realtime is ~$0.06/min input audio
                # + $0.24/min output audio (gpt-4o-realtime). Audio_out is the
                # bytes we shipped to the browser at 16 kHz PCM16 (2 bytes/sample).
                out_seconds = audio_bytes_out / (2 * _BROWSER_RATE)
                cost_usd = round(out_seconds * (0.24 / 60.0), 5)
                await _send_event(
                    ws,
                    event="done",
                    mode="realtime",
                    tts_tier="openai_realtime",
                    agent_tier="openai_realtime",
                    badge=f"realtime · ~{round(out_seconds * 1000)}ms audio out",
                    cost_usd=cost_usd,
                    metrics={
                        "turns_served": 1,
                        "audio_seconds": round(out_seconds, 2),
                    },
                    transcript_user=transcript_user_acc,
                    transcript_agent=transcript_agent_acc,
                )
                # Reset per-turn accumulators so the next turn's done event is honest.
                transcript_user_acc = ""
                transcript_agent_acc = ""
                audio_bytes_out = 0
            elif chunk.kind == "error":
                await _send_event(
                    ws,
                    event="error",
                    stage="realtime",
                    message=chunk.message or "realtime error",
                    code=chunk.code,
                )
    except WebSocketDisconnect:
        log.info("realtime: client disconnected")
    except Exception as exc:  # noqa: BLE001 - we want a clean WS error path
        log.exception("realtime session error")
        with contextlib.suppress(Exception):
            await _send_event(
                ws, event="error", stage="realtime", message=str(exc) or type(exc).__name__
            )
    finally:
        end_evt.set()
        with contextlib.suppress(asyncio.QueueFull):
            audio_in_q.put_nowait(None)
        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await recv_task
        # C3: realtime path decrement.
        _session_exited()


@app.on_event("startup")
async def _record_start_time() -> None:
    """Stamp the boot time for /health's uptime field (C3)."""
    global _started_at_monotonic, _started_at_wall_iso  # noqa: PLW0603
    _started_at_monotonic = _time.monotonic()
    _started_at_wall_iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    keys = load_keys()
    print(f"mdk-voice demo on http://localhost:8765  (keys: {keys})")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
