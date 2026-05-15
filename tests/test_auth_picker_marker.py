"""Picker installed-marker: `mdk auth login` (no arg) shows a green
✓ next to providers that are already configured.

Visual cue for operators: at a glance which providers are wired and
which still need setup. Matches the `mdk add --list` `✓ installed`
pattern from Bundle F.

Telegram is special: needs BOTH TELEGRAM_BOT_TOKEN AND TELEGRAM_CHAT_ID
to be set before the marker fires.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.auth import _provider_is_configured
from movate.cli.main import app
from movate.credentials import PROVIDER_KEY_ENV_VARS, CredentialsStore, autoload_credentials

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same shape as test_credentials_store.py's fixture."""
    path = tmp_path / "credentials"
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
    for key in PROVIDER_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MOVATE_DEPLOY_WEBHOOK"):
        monkeypatch.delenv(key, raising=False)
    return path


# ---------------------------------------------------------------------------
# Helper: _provider_is_configured
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderIsConfigured:
    def test_unset_provider_returns_false(self, isolated_creds: Path) -> None:
        assert _provider_is_configured("openai") is False
        assert _provider_is_configured("anthropic") is False

    def test_set_provider_returns_true(self, isolated_creds: Path) -> None:
        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        autoload_credentials()
        assert _provider_is_configured("openai") is True

    def test_telegram_needs_both_secrets(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram marker requires BOTH the bot token AND chat ID."""
        # Only one set → still "not configured".
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
        autoload_credentials()
        assert _provider_is_configured("telegram") is False

        # Add the other → now "configured".
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        autoload_credentials()
        assert _provider_is_configured("telegram") is True

    def test_unknown_provider_returns_false(self, isolated_creds: Path) -> None:
        """Defensive: unknown provider name returns False, never raises."""
        assert _provider_is_configured("madeup-provider") is False


# ---------------------------------------------------------------------------
# Picker shows the marker in the rendered list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPickerMarker:
    def test_picker_renders_marker_for_configured_provider(self, isolated_creds: Path) -> None:
        # Configure OpenAI before showing the picker.
        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        autoload_credentials()
        # Invoke `mdk auth login` with no provider; pipe "1\n" so the
        # picker can return without hanging on the API-key prompt.
        result = runner.invoke(
            app,
            ["auth", "login", "--key", "sk-new", "--no-verify"],
            input="1\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # The OpenAI row shows the green check.
        openai_lines = [
            line for line in result.stdout.splitlines() if "OpenAI" in line and "openai" in line
        ]
        assert openai_lines, "expected OpenAI row in picker output"
        # The marker text appears on that row.
        assert any("configured" in line.lower() for line in openai_lines), (
            f"expected ✓ configured marker on OpenAI row: {openai_lines}"
        )

    def test_picker_omits_marker_for_unconfigured(self, isolated_creds: Path) -> None:
        """Anthropic isn't set — its row should NOT show the marker."""
        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        autoload_credentials()
        result = runner.invoke(
            app,
            ["auth", "login", "--key", "sk-new", "--no-verify"],
            input="1\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        anthropic_lines = [line for line in result.stdout.splitlines() if "Anthropic" in line]
        assert anthropic_lines
        for line in anthropic_lines:
            assert "configured" not in line.lower(), (
                f"Anthropic row should NOT be marked configured: {line}"
            )

    def test_picker_marks_telegram_only_with_both_secrets(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram-specific: marker requires BOTH token + chat_id.
        Set just one — picker should NOT show the marker for telegram."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
        # TELEGRAM_CHAT_ID intentionally unset.
        autoload_credentials()
        result = runner.invoke(
            app,
            ["auth", "login", "openai", "--key", "sk-test", "--no-verify"],
            env={"COLUMNS": "200"},
        )
        # That run skipped the picker (explicit provider). Now invoke
        # without an arg to render the picker.
        result = runner.invoke(
            app,
            ["auth", "login", "--key", "sk-test", "--no-verify"],
            input="1\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        telegram_lines = [line for line in result.stdout.splitlines() if "Telegram" in line]
        assert telegram_lines
        for line in telegram_lines:
            assert "configured" not in line.lower(), (
                f"Telegram should require both secrets to mark configured: {line}"
            )
