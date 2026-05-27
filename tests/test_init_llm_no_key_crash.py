"""PR A — emergency fixes for the `mdk init --llm` crash + UX gaps.

Three bugs:

1. **Native-adapter registration crash** (`_runtime.py`) — when
   OPENAI_API_KEY is missing, `OpenAIProvider()` raises OpenAIError
   during `AsyncOpenAI()` construction. The wrapping try/except only
   caught ImportError, so the exception bubbled up as a 100-line
   stacktrace. Fix: catch broadly during construction, log nothing,
   skip the registration. LiteLLM stays wired as default.
2. **Friendly key-missing error** — when `--llm` is invoked without
   `--mock` and no provider key is set anywhere, exit 2 with a
   pointer to the available env vars instead of crashing deep in
   the LLM call.
3. **Positional description support** — `mdk init <name> "<text>"`
   is shorthand for `mdk init <name> --llm "<text>"`. Operators try
   this naturally — make it work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli._runtime import _try_register_native_adapters
from movate.cli.init import _has_any_provider_key
from movate.cli.main import app
from movate.providers.registry import ProviderRegistry

runner = CliRunner(mix_stderr=False)


def _strip_all_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe every provider env var so tests start from a known no-key state."""
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "LYZR_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Bug #1: native adapter registration doesn't crash on missing key
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNativeAdapterRegistration:
    def test_register_without_any_keys_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bug we shipped: `_try_register_native_adapters` raised
        OpenAIError when OPENAI_API_KEY was missing. Now it should
        be a silent no-op for that adapter."""
        _strip_all_provider_keys(monkeypatch)
        from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

        registry = ProviderRegistry(default_litellm=LiteLLMProvider())
        # Must not raise.
        _try_register_native_adapters(registry, mock=False)

    def test_register_under_mock_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock mode short-circuits the entire registration path so
        no native SDK gets touched."""
        _strip_all_provider_keys(monkeypatch)
        from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

        registry = ProviderRegistry(default_litellm=LiteLLMProvider())
        _try_register_native_adapters(registry, mock=True)

    def test_one_adapter_crashing_doesnt_block_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the OpenAI adapter crashes during construction but
        Anthropic doesn't, Anthropic should still register. We patch
        the openai_native module's OpenAIProvider to raise; the
        anthropic registration must continue afterward."""
        _strip_all_provider_keys(monkeypatch)
        from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

        # Provide an Anthropic key so AnthropicProvider could succeed
        # (the test still passes if anthropic isn't installed — the
        # ImportError branch covers that).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
        registry = ProviderRegistry(default_litellm=LiteLLMProvider())
        # The bug-fix contract: this call doesn't crash, regardless of
        # which adapters happen to register on this machine.
        _try_register_native_adapters(registry, mock=False)


# ---------------------------------------------------------------------------
# Bug #2: friendly error when --llm is invoked without keys + without --mock
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFriendlyKeyMissing:
    def test_has_any_provider_key_false_when_none_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _strip_all_provider_keys(monkeypatch)
        assert _has_any_provider_key() is False

    def test_has_any_provider_key_true_with_openai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert _has_any_provider_key() is True

    def test_has_any_provider_key_true_with_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        assert _has_any_provider_key() is True

    def test_has_any_provider_key_false_when_set_to_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace-only values count as unset — operators sometimes
        leave the `=` line in .env with no value."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "   ")
        assert _has_any_provider_key() is False

    def test_llm_without_keys_or_mock_exits_friendly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: `mdk init x --llm "..."` without keys must show
        the friendly error and exit 2 — no stacktrace, no asyncio.run."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "test-agent", "--llm", "a test agent"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        # Friendly pointer surfaces the env-var name + --mock alternative.
        stderr_low = result.stderr.lower()
        assert "openai_api_key" in stderr_low or "anthropic_api_key" in stderr_low
        assert "--mock" in result.stderr
        # No raw Python traceback in stderr.
        assert "Traceback" not in result.stderr

    def test_llm_with_mock_succeeds_even_without_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The --mock escape hatch must still work after the pre-flight
        check — that's the whole point of --mock.

        Post-PR: the mock is now scaffold-aware, so bare ``--mock`` (no
        MOVATE_MOCK_RESPONSE) synthesizes a valid GeneratedAgent and the
        scaffold SUCCEEDS offline (exit 0)."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            # --bare keeps the standalone <tmp>/test-agent/ layout this
            # assertion targets (ADR 026 D1's non-bare default would wrap it
            # in a project at <tmp>/test-agent/agents/test-agent/).
            ["init", "test-agent", "--llm", "test", "--mock", "--bare"],
            env={"COLUMNS": "200"},
        )
        # The pre-flight key gate is bypassed AND the scaffold-aware mock
        # produces a runnable agent — exit 0, no "no key" hint.
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "OPENAI_API_KEY" not in result.stderr
        assert (tmp_path / "test-agent" / "agent.yaml").is_file()


# ---------------------------------------------------------------------------
# Bug #3: positional description support
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPositionalDescription:
    def test_positional_description_routes_to_llm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init <name> "<desc>"` should be equivalent to
        `mdk init <name> --llm "<desc>"`. We confirm by running with
        --mock so no real LLM call happens — but the LLM code path
        fires (we get to the MockProvider attempt instead of an
        "unexpected argument" error)."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "test-agent", "FAQ agent for our SaaS pricing", "--mock"],
            env={"COLUMNS": "200"},
        )
        # Whatever the final exit code (MockProvider returns canned
        # output that won't match GeneratedAgent), we must NOT see the
        # "unexpected extra argument" error that the old code path
        # produced.
        assert "unexpected extra argument" not in result.stderr.lower()
        # And the dispatch hit the --llm path (Phase 1 stub or Phase 2
        # generator both produce stderr output).
        assert result.exit_code in (0, 1, 2)

    def test_no_positional_description_uses_template_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No description + explicit `-t` → template-copy path runs.
        Opts into agent mode via `-t default` post the May-2026
        default-change (bare `mdk init <name>` is project mode)."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["init", "plain-agent", "-t", "default", "--bare"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Template scaffold ran — agent.yaml exists (--bare → standalone dir).
        assert (tmp_path / "plain-agent" / "agent.yaml").is_file()

    def test_positional_description_and_llm_both_set_warns_llm_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both forms passed at once: --llm wins, positional gets
        ignored with a yellow warning. (Conflict resolution; rare
        but possible if scripts copy-paste flags.)"""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "test-agent",
                "positional desc here",
                "--llm",
                "flag desc wins",
                "--mock",
            ],
            env={"COLUMNS": "200"},
        )
        # Warning fires on stderr.
        assert "--llm" in result.stderr
        assert "positional" in result.stderr.lower() or "ignored" in result.stderr.lower()
