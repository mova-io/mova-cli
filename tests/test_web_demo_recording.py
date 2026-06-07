"""Tests for the web-demo call-recording feature (B4).

The module under test lives at ``examples/web_demo/recording.py`` — outside
``src/mdk_voice`` because recording is a demo-level concern (CLAUDE.md rule
6: control plane vs adapter seams). We add it to ``sys.path`` here so the
tests can import it without packaging the demo.
"""

from __future__ import annotations

import io
import struct
import sys
import wave
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make the demo dir importable. The web-demo isn't a Python package; this
# mirrors what server.py does at startup.
_DEMO_DIR = Path(__file__).resolve().parent.parent / "examples" / "web_demo"
sys.path.insert(0, str(_DEMO_DIR))

from recording import (  # noqa: E402 - sys.path tweak above
    CallRecorder,
    is_recording_enabled,
    upload_recording,
)

# ── CallRecorder buffer + WAV encoding ────────────────────────────────────


def test_call_recorder_buffers_separately() -> None:
    """add_caller / add_agent append to the right leg without crosstalk."""
    r = CallRecorder()
    r.add_caller(b"\x01\x02\x03\x04")
    r.add_agent(b"\xaa\xbb")
    r.add_caller(b"\x05\x06")

    assert bytes(r.caller_pcm) == b"\x01\x02\x03\x04\x05\x06"
    assert bytes(r.agent_pcm) == b"\xaa\xbb"


def test_call_recorder_ignores_empty_writes() -> None:
    """Empty frames are a no-op — defensive against zero-length WS payloads."""
    r = CallRecorder()
    r.add_caller(b"")
    r.add_agent(b"")
    assert len(r.caller_pcm) == 0
    assert len(r.agent_pcm) == 0


def test_to_wav_stereo_produces_valid_wav() -> None:
    """The bytes round-trip through stdlib ``wave`` with stereo PCM16 shape."""
    r = CallRecorder()
    # 100 samples of caller (PCM16 = 200 B), 100 samples of agent
    r.add_caller(struct.pack("<100h", *range(100)))
    r.add_agent(struct.pack("<100h", *range(1000, 1100)))

    wav = r.to_wav_stereo()
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"

    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16_000
        # Each "frame" in wave-speak = one sample per channel.
        assert wf.getnframes() == 100
        # The PCM data section is 100 frames * 2 channels * 2 bytes = 400 B.
        frames = wf.readframes(100)
        assert len(frames) == 400
        # Channels are interleaved L,R,L,R — caller=L should be sample[0],
        # agent=R should be sample[1] of the first frame.
        first_l, first_r = struct.unpack("<hh", frames[:4])
        assert first_l == 0  # caller's first sample
        assert first_r == 1000  # agent's first sample


def test_to_wav_stereo_pads_shorter_channel() -> None:
    """When one leg is shorter, the other is zero-padded so both align."""
    r = CallRecorder()
    # caller: 10 samples, agent: 100 samples → agent is 10x longer
    r.add_caller(struct.pack("<10h", *([42] * 10)))
    r.add_agent(struct.pack("<100h", *([99] * 100)))

    wav = r.to_wav_stereo()
    with wave.open(io.BytesIO(wav), "rb") as wf:
        # Must be the LONGER channel's sample count.
        assert wf.getnframes() == 100
        frames = wf.readframes(100)
    # Last frame's caller channel should be the zero pad (caller ran short).
    last_l, last_r = struct.unpack("<hh", frames[-4:])
    assert last_l == 0  # pad sample
    assert last_r == 99  # agent's real sample


def test_to_wav_stereo_empty_recorder() -> None:
    """An empty recorder still emits a structurally valid (zero-frame) WAV."""
    r = CallRecorder()
    wav = r.to_wav_stereo()
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getnframes() == 0


def test_to_wav_stereo_truncates_misaligned_bytes() -> None:
    """A stray odd-byte tail is dropped, not encoded into a corrupt frame."""
    r = CallRecorder()
    r.add_caller(b"\x01\x02\x03")  # 3 bytes = 1.5 samples → truncate to 1
    r.add_agent(b"\xaa\xbb")  # 2 bytes = 1 sample
    wav = r.to_wav_stereo()
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnframes() == 1


def test_recorder_sample_rate_overridable() -> None:
    """A non-16k browser capture rate flows through to the WAV header."""
    r = CallRecorder(sample_rate=48_000)
    wav = r.to_wav_stereo()
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getframerate() == 48_000


# ── is_recording_enabled feature-flag gating ──────────────────────────────


def test_is_recording_enabled_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults to OFF — privacy + cost (no env vars set)."""
    monkeypatch.delenv("RECORD_CALLS", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    assert is_recording_enabled() is False


def test_is_recording_enabled_needs_both_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either env var alone is insufficient — we need the flag AND a destination."""
    monkeypatch.setenv("RECORD_CALLS", "1")
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    assert is_recording_enabled() is False

    monkeypatch.delenv("RECORD_CALLS", raising=False)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=…")
    assert is_recording_enabled() is False


def test_is_recording_enabled_on_when_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both env vars present → recording is on."""
    monkeypatch.setenv("RECORD_CALLS", "1")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=…")
    assert is_recording_enabled() is True


def test_is_recording_enabled_record_calls_must_be_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RECORD_CALLS`` only counts when set to exactly ``"1"`` — avoids
    accidentally enabling on ``RECORD_CALLS=0`` / ``=false`` / etc."""
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "x")
    for bad in ("0", "false", "no", "", "true"):
        monkeypatch.setenv("RECORD_CALLS", bad)
        assert is_recording_enabled() is False, f"unexpected enable for RECORD_CALLS={bad!r}"


# ── upload_recording: mock the BlobServiceClient ───────────────────────────


@pytest.mark.asyncio
async def test_upload_recording_calls_blob_sdk_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserts the SDK is invoked with the expected container/blob/data."""
    captured: dict[str, Any] = {}

    class _FakeBlobClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def upload_blob(self, data: bytes, **kwargs: Any) -> None:
            captured["data"] = data
            captured["upload_kwargs"] = kwargs

    class _FakeServiceClient:
        account_name = "fakeacct"

        def __init__(self) -> None:
            self.credential = MagicMock(account_key="FAKEKEY")

        @classmethod
        def from_connection_string(cls, conn_str: str) -> _FakeServiceClient:
            captured["conn_str"] = conn_str
            return cls()

        def create_container(self, name: str) -> None:
            captured["container_created"] = name

        def get_blob_client(self, *, container: str, blob: str) -> _FakeBlobClient:
            captured["container"] = container
            captured["blob"] = blob
            return _FakeBlobClient(url=f"https://fakeacct.blob.core.windows.net/{container}/{blob}")

    class _FakeContentSettings:
        def __init__(self, *, content_type: str) -> None:
            captured["content_type"] = content_type

    class _FakeSasPerms:
        def __init__(self, *, read: bool) -> None:
            captured["sas_read"] = read

    def _fake_generate_sas(**kwargs: Any) -> str:
        captured["sas_kwargs"] = kwargs
        return "sig=FAKESIG&se=tomorrow"

    # Inject a fake ``azure.storage.blob`` so the lazy import inside
    # upload_recording resolves to our doubles. We use a module-like object
    # so getattr-style imports work.
    fake_mod = MagicMock()
    fake_mod.BlobServiceClient = _FakeServiceClient
    fake_mod.BlobSasPermissions = _FakeSasPerms
    fake_mod.ContentSettings = _FakeContentSettings
    fake_mod.generate_blob_sas = _fake_generate_sas
    monkeypatch.setitem(sys.modules, "azure", MagicMock())
    monkeypatch.setitem(sys.modules, "azure.storage", MagicMock())
    monkeypatch.setitem(sys.modules, "azure.storage.blob", fake_mod)

    wav_bytes = b"RIFF\x00\x00\x00\x00WAVEfmt "
    url = await upload_recording(
        wav_bytes,
        session_id="abc123",
        container="mdk-voice-recordings",
        conn_str="DefaultEndpointsProtocol=https;AccountName=fakeacct;AccountKey=FAKEKEY;",
    )

    assert captured["container"] == "mdk-voice-recordings"
    assert captured["blob"] == "abc123.wav"
    assert captured["container_created"] == "mdk-voice-recordings"
    assert captured["data"] == wav_bytes
    assert captured["content_type"] == "audio/wav"
    assert captured["sas_read"] is True
    # URL is the blob URL + the SAS query string.
    assert url is not None
    assert url.startswith("https://fakeacct.blob.core.windows.net/mdk-voice-recordings/abc123.wav?")
    assert "FAKESIG" in url


@pytest.mark.asyncio
async def test_upload_recording_returns_none_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If azure-storage-blob isn't installed, upload no-ops (returns None).

    We simulate the missing SDK by making the import raise. ``CallRecorder``
    and the WS path must keep working — recording is best-effort.
    """
    # Force the lazy import inside upload_recording to fail.
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("azure.storage.blob") or name == "azure.storage.blob":
            raise ImportError("simulated: azure-storage-blob not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    # Also ensure no cached azure.storage.blob from a prior test bleeds in.
    monkeypatch.delitem(sys.modules, "azure.storage.blob", raising=False)

    url = await upload_recording(
        b"RIFF...",
        session_id="x",
        container="c",
        conn_str="anything",
    )
    assert url is None


@pytest.mark.asyncio
async def test_upload_recording_returns_none_on_upload_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network/SDK exceptions during upload are swallowed → return None.

    Recording must NEVER take the call down with it.
    """

    class _BoomServiceClient:
        @classmethod
        def from_connection_string(cls, _: str) -> _BoomServiceClient:
            raise RuntimeError("simulated blob storage outage")

    fake_mod = MagicMock()
    fake_mod.BlobServiceClient = _BoomServiceClient
    monkeypatch.setitem(sys.modules, "azure", MagicMock())
    monkeypatch.setitem(sys.modules, "azure.storage", MagicMock())
    monkeypatch.setitem(sys.modules, "azure.storage.blob", fake_mod)

    url = await upload_recording(
        b"RIFF...",
        session_id="x",
        container="c",
        conn_str="anything",
    )
    assert url is None
