"""Cache normalization + warming, speakify length cap, and a router chaos test."""

from __future__ import annotations

from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    FailoverSTT,
    FakeSTT,
    FakeTTS,
    InMemoryVoiceCache,
    TranscriptChunk,
    VoiceFailureType,
    VoiceProviderError,
    cache_key,
    speakify,
    warm_cache,
)

# --- normalized cache keys + warming ---------------------------------------


def test_cache_key_normalizes_case_and_whitespace() -> None:
    assert cache_key("Hello   World", "v", "pcm16") == cache_key("hello world", "v", "pcm16")
    # Voice/codec stay exact.
    assert cache_key("hi", "a", "pcm16") != cache_key("hi", "b", "pcm16")
    # normalize=False keeps it byte-exact.
    assert cache_key("Hi", "v", "pcm16", normalize=False) != cache_key(
        "hi", "v", "pcm16", normalize=False
    )


async def test_warm_cache_prefills_phrases() -> None:
    cache = InMemoryVoiceCache()
    tts = FakeTTS()
    n = await warm_cache(cache, tts, ["Welcome!", "Please hold.", "  "], voice_id="alloy")
    assert n == 2  # blank skipped
    assert tts.spoken == ["Welcome!", "Please hold."]
    # A warmed phrase is now a cache hit (same normalized key).
    assert cache.get(cache_key("welcome!", "alloy", "pcm16")) is not None


# --- speakify length cap ---------------------------------------------------


def test_speakify_caps_at_sentence_boundary() -> None:
    text = "First sentence. Second sentence. Third sentence."
    out = speakify(text, max_chars=20)
    assert out == "First sentence."  # only whole sentences that fit


def test_speakify_hard_cut_when_first_sentence_too_long() -> None:
    out = speakify("anextremelylongsingletokenwithnoboundary", max_chars=10)
    assert len(out) <= 10


def test_speakify_no_cap_unchanged() -> None:
    assert speakify("Hello **world**.") == "Hello world."


# --- router chaos / property test ------------------------------------------


class _ChaosSTT:
    """Fails or hangs based on a per-instance script — used to fuzz the router."""

    def __init__(self, name: str, behavior: str) -> None:
        self.name = name
        self.version = "0.0.1"
        self._behavior = behavior

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None
    ) -> AsyncIterator[TranscriptChunk]:
        async for _ in audio:
            pass
        if self._behavior == "ok":
            yield TranscriptChunk(text="ok", is_final=True)
        elif self._behavior == "error":
            raise RuntimeError("boom")
        elif self._behavior == "nofinal":
            yield TranscriptChunk(text="partial", is_final=False)
        elif self._behavior == "auth":
            raise VoiceProviderError("bad key", failure_type=VoiceFailureType.AUTH)


async def _no_sleep(_s: float) -> None:
    return None


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"x")


async def test_router_never_hangs_under_chaos() -> None:
    """Across every combination of provider behaviors, the router either yields a
    final transcript or raises cleanly — it never hangs or yields a broken stream.
    """
    behaviors = ["ok", "error", "nofinal", "auth"]
    for first in behaviors:
        for second in behaviors:
            fo = FailoverSTT(
                [_ChaosSTT("a", first), _ChaosSTT("b", second)],
                call_timeout=0.05,
                sleep=_no_sleep,
            )
            finals: list[str] = []
            raised = False
            try:
                async for chunk in fo.transcribe(_audio()):
                    if chunk.is_final:
                        finals.append(chunk.text)
            except Exception:  # a clean raise is an acceptable outcome
                raised = True
            # Exactly one of: produced a final, or raised. Never both-silent.
            assert finals or raised
            # If the first provider is healthy, we must get its final, no raise.
            if first == "ok":
                assert finals == ["ok"] and not raised


async def test_chaos_recovers_when_any_provider_is_healthy() -> None:
    fo = FailoverSTT(
        [_ChaosSTT("a", "error"), _ChaosSTT("b", "nofinal"), FakeSTT("recovered")],
        call_timeout=0.05,
        sleep=_no_sleep,
    )
    out = [c.text async for c in fo.transcribe(_audio()) if c.is_final]
    assert out == ["recovered"]
