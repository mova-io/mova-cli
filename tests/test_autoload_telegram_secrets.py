"""Regression test for the Telegram-autoload gap.

Before this fix, ``autoload_credentials()`` only loaded keys in
``PROVIDER_KEY_ENV_VARS`` (the LLM providers). Telegram secrets
written via ``mdk auth login telegram`` landed in the credentials
file but never made it into ``os.environ`` on subsequent invocations
— breaking both ``mdk deploy --notify`` AND the auth-picker
``✓ configured`` marker for the Telegram row.

These tests cover the bundle of env vars now autoloaded
(``ALL_AUTOLOADED_ENV_VARS``):

* Provider keys still autoload (no regression on PR #66 behavior).
* Telegram + webhook secrets ALSO autoload now.
* The auth picker's ``✓ configured`` marker fires for Telegram when
  both secrets are set via the credentials file alone.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.credentials import CredentialsStore, autoload_credentials
from movate.credentials.loader import (
    ALL_AUTOLOADED_ENV_VARS,
    NOTIFICATION_KEY_ENV_VARS,
    OBSERVABILITY_KEY_ENV_VARS,
    PROVIDER_KEY_ENV_VARS,
    TEMPORAL_KEY_ENV_VARS,
    VOICE_KEY_ENV_VARS,
)

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the credentials store at a tempfile + strip every
    autoloaded env var so each test starts from a known-clean state."""
    path = tmp_path / "credentials"
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
    for key in ALL_AUTOLOADED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return path


# ---------------------------------------------------------------------------
# Constants — make sure the registry includes what we expect
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoloadedRegistry:
    def test_notification_env_vars_include_telegram_and_webhook(self) -> None:
        assert "TELEGRAM_BOT_TOKEN" in NOTIFICATION_KEY_ENV_VARS
        assert "TELEGRAM_CHAT_ID" in NOTIFICATION_KEY_ENV_VARS
        assert "MOVATE_DEPLOY_WEBHOOK" in NOTIFICATION_KEY_ENV_VARS

    def test_all_autoloaded_is_union(self) -> None:
        """ALL_AUTOLOADED_ENV_VARS should be the union of the provider,
        notification, observability, voice, and temporal groups. Catches
        accidental dropping when any list is edited in isolation."""
        expected = (
            set(PROVIDER_KEY_ENV_VARS)
            | set(NOTIFICATION_KEY_ENV_VARS)
            | set(OBSERVABILITY_KEY_ENV_VARS)
            | set(VOICE_KEY_ENV_VARS)
            | set(TEMPORAL_KEY_ENV_VARS)
        )
        assert set(ALL_AUTOLOADED_ENV_VARS) == expected

    def test_voice_env_vars_include_deepgram_and_cartesia(self) -> None:
        assert "DEEPGRAM_API_KEY" in VOICE_KEY_ENV_VARS
        assert "CARTESIA_API_KEY" in VOICE_KEY_ENV_VARS

    def test_voice_env_vars_include_elevenlabs(self) -> None:
        # T2 premium-voice TTS (ADR 048/049) autoloads the same way.
        assert "ELEVENLABS_API_KEY" in VOICE_KEY_ENV_VARS

    def test_temporal_env_vars_include_host_namespace_and_tls(self) -> None:
        # Temporal connection (ADR 054) autoloads the same way: host +
        # namespace required, TLS cert/key optional (Temporal Cloud only).
        assert "TEMPORAL_HOST" in TEMPORAL_KEY_ENV_VARS
        assert "TEMPORAL_NAMESPACE" in TEMPORAL_KEY_ENV_VARS
        assert "TEMPORAL_TLS_CERT" in TEMPORAL_KEY_ENV_VARS
        assert "TEMPORAL_TLS_KEY" in TEMPORAL_KEY_ENV_VARS


# ---------------------------------------------------------------------------
# Autoload: Telegram secrets get picked up from the credentials file
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoloadTelegram:
    def test_telegram_bot_token_autoloads(self, isolated_env: Path) -> None:
        CredentialsStore().set("TELEGRAM_BOT_TOKEN", "fake-token")
        assert os.environ.get("TELEGRAM_BOT_TOKEN") is None
        autoload_credentials()
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "fake-token"

    def test_telegram_chat_id_autoloads(self, isolated_env: Path) -> None:
        CredentialsStore().set("TELEGRAM_CHAT_ID", "12345")
        autoload_credentials()
        assert os.environ["TELEGRAM_CHAT_ID"] == "12345"

    def test_webhook_url_autoloads(self, isolated_env: Path) -> None:
        CredentialsStore().set("MOVATE_DEPLOY_WEBHOOK", "https://hooks.example.com")
        autoload_credentials()
        assert os.environ["MOVATE_DEPLOY_WEBHOOK"] == "https://hooks.example.com"

    def test_shell_env_still_wins_over_file(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same precedence as provider keys — shell beats file."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "shell-token")
        CredentialsStore().set("TELEGRAM_BOT_TOKEN", "file-token")
        autoload_credentials()
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "shell-token"

    def test_provider_keys_still_autoload(self, isolated_env: Path) -> None:
        """Don't regress PR #66 — provider keys must still load."""
        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        autoload_credentials()
        assert os.environ["OPENAI_API_KEY"] == "sk-test"


# ---------------------------------------------------------------------------
# Autoload: voice provider keys get picked up from the credentials file
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoloadVoiceKeys:
    """Deepgram/Cartesia/ElevenLabs keys autoload from the credentials file
    the same way the LLM provider keys do (so operators set them once via
    `mdk auth login deepgram`/`cartesia`/`elevenlabs` and never re-export)."""

    def test_deepgram_key_autoloads(self, isolated_env: Path) -> None:
        CredentialsStore().set("DEEPGRAM_API_KEY", "dg-test")
        assert os.environ.get("DEEPGRAM_API_KEY") is None
        autoload_credentials()
        assert os.environ["DEEPGRAM_API_KEY"] == "dg-test"

    def test_cartesia_key_autoloads(self, isolated_env: Path) -> None:
        CredentialsStore().set("CARTESIA_API_KEY", "ct-test")
        autoload_credentials()
        assert os.environ["CARTESIA_API_KEY"] == "ct-test"

    def test_elevenlabs_key_autoloads(self, isolated_env: Path) -> None:
        CredentialsStore().set("ELEVENLABS_API_KEY", "el-test")
        assert os.environ.get("ELEVENLABS_API_KEY") is None
        autoload_credentials()
        assert os.environ["ELEVENLABS_API_KEY"] == "el-test"

    def test_shell_env_still_wins_over_file(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same never-clobber precedence as the LLM provider keys."""
        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-shell")
        CredentialsStore().set("DEEPGRAM_API_KEY", "dg-file")
        autoload_credentials()
        assert os.environ["DEEPGRAM_API_KEY"] == "dg-shell"


# ---------------------------------------------------------------------------
# End-to-end: `mdk auth login deepgram`/`cartesia`/`elevenlabs` are recognized
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "env_var"),
    [
        ("deepgram", "DEEPGRAM_API_KEY"),
        ("cartesia", "CARTESIA_API_KEY"),
        ("elevenlabs", "ELEVENLABS_API_KEY"),
    ],
)
def test_auth_login_voice_provider_recognized(
    isolated_env: Path, provider: str, env_var: str
) -> None:
    """`mdk auth login deepgram`/`cartesia`/`elevenlabs` is a recognized provider and
    writes its key to the credentials file (not an 'unknown provider' error).
    --no-verify because voice providers have no live-verify probe wired."""
    result = runner.invoke(
        app,
        ["auth", "login", provider, "--key", f"{provider}-key", "--no-verify"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert CredentialsStore().get(env_var) == f"{provider}-key"


# ---------------------------------------------------------------------------
# End-to-end: `mdk auth login telegram` → restart → picker shows ✓
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_telegram_picker_marker_after_credentials_file_save(
    isolated_env: Path,
) -> None:
    """Full round-trip: write telegram secrets to the credentials
    file (same as `mdk auth login telegram --no-verify`), then
    autoload, then render the picker — Telegram row should show
    the configured marker.

    Pre-fix this failed because autoload skipped the Telegram secrets,
    so ``_provider_is_configured("telegram")`` saw them as unset.

    Note: as of 2026-05-19 the marker text changed from
    ``✓ configured`` to ``✓ verified`` for LLM providers, but
    Telegram still gets ``✓ verified`` (no live-verify probe — its
    API isn't an LLM one, just confirmed-set-in-env). Assert against
    the new text.
    """
    store = CredentialsStore()
    store.set("TELEGRAM_BOT_TOKEN", "fake-token")
    store.set("TELEGRAM_CHAT_ID", "12345")
    autoload_credentials()

    # Now render the picker by invoking `mdk auth login` with no arg.
    # Pipe "1\n" so the picker returns OpenAI without hanging.
    result = runner.invoke(
        app,
        ["auth", "login", "--key", "sk-test", "--no-verify"],
        input="1\n",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    telegram_lines = [line for line in result.stdout.splitlines() if "Telegram" in line]
    assert telegram_lines
    # The marker fires: telegram row shows the new ``✓ verified`` text.
    assert any("verified" in line.lower() for line in telegram_lines), (
        f"Telegram row should be marked verified after autoload: {telegram_lines}"
    )
