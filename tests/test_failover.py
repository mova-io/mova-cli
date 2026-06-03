"""The resilient failover composites (ADR 068 D1/D2/D3/D7)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from movate.voice import (
    AudioChunk,
    FailoverSTT,
    FailoverTTS,
    FakeSTT,
    FakeTTS,
    TranscriptChunk,
    VoiceFailureType,
    VoiceProviderError,
)


async def _no_sleep(_seconds: float) -> None:
    return None


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"frame")


async def _text(s: str) -> AsyncIterator[str]:
    yield s


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def on_event(self, event: str, /, **fields: object) -> None:
        self.events.append((event, dict(fields)))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


# --- STT doubles -----------------------------------------------------------


class _FailingSTT:
    def __init__(self, name: str = "failing_stt", *, exc: Exception | None = None) -> None:
        self.name = name
        self.version = "0.0.1"
        self.exc = exc or RuntimeError("provider down")
        self.calls = 0

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None
    ) -> AsyncIterator[TranscriptChunk]:
        self.calls += 1
        async for _ in audio:
            pass
        raise self.exc
        yield  # pragma: no cover - makes this an async generator


class _FlakySTT:
    """Fails its first ``fail_first`` calls, then succeeds (for retry tests)."""

    def __init__(self, answer: str = "ok", *, fail_first: int = 1) -> None:
        self.name = "flaky_stt"
        self.version = "0.0.1"
        self._answer = answer
        self._fail_first = fail_first
        self.calls = 0

    async def transcribe(
        self, audio, *, language=None, api_key=None, keyterms=None
    ) -> AsyncIterator[TranscriptChunk]:
        self.calls += 1
        async for _ in audio:
            pass
        if self.calls <= self._fail_first:
            raise RuntimeError("429 rate limit")
        yield TranscriptChunk(text=self._answer, is_final=True)


def _named_stt(name: str, answer: str = "hi") -> FakeSTT:
    s = FakeSTT(answer)
    s.name = name  # shadow the class attr so the manifest lookup keys off it
    return s


async def _final_text(stt) -> str:
    async for ch in stt.transcribe(_audio()):
        if ch.is_final:
            return ch.text
    return ""


# --- STT tests -------------------------------------------------------------


async def test_failover_stt_uses_single_provider_on_success() -> None:
    obs = _RecordingObserver()
    fo = FailoverSTT([FakeSTT("hello")], observer=obs, sleep=_no_sleep)
    assert await _final_text(fo) == "hello"
    assert ("provider_selected", {"provider": "fake_stt", "kind": "stt"}) in obs.events


async def test_failover_stt_fails_over_to_next_provider() -> None:
    obs = _RecordingObserver()
    bad = _FailingSTT()
    good = FakeSTT("recovered")
    fo = FailoverSTT([bad, good], observer=obs, sleep=_no_sleep)
    assert await _final_text(fo) == "recovered"
    assert bad.calls >= 1
    assert "failover" in obs.names()
    assert obs.events[-1][0] == "provider_selected"


async def test_failover_stt_auth_is_terminal_no_failover() -> None:
    bad = _FailingSTT(exc=VoiceProviderError("bad key", failure_type=VoiceFailureType.AUTH))
    good = FakeSTT("never reached")
    fo = FailoverSTT([bad, good], sleep=_no_sleep)
    with pytest.raises(VoiceProviderError):
        await _final_text(fo)
    assert good.received == []  # the second provider was never tried


async def test_failover_stt_retries_same_provider_then_succeeds() -> None:
    obs = _RecordingObserver()
    flaky = _FlakySTT("done", fail_first=1)
    fo = FailoverSTT([flaky], observer=obs, sleep=_no_sleep)
    assert await _final_text(fo) == "done"
    assert flaky.calls == 2  # failed once, retried, succeeded
    assert "retry" in obs.names()


async def test_failover_stt_orders_latency_first_by_manifest() -> None:
    obs = _RecordingObserver()
    # Given in cost-tier order, the tier-1 (deepgram) must still be chosen first.
    slow = _named_stt("openai_whisper", "slow")
    fast = _named_stt("deepgram", "fast")
    fo = FailoverSTT([slow, fast], observer=obs, sleep=_no_sleep)
    assert await _final_text(fo) == "fast"
    assert fast.received != []
    assert slow.received == []  # the slower provider was never invoked
    assert ("provider_selected", {"provider": "deepgram", "kind": "stt"}) in obs.events


async def test_failover_stt_emits_circuit_open_then_recovers() -> None:
    obs = _RecordingObserver()
    bad = _FailingSTT()
    good = FakeSTT("ok")
    fo = FailoverSTT([bad, good], observer=obs, breaker_threshold=1, sleep=_no_sleep)
    await _final_text(fo)
    assert ("circuit_open", {"provider": "failing_stt"}) in obs.events


async def test_failover_stt_all_providers_fail_raises() -> None:
    fo = FailoverSTT([_FailingSTT("a"), _FailingSTT("b")], sleep=_no_sleep)
    with pytest.raises(RuntimeError):
        await _final_text(fo)


# --- TTS doubles + tests ---------------------------------------------------


class _FailingTTS:
    def __init__(self, name: str = "failing_tts", *, exc: Exception | None = None) -> None:
        self.name = name
        self.version = "0.0.1"
        self.exc = exc or RuntimeError("tts down")
        self.calls = 0

    async def synthesize(
        self, text, *, voice_id="", codec="pcm16", api_key=None
    ) -> AsyncIterator[AudioChunk]:
        self.calls += 1
        async for _ in text:
            pass
        raise self.exc
        yield  # pragma: no cover


class _MidStreamFailTTS:
    """Yields one audio frame, then fails — must NOT trigger failover (committed)."""

    def __init__(self) -> None:
        self.name = "midstream_tts"
        self.version = "0.0.1"

    async def synthesize(
        self, text, *, voice_id="", codec="pcm16", api_key=None
    ) -> AsyncIterator[AudioChunk]:
        async for _ in text:
            pass
        yield AudioChunk(data=b"partial")
        raise RuntimeError("died mid-stream")


async def _collect_audio(tts) -> bytes:
    out = b""
    async for ch in tts.synthesize(_text("speak this")):
        out += ch.data
    return out


async def test_failover_tts_fails_over_to_next_provider() -> None:
    obs = _RecordingObserver()
    fo = FailoverTTS([_FailingTTS(), FakeTTS()], observer=obs, sleep=_no_sleep)
    assert await _collect_audio(fo) == b"speak this"
    assert "failover" in obs.names()


async def test_failover_tts_no_failover_after_audio_started() -> None:
    # Once the first provider emitted audio, a mid-stream failure is re-raised
    # rather than re-synthesized on another provider (no double audio).
    good = FakeTTS()
    fo = FailoverTTS([_MidStreamFailTTS(), good], sleep=_no_sleep)
    with pytest.raises(RuntimeError):
        await _collect_audio(fo)
    assert good.spoken == []  # the fallback was never invoked


async def test_failover_tts_auth_is_terminal() -> None:
    good = FakeTTS()
    fo = FailoverTTS(
        [_FailingTTS(exc=VoiceProviderError("bad", failure_type=VoiceFailureType.AUTH)), good],
        sleep=_no_sleep,
    )
    with pytest.raises(VoiceProviderError):
        await _collect_audio(fo)
    assert good.spoken == []


def test_failover_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError):
        FailoverSTT([])
