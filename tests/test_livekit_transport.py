"""LiveKit transport — mock-based unit tests (ADR 074 Phase 3a).

These exercise the shipped ``movate.voice.transports.livekit.LiveKitTransport``
(which does ``from livekit import rtc`` lazily) by injecting a fake ``livekit``
module — so they run in CI WITHOUT the heavy ``[telephony]`` extra installed and
validate the real transport code, not a stand-in.

Coverage: the ``TelephonyTransport`` Protocol conformance, the connect /
publish / disconnect lifecycle against main's ``session_config`` contract
(``livekit_url`` + ``token`` + ``room_name``), and the LiveKit credential
autoload wiring.
"""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

from movate.credentials.loader import (
    ALL_AUTOLOADED_ENV_VARS,
    TELEPHONY_KEY_ENV_VARS,
    VOICE_KEY_ENV_VARS,
)
from movate.voice.base import AudioChunk
from movate.voice.transports.base import AudioStream, TelephonyTransport

# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class _DummyTransport:
    """Minimal TelephonyTransport for Protocol conformance check."""

    name = "dummy"

    async def connect(self, session_config: dict) -> AudioStream:
        async def _gen() -> AsyncIterator[AudioChunk]:
            yield AudioChunk(data=b"\x00\x00", codec="pcm16", sample_rate=16_000)

        return _gen()

    async def publish(self, audio: AudioChunk) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def test_dummy_satisfies_telephony_transport_protocol() -> None:
    """A minimal implementation is a runtime-checkable TelephonyTransport."""
    assert isinstance(_DummyTransport(), TelephonyTransport)


def test_protocol_rejects_non_transport() -> None:
    """A bare object does not satisfy TelephonyTransport."""

    class _Not:
        pass

    assert not isinstance(_Not(), TelephonyTransport)


# ---------------------------------------------------------------------------
# Fake ``livekit.rtc`` — matches exactly what main's livekit.py references.
# ---------------------------------------------------------------------------


class _FakeAudioSource:
    def __init__(self, sample_rate: int = 16_000, num_channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.captured: list[Any] = []

    async def capture_frame(self, frame: Any) -> None:
        self.captured.append(frame)


class _FakeAudioFrame:
    def __init__(
        self, *, data: bytes, sample_rate: int, num_channels: int, samples_per_channel: int
    ) -> None:
        self.data = data
        self.sample_rate = sample_rate


class _FakeLocalAudioTrack:
    @staticmethod
    def create_audio_track(name: str, source: Any) -> _FakeLocalAudioTrack:
        return _FakeLocalAudioTrack()


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish_track(self, track: Any, options: Any) -> None:
        self.published.append(track)


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()
        self._handlers: dict[str, Any] = {}
        self.connected = False

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    async def connect(self, url: str, token: str) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


def _install_fake_livekit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake ``livekit`` package exposing the ``rtc`` names main uses."""
    rtc = types.ModuleType("livekit.rtc")
    rtc.Room = _FakeRoom
    rtc.AudioSource = _FakeAudioSource
    rtc.AudioFrame = _FakeAudioFrame
    rtc.AudioStream = object
    rtc.LocalAudioTrack = _FakeLocalAudioTrack
    rtc.TrackPublishOptions = lambda **kw: object()

    class _TrackSource:
        SOURCE_MICROPHONE = "mic"

    class _TrackKind:
        KIND_AUDIO = "audio"

    rtc.TrackSource = _TrackSource
    rtc.TrackKind = _TrackKind

    livekit_mod = types.ModuleType("livekit")
    livekit_mod.rtc = rtc
    monkeypatch.setitem(sys.modules, "livekit", livekit_mod)
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)


_SESSION = {
    "livekit_url": "wss://test.livekit.cloud",
    "token": "tok_test",  # main's connect() reads the access token under `token`
    "room_name": "test-room",
}


# ---------------------------------------------------------------------------
# LiveKitTransport — lifecycle against main's interface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_livekit_transport_connect_and_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect joins the room (publishing a local track); disconnect tears down."""
    _install_fake_livekit(monkeypatch)
    from movate.voice.transports.livekit import LiveKitTransport  # noqa: PLC0415

    t = LiveKitTransport()
    audio_in = await t.connect(_SESSION)
    assert isinstance(audio_in, AsyncIterator)
    assert t._room is not None
    assert t._room.connected is True
    assert t._room.local_participant.published, "should publish a local audio track"

    await t.disconnect()
    assert t._room is None


@pytest.mark.asyncio
async def test_livekit_transport_publish_sends_to_audio_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """publish captures a frame on the LiveKit AudioSource."""
    _install_fake_livekit(monkeypatch)
    from movate.voice.transports.livekit import LiveKitTransport  # noqa: PLC0415

    t = LiveKitTransport()
    await t.connect(_SESSION)
    await t.publish(AudioChunk(data=b"\x00\x01" * 160, codec="pcm16", sample_rate=16_000))
    assert t._audio_source is not None
    assert t._audio_source.captured, "publish should capture a frame on the audio source"


@pytest.mark.asyncio
async def test_livekit_transport_disconnect_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect is safe to call twice and after a fresh construct."""
    _install_fake_livekit(monkeypatch)
    from movate.voice.transports.livekit import LiveKitTransport  # noqa: PLC0415

    t = LiveKitTransport()
    await t.disconnect()  # never connected — no-op, no raise
    await t.connect(_SESSION)
    await t.disconnect()
    await t.disconnect()  # second call — still safe


@pytest.mark.asyncio
async def test_livekit_transport_publish_noop_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """publish before connect (no audio source) is a silent no-op, not a crash."""
    _install_fake_livekit(monkeypatch)
    from movate.voice.transports.livekit import LiveKitTransport  # noqa: PLC0415

    t = LiveKitTransport()
    # No connect() → no audio source → publish must be a no-op.
    await t.publish(AudioChunk(data=b"\x00\x01" * 160, codec="pcm16", sample_rate=16_000))


# ---------------------------------------------------------------------------
# Credential autoload
# ---------------------------------------------------------------------------


def test_telephony_env_vars_in_autoload_whitelist() -> None:
    """LIVEKIT_* env vars are included in the credentials autoload whitelist."""
    assert "LIVEKIT_URL" in ALL_AUTOLOADED_ENV_VARS
    assert "LIVEKIT_API_KEY" in ALL_AUTOLOADED_ENV_VARS
    assert "LIVEKIT_API_SECRET" in ALL_AUTOLOADED_ENV_VARS


def test_telephony_env_vars_separate_from_voice() -> None:
    """LIVEKIT_* vars are in TELEPHONY_KEY_ENV_VARS, not VOICE_KEY_ENV_VARS."""
    for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        assert var in TELEPHONY_KEY_ENV_VARS
        assert var not in VOICE_KEY_ENV_VARS
