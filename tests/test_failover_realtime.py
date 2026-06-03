"""Open-time failover for the realtime seam (ADR 068 D1 / realtime MVP)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from movate.voice import (
    AudioChunk,
    FailoverRealtime,
    FakeRealtime,
    RealtimeChunk,
    VoiceFailureType,
    VoiceProviderError,
)


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"frame")


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def on_event(self, event: str, /, **fields: object) -> None:
        self.events.append((event, dict(fields)))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


class _RaisingRealtime:
    """Fails at session open (before any output)."""

    def __init__(self, name: str = "openai_realtime", *, exc: Exception | None = None) -> None:
        self.name = name
        self.version = "0.0.1"
        self._exc = exc or RuntimeError("realtime down")

    async def session(self, audio_in, **kwargs) -> AsyncIterator[RealtimeChunk]:
        async for _ in audio_in:
            break
        raise self._exc
        yield  # pragma: no cover


class _ErrorChunkRealtime:
    """Emits an error chunk before any usable output (an open-time failure)."""

    name = "azure_openai_realtime"
    version = "0.0.1"

    async def session(self, audio_in, **kwargs) -> AsyncIterator[RealtimeChunk]:
        yield RealtimeChunk(kind="error", message="bad session", code="boom")


async def _collect(gen) -> list[RealtimeChunk]:
    return [c async for c in gen]


async def test_realtime_happy_path_single_provider() -> None:
    obs = _RecordingObserver()
    fo = FailoverRealtime([FakeRealtime(answer="hi there")], observer=obs)
    chunks = await _collect(fo.session(_audio()))
    kinds = [c.kind for c in chunks]
    assert "audio" in kinds
    assert kinds[-1] == "response_done"
    assert "provider_selected" in obs.names()


async def test_realtime_fails_over_on_open_error() -> None:
    obs = _RecordingObserver()
    fo = FailoverRealtime([_RaisingRealtime(), FakeRealtime(answer="recovered")], observer=obs)
    chunks = await _collect(fo.session(_audio()))
    audio = b"".join(c.audio.data for c in chunks if c.kind == "audio" and c.audio)
    assert audio.decode("utf-8") == "recovered"
    assert "failover" in obs.names()


async def test_realtime_fails_over_on_error_chunk() -> None:
    fo = FailoverRealtime([_ErrorChunkRealtime(), FakeRealtime(answer="ok")])
    chunks = await _collect(fo.session(_audio()))
    # The pre-output error chunk was NOT forwarded; the fallback served instead.
    assert all(c.kind != "error" for c in chunks)
    assert any(c.kind == "audio" for c in chunks)


async def test_realtime_auth_is_terminal() -> None:
    bad = _RaisingRealtime(exc=VoiceProviderError("bad key", failure_type=VoiceFailureType.AUTH))
    fo = FailoverRealtime([bad, FakeRealtime()])
    with pytest.raises(VoiceProviderError):
        await _collect(fo.session(_audio()))


async def test_realtime_all_providers_fail_raises() -> None:
    fo = FailoverRealtime([_RaisingRealtime("a"), _RaisingRealtime("b")])
    with pytest.raises(RuntimeError):
        await _collect(fo.session(_audio()))
