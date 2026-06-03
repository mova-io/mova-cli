"""TTS phrase cache (ADR 068 D6).

Synthesizing the *same* phrase repeatedly — greetings, disclaimers, IVR prompts,
canned answers — is wasted cost and latency. The cache stores synthesized audio
keyed by ``(text, voice_id, codec)`` so a repeat phrase is served from memory
instead of re-synthesized: a deterministic cost + latency win.

The cache is a small Protocol so an embedder can plug in Redis / blob storage
without changing the router; :class:`InMemoryVoiceCache` is the zero-config
default (a bounded dict). A pinned voice-model version is part of the key the
caller passes in, so a voice change naturally misses (ADR 068 D6 / ADR 049 D3).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from movate.voice.base import AudioChunk, AudioCodec

if TYPE_CHECKING:
    from movate.voice.base import TextToSpeechProvider

_WS = re.compile(r"\s+")


def cache_key(text: str, voice_id: str, codec: str, *, normalize: bool = True) -> str:
    """The canonical cache key for one synthesized phrase.

    With ``normalize`` (default), the text is lower-cased and its whitespace
    collapsed so near-identical phrases ("Hello   World" / "hello world") share a
    cache entry — more hits, lower cost. Voice and codec are always exact (they
    change the audio). Set ``normalize=False`` for byte-exact keying.
    """
    key_text = _WS.sub(" ", text.strip().lower()) if normalize else text
    return f"{codec}\x1f{voice_id}\x1f{key_text}"


@runtime_checkable
class VoiceCache(Protocol):
    """Stores/serves the audio frames synthesized for a phrase key."""

    def get(self, key: str) -> list[AudioChunk] | None: ...

    def put(self, key: str, audio: list[AudioChunk]) -> None: ...


class InMemoryVoiceCache:
    """A bounded, in-process LRU cache (the zero-config default).

    ``max_entries`` caps memory; the least-recently-used phrase is evicted when
    full. Suitable for a single worker; an embedder serving many workers should
    plug a shared :class:`VoiceCache` (Redis/blob) instead.
    """

    def __init__(self, *, max_entries: int = 256) -> None:
        self._max = max(1, max_entries)
        self._store: OrderedDict[str, list[AudioChunk]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> list[AudioChunk] | None:
        if key not in self._store:
            self._misses += 1
            return None
        self._hits += 1
        self._store.move_to_end(key)  # mark recently used
        return list(self._store[key])

    def put(self, key: str, audio: list[AudioChunk]) -> None:
        self._store[key] = list(audio)
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)  # evict LRU
            self._evictions += 1

    def stats(self) -> dict[str, float | int]:
        """A snapshot of cache effectiveness (cost-observability, ADR 068 D7).

        ``hit_rate`` is the fraction of lookups served from memory — the headline
        for "is the phrase cache earning its keep?". Every cache hit is one TTS
        synthesis NOT paid for, so a rising hit-rate is a falling $/turn. Wire
        this into a :class:`~movate.voice.observer.VoiceObserver` or a dashboard
        to watch it live. Pure read — calling it never perturbs the cache.
        """
        lookups = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "lookups": lookups,
            "entries": len(self._store),
            "evictions": self._evictions,
            "hit_rate": (self._hits / lookups) if lookups else 0.0,
        }


async def warm_cache(
    cache: VoiceCache,
    tts: TextToSpeechProvider,
    phrases: Iterable[str],
    *,
    voice_id: str = "",
    codec: AudioCodec = "pcm16",
    api_key: str | None = None,
) -> int:
    """Pre-synthesize common ``phrases`` into ``cache`` (deploy-time warming).

    Greetings, disclaimers, IVR prompts, hold messages — synthesize them once up
    front so the first *live* caller already hits the cache. Returns the number
    of phrases warmed. Safe to re-run (idempotent on the same key).
    """
    warmed = 0
    for phrase in phrases:
        if not phrase.strip():
            continue

        async def _one(_text: str = phrase) -> AsyncIterator[str]:
            yield _text

        audio = [
            chunk
            async for chunk in tts.synthesize(
                _one(), voice_id=voice_id, codec=codec, api_key=api_key
            )
        ]
        cache.put(cache_key(phrase, voice_id, codec), audio)
        warmed += 1
    return warmed
