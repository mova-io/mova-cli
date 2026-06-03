"""Tests for the three voice UX improvements (feat/voice-cli-ux).

1. Voice capabilities on GET /api/v1/capabilities — the ``voice`` block.
2. ``mdk voice try`` CLI command — registration, flag parsing, graceful
   error when [voice] is absent, and WS connection mocked.
3. ``mdk voice providers list`` — reads capabilities and renders voice section.
4. VoiceConfig on AgentSpec — additive optional field; existing YAML unchanged.

ADR 048 D5 / ADR 050 D4 / CLAUDE.md rule 5 (additive, flagged).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from click.testing import Result
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import AgentSpec, VoiceConfig
from movate.runtime import build_app
from movate.runtime.capabilities import (
    _configured_stt_providers,
    _configured_tts_providers,
    build_voice_capabilities,
)
from movate.testing import InMemoryStorage


def _help_text(result: Result) -> str:
    """Flatten a Rich-rendered ``--help`` output for CI-robust substring matching.

    Two CI-specific gotchas:

    1. **Narrow-terminal truncation/wrap**: in CI's non-TTY terminal Rich
       renders the options panel too narrow, so flag names wrap or get
       elided. Tests invoke the CLI with ``env={"COLUMNS": "200"}`` to
       force a wide terminal.
    2. **ANSI escapes inside option names**: CI runs with ``FORCE_COLOR=1``,
       so Rich styles ``--`` and the flag name as separate spans — a raw
       substring search misses them. Strip ANSI, then collapse whitespace
       so wrapped/padded flag rows flatten to a single searchable string.
    """
    plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", result.output)
    return " ".join(plain.split())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path, rate_limit_per_minute=60))


@pytest.fixture
async def read_auth(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="voice-ux-test")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


# ---------------------------------------------------------------------------
# Item 2: voice block on GET /api/v1/capabilities
# ---------------------------------------------------------------------------


class TestVoiceCapabilitiesBlock:
    """The ``voice`` block is present and structured correctly."""

    def test_voice_block_present_in_full_view(self, client: TestClient, read_auth) -> None:
        header, _ = read_auth
        body = client.get("/api/v1/capabilities", headers=header).json()
        assert "voice" in body, "voice block missing from full capabilities view"
        voice = body["voice"]
        assert isinstance(voice, dict)
        assert "enabled" in voice
        assert "modes" in voice
        assert "stt_providers" in voice
        assert "tts_providers" in voice

    def test_voice_block_absent_in_minimal_view(self, client: TestClient) -> None:
        body = client.get("/api/v1/capabilities").json()
        assert body["minimal"] is True
        # The voice block is omitted in the minimal (unauthenticated) view — it
        # is either absent or None (the schema allows both).
        voice = body.get("voice")
        assert voice is None, "voice block should be absent/None in minimal view"

    def test_voice_enabled_false_when_no_keys(self, client: TestClient, read_auth) -> None:
        """No voice env vars → enabled=False but block is still present."""
        header, _ = read_auth
        # Ensure no voice keys leak in from the test environment.
        env_patch = {
            "DEEPGRAM_API_KEY": "",
            "CARTESIA_API_KEY": "",
            "OPENAI_API_KEY": "",
            "ELEVENLABS_API_KEY": "",
            "AZURE_SPEECH_KEY": "",
        }
        with patch.dict(os.environ, env_patch):
            body = client.get("/api/v1/capabilities", headers=header).json()
        voice = body["voice"]
        assert voice["enabled"] is False
        assert isinstance(voice["stt_providers"], list)
        assert isinstance(voice["tts_providers"], list)

    def test_voice_enabled_true_when_stt_and_tts_keyed(
        self, storage: InMemoryStorage, agents_path: Path, read_auth
    ) -> None:
        """Setting STT + TTS env vars flips enabled=True."""
        header, _ = read_auth
        app = build_app(storage, agents_path=agents_path, rate_limit_per_minute=60)
        env_patch = {
            "DEEPGRAM_API_KEY": "dg-fake-key",
            "CARTESIA_API_KEY": "ca-fake-key",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            body = TestClient(app).get("/api/v1/capabilities", headers=header).json()
        voice = body["voice"]
        assert voice["enabled"] is True
        assert "deepgram" in voice["stt_providers"]
        assert "cartesia" in voice["tts_providers"]

    def test_voice_modes_pipeline_always_present_when_route_registered(
        self, client: TestClient, read_auth
    ) -> None:
        """``pipeline`` is in modes when the voice WS route is registered."""
        header, _ = read_auth
        body = client.get("/api/v1/capabilities", headers=header).json()
        # The standard build_app registers the voice WS route (voice_stt_factory
        # is set on app.state), so pipeline must be in modes.
        voice = body["voice"]
        assert "pipeline" in voice["modes"]

    def test_voice_modes_realtime_absent_when_not_configured(self, tmp_path: Path) -> None:
        """``realtime`` is NOT in modes when MDK_VOICE_REALTIME is unset.

        Rebuilds the app with the env patched so the build_app factory reads
        the cleared MDK_VOICE_REALTIME.
        """
        import asyncio  # noqa: PLC0415

        from movate.runtime import build_app as _build_app  # noqa: PLC0415

        agents2 = tmp_path / "agents"
        agents2.mkdir()

        async def _make_storage() -> InMemoryStorage:
            s = InMemoryStorage()
            await s.init()
            return s

        # ``asyncio.get_event_loop()`` raises in Python 3.12+ (and on CI
        # under pytest-asyncio strict mode) when no loop is currently
        # running. Create a fresh loop explicitly and run the coroutines
        # against it — pure synchronous helper invocation, no fixture
        # needed.
        loop = asyncio.new_event_loop()
        try:
            storage2 = loop.run_until_complete(_make_storage())
            with patch.dict(os.environ, {"MDK_VOICE_REALTIME": ""}, clear=False):
                app2 = _build_app(storage2, agents_path=agents2, rate_limit_per_minute=60)
                tenant_id = uuid4().hex
                minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="rt-test")
                loop.run_until_complete(storage2.save_api_key(minted.record))
                hdr = {"Authorization": f"Bearer {minted.full_key}"}
                body = TestClient(app2).get("/api/v1/capabilities", headers=hdr).json()
        finally:
            loop.close()
        assert "realtime" not in body["voice"]["modes"]


# ---------------------------------------------------------------------------
# Unit tests for voice capability detection helpers
# ---------------------------------------------------------------------------


class TestVoiceCapabilityDetection:
    """Unit tests for the build_voice_capabilities() helper."""

    def test_configured_stt_providers_empty_when_no_keys(self) -> None:
        env = {
            "DEEPGRAM_API_KEY": "",
            "OPENAI_API_KEY": "",
            "AZURE_SPEECH_KEY": "",
        }
        with patch.dict(os.environ, env):
            providers = _configured_stt_providers()
        assert providers == []

    def test_configured_stt_providers_deepgram_when_key_set(self) -> None:
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "dg-fake", "OPENAI_API_KEY": ""}):
            providers = _configured_stt_providers()
        assert "deepgram" in providers

    def test_configured_tts_providers_cartesia_when_key_set(self) -> None:
        with patch.dict(os.environ, {"CARTESIA_API_KEY": "ca-fake"}):
            providers = _configured_tts_providers()
        assert "cartesia" in providers

    def test_build_voice_capabilities_enabled_true(self) -> None:
        """enabled=True when route is registered + at least 1 STT + 1 TTS key."""
        app = MagicMock()
        app.state.voice_stt_factory = MagicMock()  # route registered
        app.state.voice_realtime_factory = None
        with patch.dict(
            os.environ,
            {"DEEPGRAM_API_KEY": "dg-fake", "CARTESIA_API_KEY": "ca-fake"},
            clear=False,
        ):
            vc = build_voice_capabilities(app)
        assert vc.enabled is True
        assert "pipeline" in vc.modes
        assert "realtime" not in vc.modes
        assert "deepgram" in vc.stt_providers
        assert "cartesia" in vc.tts_providers

    def test_build_voice_capabilities_enabled_false_no_route(self) -> None:
        """enabled=False when voice_stt_factory is None (mdk[voice] not installed)."""
        app = MagicMock()
        app.state.voice_stt_factory = None
        app.state.voice_realtime_factory = None
        with patch.dict(
            os.environ,
            {"DEEPGRAM_API_KEY": "dg-fake", "CARTESIA_API_KEY": "ca-fake"},
            clear=False,
        ):
            vc = build_voice_capabilities(app)
        assert vc.enabled is False
        assert vc.modes == []  # route not registered → no modes

    def test_build_voice_capabilities_realtime_in_modes(self) -> None:
        app = MagicMock()
        app.state.voice_stt_factory = MagicMock()
        app.state.voice_realtime_factory = MagicMock()
        with patch.dict(
            os.environ,
            {"DEEPGRAM_API_KEY": "dg-fake", "CARTESIA_API_KEY": "ca-fake"},
            clear=False,
        ):
            vc = build_voice_capabilities(app)
        assert "realtime" in vc.modes

    def test_providers_sorted(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPGRAM_API_KEY": "dg",
                "OPENAI_API_KEY": "oai",
                "AZURE_SPEECH_KEY": "az",
            },
            clear=False,
        ):
            stt = _configured_stt_providers()
        assert stt == sorted(stt)


# ---------------------------------------------------------------------------
# Item 4 (optional): VoiceConfig on AgentSpec
# ---------------------------------------------------------------------------


class TestVoiceConfigOnAgentSpec:
    """VoiceConfig is an additive optional field on AgentSpec."""

    def _minimal_spec(self, **extra: Any) -> dict[str, Any]:
        """Minimal valid agent.yaml dict.

        Uses the ``schema`` alias (``SchemaPaths`` field) with valid string
        paths (None is not accepted).
        """
        from movate.core.models import ModelConfig  # noqa: PLC0415

        return {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": "test-agent",
            "version": "0.1.0",
            "model": ModelConfig(provider="openai/gpt-4o"),
            "prompt": "./prompt.md",
            "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
            **extra,
        }

    def test_voice_field_absent_is_valid(self) -> None:
        """Every existing agent.yaml (no voice block) loads unchanged."""
        spec = AgentSpec(**self._minimal_spec())
        assert spec.voice is None

    def test_voice_field_present_and_parsed(self) -> None:
        """A voice block parses correctly into a VoiceConfig."""
        spec = AgentSpec(
            **self._minimal_spec(
                voice={
                    "enabled": True,
                    "mode": "pipeline",
                    "stt": "deepgram",
                    "tts": "cartesia",
                    "voice_id": "rachel",
                    "language": "en-US",
                }
            )
        )
        assert spec.voice is not None
        assert spec.voice.enabled is True
        assert spec.voice.mode == "pipeline"
        assert spec.voice.stt == "deepgram"
        assert spec.voice.tts == "cartesia"
        assert spec.voice.voice_id == "rachel"
        assert spec.voice.language == "en-US"

    def test_voice_config_defaults(self) -> None:
        """VoiceConfig with no fields uses sensible defaults."""
        vc = VoiceConfig()
        assert vc.enabled is False
        assert vc.mode == "pipeline"
        assert vc.stt is None
        assert vc.tts is None
        assert vc.voice_id == ""
        assert vc.language is None

    def test_voice_config_partial_override(self) -> None:
        """An author can override just the TTS provider."""
        spec = AgentSpec(**self._minimal_spec(voice={"tts": "elevenlabs", "voice_id": "nova"}))
        assert spec.voice is not None
        assert spec.voice.tts == "elevenlabs"
        assert spec.voice.voice_id == "nova"
        assert spec.voice.stt is None  # not overridden → tenant default
        assert spec.voice.mode == "pipeline"  # default

    def test_realtime_mode_accepted(self) -> None:
        spec = AgentSpec(**self._minimal_spec(voice={"mode": "realtime"}))
        assert spec.voice is not None
        assert spec.voice.mode == "realtime"


# ---------------------------------------------------------------------------
# Item 1: mdk voice try — CLI registration and flag parsing
# ---------------------------------------------------------------------------


class TestVoiceTryCLI:
    """``mdk voice try`` is registered and flags parse correctly."""

    def test_voice_group_registered(self) -> None:
        """``mdk voice`` appears as a command group on the top-level app."""
        from movate.cli.main import app  # noqa: PLC0415

        runner = CliRunner()
        result = runner.invoke(app, ["voice", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        plain = _help_text(result)
        assert "try" in plain
        assert "providers" in plain

    def test_voice_try_help(self) -> None:
        from movate.cli.main import app  # noqa: PLC0415

        runner = CliRunner()
        result = runner.invoke(app, ["voice", "try", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        plain = _help_text(result)
        assert "--mode" in plain
        assert "--stt" in plain
        assert "--tts" in plain
        assert "--target" in plain
        assert "--api-key" in plain

    def test_voice_providers_list_help(self) -> None:
        from movate.cli.main import app  # noqa: PLC0415

        runner = CliRunner()
        result = runner.invoke(
            app, ["voice", "providers", "list", "--help"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.output
        assert "--target" in _help_text(result)

    def test_voice_try_invalid_mode(self) -> None:
        """Unknown --mode exits with an error before touching the network."""
        from movate.cli.voice_cmd import voice_app  # noqa: PLC0415

        runner = CliRunner()
        result = runner.invoke(
            voice_app,
            ["try", "my-agent", "--mode", "invalid"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "invalid" in result.output.lower() or "mode" in result.output.lower()

    def test_voice_providers_list_renders_voice_block(self) -> None:
        """``mdk voice providers list`` renders the voice section from capabilities."""

        from movate.cli.voice_cmd import voice_app  # noqa: PLC0415

        caps_response = {
            "mdk_version": "2026.5.30.1",
            "api_version": "v1",
            "voice": {
                "enabled": True,
                "modes": ["pipeline"],
                "stt_providers": ["deepgram"],
                "tts_providers": ["cartesia"],
            },
        }

        class _FakeClient:
            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *a: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> MagicMock:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = caps_response
                return resp

        runner = CliRunner()
        with patch("httpx.AsyncClient", return_value=_FakeClient()):
            result = runner.invoke(voice_app, ["providers", "list"])
        assert result.exit_code == 0, result.output
        assert "enabled" in result.output or "deepgram" in result.output

    def test_voice_providers_list_not_configured(self) -> None:
        """``mdk voice providers list`` shows a hint when voice is disabled."""
        from movate.cli.voice_cmd import voice_app  # noqa: PLC0415

        caps_response = {
            "mdk_version": "2026.5.30.1",
            "api_version": "v1",
            "voice": {
                "enabled": False,
                "modes": [],
                "stt_providers": [],
                "tts_providers": [],
            },
        }

        class _FakeClient:
            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *a: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> MagicMock:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = caps_response
                return resp

        runner = CliRunner()
        with patch("httpx.AsyncClient", return_value=_FakeClient()):
            result = runner.invoke(voice_app, ["providers", "list"])
        assert result.exit_code == 0, result.output
        # Must tell the operator to set keys.
        assert "not configured" in result.output or "DEEPGRAM" in result.output
