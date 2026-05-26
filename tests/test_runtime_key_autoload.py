"""Tests for PR #96 — MDK runtime bearer keys autoload from credentials store.

Closes the bearer-key handoff gap: today operators have to re-export
``MDK_<TARGET>_KEY`` in every new shell. After this PR:

1. ``mdk auth save-runtime-key <target> <key>`` writes the bearer
   to ``~/.movate/credentials`` under the target's ``key_env`` name.
2. ``autoload_credentials()`` pattern-matches ``MDK_*_KEY`` entries
   in the credentials file and exports them into ``os.environ`` —
   so the next shell sees ``$MDK_DEV_KEY`` without manual export.

Same precedence as provider keys: shell > .env > credentials file
(never clobbers an explicit shell-set value).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.credentials.loader import (
    _looks_like_runtime_key_env,
    autoload_credentials,
    key_source,
    runtime_key_shadowed,
)
from movate.credentials.store import CredentialsStore

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Pattern matcher
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuntimeKeyEnvPattern:
    def test_canonical_shape_matches(self) -> None:
        assert _looks_like_runtime_key_env("MDK_DEV_KEY")
        assert _looks_like_runtime_key_env("MDK_PROD_KEY")
        assert _looks_like_runtime_key_env("MDK_STAGING_KEY")
        assert _looks_like_runtime_key_env("MDK_CUSTOMER_FOO_KEY")

    def test_non_matching_shapes_rejected(self) -> None:
        # Wrong prefix.
        assert not _looks_like_runtime_key_env("DEV_KEY")
        assert not _looks_like_runtime_key_env("MOVATE_DEV_KEY")
        # Wrong suffix.
        assert not _looks_like_runtime_key_env("MDK_DEV_TOKEN")
        assert not _looks_like_runtime_key_env("MDK_DEV")
        # Provider keys must NOT match (they use the whitelist path).
        assert not _looks_like_runtime_key_env("OPENAI_API_KEY")
        # The minimal-but-empty middle ("MDK__KEY") should be rejected
        # — it doesn't name a real target.
        assert not _looks_like_runtime_key_env("MDK__KEY")
        # Empty string + edge cases.
        assert not _looks_like_runtime_key_env("")
        assert not _looks_like_runtime_key_env("MDK_KEY")


# ---------------------------------------------------------------------------
# Autoload behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoloadRuntimeKeys:
    def _isolated_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> CredentialsStore:
        """Point CredentialsStore at a tmp HOME so the test doesn't
        touch the operator's real credentials."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # CredentialsStore reads HOME at construction; force a fresh
        # instance per test.
        store = CredentialsStore()
        # Belt + braces: write a marker that the store is empty so
        # we don't bleed state from the operator's actual file.
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("")
        return store

    def test_credentials_file_runtime_key_loads_into_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``MDK_DEV_KEY=...`` line in credentials should become
        ``os.environ['MDK_DEV_KEY']`` after autoload."""
        store = self._isolated_credentials(tmp_path, monkeypatch)
        store.set("MDK_DEV_KEY", "mvt_live_demo_KID_SECRET")
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        autoload_credentials()
        assert os.environ.get("MDK_DEV_KEY") == "mvt_live_demo_KID_SECRET"

    def test_saved_value_wins_over_differing_shell_export(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR 022 inversion: for a runtime-bearer key, the SAVED file
        value is authoritative and OVERRIDES a differing shell export
        (this was the #1 recurring 401 — a stale shell key shadowing
        the freshly-saved one). The override is recorded so callers can
        surface it (never silent)."""
        store = self._isolated_credentials(tmp_path, monkeypatch)
        store.set("MDK_DEV_KEY", "mvt_live_FILE_VERSION")
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_SHELL_VERSION")
        autoload_credentials()
        # File value wins (the inversion).
        assert os.environ.get("MDK_DEV_KEY") == "mvt_live_FILE_VERSION"
        # …and the shadow is recorded for point-of-use surfacing.
        assert runtime_key_shadowed("MDK_DEV_KEY") is True

    def test_non_runtime_keys_in_credentials_file_not_autoloaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Security guard: arbitrary keys in the credentials file
        (e.g. ``AWS_SECRET_ACCESS_KEY``) must NOT be autoloaded just
        because they happen to live in the same file. Only canonical
        provider/notification whitelist + ``MDK_*_KEY`` pattern."""
        store = self._isolated_credentials(tmp_path, monkeypatch)
        store.set("AWS_SECRET_ACCESS_KEY", "should-not-be-autoloaded")
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        autoload_credentials()
        assert os.environ.get("AWS_SECRET_ACCESS_KEY") is None

    def test_provider_key_autoload_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: the existing whitelist (OPENAI_API_KEY etc.)
        path is unchanged — we ADDED pattern matching, didn't
        replace the whitelist."""
        store = self._isolated_credentials(tmp_path, monkeypatch)
        store.set("OPENAI_API_KEY", "sk-test-12345")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        autoload_credentials()
        assert os.environ.get("OPENAI_API_KEY") == "sk-test-12345"


# ---------------------------------------------------------------------------
# ADR 022 — file-authoritative runtime-bearer resolution (the full matrix)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuntimeKeyFileAuthoritative:
    """ADR 022 precedence matrix for the ``MDK_<TARGET>_KEY`` class.

    The decision: runtime-bearer keys are file-authoritative (the saved
    value beats a plain shell export) — but ONLY when a saved value
    exists, and the override is always recorded so it's surfaced (never a
    silent 401). Provider keys are deliberately UNCHANGED (shell wins) and
    proven separately below to confirm the class split is intact.
    """

    def _isolated_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, contents: str = ""
    ) -> CredentialsStore:
        """Point the file backend at a tmp credentials file (never the
        operator's real ~/.movate). Mirrors the hermetic setup the echo
        tests use so the two suites isolate identically."""
        creds = tmp_path / "credentials"
        creds.write_text(contents)
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(creds))
        monkeypatch.setenv("MOVATE_CRED_BACKEND", "file")
        return CredentialsStore()

    def test_file_only_uses_file_value_source_credentials_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File-only (no shell value) → file value used, no shadow,
        ``key_source`` reports ``credentials_file``."""
        self._isolated_credentials(
            tmp_path, monkeypatch, contents="MDK_DEV_KEY=mvt_live_FILEONLY\n"
        )
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        monkeypatch.chdir(tmp_path)  # no .env → not dotenv
        autoload_credentials()
        assert os.environ.get("MDK_DEV_KEY") == "mvt_live_FILEONLY"
        assert key_source("MDK_DEV_KEY") == "credentials_file"
        assert runtime_key_shadowed("MDK_DEV_KEY") is False

    def test_shell_only_no_file_value_keeps_shell_ci_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shell-only, NO saved value → the shell value is used unchanged
        (the CI / pure-shell path that must never break). No shadow."""
        # File exists (so autoload runs the runtime-key loop) but has NO
        # MDK_DEV_KEY entry — the rule-3 fallthrough.
        self._isolated_credentials(tmp_path, monkeypatch, contents="OPENAI_API_KEY=sk-x\n")
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_SHELLONLY")
        monkeypatch.chdir(tmp_path)
        autoload_credentials()
        assert os.environ.get("MDK_DEV_KEY") == "mvt_live_SHELLONLY"
        assert key_source("MDK_DEV_KEY") == "shell"
        assert runtime_key_shadowed("MDK_DEV_KEY") is False

    def test_file_equals_shell_is_silent_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File == shell → file value used (a no-op), no shadow notice —
        there's nothing to warn about when the values match."""
        self._isolated_credentials(tmp_path, monkeypatch, contents="MDK_DEV_KEY=mvt_live_SAME\n")
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_SAME")
        autoload_credentials()
        assert os.environ.get("MDK_DEV_KEY") == "mvt_live_SAME"
        assert runtime_key_shadowed("MDK_DEV_KEY") is False

    def test_file_differs_from_shell_file_wins_and_shadow_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File != shell → FILE value wins, the override is recorded, and
        ``key_source`` reports ``credentials_file`` (the truthful source
        after the file won)."""
        self._isolated_credentials(
            tmp_path, monkeypatch, contents="MDK_DEV_KEY=mvt_live_FILEWINS\n"
        )
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_STALESHELL")
        monkeypatch.chdir(tmp_path)
        autoload_credentials()
        assert os.environ.get("MDK_DEV_KEY") == "mvt_live_FILEWINS"
        assert key_source("MDK_DEV_KEY") == "credentials_file"
        assert runtime_key_shadowed("MDK_DEV_KEY") is True

    def test_provider_key_shell_wins_class_split_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The class split: a PROVIDER key (OPENAI_API_KEY) with BOTH a
        shell value and a file value → the SHELL value wins (unchanged
        env-overrides-config convention). Proves ADR 022 is scoped to the
        runtime-bearer class only."""
        self._isolated_credentials(tmp_path, monkeypatch, contents="OPENAI_API_KEY=sk-FILE\n")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-SHELL")
        monkeypatch.chdir(tmp_path)
        autoload_credentials()
        # Shell wins for provider keys — never clobbered.
        assert os.environ.get("OPENAI_API_KEY") == "sk-SHELL"
        assert key_source("OPENAI_API_KEY") == "shell"
        # The shadow ledger is runtime-key-only; a provider key is never in it.
        assert runtime_key_shadowed("OPENAI_API_KEY") is False

    def test_key_source_states_for_provider_keys_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``key_source`` still returns the correct 4 states for provider
        keys: unset / credentials_file / shell."""
        self._isolated_credentials(tmp_path, monkeypatch, contents="OPENAI_API_KEY=sk-FILE\n")
        monkeypatch.chdir(tmp_path)
        # unset
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert key_source("ANTHROPIC_API_KEY") == "unset"
        # credentials_file (autoload hydrates the unset env from file)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        autoload_credentials()
        assert key_source("OPENAI_API_KEY") == "credentials_file"
        # shell (an explicit export that matches neither file nor .env)
        monkeypatch.setenv("GEMINI_API_KEY", "gm-shell")
        assert key_source("GEMINI_API_KEY") == "shell"

    def test_repeat_autoload_clears_stale_shadow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shadow ledger is rebuilt on every autoload — a prior run's
        override never lingers into a later, no-longer-shadowing run."""
        self._isolated_credentials(tmp_path, monkeypatch, contents="MDK_DEV_KEY=mvt_live_FILE\n")
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_STALE")
        autoload_credentials()
        assert runtime_key_shadowed("MDK_DEV_KEY") is True
        # Now the shell matches the file (operator unset the stale export and
        # a fresh shell picked up the saved value) — a re-autoload must clear
        # the shadow.
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_FILE")
        autoload_credentials()
        assert runtime_key_shadowed("MDK_DEV_KEY") is False


# ---------------------------------------------------------------------------
# `mdk auth save-runtime-key` command
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSaveRuntimeKeyCommand:
    def _bootstrap_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point HOME at tmp_path + register a 'dev' target."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
        result = runner.invoke(
            app,
            [
                "config",
                "add-target",
                "dev",
                "--url",
                "https://fake.example.com",
                "--key-env",
                "MDK_DEV_KEY",
                "--azure-subscription",
                "00000000-0000-0000-0000-000000000000",
                "--azure-resource-group",
                "fake-rg",
                "--azure-acr",
                "fakeacr",
                "--azure-env",
                "dev",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr

    def test_saves_under_targets_key_env_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk auth save-runtime-key dev mvt_live_...` should write
        the value to the credentials file under MDK_DEV_KEY (the
        target's `key_env`)."""
        self._bootstrap_target(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "auth",
                "save-runtime-key",
                "dev",
                "mvt_live_demo_ABC123_secretvalue",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        store = CredentialsStore()
        entries = store.read()
        assert entries.get("MDK_DEV_KEY") == "mvt_live_demo_ABC123_secretvalue"
        # Success message names the env var so the operator knows
        # exactly which variable was set. The `success()` helper writes
        # to stderr (so stdout stays clean for piping); check both.
        combined = result.stdout + result.stderr
        assert "MDK_DEV_KEY" in combined

    def test_unknown_target_errors_with_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saving a key for an unregistered target should error with
        a list of registered targets, not silently write to a wrong
        env var."""
        self._bootstrap_target(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "auth",
                "save-runtime-key",
                "nonexistent",
                "mvt_live_demo_ABC123_secret",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "unknown target" in combined.lower()
        assert "dev" in combined  # the registered one is named in the hint

    def test_malformed_key_warns_but_saves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key that doesn't look like a movate bearer (e.g. operator
        pasted a partial value) should fire a warning but still save —
        we don't want to block the operator if the runtime later
        accepts a non-canonical shape."""
        self._bootstrap_target(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["auth", "save-runtime-key", "dev", "totally-wrong-shape"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # Warning goes to stderr via err.print(); check both streams.
        combined = result.stdout + result.stderr
        assert "doesn't look like a movate bearer" in combined.lower()
        # Value still saved (operator override).
        store = CredentialsStore()
        assert store.read().get("MDK_DEV_KEY") == "totally-wrong-shape"

    def test_full_flow_save_then_autoload_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: save the key, then simulate a fresh shell by
        clearing the env var and calling autoload. The key should
        come back from the credentials file."""
        self._bootstrap_target(tmp_path, monkeypatch)
        runner.invoke(
            app,
            [
                "auth",
                "save-runtime-key",
                "dev",
                "mvt_live_demo_FRESHKEY_secret",
            ],
            env={"COLUMNS": "200"},
        )
        # Simulate fresh shell: env var unset.
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        autoload_credentials()
        assert os.environ.get("MDK_DEV_KEY") == "mvt_live_demo_FRESHKEY_secret"


# ---------------------------------------------------------------------------
# `mdk auth save-runtime-key dev -` — read from stdin
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSaveRuntimeKeyFromStdin:
    """Piped recovery flow: the secret is read from stdin so it never
    lands in shell history or `ps`. The intended usage is:

        az containerapp exec ... 'mdk auth create-key ...' \
          | mdk auth save-runtime-key dev -

    where `-` is the conventional stdin sentinel.
    """

    def _bootstrap_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
        result = runner.invoke(
            app,
            [
                "config",
                "add-target",
                "dev",
                "--url",
                "https://fake.example.com",
                "--key-env",
                "MDK_DEV_KEY",
                "--azure-subscription",
                "00000000-0000-0000-0000-000000000000",
                "--azure-resource-group",
                "fake-rg",
                "--azure-acr",
                "fakeacr",
                "--azure-env",
                "dev",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr

    def test_reads_bare_key_from_stdin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The simplest case: stdin contains exactly the mvt_… token
        with a trailing newline (what `mdk auth create-key --quiet`
        emits). Save it under the target's key_env."""
        self._bootstrap_target(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["auth", "save-runtime-key", "dev", "-"],
            input="mvt_live_demotena_kid123abc_secretvalueXYZ\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        store = CredentialsStore()
        assert store.read().get("MDK_DEV_KEY") == "mvt_live_demotena_kid123abc_secretvalueXYZ"

    def test_scrapes_mvt_key_from_az_exec_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The realistic recovery case: stdin has the full `az
        containerapp exec` output mixed with INFO lines + the
        'save this now' preamble — our scraper extracts the first
        `mvt_*` token regardless of surrounding noise."""
        self._bootstrap_target(tmp_path, monkeypatch)
        piped = (
            "INFO: Connecting to the container 'movate-api'...\n"
            "INFO: Successfully connected to container.\n"
            "mvt_live_demotena_kid123abc_secretvalueXYZ\n"
            "save this now — never shown again\n"
        )
        result = runner.invoke(
            app,
            ["auth", "save-runtime-key", "dev", "-"],
            input=piped,
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        store = CredentialsStore()
        # Only the mvt_ token, not the surrounding text.
        assert store.read().get("MDK_DEV_KEY") == "mvt_live_demotena_kid123abc_secretvalueXYZ"

    def test_stdin_with_no_mvt_token_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Piped garbage (network error before az emitted a key, etc.)
        is a hard error — we don't want to save `INFO: connecting...`
        as someone's MDK_DEV_KEY and confuse them later."""
        self._bootstrap_target(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["auth", "save-runtime-key", "dev", "-"],
            input="some random text\nno key here\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        combined = (result.stdout + result.stderr).replace("\n", " ")
        assert "no `mvt_" in combined or "token found on stdin" in combined
