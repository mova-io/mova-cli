"""PR B — `~/.movate/credentials` machine-global API key store.

Covers:

1. **`CredentialsStore`** — read / get / set / delete; mode 0600;
   atomic write; idempotent re-write.
2. **`autoload_credentials`** — fills unset env vars from the file;
   doesn't clobber existing shell / dotenv values.
3. **`key_source`** — attributes each env var to its origin (shell /
   dotenv / credentials_file / unset).
4. **`mdk auth login`** — CLI prompt path, --key flag, --no-verify,
   --save-to project / global, unknown provider rejection.
5. **`mdk auth status`** — renders the table + greppable summary line.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.credentials import (
    PROVIDER_KEY_ENV_VARS,
    CredentialsStore,
    autoload_credentials,
    key_source,
    verify_provider_key,
)
from movate.credentials.verify import VerifyResult

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the credentials store at a tempfile + strip all provider
    env vars so each test starts from a known-clean state."""
    path = tmp_path / "credentials"
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(path))
    for key in PROVIDER_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return path


# ---------------------------------------------------------------------------
# CredentialsStore — read/write/delete/atomicity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCredentialsStore:
    def test_read_missing_file_returns_empty(self, isolated_creds: Path) -> None:
        assert CredentialsStore().read() == {}

    def test_set_and_read_round_trip(self, isolated_creds: Path) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-test-123")
        assert store.read() == {"OPENAI_API_KEY": "sk-test-123"}
        assert store.get("OPENAI_API_KEY") == "sk-test-123"

    def test_set_preserves_other_keys(self, isolated_creds: Path) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-1")
        store.set("ANTHROPIC_API_KEY", "ant-1")
        assert store.read() == {
            "OPENAI_API_KEY": "sk-1",
            "ANTHROPIC_API_KEY": "ant-1",
        }

    def test_set_updates_existing(self, isolated_creds: Path) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-old")
        store.set("OPENAI_API_KEY", "sk-new")
        assert store.get("OPENAI_API_KEY") == "sk-new"

    def test_delete_returns_true_when_present(
        self, isolated_creds: Path
    ) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-x")
        assert store.delete("OPENAI_API_KEY") is True
        assert store.get("OPENAI_API_KEY") is None

    def test_delete_returns_false_when_missing(
        self, isolated_creds: Path
    ) -> None:
        assert CredentialsStore().delete("OPENAI_API_KEY") is False

    def test_file_is_mode_0600(self, isolated_creds: Path) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-x")
        mode = isolated_creds.stat().st_mode & 0o777
        assert mode == 0o600, (
            f"credentials file should be 0600, got {oct(mode)}"
        )

    def test_comments_in_file_are_skipped(
        self, isolated_creds: Path
    ) -> None:
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text(
            "# comment\n"
            "OPENAI_API_KEY=sk-x\n"
            "# another comment\n"
            "\n"
            "ANTHROPIC_API_KEY=ant-y\n"
        )
        store = CredentialsStore()
        assert store.read() == {
            "OPENAI_API_KEY": "sk-x",
            "ANTHROPIC_API_KEY": "ant-y",
        }

    def test_quoted_values_are_unquoted(self, isolated_creds: Path) -> None:
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text('OPENAI_API_KEY="sk-quoted"\n')
        assert CredentialsStore().read() == {"OPENAI_API_KEY": "sk-quoted"}


# ---------------------------------------------------------------------------
# autoload_credentials — fills unset env vars; doesn't clobber set ones
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoload:
    def test_loads_into_unset_env_var(
        self, isolated_creds: Path
    ) -> None:
        CredentialsStore().set("OPENAI_API_KEY", "sk-from-file")
        assert os.environ.get("OPENAI_API_KEY") is None
        autoload_credentials()
        assert os.environ["OPENAI_API_KEY"] == "sk-from-file"

    def test_does_not_clobber_set_env_var(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shell-set value wins — file shouldn't overwrite it."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")
        CredentialsStore().set("OPENAI_API_KEY", "sk-from-file")
        autoload_credentials()
        assert os.environ["OPENAI_API_KEY"] == "sk-from-shell"

    def test_missing_file_is_noop(self, isolated_creds: Path) -> None:
        # File doesn't exist — autoload must not raise.
        autoload_credentials()
        assert os.environ.get("OPENAI_API_KEY") is None


# ---------------------------------------------------------------------------
# key_source — attribute origin per env var
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeySource:
    def test_unset_when_no_value(self, isolated_creds: Path) -> None:
        assert key_source("OPENAI_API_KEY") == "unset"

    def test_credentials_file_attribution(
        self, isolated_creds: Path
    ) -> None:
        CredentialsStore().set("OPENAI_API_KEY", "sk-x")
        autoload_credentials()
        assert key_source("OPENAI_API_KEY") == "credentials_file"

    def test_shell_attribution(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A value set in os.environ that doesn't match any source
        file gets attributed to the shell."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell-only")
        assert key_source("OPENAI_API_KEY") == "shell"


# ---------------------------------------------------------------------------
# verify_provider_key — provider verification stubs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerify:
    def test_unknown_provider_skips_verification(self) -> None:
        result = verify_provider_key("madeup-provider", "x")
        assert result.ok is True
        assert "not wired" in result.detail.lower()

    def test_openai_200_returns_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx  # noqa: PLC0415

        class _Resp:
            status_code = 200
            text = "ok"

            def json(self) -> dict:
                return {"data": [{"id": "gpt-4"}, {"id": "gpt-4o-mini"}]}

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
        result = verify_provider_key("openai", "sk-test")
        assert result.ok is True
        assert "2 models" in result.detail

    def test_openai_401_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx  # noqa: PLC0415

        class _Resp:
            status_code = 401
            text = "Unauthorized"

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
        result = verify_provider_key("openai", "sk-bad")
        assert result.ok is False
        assert "401" in result.detail

    def test_network_error_flagged_separately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Connectivity failures should set network_error=True so the
        caller can save the key anyway (offline-setup scenario)."""
        import httpx  # noqa: PLC0415

        def boom(*args: object, **kwargs: object) -> object:
            raise httpx.HTTPError("connection refused")

        monkeypatch.setattr(httpx, "get", boom)
        result = verify_provider_key("openai", "sk-test")
        assert result.ok is False
        assert result.network_error is True


# ---------------------------------------------------------------------------
# `mdk auth login` end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthLogin:
    def test_login_with_key_flag_and_no_verify_writes_file(
        self, isolated_creds: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "auth",
                "login",
                "openai",
                "--key",
                "sk-from-flag",
                "--no-verify",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert CredentialsStore().get("OPENAI_API_KEY") == "sk-from-flag"
        # Success message references the env var (lives on stderr via
        # the _console.success helper).
        combined = result.stdout + result.stderr
        assert "OPENAI_API_KEY" in combined

    def test_login_unknown_provider_errors(
        self, isolated_creds: Path
    ) -> None:
        result = runner.invoke(
            app, ["auth", "login", "madeup-provider", "--key", "x"]
        )
        assert result.exit_code == 2
        assert "unknown provider" in result.stderr.lower()

    def test_login_empty_key_errors(self, isolated_creds: Path) -> None:
        result = runner.invoke(
            app, ["auth", "login", "openai", "--key", "   ", "--no-verify"]
        )
        assert result.exit_code == 2
        assert "empty key" in result.stderr.lower()

    def test_login_with_verify_failure_aborts(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the verify call returns ok=False and network_error=False,
        the key is rejected and not saved."""

        def fake_verify(provider: str, key: str) -> VerifyResult:
            return VerifyResult(ok=False, detail="401 Unauthorized")

        with patch(
            "movate.credentials.verify_provider_key", side_effect=fake_verify
        ):
            result = runner.invoke(
                app, ["auth", "login", "openai", "--key", "sk-bad"]
            )
        assert result.exit_code == 2
        assert "401" in result.stderr or "verification failed" in result.stderr.lower()
        # Key did NOT get saved.
        assert CredentialsStore().get("OPENAI_API_KEY") is None

    def test_login_with_network_error_still_saves(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Network errors during verify should NOT block save —
        operator may be offline at setup time."""

        def fake_verify(provider: str, key: str) -> VerifyResult:
            return VerifyResult(
                ok=False, detail="connection refused", network_error=True
            )

        with patch(
            "movate.credentials.verify_provider_key", side_effect=fake_verify
        ):
            result = runner.invoke(
                app, ["auth", "login", "openai", "--key", "sk-test"]
            )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert CredentialsStore().get("OPENAI_API_KEY") == "sk-test"

    def test_login_save_to_project_appends_dotenv(
        self,
        isolated_creds: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "auth",
                "login",
                "anthropic",
                "--key",
                "ant-test",
                "--no-verify",
                "--save-to",
                "project",
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        dotenv = tmp_path / ".env"
        assert dotenv.is_file()
        assert "ANTHROPIC_API_KEY=ant-test" in dotenv.read_text()
        # And NOT in the global credentials file.
        assert CredentialsStore().get("ANTHROPIC_API_KEY") is None


# ---------------------------------------------------------------------------
# `mdk auth status` end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthStatus:
    def test_all_unset_renders_all_rows(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bundle F added Notifications rows (TELEGRAM_BOT_TOKEN +
        # TELEGRAM_CHAT_ID + MOVATE_DEPLOY_WEBHOOK) to the status
        # table. Strip those too so the no-keys baseline is clean.
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "MOVATE_DEPLOY_WEBHOOK",
        ):
            monkeypatch.delenv(key, raising=False)
        result = runner.invoke(
            app, ["auth", "status"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        for env_var in PROVIDER_KEY_ENV_VARS:
            assert env_var in result.stdout
        assert "not set" in result.stdout.lower()
        # Greppable summary line: 5 provider env vars + 3 notification
        # env vars = 8 unset total.
        assert "mdk_auth_status_summary:" in result.stdout
        assert "set=0" in result.stdout
        assert "unset=8" in result.stdout

    def test_set_keys_show_as_set(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "MOVATE_DEPLOY_WEBHOOK",
        ):
            monkeypatch.delenv(key, raising=False)
        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        # The mdk CLI runs autoload at startup, but CliRunner does NOT
        # re-import main.py — we need to manually re-autoload before
        # the test asserts.
        autoload_credentials()
        result = runner.invoke(
            app, ["auth", "status"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        # One key set, seven unset (4 LLM + 3 notification).
        assert "set=1" in result.stdout
        assert "unset=7" in result.stdout
