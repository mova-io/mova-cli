"""Lyzr provider-parity check — "every provider Lyzr lists, mdk-voice covers."

Lyzr's voice runtime exposes its supported-provider menu via two HTTP discovery
endpoints, which their Studio uses to populate the agent-creation form:

* ``GET /v1/config/pipeline-options`` — ``stt`` / ``llm`` / ``tts`` providers.
* ``GET /v1/config/realtime-options`` — full-duplex realtime providers.

mdk-voice adapts the **same** providers behind the ADR-048 speech Protocols and
ADR-067 :class:`~movate.voice.AgentTurn` seam (plus the ADR-068 failover composites
a single-pick menu *can't* express). This module pulls those menus at runtime
and verifies the claim — for CI ("Lyzr added a provider; we haven't"), for the
sales pitch ("here, literally — every box you list, we have an adapter for"),
and for an honest gap report when something is missing.

LLM providers are mapped to ``"via-lyzr-v4-openai-compat"`` because every model
Lyzr exposes (OpenAI, Gemini, DeepSeek, Moonshot/Kimi) is reachable through
Lyzr's own OpenAI-compatible ``/v4/chat/completions`` endpoint — the path the
demo's Lyzr tier uses today via :class:`OpenAIChatAgent`. That counts as
covered: same wire protocol, same code path, just a different ``model``.

**Stdlib only** — no third-party dep. The two GETs are issued in a thread so
the call is awaitable without forcing a new HTTP client into the base install.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal

LYZR_VOICE_BASE = "https://voice-livekit.studio.lyzr.ai"

ProviderKind = Literal["stt", "tts", "llm", "realtime"]

# Lyzr ``providerId`` → mdk-voice adapter ``.name`` (empty string = gap).
#
# Update by adding/removing entries when adapters land or Lyzr extends the menu.
# Keep the mapping additive: never delete an existing entry — mark it ``""`` so
# the parity report keeps surfacing the gap rather than silently hiding it.
LYZR_PROVIDER_MAP: dict[ProviderKind, dict[str, str]] = {
    "stt": {
        "deepgram": "deepgram",  # movate.voice.deepgram.DeepgramSTT
        "assemblyai": "",  # gap — Lyzr-supported, no mdk-voice adapter yet
        "cartesia": "cartesia_stt",  # movate.voice.cartesia_stt.CartesiaSTT (Ink Whisper)
        "elevenlabs": "",  # gap — ElevenLabs STT (we adapt only their TTS)
        "sarvam": "",  # gap
    },
    "tts": {
        "cartesia": "cartesia",  # movate.voice.cartesia.CartesiaTTS
        "elevenlabs": "elevenlabs",  # movate.voice.elevenlabs.ElevenLabsTTS
        "deepgram": "deepgram_aura",  # movate.voice.deepgram_tts.DeepgramAuraTTS
        "inworld": "",  # gap
        "rime": "",  # gap
        "sarvam": "",  # gap
    },
    "llm": {
        # All Lyzr-hosted LLMs are reachable via Lyzr's OpenAI-compatible
        # `/v4/chat/completions` endpoint, driven by OpenAIChatAgent with
        # `base_url=https://agent-prod.studio.lyzr.ai/v4`. Same code path the
        # demo's Lyzr tier already uses — the ``model`` field picks which.
        "openai": "via-lyzr-v4-openai-compat",
        "google": "via-lyzr-v4-openai-compat",
        "moonshotai": "via-lyzr-v4-openai-compat",
        "deepseek-ai": "via-lyzr-v4-openai-compat",
    },
    "realtime": {
        "openai": "openai_realtime",  # movate.voice.realtime_openai.OpenAIRealtime
        "google": "",  # gap — Gemini Live
        "ultravox": "",  # gap
        "xai": "",  # gap — xAI Grok (Lyzr's providerId is just "xai")
    },
}


@dataclass(frozen=True)
class LyzrProvider:
    """One entry from a Lyzr discovery endpoint, normalized across the two shapes."""

    kind: ProviderKind
    provider_id: str
    display_name: str
    model_count: int


@dataclass(frozen=True)
class ParityReport:
    """Result of comparing a Lyzr menu against :data:`LYZR_PROVIDER_MAP`.

    ``covered`` lists Lyzr providers an mdk-voice adapter (or the OpenAI-compat
    Lyzr-v4 route, for LLMs) covers. ``gaps`` lists Lyzr providers we don't
    cover yet. Empty ``gaps`` means full parity; non-empty is a backlog list.
    """

    covered: tuple[LyzrProvider, ...] = field(default_factory=tuple)
    gaps: tuple[LyzrProvider, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.covered) + len(self.gaps)

    @property
    def coverage_pct(self) -> float:
        """0-100, the share of Lyzr providers mdk-voice covers. 100 = full parity."""
        return 100.0 * len(self.covered) / self.total if self.total else 100.0

    @property
    def is_parity(self) -> bool:
        return not self.gaps


def _parse_pipeline(payload: dict[str, Any]) -> list[LyzrProvider]:
    out: list[LyzrProvider] = []
    kinds: tuple[ProviderKind, ...] = ("stt", "llm", "tts")
    for kind in kinds:
        for p in payload.get(kind, []) or []:
            out.append(
                LyzrProvider(
                    kind=kind,
                    provider_id=str(p.get("providerId", "")),
                    display_name=str(p.get("displayName", p.get("providerId", ""))),
                    model_count=len(p.get("models", []) or []),
                )
            )
    return out


def _parse_realtime(payload: dict[str, Any]) -> list[LyzrProvider]:
    return [
        LyzrProvider(
            kind="realtime",
            provider_id=str(p.get("providerId", "")),
            display_name=str(p.get("displayName", p.get("providerId", ""))),
            model_count=len(p.get("models", []) or []),
        )
        for p in (payload.get("providers", []) or [])
    ]


def _http_get_json(url: str, *, api_key: str, timeout: float) -> dict[str, Any]:
    """Issue one ``x-api-key``-authenticated GET; return parsed JSON."""
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload: dict[str, Any] = json.loads(r.read().decode("utf-8"))
    return payload


async def fetch_lyzr_voice_options(
    *,
    api_key: str,
    base_url: str = LYZR_VOICE_BASE,
    timeout: float = 5.0,
) -> list[LyzrProvider]:
    """Fetch both Lyzr discovery endpoints concurrently → flat provider list.

    Each GET is offloaded to a thread so the two requests run in parallel without
    pulling an async HTTP client into the base install. Raises
    :class:`urllib.error.URLError` on network failure and ``ValueError`` on a
    non-JSON response — callers can choose to log + skip or fail loudly.
    """
    pipe_url = f"{base_url.rstrip('/')}/v1/config/pipeline-options"
    rt_url = f"{base_url.rstrip('/')}/v1/config/realtime-options"
    pipe, rt = await asyncio.gather(
        asyncio.to_thread(_http_get_json, pipe_url, api_key=api_key, timeout=timeout),
        asyncio.to_thread(_http_get_json, rt_url, api_key=api_key, timeout=timeout),
    )
    return _parse_pipeline(pipe) + _parse_realtime(rt)


def check_parity(
    providers: list[LyzrProvider],
    *,
    mapping: dict[ProviderKind, dict[str, str]] | None = None,
) -> ParityReport:
    """Split a Lyzr provider list into covered / gap buckets.

    A provider is **covered** when the mapping (default
    :data:`LYZR_PROVIDER_MAP`) has a non-empty entry under its ``kind`` /
    ``provider_id``; anything else (including providers entirely absent from
    the mapping) is a gap. Pass a custom ``mapping`` to simulate
    "what if we added an X adapter" without editing the module.
    """
    m = mapping if mapping is not None else LYZR_PROVIDER_MAP
    covered: list[LyzrProvider] = []
    gaps: list[LyzrProvider] = []
    for p in providers:
        adapter = m.get(p.kind, {}).get(p.provider_id, "")
        (covered if adapter else gaps).append(p)
    return ParityReport(covered=tuple(covered), gaps=tuple(gaps))


async def check_lyzr_parity(
    *,
    api_key: str,
    base_url: str = LYZR_VOICE_BASE,
    timeout: float = 5.0,
    mapping: dict[ProviderKind, dict[str, str]] | None = None,
) -> ParityReport:
    """One-call convenience: fetch both menus + run the parity check."""
    providers = await fetch_lyzr_voice_options(api_key=api_key, base_url=base_url, timeout=timeout)
    return check_parity(providers, mapping=mapping)


def format_parity_report(report: ParityReport) -> str:
    """Render a human-readable summary suitable for CLI output or a log line."""
    lines = [
        f"Lyzr voice-provider parity: {len(report.covered)}/{report.total} "
        f"({report.coverage_pct:.0f}%)",
        "",
    ]
    for label, items in (("Covered", report.covered), ("Gaps", report.gaps)):
        if not items:
            continue
        lines.append(f"  {label}:")
        for p in items:
            lines.append(
                f"    [{p.kind:8s}] {p.provider_id:14s} "
                f"({p.display_name}) — {p.model_count} model(s)"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "LYZR_PROVIDER_MAP",
    "LYZR_VOICE_BASE",
    "LyzrProvider",
    "ParityReport",
    "ProviderKind",
    "check_lyzr_parity",
    "check_parity",
    "fetch_lyzr_voice_options",
    "format_parity_report",
]
