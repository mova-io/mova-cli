"""Azure Speech adapter conformance + wiring (ADR 048/049, T1 voice pair).

Covers the Azure Speech STT + Azure Neural TTS adapters — one provider, both
directions — the same way ``test_voice_protocols.py`` covers the OpenAI pair,
PLUS the key+region BYOK resolution and the credential/CLI registration that
distinguishes Azure Speech from the single-key LLM providers:

* runtime-checkable Protocol conformance (so a future provider is checked the
  same way ``isinstance(p, BaseLLMProvider)`` works);
* lazy SDK import — constructing the adapters does NOT import
  ``azure.cognitiveservices.speech`` (the suite runs with the SDK uninstalled);
* key+region resolution: per-call ``api_key`` wins over the constructor /
  ``$AZURE_SPEECH_KEY`` default; a missing key OR region is a clear error;
* STT streaming bridge: partial (``recognizing``) → final (``recognized``)
  endpointing, driven by a fake recognizer with NO SDK / NO network;
* TTS: buffered text → raw-PCM audio sliced into chunks; non-completed result
  raises rather than emitting silent audio;
* ``AZURE_SPEECH_KEY`` / ``AZURE_SPEECH_REGION`` autoload from the credentials
  file (and shell still wins);
* ``mdk auth login azure-speech`` is recognized + persists both values.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.voice import (
    AudioChunk,
    AzureNeuralTTS,
    AzureSpeechSTT,
    SpeechToTextProvider,
    TextToSpeechProvider,
)
from movate.voice.azure_speech import _read_synthesis_bytes, _resolve_key_region


async def _audio_stream(*blobs: bytes) -> AsyncIterator[AudioChunk]:
    for b in blobs:
        yield AudioChunk(data=b)


async def _text_stream(*parts: str) -> AsyncIterator[str]:
    for p in parts:
        yield p


# ---------------------------------------------------------------------------
# Protocol conformance + lazy import
# ---------------------------------------------------------------------------


def test_azure_adapters_satisfy_protocols() -> None:
    # Constructing with no factory must NOT import the Azure SDK (lazy).
    assert isinstance(AzureSpeechSTT(api_key="k", region="eastus"), SpeechToTextProvider)
    assert isinstance(AzureNeuralTTS(api_key="k", region="eastus"), TextToSpeechProvider)


def test_constructing_adapters_does_not_import_sdk() -> None:
    # The whole point of the lazy guard: importing + constructing the adapters
    # works with the optional SDK absent. Assert it isn't pulled into sys.modules
    # by construction.
    AzureSpeechSTT(api_key="k", region="eastus")
    AzureNeuralTTS(api_key="k", region="eastus")
    assert "azure.cognitiveservices.speech" not in sys.modules


# ---------------------------------------------------------------------------
# Key + region resolution (ADR 018 BYOK)
# ---------------------------------------------------------------------------


def test_resolve_prefers_per_call_key_over_ctor() -> None:
    key, region = _resolve_key_region(api_key="per-call", ctor_key="ctor", region="eastus")
    assert key == "per-call"
    assert region == "eastus"


def test_resolve_falls_back_to_ctor_key() -> None:
    key, region = _resolve_key_region(api_key=None, ctor_key="ctor", region="westus")
    assert key == "ctor"
    assert region == "westus"


def test_resolve_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="AZURE_SPEECH_KEY"):
        _resolve_key_region(api_key=None, ctor_key=None, region="eastus")


def test_resolve_missing_region_raises() -> None:
    with pytest.raises(ValueError, match="AZURE_SPEECH_REGION"):
        _resolve_key_region(api_key="k", ctor_key=None, region=None)


def test_ctor_defaults_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SPEECH_KEY", "env-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "env-region")
    stt = AzureSpeechSTT()
    assert stt._ctor_key == "env-key"
    assert stt._region == "env-region"


async def test_transcribe_missing_region_raises() -> None:
    # Key present, region missing → ValueError before any SDK touch.
    stt = AzureSpeechSTT(api_key="k", region=None)
    with pytest.raises(ValueError, match="AZURE_SPEECH_REGION"):
        async for _ in stt.transcribe(_audio_stream(b"aa")):
            pass


async def test_synthesize_missing_key_raises() -> None:
    tts = AzureNeuralTTS(api_key=None, region="eastus")
    with pytest.raises(ValueError, match="AZURE_SPEECH_KEY"):
        async for _ in tts.synthesize(_text_stream("hi")):
            pass


# ---------------------------------------------------------------------------
# STT — streaming bridge with a fake recognizer (no SDK, no network)
# ---------------------------------------------------------------------------


class _Evt:
    """Mimics the SDK's event arg shape: ``evt.result.text``."""

    def __init__(self, text: str) -> None:
        self.result = type("R", (), {"text": text})()


class _Signal:
    """Mimics an SDK EventSignal — stores the connected callback."""

    def __init__(self) -> None:
        self._cb = None

    def connect(self, cb) -> None:
        self._cb = cb

    def fire(self, evt) -> None:
        if self._cb is not None:
            self._cb(evt)


class _FakeRecognizer:
    """Scripted continuous-recognition recognizer.

    On ``start_continuous_recognition`` it replays a scripted sequence of
    (signal-name, text) events, then fires ``session_stopped`` — exercising the
    adapter's queue/callback bridge end to end. Records the key/region/language
    it was built with so the test can assert BYOK plumbing."""

    def __init__(self, *, key: str, region: str, language: str | None, script) -> None:
        self.key = key
        self.region = region
        self.language = language
        self._script = script
        self.recognizing = _Signal()
        self.recognized = _Signal()
        self.session_stopped = _Signal()
        self.canceled = _Signal()
        self.stopped = False

    def start_continuous_recognition(self) -> None:
        for signal_name, text in self._script:
            getattr(self, signal_name).fire(_Evt(text))
        self.session_stopped.fire(_Evt(""))

    def stop_continuous_recognition(self) -> None:
        self.stopped = True


async def test_stt_streams_partials_then_final() -> None:
    captured: dict = {}

    def factory(*, key, region, push_stream, language):
        rec = _FakeRecognizer(
            key=key,
            region=region,
            language=language,
            script=[
                ("recognizing", "the"),
                ("recognizing", "the full"),
                ("recognized", "the full utterance"),
            ],
        )
        captured["rec"] = rec
        captured["push_stream"] = push_stream
        return rec

    stt = AzureSpeechSTT(api_key="sub-key", region="eastus", recognizer_factory=factory)
    chunks = [
        c
        async for c in stt.transcribe(
            _audio_stream(b"aa", b"bb"), language="en-US", api_key="byok-key"
        )
    ]

    assert [c.text for c in chunks] == ["the", "the full", "the full utterance"]
    assert [c.is_final for c in chunks] == [False, False, True]
    # Per-call BYOK key won over the constructor's "sub-key".
    assert captured["rec"].key == "byok-key"
    assert captured["rec"].region == "eastus"
    # Azure gets the full BCP-47 form, NOT the bare code (unlike OpenAI/Whisper).
    assert captured["rec"].language == "en-US"
    # Inbound audio was written to the push stream and the stream was closed.
    assert captured["push_stream"].written == [b"aa", b"bb"]
    assert captured["push_stream"].closed is True
    # The recognition session was torn down.
    assert captured["rec"].stopped is True


async def test_stt_empty_recognized_still_emits_final() -> None:
    # A session that endpoints with no text still yields one is_final chunk so a
    # downstream "wait for is_final" loop unblocks rather than hanging.
    def factory(*, key, region, push_stream, language):
        return _FakeRecognizer(
            key=key, region=region, language=language, script=[("recognized", "")]
        )

    stt = AzureSpeechSTT(api_key="k", region="eastus", recognizer_factory=factory)
    chunks = [c async for c in stt.transcribe(_audio_stream(b"x"))]
    assert len(chunks) == 1
    assert chunks[0].is_final is True
    assert chunks[0].text == ""


# ---------------------------------------------------------------------------
# TTS — buffered text → chunked raw-PCM audio with a fake synthesizer
# ---------------------------------------------------------------------------


class _FakeSynthResult:
    def __init__(self, audio: bytes, reason_name: str = "SynthesizingAudioCompleted") -> None:
        self.audio_data = audio
        self.reason = type("Reason", (), {"name": reason_name})()


class _FakeSynthesizer:
    def __init__(self, *, key: str, region: str, voice: str, body: bytes) -> None:
        self.key = key
        self.region = region
        self.voice = voice
        self._body = body
        self.spoken: list[str] = []

    def speak_text(self, text: str) -> _FakeSynthResult:
        self.spoken.append(text)
        return _FakeSynthResult(self._body)


async def test_tts_buffers_text_and_chunks_audio() -> None:
    body = b"\x00\x01" * 2048  # 4096 bytes → multiple frames at 1920/frame
    captured: dict = {}

    def factory(*, key, region, voice):
        synth = _FakeSynthesizer(key=key, region=region, voice=voice, body=body)
        captured["synth"] = synth
        return synth

    tts = AzureNeuralTTS(api_key="sub-key", region="eastus", synthesizer_factory=factory)
    audio = [
        c
        async for c in tts.synthesize(
            _text_stream("hello ", "there"), voice_id="en-US-JennyNeural", api_key="byok-key"
        )
    ]

    assert len(audio) >= 2
    assert b"".join(c.data for c in audio) == body
    assert all(c.codec == "pcm16" for c in audio)
    assert all(c.sample_rate == 24_000 for c in audio)
    # Token stream buffered into ONE synthesis call.
    assert captured["synth"].spoken == ["hello there"]
    # Per-call BYOK key won; the requested voice was passed through.
    assert captured["synth"].key == "byok-key"
    assert captured["synth"].voice == "en-US-JennyNeural"


async def test_tts_blank_text_makes_no_call() -> None:
    called: dict = {"built": False}

    def factory(*, key, region, voice):
        called["built"] = True
        return _FakeSynthesizer(key=key, region=region, voice=voice, body=b"unused")

    tts = AzureNeuralTTS(api_key="k", region="eastus", synthesizer_factory=factory)
    audio = [c async for c in tts.synthesize(_text_stream("   "))]
    assert audio == []
    # Blank text short-circuits BEFORE building a synthesizer (no Azure call).
    assert called["built"] is False


async def test_tts_default_voice_used_when_unset() -> None:
    captured: dict = {}

    def factory(*, key, region, voice):
        captured["voice"] = voice
        return _FakeSynthesizer(key=key, region=region, voice=voice, body=b"\x00" * 16)

    tts = AzureNeuralTTS(api_key="k", region="eastus", synthesizer_factory=factory)
    _ = [c async for c in tts.synthesize(_text_stream("hi"))]
    assert captured["voice"] == "en-US-JennyNeural"


def test_read_synthesis_bytes_accepts_completed() -> None:
    result = _FakeSynthResult(b"pcmbytes", reason_name="SynthesizingAudioCompleted")
    assert _read_synthesis_bytes(result) == b"pcmbytes"


def test_read_synthesis_bytes_raises_on_canceled() -> None:
    class _Canceled:
        audio_data = b""
        reason = type("Reason", (), {"name": "Canceled"})()
        cancellation_details = type("C", (), {"error_details": "bad key", "reason": "x"})()

    with pytest.raises(RuntimeError, match="bad key"):
        _read_synthesis_bytes(_Canceled())


# ---------------------------------------------------------------------------
# Credentials autoload — AZURE_SPEECH_KEY / _REGION (distinct from AZURE_OPENAI)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from movate.credentials.loader import ALL_AUTOLOADED_ENV_VARS  # noqa: PLC0415

    path = tmp_path / "credentials"
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
    for key in ALL_AUTOLOADED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return path


def test_voice_vars_in_autoload_registry() -> None:
    from movate.credentials.loader import (  # noqa: PLC0415
        ALL_AUTOLOADED_ENV_VARS,
        VOICE_KEY_ENV_VARS,
    )

    assert "AZURE_SPEECH_KEY" in VOICE_KEY_ENV_VARS
    assert "AZURE_SPEECH_REGION" in VOICE_KEY_ENV_VARS
    assert set(VOICE_KEY_ENV_VARS) <= set(ALL_AUTOLOADED_ENV_VARS)
    # Distinct from the Azure OpenAI key — different Azure resource.
    assert "AZURE_OPENAI_API_KEY" not in VOICE_KEY_ENV_VARS


def test_azure_speech_key_and_region_autoload(isolated_env: Path) -> None:
    from movate.credentials import CredentialsStore, autoload_credentials  # noqa: PLC0415

    store = CredentialsStore()
    store.set("AZURE_SPEECH_KEY", "file-key")
    store.set("AZURE_SPEECH_REGION", "eastus")
    autoload_credentials()
    assert os.environ["AZURE_SPEECH_KEY"] == "file-key"
    assert os.environ["AZURE_SPEECH_REGION"] == "eastus"


def test_shell_still_wins_over_file_for_voice(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from movate.credentials import CredentialsStore, autoload_credentials  # noqa: PLC0415

    monkeypatch.setenv("AZURE_SPEECH_KEY", "shell-key")
    CredentialsStore().set("AZURE_SPEECH_KEY", "file-key")
    autoload_credentials()
    assert os.environ["AZURE_SPEECH_KEY"] == "shell-key"


# ---------------------------------------------------------------------------
# CLI — `mdk auth login azure-speech` recognized + persists both values
# ---------------------------------------------------------------------------


runner = CliRunner(mix_stderr=False)


def test_auth_login_azure_speech_persists_key_and_region(
    isolated_env: Path,
) -> None:
    from movate.cli.main import app  # noqa: PLC0415
    from movate.credentials import CredentialsStore  # noqa: PLC0415

    # --key supplies the subscription key non-interactively; region is prompted.
    result = runner.invoke(
        app,
        ["auth", "login", "azure-speech", "--key", "sub-key-123"],
        input="eastus\n",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    store = CredentialsStore()
    assert store.get("AZURE_SPEECH_KEY") == "sub-key-123"
    assert store.get("AZURE_SPEECH_REGION") == "eastus"


def test_auth_login_unknown_provider_lists_azure_speech(isolated_env: Path) -> None:
    from movate.cli.main import app  # noqa: PLC0415

    result = runner.invoke(
        app,
        ["auth", "login", "not-a-provider", "--key", "x"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    # The valid-provider list now includes azure-speech.
    assert "azure-speech" in (result.stdout + result.stderr)


def test_auth_login_picker_lists_azure_speech(isolated_env: Path) -> None:
    from movate.cli.main import app  # noqa: PLC0415

    # No provider arg → picker renders; type the azure-speech name to pick it,
    # then supply key + region.
    result = runner.invoke(
        app,
        ["auth", "login"],
        input="azure-speech\nsub-key\neastus\n",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Azure Speech" in result.stdout
