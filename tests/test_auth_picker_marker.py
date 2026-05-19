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

from movate.cli import auth as auth_mod
from movate.cli.auth import _provider_is_configured
from movate.cli.main import app
from movate.credentials import PROVIDER_KEY_ENV_VARS, CredentialsStore, autoload_credentials
from movate.credentials.verify import VerifyResult

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
    def test_picker_renders_marker_for_verified_provider(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured + verified provider shows the ✓ marker.

        The 2026-05-19 change made the picker live-verify each
        configured key (so a stub like ``sk-test-*`` no longer
        silently shows ✓ configured). Test mocks ``verify_provider_key``
        to simulate a verify-OK response without hitting the real API.
        """

        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        autoload_credentials()
        # Reset the per-process verify cache before this test (auth.py
        # caches results across calls within one CLI invocation).

        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=True, detail="OK — 47 models"),
        )

        result = runner.invoke(
            app,
            ["auth", "login", "--key", "sk-new", "--no-verify"],
            input="1\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        openai_lines = [
            line for line in result.stdout.splitlines() if "OpenAI" in line and "openai" in line
        ]
        assert openai_lines, "expected OpenAI row in picker output"
        # Marker text on the row — the literal word "verified" comes
        # from ``_provider_status_marker`` for the verified state.
        assert any("verified" in line.lower() for line in openai_lines), (
            f"expected ✓ verified marker on OpenAI row: {openai_lines}"
        )

    def test_picker_marks_rejected_when_verify_returns_401(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured but rejected key (provider returned 401)
        shows the ✗ marker — distinct from ✓ verified. This is the
        bug fix: pre-2026-05-19, ANY value (including stubs like
        ``sk-test-*****2345``) showed ✓ configured."""

        CredentialsStore().set("OPENAI_API_KEY", "sk-test-*****2345")
        autoload_credentials()

        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=False, detail="401 Unauthorized — key rejected"),
        )

        result = runner.invoke(
            app,
            ["auth", "login", "--key", "sk-new", "--no-verify"],
            input="1\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        openai_lines = [
            line for line in result.stdout.splitlines() if "OpenAI" in line and "openai" in line
        ]
        assert openai_lines
        # Marker text now says "rejected", NOT "verified".
        assert any("rejected" in line.lower() for line in openai_lines), (
            f"expected ✗ rejected marker: {openai_lines}"
        )
        assert not any("verified" in line.lower() for line in openai_lines), (
            f"rejected key must not show as verified: {openai_lines}"
        )

    def test_picker_omits_marker_for_unconfigured(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anthropic isn't set — its row should NOT show any marker
        (no ✓, no ✗, no ⚠)."""

        CredentialsStore().set("OPENAI_API_KEY", "sk-test")
        autoload_credentials()

        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=True, detail="OK"),
        )

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
            assert "verified" not in line.lower(), (
                f"Anthropic row (unconfigured) must not show verified: {line}"
            )
            assert "rejected" not in line.lower(), (
                f"Anthropic row (unconfigured) must not show rejected: {line}"
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
        # Telegram has no live-verify path (no LLM-style metadata
        # endpoint to probe); ``_provider_status`` returns "verified"
        # iff both env vars are set, "unset" otherwise. Either way, a
        # half-configured Telegram should not show any marker.
        for line in telegram_lines:
            assert "verified" not in line.lower(), (
                f"Telegram should require both secrets to mark verified: {line}"
            )


# ---------------------------------------------------------------------------
# _provider_status — live-verify state machine
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderStatus:
    """``_provider_status`` returns finer-grained state than the legacy
    ``_provider_is_configured`` boolean so the picker + status table
    can distinguish "set + verified" from "set + rejected by provider"
    (the stub-key footgun the 2026-05-19 reproduction surfaced).
    """

    def test_unset_provider_returns_unset(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        auth_mod._verify_cache.clear()
        assert auth_mod._provider_status("openai") == "unset"
        assert auth_mod._provider_status("anthropic") == "unset"

    def test_verified_when_provider_accepts_key(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        CredentialsStore().set("OPENAI_API_KEY", "sk-real-looking")
        autoload_credentials()
        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=True, detail="OK — 47 models"),
        )
        assert auth_mod._provider_status("openai") == "verified"

    def test_rejected_when_provider_returns_401(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual bug fix: a key set in env but rejected by the
        provider must NOT show ``verified``. Pre-2026-05-19 the
        marker just checked env presence."""

        CredentialsStore().set("OPENAI_API_KEY", "sk-test-*****2345")
        autoload_credentials()
        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=False, detail="401 Unauthorized — key rejected"),
        )
        assert auth_mod._provider_status("openai") == "rejected"

    def test_unverifiable_on_network_error(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Network error during verify → don't lie either way; the
        marker shows ``set, couldn't verify``. Operator decides.
        """

        CredentialsStore().set("OPENAI_API_KEY", "sk-could-be-real")
        autoload_credentials()
        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(
                ok=False, network_error=True, detail="network error: timeout"
            ),
        )
        assert auth_mod._provider_status("openai") == "unverifiable"

    def test_cache_prevents_double_probe(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The picker calls ``_provider_status`` once per provider, the
        status table iterates them too — without a cache, every CLI
        invocation would double-probe every set provider. Pin the
        cache so subsequent calls don't hit the provider again."""

        CredentialsStore().set("OPENAI_API_KEY", "sk-real-looking")
        autoload_credentials()
        auth_mod._verify_cache.clear()
        probe_calls = 0

        def counting_verify(provider: str, key: str) -> VerifyResult:
            nonlocal probe_calls
            probe_calls += 1
            return VerifyResult(ok=True, detail="OK")

        monkeypatch.setattr("movate.credentials.verify_provider_key", counting_verify)
        # Call N times.
        for _ in range(5):
            auth_mod._provider_status("openai")
        # Verify the verify call only fired ONCE.
        assert probe_calls == 1

    def test_telegram_does_not_call_verify(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram has no LLM-style metadata endpoint. ``_provider_status``
        returns ``verified`` based purely on env-presence + must NOT
        hit verify_provider_key (which has no telegram impl)."""

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat-id")
        autoload_credentials()
        auth_mod._verify_cache.clear()

        probe_calls = 0

        def counting_verify(provider: str, key: str) -> object:
            nonlocal probe_calls
            probe_calls += 1
            raise AssertionError("verify must not be called for telegram")

        monkeypatch.setattr("movate.credentials.verify_provider_key", counting_verify)
        assert auth_mod._provider_status("telegram") == "verified"
        assert probe_calls == 0


@pytest.mark.unit
class TestAuthStatusTable:
    """The ``mdk auth status`` table renders the same finer states as
    the picker. CI scrapers reading the greppable summary line gain
    a ``rejected=N`` count when any set keys are 401'd by the provider.
    """

    def test_status_table_shows_verified_marker(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        CredentialsStore().set("OPENAI_API_KEY", "sk-works")
        autoload_credentials()
        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=True, detail="OK"),
        )

        result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout
        assert "verified" in result.stdout.lower()
        # Summary line keeps backwards-compat ``set=`` key — exact
        # count varies with the operator's environment (the test
        # picks up real ~/.movate/config.yaml runtime targets), so
        # just pin the field's presence.
        assert "mdk_auth_status_summary: set=" in result.stdout

    def test_status_table_shows_rejected_with_rotation_hint(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actionable diff vs pre-fix: rejected keys are flagged
        and the hint column tells the operator HOW to fix it
        (``mdk auth login {provider}`` to rotate)."""

        CredentialsStore().set("OPENAI_API_KEY", "sk-test-*****2345")
        autoload_credentials()
        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=False, detail="401 Unauthorized"),
        )

        result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout
        # New markers + hint surface.
        assert "rejected" in result.stdout.lower()
        assert "mdk auth login openai" in result.stdout
        # Summary line carries ``rejected=1`` so CI can gate on it.
        assert "rejected=1" in result.stdout

    def test_status_table_omits_rejected_count_when_zero(
        self, isolated_creds: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``rejected=N`` only appears in the summary line when N > 0
        so happy-path output stays terse + downstream scrapers built
        against the pre-rejected format don't see a new field
        unexpectedly."""

        CredentialsStore().set("OPENAI_API_KEY", "sk-works")
        autoload_credentials()
        auth_mod._verify_cache.clear()
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=True, detail="OK"),
        )

        result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # Summary line MUST NOT include "rejected=" when count is 0.
        summary_line = [ln for ln in result.stdout.splitlines() if "mdk_auth_status_summary" in ln]
        assert summary_line
        assert "rejected=" not in summary_line[0]


# ---------------------------------------------------------------------------
# Python-callable login() (2026-05-19)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoginCallableFromPython:
    """``cli.auth.login`` is a ``@auth_app.command`` — designed for
    Typer's CLI dispatcher. But two code paths import + invoke it
    directly from Python: ``_offer_inline_auth_recovery`` in
    ``eval_scorecard_cmd`` (when preflight finds every provider's
    key rejected) and ``_require_llm_provider_key_or_offer_setup``
    in ``eval`` (when no LLM key is configured at all).

    When called from Python with no args, the ``typer.Argument(None)``
    / ``typer.Option(...)`` defaults pass through as
    ``ArgumentInfo`` / ``OptionInfo`` sentinel objects rather than
    being resolved to their declared defaults. The next ``.lower()``
    / ``.strip()`` call then dies with ``AttributeError: 'ArgumentInfo'
    object has no attribute 'lower'`` — exactly what an operator hit
    in production on 2026-05-19 when ``mdk eval`` tried to recover
    from a placeholder ``OPENAI_API_KEY`` and dropped into the
    inline auth-recovery flow.
    """

    def test_login_no_args_does_not_attribute_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The regression: bare ``login()`` from Python must NOT
        crash with ``AttributeError``. The function may then prompt
        interactively or raise ``typer.Exit`` — either is fine, as
        long as the Typer sentinel doesn't bleed into ``.lower()``."""
        from movate.cli.auth import login  # noqa: PLC0415

        # The picker would call input() — short-circuit it by stubbing
        # ``_prompt_for_provider`` to a known answer. The point of this
        # test isn't to exercise the picker; it's to confirm the
        # function reaches the picker call WITHOUT crashing on the
        # sentinel-defaulted args before that.
        monkeypatch.setattr("movate.cli.auth._prompt_for_provider", lambda: "openai")
        # Stub the prompt + verifier so we don't try to make network calls.
        monkeypatch.setattr("typer.prompt", lambda *a, **kw: "sk-test-noop")
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=False, detail="401 Unauthorized"),
        )

        # The function will typer.Exit(2) on the rejected key — that's
        # fine. We're guarding the AttributeError specifically.
        import typer  # noqa: PLC0415

        try:
            login()
        except typer.Exit:
            # Acceptable terminal state — the verifier rejected our
            # stub key, login bails with exit code 2.
            pass
        except AttributeError as exc:  # pragma: no cover - the regression
            pytest.fail(
                f"login() crashed with AttributeError when called from Python: {exc}. "
                "Typer sentinel defaults (ArgumentInfo / OptionInfo) leaked through "
                "instead of being normalized to their declared defaults."
            )

    def test_login_normalizes_argumentinfo_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Defense-in-depth: explicitly pass typer's sentinel objects
        and verify the function tolerates them. This guards against
        future Typer-call-from-Python sites that might forward
        sentinel-defaulted args without thinking about it."""
        import typer  # noqa: PLC0415

        from movate.cli.auth import login  # noqa: PLC0415

        # The picker would normally fire — stub it to a known value.
        provider_picker_calls = []

        def fake_picker() -> str:
            provider_picker_calls.append(True)
            return "openai"

        monkeypatch.setattr("movate.cli.auth._prompt_for_provider", fake_picker)
        monkeypatch.setattr("typer.prompt", lambda *a, **kw: "sk-noop")
        monkeypatch.setattr(
            "movate.credentials.verify_provider_key",
            lambda provider, key: VerifyResult(ok=False, detail="401 Unauthorized"),
        )

        # These are EXACTLY the sentinels Python sees when ``login()``
        # is invoked without args: the typer.Argument(None) /
        # typer.Option(...) call return values.
        sentinel_provider = typer.Argument(None)
        sentinel_key = typer.Option(None, "--key")
        sentinel_no_verify = typer.Option(False, "--no-verify")
        sentinel_save_to = typer.Option("global", "--save-to")

        try:
            login(
                provider=sentinel_provider,
                key=sentinel_key,
                no_verify=sentinel_no_verify,
                save_to=sentinel_save_to,
            )
        except typer.Exit:
            pass  # acceptable terminal state
        except AttributeError as exc:  # pragma: no cover - the regression
            pytest.fail(f"login() failed to normalize Typer sentinel defaults: {exc}")

        # Picker fired — proves provider was normalized to None and
        # the if-None branch was taken (not the ``.lower()`` branch).
        assert provider_picker_calls, (
            "provider sentinel should have been normalized to None, triggering the picker"
        )
