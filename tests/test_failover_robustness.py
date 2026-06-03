"""Router robustness: call timeouts, bounded audio buffer, language routing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from movate.voice import AudioChunk, FailoverSTT, FakeSTT, TranscriptChunk, VoiceManifest
from movate.voice import manifest as manifest_mod


async def _no_sleep(_seconds: float) -> None:
    return None


async def _audio(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def on_event(self, event: str, /, **fields: object) -> None:
        self.events.append((event, dict(fields)))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


def _named_stt(name: str, answer: str) -> FakeSTT:
    s = FakeSTT(answer)
    s.name = name
    return s


async def _final(stt, *, language=None, blobs=(b"x",)) -> str:
    async for ch in stt.transcribe(_audio(*blobs), language=language):
        if ch.is_final:
            return ch.text
    return ""


# --- call timeout ----------------------------------------------------------


class _HangingSTT:
    """Connects but never produces a chunk (a hung provider)."""

    name = "hanging_stt"
    version = "0.0.1"

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None
    ) -> AsyncIterator[TranscriptChunk]:
        async for _ in audio:
            pass
        await asyncio.sleep(10)  # hang well past the timeout
        yield TranscriptChunk(text="never", is_final=True)  # pragma: no cover


async def test_call_timeout_fails_over_from_a_hung_provider() -> None:
    obs = _RecordingObserver()
    fo = FailoverSTT(
        [_HangingSTT(), FakeSTT("recovered")],
        observer=obs,
        call_timeout=0.02,
        sleep=_no_sleep,
    )
    assert await _final(fo) == "recovered"
    assert "failover" in obs.names()


async def test_connect_timeout_fails_over_when_first_chunk_is_late() -> None:
    # call_timeout disabled; only the connect (first-chunk) guard trips.
    fo = FailoverSTT(
        [_HangingSTT(), FakeSTT("recovered")],
        connect_timeout=0.02,
        call_timeout=None,
        sleep=_no_sleep,
    )
    assert await _final(fo) == "recovered"


async def test_jitter_does_not_break_retry() -> None:
    # A flaky provider that fails once then succeeds; jitter must not change the
    # outcome (sleep is a no-op so timing is irrelevant, only correctness).
    class _Flaky:
        name = "flaky"
        version = "0.0.1"

        def __init__(self) -> None:
            self.calls = 0

        async def transcribe(self, audio, *, language=None, api_key=None, keyterms=None):
            self.calls += 1
            async for _ in audio:
                pass
            if self.calls == 1:
                raise RuntimeError("429 rate limit")
            yield TranscriptChunk(text="ok", is_final=True)

    flaky = _Flaky()
    fo = FailoverSTT([flaky], jitter=0.5, sleep=_no_sleep)
    assert await _final(fo) == "ok"
    assert flaky.calls == 2


# --- bounded audio buffer --------------------------------------------------


async def test_audio_buffer_is_capped_and_emits_truncation() -> None:
    obs = _RecordingObserver()
    fo = FailoverSTT([FakeSTT("ok")], observer=obs, max_audio_bytes=10, sleep=_no_sleep)
    # Three 8-byte frames = 24 bytes > 10 → buffering stops early, event emitted.
    result = await _final(fo, blobs=(b"abcdefgh", b"ijklmnop", b"qrstuvwx"))
    assert result == "ok"
    assert "audio_truncated" in obs.names()


# --- language-aware routing ------------------------------------------------


async def test_language_routing_prefers_supporting_provider(monkeypatch) -> None:
    monkeypatch.setitem(
        manifest_mod.DEFAULT_MANIFESTS,
        "en_only",
        VoiceManifest("en_only", "stt", latency_tier=1, languages=("en",)),
    )
    monkeypatch.setitem(
        manifest_mod.DEFAULT_MANIFESTS,
        "es_only",
        VoiceManifest("es_only", "stt", latency_tier=1, languages=("es",)),
    )
    english = _named_stt("en_only", "english")
    spanish = _named_stt("es_only", "spanish")

    # English provider listed first, but a Spanish turn must route to es_only.
    fo = FailoverSTT([english, spanish], sleep=_no_sleep)
    assert await _final(fo, language="es-MX") == "spanish"
    assert spanish.received != []
    assert english.received == []  # wrong language → not tried

    # And an English turn routes to en_only.
    english2 = _named_stt("en_only", "english")
    spanish2 = _named_stt("es_only", "spanish")
    fo2 = FailoverSTT([spanish2, english2], sleep=_no_sleep)
    assert await _final(fo2, language="en-US") == "english"


async def test_unknown_language_does_not_penalize_unannotated_providers() -> None:
    # FakeSTT has no manifest languages → "any"; a language hint must not break it.
    fo = FailoverSTT([FakeSTT("fine")], sleep=_no_sleep)
    assert await _final(fo, language="zh-CN") == "fine"
