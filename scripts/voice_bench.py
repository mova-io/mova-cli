#!/usr/bin/env python
"""Voice perf baseline — real numbers for the cheap wins + the ADR 070 gate.

    python scripts/voice_bench.py            # full run (TTS + STT, costs ~pennies)
    python scripts/voice_bench.py --n 3      # smaller corpus

What it measures, on *your* keys, end-to-end:

1. **Keyterm WER win** — runs each utterance through Deepgram STT twice (keyterms
   OFF vs ON) and reports word-error-rate for both. Validates perf Win 1
   (nova-3 + keyterm boosting) with a number instead of a hunch.
2. **Endpointing headroom** — the wall-clock gap between the last *partial* whose
   text already equals the final transcript and the *final* (endpointed) chunk.
   That gap is the silence-wait ADR 070's speculative kickoff recovers; it bounds
   the achievable latency win.
3. **Interim-stability proxy** — whether the pre-final stable partial's text
   equals the final text. When it does, a speculation on that interim would have
   *committed* (no waste); when it differs, it would have *cancelled*. This is an
   OPTIMISTIC proxy: TTS audio has cleaner endpointing than human speech, so the
   real cancel ratio will be higher. Treat as a ceiling, not a forecast.

HONESTY NOTE: the corpus is TTS-synthesized speech, not human speech. TTS gives
unrealistically low WER and unrealistically stable endpointing. These numbers are
a *floor* on the keyterm win and a *ceiling* on speculation stability. The real
distribution must come from production telemetry (the demo's latency badge
already records stt_final per turn). This script exists to ground the orders of
magnitude and to be re-runnable in CI, not to replace live measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

from movate.voice import DeepgramSTT, OpenAITTS
from movate.voice.base import AudioChunk
from movate.voice.bench import word_error_rate
from movate.voice.observer import speculation_ab_report

# Enterprise IT-support utterances seeded with the exact vocab the keyterm list
# boosts (VPN/VIP/Okta/MFA/SSO/...). These are the words a general model fumbles.
CORPUS: list[str] = [
    "One of our VIP users says the VPN connects but there is no network access.",
    "The user cannot pass MFA after the Okta SSO migration.",
    "Reset the Active Directory password and re-enroll the device in Azure.",
    "Outlook keeps prompting for credentials even though SSO is configured.",
    "The VPN client drops every time the VIP joins a SharePoint call.",
    "Escalate to tier two: Okta is rejecting the MFA push for this account.",
]

KEYTERMS = [
    "VPN", "VIP", "Okta", "Mova-iO", "Movate", "Lyzr",
    "SSO", "MFA", "Active Directory", "Outlook", "SharePoint", "Azure",
]  # fmt: skip

_SAMPLE_RATE = 24_000
_FRAME_MS = 20  # real-time pacing granularity
_FRAME_BYTES = _SAMPLE_RATE * 2 * _FRAME_MS // 1000  # pcm16 mono
_TRAIL_SILENCE_S = 2.0  # trailing silence so Deepgram endpoints naturally


def _read_key(name: str) -> str | None:
    env = os.environ.get(f"{name.upper()}_API_KEY")
    if env:
        return env
    path = Path.home() / f".mdk_{name}_key"
    return path.read_text().strip() if path.is_file() else None


async def _synthesize(tts: OpenAITTS, text: str, api_key: str | None) -> bytes:
    async def _one() -> AsyncIterator[str]:
        yield text

    parts = [c.data async for c in tts.synthesize(_one(), api_key=api_key)]
    return b"".join(parts)


async def _paced_audio(pcm: bytes) -> AsyncIterator[AudioChunk]:
    """Replay PCM at real time, then trailing silence (so VAD endpoints)."""
    for start in range(0, len(pcm), _FRAME_BYTES):
        yield AudioChunk(data=pcm[start : start + _FRAME_BYTES], sample_rate=_SAMPLE_RATE)
        await asyncio.sleep(_FRAME_MS / 1000.0)
    silence = b"\x00" * _FRAME_BYTES
    for _ in range(int(_TRAIL_SILENCE_S * 1000 / _FRAME_MS)):
        yield AudioChunk(data=silence, sample_rate=_SAMPLE_RATE)
        await asyncio.sleep(_FRAME_MS / 1000.0)


async def _run_once(
    pcm: bytes, reference: str, *, keyterms: list[str] | None, dg_key: str | None
) -> tuple[str, float, float | None, bool]:
    """One STT pass. Returns (final_text, wer, headroom_ms, interim_matched_final)."""
    stt = DeepgramSTT(keyterms=keyterms)
    t0 = time.monotonic()
    last_partial_text = ""
    last_partial_match_ts: float | None = None
    final_text = ""
    final_ts: float | None = None
    async for chunk in stt.transcribe(_paced_audio(pcm), api_key=dg_key):
        now = time.monotonic()
        if chunk.is_final:
            final_text = chunk.text
            final_ts = now
        else:
            last_partial_text = chunk.text
            # Remember when a partial first reached the eventual final text.
            last_partial_match_ts = now if chunk.text else last_partial_match_ts
    # Headroom: gap from the last meaningful partial to the endpointed final.
    headroom_ms: float | None = None
    if final_ts is not None and last_partial_match_ts is not None:
        headroom_ms = max(0.0, (final_ts - last_partial_match_ts) * 1000.0)
    interim_matched = bool(last_partial_text) and (_norm(last_partial_text) == _norm(final_text))
    _ = t0
    return final_text, word_error_rate(reference, final_text), headroom_ms, interim_matched


def _norm(s: str) -> str:
    return " ".join(s.lower().replace(".", "").replace(",", "").split())


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=len(CORPUS), help="number of utterances")
    args = ap.parse_args()

    dg_key = _read_key("deepgram")
    oa_key = _read_key("openai")
    if not dg_key:
        print("FATAL: no Deepgram key (~/.mdk_deepgram_key or DEEPGRAM_API_KEY)")
        return 1
    if not oa_key:
        print("FATAL: no OpenAI key (~/.mdk_openai_key or OPENAI_API_KEY)")
        return 1

    corpus = CORPUS[: args.n]
    tts = OpenAITTS()

    print(f"# Voice perf baseline — n={len(corpus)}, real-time-paced TTS→Deepgram\n")
    print("Synthesizing corpus (OpenAI TTS)...")
    audios = [await _synthesize(tts, text, oa_key) for text in corpus]

    wer_off: list[float] = []
    wer_on: list[float] = []
    headrooms: list[float] = []
    matched = 0

    for i, (text, pcm) in enumerate(zip(corpus, audios, strict=True), 1):
        off_text, off_wer, _, _ = await _run_once(pcm, text, keyterms=None, dg_key=dg_key)
        on_text, on_wer, headroom, interim_ok = await _run_once(
            pcm, text, keyterms=KEYTERMS, dg_key=dg_key
        )
        wer_off.append(off_wer)
        wer_on.append(on_wer)
        if headroom is not None:
            headrooms.append(headroom)
        matched += int(interim_ok)
        print(f"\n[{i}] ref: {text}")
        print(f"    keyterms OFF → WER {off_wer:.0%}  | {off_text}")
        print(f"    keyterms ON  → WER {on_wer:.0%}  | {on_text}")
        if headroom is not None:
            print(f"    endpointing headroom: {round(headroom)}ms  interim==final: {interim_ok}")

    print("\n" + "=" * 64)
    print("## Summary")
    print(f"- Keyterm WER:    OFF {_mean(wer_off):.1%}  →  ON {_mean(wer_on):.1%}")
    print(
        f"- Endpointing headroom (speculation ceiling): "
        f"mean {round(_mean(headrooms))}ms over n={len(headrooms)}"
    )
    print(
        f"- Interim==final (optimistic commit rate): "
        f"{matched}/{len(corpus)} = {matched / len(corpus):.0%}  "
        f"(→ ~{(1 - matched / len(corpus)):.0%} would cancel; HIGHER on human speech)"
    )
    # ADR 073 Phase 1 — run the SAME flip/no-flip verdict logic the live runtime
    # uses, fed by this offline run's proxy snapshot (interim==final → committed,
    # mean headroom → head-start). Live telemetry (MetricsObserver over real
    # turns) is the real input; this is the synthetic ceiling.
    proxy = {
        "started": len(corpus),
        "committed": matched,
        "cancelled": len(corpus) - matched,
        "commit_ratio": matched / len(corpus) if corpus else 0.0,
        "avg_head_start_ms": _mean(headrooms),
    }
    verdict = speculation_ab_report(proxy, min_samples=1)
    print(
        f"- Speculation A/B verdict (offline proxy): "
        f"{verdict.recommendation.upper()} — {verdict.rationale}"
    )
    print(
        "  (offline proxy uses synthetic TTS speech; the binding decision is the "
        "LIVE verdict over real human turns — run the validation runbook.)"
    )
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
