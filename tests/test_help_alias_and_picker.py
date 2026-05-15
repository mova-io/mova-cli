"""Two intuitiveness wins shipped together:

1. **`mdk <subcmd> help` → `mdk <subcmd> --help`** — natural-language
   help, no flag required. Implemented as a sys.argv pre-rewrite in
   `main.py` before Typer parses anything.

2. **`mdk auth login` (no arg) → interactive provider picker** —
   numbered list of OpenAI / Anthropic / Azure / Gemini / Lyzr / Telegram.
   Operators without prior knowledge can browse.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import _expand_help_alias, app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Item 1: `help` token alias for --help
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHelpAliasRewrite:
    """The sys.argv rewrite happens before Typer parses, so we test the
    helper in isolation with a stubbed argv."""

    def _run_with_argv(self, argv: list[str]) -> list[str]:
        """Run the rewrite against a custom argv and return the result."""
        saved = sys.argv
        try:
            sys.argv = argv[:]  # copy so the helper mutates a fresh list
            _expand_help_alias()
            return sys.argv[:]
        finally:
            sys.argv = saved

    def test_help_token_alone_swapped(self) -> None:
        result = self._run_with_argv(["mdk", "help"])
        assert result == ["mdk", "--help"]

    def test_help_after_subcommand_swapped(self) -> None:
        result = self._run_with_argv(["mdk", "init", "help"])
        assert result == ["mdk", "init", "--help"]

    def test_help_after_sub_app_swapped(self) -> None:
        result = self._run_with_argv(["mdk", "auth", "login", "help"])
        assert result == ["mdk", "auth", "login", "--help"]

    def test_question_mark_also_aliased(self) -> None:
        """`mdk ?` reads as "what does this do?" — alias to --help too."""
        result = self._run_with_argv(["mdk", "init", "?"])
        assert result == ["mdk", "init", "--help"]

    def test_help_after_flag_value_left_alone(self) -> None:
        """`--llm help` means the description is the word "help" —
        don't intercept as a help request."""
        result = self._run_with_argv(["mdk", "init", "x", "--llm", "help"])
        # Last arg is "help" but preceded by "--llm" (a flag); leave alone.
        assert result == ["mdk", "init", "x", "--llm", "help"]

    def test_no_args_no_change(self) -> None:
        result = self._run_with_argv(["mdk"])
        assert result == ["mdk"]

    def test_explicit_help_flag_not_double_processed(self) -> None:
        result = self._run_with_argv(["mdk", "init", "--help"])
        assert result == ["mdk", "init", "--help"]

    def test_h_short_flag_skips_alias(self) -> None:
        """If -h is already present, don't add --help on top."""
        result = self._run_with_argv(["mdk", "-h"])
        assert result == ["mdk", "-h"]

    def test_opt_out_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MDK_NO_HELP_ALIAS=1 disables the rewrite entirely — needed
        for the rare case where `help` is genuinely a payload value."""
        monkeypatch.setenv("MDK_NO_HELP_ALIAS", "1")
        result = self._run_with_argv(["mdk", "init", "help"])
        assert result == ["mdk", "init", "help"]


@pytest.mark.unit
class TestHelpAliasEndToEnd:
    """End-to-end smoke through Typer's CliRunner. CliRunner doesn't
    invoke main.py's module-level _expand_help_alias() — it goes
    straight to the Typer app. So these tests verify Typer's own
    behavior matches what we'd see after the rewrite."""

    def test_mdk_init_help_renders_usage(self) -> None:
        result = runner.invoke(app, ["init", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "Usage: " in result.stdout
        assert "init" in result.stdout

    def test_mdk_auth_help_lists_subcommands(self) -> None:
        result = runner.invoke(app, ["auth", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # Help table includes the create-key / login / status subcommands.
        for sub in ("create-key", "list-keys", "revoke-key", "login", "status"):
            assert sub in result.stdout


# ---------------------------------------------------------------------------
# Item 2: interactive provider picker in `mdk auth login`
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderPicker:
    def test_no_arg_renders_picker_then_dispatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When `mdk auth login` is called with no provider arg, the
        picker prompts. Typing "1" selects OpenAI; we pipe a --key +
        --no-verify so the rest of the flow doesn't hang on more
        prompts."""
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds"))
        result = runner.invoke(
            app,
            ["auth", "login", "--key", "sk-test", "--no-verify"],
            input="1\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Picker prompt surfaced.
        assert "Which provider" in result.stdout
        # OpenAI was chosen + the key landed in the store.
        from movate.credentials import CredentialsStore  # noqa: PLC0415

        assert CredentialsStore().get("OPENAI_API_KEY") == "sk-test"

    def test_picker_lists_all_supported_providers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every supported provider should appear in the numbered list
        — operators discover what MDK supports without reading the
        --help table."""
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds"))
        result = runner.invoke(
            app,
            ["auth", "login", "--key", "sk-test", "--no-verify"],
            input="1\n",
            env={"COLUMNS": "200"},
        )
        for provider in ("OpenAI", "Anthropic", "Azure", "Gemini", "Lyzr", "Telegram"):
            assert provider in result.stdout, f"missing provider in picker: {provider}"

    def test_picker_accepts_typed_provider_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operators who already know the provider can skip the
        numeric pick by typing the name."""
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds"))
        result = runner.invoke(
            app,
            ["auth", "login", "--key", "ant-test", "--no-verify"],
            input="anthropic\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        from movate.credentials import CredentialsStore  # noqa: PLC0415

        assert CredentialsStore().get("ANTHROPIC_API_KEY") == "ant-test"

    def test_picker_out_of_range_choice_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds"))
        result = runner.invoke(
            app,
            ["auth", "login", "--key", "x", "--no-verify"],
            input="99\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        assert "out of range" in result.stderr.lower()

    def test_explicit_provider_skips_picker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The picker only fires when provider is None. Passing one
        explicitly bypasses it — the existing flow."""
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds"))
        result = runner.invoke(
            app,
            ["auth", "login", "openai", "--key", "sk-test", "--no-verify"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # No picker prompt appeared.
        assert "Which provider" not in result.stdout
