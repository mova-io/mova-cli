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

    def test_delete_returns_true_when_present(self, isolated_creds: Path) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-x")
        assert store.delete("OPENAI_API_KEY") is True
        assert store.get("OPENAI_API_KEY") is None

    def test_delete_returns_false_when_missing(self, isolated_creds: Path) -> None:
        assert CredentialsStore().delete("OPENAI_API_KEY") is False

    def test_file_is_mode_0600(self, isolated_creds: Path) -> None:
        store = CredentialsStore()
        store.set("OPENAI_API_KEY", "sk-x")
        mode = isolated_creds.stat().st_mode & 0o777
        assert mode == 0o600, f"credentials file should be 0600, got {oct(mode)}"

    def test_comments_in_file_are_skipped(self, isolated_creds: Path) -> None:
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text(
            "# comment\nOPENAI_API_KEY=sk-x\n# another comment\n\nANTHROPIC_API_KEY=ant-y\n"
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

    def test_set_preserves_comments_and_ordering(self, isolated_creds: Path) -> None:
        """A hand-edited file survives set(): comments, other keys, and
        ordering stay intact — only the target key's line is rewritten."""
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text(
            "# my notes\nOPENAI_API_KEY=sk-old\n# section two\nANTHROPIC_API_KEY=ant-1\n"
        )
        CredentialsStore().set("OPENAI_API_KEY", "sk-new")
        text = isolated_creds.read_text()
        assert "# my notes" in text
        assert "# section two" in text
        assert "OPENAI_API_KEY=sk-new" in text
        assert "sk-old" not in text
        assert "ANTHROPIC_API_KEY=ant-1" in text
        # Ordering preserved: the OPENAI line is still before the section-two comment.
        assert text.index("OPENAI_API_KEY") < text.index("# section two")

    def test_set_new_key_appends_and_keeps_comments(self, isolated_creds: Path) -> None:
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text("# keep me\nOPENAI_API_KEY=sk-1\n")
        CredentialsStore().set("ANTHROPIC_API_KEY", "ant-1")
        text = isolated_creds.read_text()
        assert "# keep me" in text
        assert "OPENAI_API_KEY=sk-1" in text
        assert "ANTHROPIC_API_KEY=ant-1" in text

    def test_delete_preserves_comments(self, isolated_creds: Path) -> None:
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text("# notes\nOPENAI_API_KEY=sk-1\nANTHROPIC_API_KEY=ant-1\n")
        assert CredentialsStore().delete("OPENAI_API_KEY") is True
        text = isolated_creds.read_text()
        assert "# notes" in text
        assert "OPENAI_API_KEY" not in text
        assert "ANTHROPIC_API_KEY=ant-1" in text


# ---------------------------------------------------------------------------
# Duplicate-key dedupe — regression for the stale-shadow bug
#
# Real-user scenario: `mdk auth pull-runtime-key dev` reported the key was
# saved, but `mdk run` kept sending an OLD value (shell env empty, so it
# came from the file). The credentials file had TWO `MDK_DEV_KEY=` lines:
# set() updated the first but left the stale later one, and read resolved
# the wrong one — permanently shadowing the freshly-saved value. The fix:
# set() collapses duplicates to a single line, and read is last-wins so a
# pre-existing dupe stops shadowing the newer value.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDuplicateKeyDedupe:
    def test_set_existing_key_leaves_exactly_one_line(self, isolated_creds: Path) -> None:
        """set() on an existing key replaces it — one line, new value."""
        store = CredentialsStore()
        store.set("MDK_DEV_KEY", "old")
        store.set("MDK_DEV_KEY", "new")
        text = isolated_creds.read_text()
        assert text.count("MDK_DEV_KEY=") == 1
        assert "MDK_DEV_KEY=new" in text
        assert "old" not in text
        assert store.get("MDK_DEV_KEY") == "new"

    def test_set_collapses_preexisting_duplicates(self, isolated_creds: Path) -> None:
        """A file that ALREADY has duplicate lines for the key collapses
        to ONE line with the new value on the next set()."""
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        # Legacy file written before the dedupe guarantee: two MDK_DEV_KEY lines.
        isolated_creds.write_text("MDK_DEV_KEY=stale-1\nOPENAI_API_KEY=sk-1\nMDK_DEV_KEY=stale-2\n")
        store = CredentialsStore()
        store.set("MDK_DEV_KEY", "fresh")
        text = isolated_creds.read_text()
        assert text.count("MDK_DEV_KEY=") == 1, text
        assert "MDK_DEV_KEY=fresh" in text
        assert "stale-1" not in text
        assert "stale-2" not in text
        # Unrelated key untouched.
        assert "OPENAI_API_KEY=sk-1" in text
        assert store.get("MDK_DEV_KEY") == "fresh"

    def test_set_dedupe_preserves_first_position_and_comments(self, isolated_creds: Path) -> None:
        """Collapsing dupes honors PR #12: the first occurrence keeps its
        position; comments, ordering, and other keys stay intact."""
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text(
            "# my notes\n"
            "MDK_DEV_KEY=stale-1\n"
            "# section two\n"
            "ANTHROPIC_API_KEY=ant-1\n"
            "MDK_DEV_KEY=stale-2\n"
        )
        CredentialsStore().set("MDK_DEV_KEY", "fresh")
        text = isolated_creds.read_text()
        assert "# my notes" in text
        assert "# section two" in text
        assert "ANTHROPIC_API_KEY=ant-1" in text
        assert text.count("MDK_DEV_KEY=") == 1
        assert "MDK_DEV_KEY=fresh" in text
        # The (single) MDK line keeps the FIRST occurrence's position:
        # before the section-two comment, not appended at the end.
        assert text.index("MDK_DEV_KEY") < text.index("# section two")

    def test_read_preexisting_duplicates_last_wins(self, isolated_creds: Path) -> None:
        """read/parse of a file with pre-existing duplicate lines returns
        the LAST value — a stale earlier line can't shadow the newer one."""
        isolated_creds.parent.mkdir(parents=True, exist_ok=True)
        isolated_creds.write_text("MDK_DEV_KEY=stale\nMDK_DEV_KEY=newest\n")
        assert CredentialsStore().get("MDK_DEV_KEY") == "newest"
        assert CredentialsStore().read()["MDK_DEV_KEY"] == "newest"

    def test_round_trip_set_a_b_a_collapses_to_one_each(self, isolated_creds: Path) -> None:
        """set A, set B, set A again → one A (latest) + one B, correct values."""
        store = CredentialsStore()
        store.set("MDK_DEV_KEY", "a1")
        store.set("OPENAI_API_KEY", "b1")
        store.set("MDK_DEV_KEY", "a2")
        text = isolated_creds.read_text()
        assert text.count("MDK_DEV_KEY=") == 1
        assert text.count("OPENAI_API_KEY=") == 1
        assert store.get("MDK_DEV_KEY") == "a2"
        assert store.get("OPENAI_API_KEY") == "b1"


# ---------------------------------------------------------------------------
# autoload_credentials — fills unset env vars; doesn't clobber set ones
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoload:
    def test_loads_into_unset_env_var(self, isolated_creds: Path) -> None:
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

    def test_credentials_file_attribution(self, isolated_creds: Path) -> None:
        CredentialsStore().set("OPENAI_API_KEY", "sk-x")
        autoload_credentials()
        assert key_source("OPENAI_API_KEY") == "credentials_file"

    def test_shell_attribution(self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_openai_401_returns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx  # noqa: PLC0415

        class _Resp:
            status_code = 401
            text = "Unauthorized"

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
        result = verify_provider_key("openai", "sk-bad")
        assert result.ok is False
        assert "401" in result.detail

    def test_network_error_flagged_separately(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    def test_login_with_key_flag_and_no_verify_writes_file(self, isolated_creds: Path) -> None:
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

    def test_login_unknown_provider_errors(self, isolated_creds: Path) -> None:
        result = runner.invoke(app, ["auth", "login", "madeup-provider", "--key", "x"])
        assert result.exit_code == 2
        assert "unknown provider" in result.stderr.lower()

    def test_login_empty_key_errors(self, isolated_creds: Path) -> None:
        result = runner.invoke(app, ["auth", "login", "openai", "--key", "   ", "--no-verify"])
        assert result.exit_code == 2
        assert "empty key" in result.stderr.lower()

    def test_login_with_verify_failure_aborts(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the verify call returns ok=False and network_error=False,
        the key is rejected and not saved."""

        def fake_verify(provider: str, key: str) -> VerifyResult:
            return VerifyResult(ok=False, detail="401 Unauthorized")

        with patch("movate.credentials.verify_provider_key", side_effect=fake_verify):
            result = runner.invoke(app, ["auth", "login", "openai", "--key", "sk-bad"])
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
            return VerifyResult(ok=False, detail="connection refused", network_error=True)

        with patch("movate.credentials.verify_provider_key", side_effect=fake_verify):
            result = runner.invoke(app, ["auth", "login", "openai", "--key", "sk-test"])
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
        # table; the voice PR (ADR 048/049) added a Voice section
        # (AZURE_SPEECH_KEY + AZURE_SPEECH_REGION); ADR 054 added a
        # Workflow backends section (TEMPORAL_HOST/NAMESPACE/TLS_CERT/
        # TLS_KEY). Strip those too so the no-keys baseline is clean.
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "MOVATE_DEPLOY_WEBHOOK",
            "AZURE_SPEECH_KEY",
            "AZURE_SPEECH_REGION",
            "TEMPORAL_HOST",
            "TEMPORAL_NAMESPACE",
            "TEMPORAL_TLS_CERT",
            "TEMPORAL_TLS_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        # PR #112 added a Runtime Targets section to `mdk auth status`
        # that reads ~/.movate/config.yaml. Isolate it too so this test
        # doesn't depend on whatever targets the operator's real
        # machine has configured.
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(isolated_creds.parent / "config.yaml"))
        result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        for env_var in PROVIDER_KEY_ENV_VARS:
            assert env_var in result.stdout
        assert "not set" in result.stdout.lower()
        # Greppable summary line: 5 provider env vars + 3 notification
        # env vars + 2 voice env vars + 4 temporal env vars = 14 unset
        # total (no runtime targets configured in the isolated config
        # path).
        assert "mdk_auth_status_summary:" in result.stdout
        assert "set=0" in result.stdout
        assert "unset=14" in result.stdout

    def test_set_keys_show_as_set(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "MOVATE_DEPLOY_WEBHOOK",
            "AZURE_SPEECH_KEY",
            "AZURE_SPEECH_REGION",
            "TEMPORAL_HOST",
            "TEMPORAL_NAMESPACE",
            "TEMPORAL_TLS_CERT",
            "TEMPORAL_TLS_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        # PR #112 — isolate user config path so the Runtime Targets
        # section doesn't bleed in counts from the operator's real
        # ~/.movate/config.yaml.
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(isolated_creds.parent / "config.yaml"))
        # `mdk auth status` LIVE-verifies every set provider key (calls the
        # provider's metadata endpoint via `verify_provider_key`). The fake
        # `sk-test` below would 401 on a CI runner WITH egress (→ classified
        # `rejected`, set=0) but error out as a network error WITHOUT egress
        # (→ classified as set, set=1) — making this assertion green/red
        # purely on the runner's network state. Pin the verifier to a
        # deterministic OK so the saved key reliably classifies as "set",
        # which is exactly what this test means to assert. `_provider_status`
        # imports the symbol from `movate.credentials`, so patch it there.
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=True, detail="OK — mocked, 1 model available"),
        )
        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        # The mdk CLI runs autoload at startup, but CliRunner does NOT
        # re-import main.py — we need to manually re-autoload before
        # the test asserts.
        autoload_credentials()
        result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # One key set, thirteen unset (4 LLM + 3 notification + 2 voice
        # + 4 temporal).
        assert "set=1" in result.stdout
        assert "unset=13" in result.stdout
