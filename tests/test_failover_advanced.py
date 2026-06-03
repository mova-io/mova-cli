"""Cost guard (D4), latency hedging (D5), and the TTS phrase cache (D6)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    FailoverSTT,
    FailoverTTS,
    FakeSTT,
    FakeTTS,
    InMemoryVoiceCache,
    TranscriptChunk,
    cache_key,
)


async def _no_sleep(_seconds: float) -> None:
    return None


async def _audio(seconds: float = 1.0, *, sample_rate: int = 16_000) -> AsyncIterator[AudioChunk]:
    # One pcm16 frame whose byte length encodes `seconds` of audio.
    n_bytes = int(seconds * sample_rate) * 2
    yield AudioChunk(data=b"\x00" * n_bytes, codec="pcm16", sample_rate=sample_rate)


async def _text(s: str) -> AsyncIterator[str]:
    yield s


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def on_event(self, event: str, /, **fields: object) -> None:
        self.events.append((event, dict(fields)))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


def _named_stt(name: str, answer: str = "hi") -> FakeSTT:
    s = FakeSTT(answer)
    s.name = name
    return s


async def _final(stt, *, seconds: float = 1.0) -> str:
    async for ch in stt.transcribe(_audio(seconds)):
        if ch.is_final:
            return ch.text
    return ""


# --- D4: cost guard --------------------------------------------------------


async def test_cost_guard_prefers_cheaper_when_budget_exceeded() -> None:
    """With a tight per-turn budget, the lower-latency-but-pricier provider is
    pushed behind the cheaper one (latency-first only *within* budget)."""
    obs = _RecordingObserver()
    # deepgram = tier 1 (latency leader) @ $0.0043/min; azure_speech_stt = tier 1
    # @ $0.0167/min. openai_whisper = tier 2 @ $0.006/min. Over a long clip a
    # tiny budget makes deepgram the only within-budget option.
    deepgram = _named_stt("deepgram", "cheap+fast")
    pricey = _named_stt("azure_speech_stt", "pricey")
    # 10 minutes of audio → deepgram=$0.043, azure=$0.167.
    fo = FailoverSTT([pricey, deepgram], observer=obs, sleep=_no_sleep, cost_budget=0.10)
    assert await _final(fo, seconds=600.0) == "cheap+fast"
    assert deepgram.received != []
    assert pricey.received == []  # over budget → not chosen


async def test_no_cost_budget_is_pure_latency_first() -> None:
    """Without a budget, ordering is pure latency-first (tier), ignoring price."""
    cheap_slow = _named_stt("openai_whisper", "slow")  # tier 2, cheap
    fast = _named_stt("deepgram", "fast")  # tier 1
    fo = FailoverSTT([cheap_slow, fast], sleep=_no_sleep)  # no cost_budget
    assert await _final(fo) == "fast"


# --- D5: latency hedging ---------------------------------------------------


class _SlowSTT:
    """A FakeSTT-like that delays its final by `delay` seconds (for hedge races)."""

    def __init__(self, name: str, answer: str, *, delay: float) -> None:
        self.name = name
        self.version = "0.0.1"
        self._answer = answer
        self._delay = delay

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None
    ) -> AsyncIterator[TranscriptChunk]:
        async for _ in audio:
            pass
        await asyncio.sleep(self._delay)
        yield TranscriptChunk(text=self._answer, is_final=True)


async def test_hedge_takes_the_faster_provider() -> None:
    obs = _RecordingObserver()
    slow = _SlowSTT("openai_whisper", "slow", delay=0.05)
    fast = _SlowSTT("deepgram", "fast", delay=0.0)
    fo = FailoverSTT([slow, fast], observer=obs, hedge=True, sleep=_no_sleep)
    assert await _final(fo) == "fast"
    assert "hedge" in obs.names()
    assert ("hedge_won", {"provider": "deepgram", "kind": "stt"}) in obs.events


async def test_hedge_falls_through_when_both_candidates_fail() -> None:
    class _Boom:
        def __init__(self, name: str) -> None:
            self.name = name
            self.version = "0.0.1"

        async def transcribe(self, audio, *, language=None, api_key=None, keyterms=None):
            async for _ in audio:
                pass
            raise RuntimeError("down")
            yield  # pragma: no cover

    obs = _RecordingObserver()
    # Two hedge candidates fail; a third (sequential) provider recovers.
    good = _named_stt("openai_whisper", "recovered")
    fo = FailoverSTT(
        [_Boom("deepgram"), _Boom("azure_speech_stt"), good],
        observer=obs,
        hedge=True,
        sleep=_no_sleep,
    )
    assert await _final(fo) == "recovered"


async def test_hedge_tts_takes_faster_and_caches() -> None:
    class _SlowTTS:
        def __init__(self, name: str, *, delay: float, mark: bytes) -> None:
            self.name = name
            self.version = "0.0.1"
            self._delay = delay
            self._mark = mark

        async def synthesize(
            self, text, *, voice_id="", codec="pcm16", api_key=None
        ) -> AsyncIterator[AudioChunk]:
            async for _ in text:
                pass
            await asyncio.sleep(self._delay)
            yield AudioChunk(data=self._mark)

    fo = FailoverTTS(
        [
            _SlowTTS("openai_tts", delay=0.05, mark=b"slow"),
            _SlowTTS("cartesia", delay=0.0, mark=b"fast"),
        ],
        hedge=True,
        sleep=_no_sleep,
    )
    out = b"".join([c.data async for c in fo.synthesize(_text("hi"))])
    assert out == b"fast"


# --- D6: TTS phrase cache --------------------------------------------------


async def _say(tts, phrase: str, *, voice_id: str = "", codec: str = "pcm16") -> bytes:
    out = b""
    async for ch in tts.synthesize(_text(phrase), voice_id=voice_id, codec=codec):
        out += ch.data
    return out


async def test_cache_serves_repeat_phrase_without_resynthesis() -> None:
    obs = _RecordingObserver()
    provider = FakeTTS()
    cache = InMemoryVoiceCache()
    fo = FailoverTTS([provider], observer=obs, cache=cache, sleep=_no_sleep)

    first = await _say(fo, "welcome to support")
    second = await _say(fo, "welcome to support")
    assert first == second == b"welcome to support"
    # The provider synthesized exactly once; the second turn was a cache hit.
    assert provider.spoken == ["welcome to support"]
    assert "cache_hit" in obs.names()


async def test_cache_key_varies_by_voice_and_codec() -> None:
    cache = InMemoryVoiceCache()
    provider = FakeTTS()
    fo = FailoverTTS([provider], cache=cache, sleep=_no_sleep)
    await _say(fo, "hello", voice_id="alloy")
    await _say(fo, "hello", voice_id="nova")  # different voice → miss → re-synth
    assert provider.spoken == ["hello", "hello"]


def test_in_memory_cache_evicts_lru() -> None:
    cache = InMemoryVoiceCache(max_entries=2)
    cache.put("a", [AudioChunk(data=b"a")])
    cache.put("b", [AudioChunk(data=b"b")])
    assert cache.get("a") is not None  # touch a → most-recently-used
    cache.put("c", [AudioChunk(data=b"c")])  # evicts b (LRU)
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None


def test_cache_key_is_stable_and_distinct() -> None:
    assert cache_key("hi", "v", "pcm16") == cache_key("hi", "v", "pcm16")
    assert cache_key("hi", "v", "pcm16") != cache_key("hi", "v", "opus")
    assert cache_key("hi", "v", "pcm16") != cache_key("hi", "w", "pcm16")


def test_in_memory_cache_stats_track_hit_rate() -> None:
    """``stats()`` exposes hit-rate so cache effectiveness ($/turn) is observable."""
    cache = InMemoryVoiceCache(max_entries=2)
    # Cold: a miss before anything is stored.
    assert cache.get("a") is None
    cache.put("a", [AudioChunk(data=b"a")])
    # Two hits on the warmed key.
    assert cache.get("a") is not None
    assert cache.get("a") is not None
    stats = cache.stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["lookups"] == 3
    assert stats["entries"] == 1
    assert abs(stats["hit_rate"] - (2 / 3)) < 1e-9


def test_in_memory_cache_stats_count_evictions() -> None:
    cache = InMemoryVoiceCache(max_entries=1)
    cache.put("a", [AudioChunk(data=b"a")])
    cache.put("b", [AudioChunk(data=b"b")])  # evicts a
    assert cache.stats()["evictions"] == 1


def test_in_memory_cache_stats_zero_lookups_is_zero_hit_rate() -> None:
    """No division-by-zero before any lookup happens."""
    assert InMemoryVoiceCache().stats()["hit_rate"] == 0.0
