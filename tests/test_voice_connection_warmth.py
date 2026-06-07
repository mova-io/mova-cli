"""STT connection warmth (ADR 073 Phase 5).

DeepgramSTT used to construct a fresh ``DeepgramClient`` (and its connector /
DNS / TLS) on every ``transcribe`` call. These tests pin the fix: the client is
reused per resolved key across turns, ``warm()`` pre-populates it for turn 1, and
the composites forward ``warm`` to their inner provider(s).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from movate.voice import FailoverSTT, SilenceGatedSTT, warm_stt
from movate.voice.base import AudioChunk, TranscriptChunk
from movate.voice.deepgram import DeepgramSTT


class _CountingDeepgramModule:
    """Fake ``deepgram`` module whose ``DeepgramClient`` counts constructions."""

    def __init__(self) -> None:
        self.constructions = 0

    def DeepgramClient(self, key: str) -> object:  # noqa: N802 - mirrors SDK name
        self.constructions += 1
        return object()


@pytest.fixture
def counting_dg(monkeypatch) -> _CountingDeepgramModule:
    mod = _CountingDeepgramModule()
    monkeypatch.setattr("movate.voice.deepgram._require_deepgram", lambda: mod)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    return mod


def test_client_reused_across_calls(counting_dg) -> None:
    """Default reuse_client=True → the client is constructed once, then cached."""
    stt = DeepgramSTT()
    c1 = stt._resolve_client(None)
    c2 = stt._resolve_client(None)
    assert c1 is c2
    assert counting_dg.constructions == 1


def test_reuse_disabled_constructs_each_call(counting_dg) -> None:
    stt = DeepgramSTT(reuse_client=False)
    stt._resolve_client(None)
    stt._resolve_client(None)
    assert counting_dg.constructions == 2


def test_cache_is_keyed_by_api_key(counting_dg) -> None:
    """Different BYOK keys get their own cached client."""
    stt = DeepgramSTT()
    a1 = stt._resolve_client("key-a")
    a2 = stt._resolve_client("key-a")
    b = stt._resolve_client("key-b")
    assert a1 is a2
    assert b is not a1
    assert counting_dg.constructions == 2


async def test_warm_prepopulates_cache(counting_dg) -> None:
    stt = DeepgramSTT()
    assert await stt.warm() is True
    assert counting_dg.constructions == 1
    # The first real resolve now hits the cache — no second construction.
    stt._resolve_client(None)
    assert counting_dg.constructions == 1


async def test_warm_noop_with_injected_client() -> None:
    """An injected (test/double) client means warming doesn't apply."""
    stt = DeepgramSTT(client=object())
    assert await stt.warm() is False


async def test_warm_noop_when_reuse_disabled(counting_dg) -> None:
    stt = DeepgramSTT(reuse_client=False)
    assert await stt.warm() is False
    assert counting_dg.constructions == 0


# ---------------------------------------------------------------------------
# Composite forwarding — warm() reaches the inner adapter through the chain.
# ---------------------------------------------------------------------------


class _WarmableSTT:
    """Minimal STT double that records whether warm() was called."""

    name = "warmable"
    version = "0"

    def __init__(self) -> None:
        self.warmed_with: list[str | None] = []

    async def warm(self, api_key: str | None = None) -> bool:
        self.warmed_with.append(api_key)
        return True

    async def transcribe(
        self, audio: AsyncIterator[AudioChunk], **_: object
    ) -> AsyncIterator[TranscriptChunk]:
        async for _chunk in audio:
            pass
        yield TranscriptChunk(text="", is_final=True)


async def test_warm_stt_helper_skips_unsupported() -> None:
    class _NoWarm:
        name = "nowarm"

    assert await warm_stt(_NoWarm(), "k") is False


async def test_silence_gated_forwards_warm() -> None:
    inner = _WarmableSTT()
    gated = SilenceGatedSTT(inner)
    assert await gated.warm("k") is True
    assert inner.warmed_with == ["k"]


async def test_failover_warms_every_provider() -> None:
    p1, p2 = _WarmableSTT(), _WarmableSTT()
    fo = FailoverSTT([p1, p2])
    assert await fo.warm("k") is True
    assert p1.warmed_with == ["k"]
    assert p2.warmed_with == ["k"]
