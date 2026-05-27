"""Intuitiveness + performance polish batch for ``mdk init --llm`` (5 parts).

Covers the polish shipped together as one PR:

1. **First-class help/text** — no user-facing "Phase 2/3" tags remain in
   ``--help`` or printed runtime messages; ``--llm`` reads as shipped.
2. **Model named in the scaffold spinner** — the spinner line names the
   model that will run and flags the offline mock path.
3. **Thin-description nudge** — a very short ``--llm`` description prints a
   yellow advisory hint to stderr but still PROCEEDS; a normal description
   does not warn.
4. **dry-run vs mock help clarity** — the ``--dry-run`` help explains it
   still calls the model; ``--mock`` help explains it is the offline path.
5. **Cacheable meta-prompt prefix** — ``_META_PROMPT`` is restructured into
   a static prefix (schema + constraints + examples) followed by a variable
   suffix (description/name/target model). No per-call variable appears
   before the static block, and the mock's detection markers survive.

All provider/network calls are mocked; no real ``~/.movate`` is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


_PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LYZR_API_KEY",
)


def _strip_all_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Part 1 + Part 4 — help text (first-class wording; dry-run vs mock clarity)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitHelpText:
    def test_help_drops_phase_language(self) -> None:
        """`mdk init --help` no longer advertises the feature as 'Phase 2'."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        assert "Phase 2" not in result.stdout
        assert "Phase 3" not in result.stdout

    def test_dry_run_help_mentions_calling_the_model(self) -> None:
        """`--dry-run` help makes clear it STILL calls the model."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        # Typer may wrap help across lines; normalize whitespace before matching.
        flat = " ".join(result.stdout.split())
        assert "calls the model" in flat.lower()

    def test_mock_help_mentions_offline(self) -> None:
        """`--mock` help makes clear it is the OFFLINE path."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        flat = " ".join(result.stdout.split())
        assert "offline" in flat.lower()


# ---------------------------------------------------------------------------
# Part 3 — thin/vague description nudge (warn, don't block)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestThinDescriptionNudge:
    def test_short_description_warns_but_proceeds(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A one-word description prints the nudge yet still scaffolds
        (mock path) — the warning is advisory, never blocking."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "thin-agent",
                "--llm",
                "chatbot",  # 1 word / 7 chars → below both thresholds
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )
        # Proceeds to a successful scaffold despite the warning.
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "thin-agent" / "agent.yaml").is_file()
        # Advisory nudge surfaced on stderr.
        assert "short description" in result.stderr.lower()

    def test_normal_description_does_not_warn(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reasonable description does NOT trigger the nudge."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "normal-agent",
                "--llm",
                "An FAQ assistant that answers SaaS pricing questions concisely",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "normal-agent" / "agent.yaml").is_file()
        assert "short description" not in result.stderr.lower()


# ---------------------------------------------------------------------------
# Part 5 — cacheable static meta-prompt prefix
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCacheableMetaPromptPrefix:
    def _format(self, *, description: str, name: str, target_model: str) -> str:
        from movate.scaffold.llm_scaffold import (  # noqa: PLC0415
            _EXAMPLE_CLASSIFIER,
            _EXAMPLE_EXTRACTION,
            _EXAMPLE_FAQ,
            _EXAMPLE_RAG,
            _EXAMPLE_SUMMARIZER,
            _META_PROMPT,
        )

        return _META_PROMPT.format(
            description=description,
            name=name,
            target_model=target_model,
            example_faq=_EXAMPLE_FAQ,
            example_classifier=_EXAMPLE_CLASSIFIER,
            example_summarizer=_EXAMPLE_SUMMARIZER,
            example_extraction=_EXAMPLE_EXTRACTION,
            example_rag=_EXAMPLE_RAG,
        )

    def test_static_block_precedes_variable_suffix(self) -> None:
        """Schema + constraints + both example bodies appear BEFORE the
        `USER DESCRIPTION:` line — i.e. nothing variable leads the prompt."""
        prompt = self._format(
            description="a unique description string",
            name="prefix-agent",
            target_model="anthropic/claude-haiku-4-5-20251001",
        )
        i_schema = prompt.index("GENERATEDAGENT SCHEMA")
        i_constraints = prompt.index("HARD CONSTRAINTS")
        i_faq = prompt.index("EXAMPLE 1 (FAQ")
        i_classifier = prompt.index("EXAMPLE 2 (Classifier agent)")
        # F2 (#111): new shape exemplars slot in BETWEEN classifier and RAG.
        i_summarizer = prompt.index("EXAMPLE 3 (Summarizer agent)")
        i_extraction = prompt.index("EXAMPLE 4 (Extraction agent)")
        i_rag = prompt.index("EXAMPLE 5 (Grounded RAG agent)")
        i_desc = prompt.index("USER DESCRIPTION:")
        i_name = prompt.index("AGENT NAME:")
        # Static blocks all come first; constraints ABOVE examples preserved.
        assert (
            i_schema
            < i_constraints
            < i_faq
            < i_classifier
            < i_summarizer
            < i_extraction
            < i_rag
            < i_desc
        )
        # Variable suffix markers come last.
        assert i_desc < i_name

    def test_no_variable_appears_before_static_block(self) -> None:
        """The portion of the prompt before `USER DESCRIPTION:` contains
        none of the per-call variable values."""
        description = "a unique description string"
        name = "prefix-agent"
        target_model = "gemini/gemini-1.5-flash"
        prompt = self._format(description=description, name=name, target_model=target_model)
        preamble = prompt[: prompt.index("USER DESCRIPTION:")]
        assert description not in preamble
        assert name not in preamble
        assert target_model not in preamble

    def test_prefix_is_byte_stable_across_calls(self) -> None:
        """The preamble (everything before the variable suffix) is
        byte-identical regardless of description/name/model — the property
        that makes it a cacheable prompt prefix."""
        p1 = self._format(
            description="first description",
            name="agent-one",
            target_model="anthropic/claude-haiku-4-5-20251001",
        )
        p2 = self._format(
            description="a completely different description",
            name="agent-two",
            target_model="gemini/gemini-1.5-flash",
        )
        pre1 = p1[: p1.index("USER DESCRIPTION:")]
        pre2 = p2[: p2.index("USER DESCRIPTION:")]
        assert pre1 == pre2
        # And it's a substantial prefix (the ~6 KB preamble), not a stub.
        assert len(pre1) > 3000

    def test_mock_markers_survive_in_static_prefix(self) -> None:
        """The mock's scaffold-detection markers still appear, and live in
        the static prefix (before the variable suffix)."""
        from movate.providers.mock import (  # noqa: PLC0415
            _SCAFFOLD_PROMPT_MARKERS,
            _looks_like_scaffold_prompt,
        )

        prompt = self._format(
            description="x", name="marker-agent", target_model="openai/gpt-4o-mini-2024-07-18"
        )
        preamble = prompt[: prompt.index("USER DESCRIPTION:")]
        for marker in _SCAFFOLD_PROMPT_MARKERS:
            assert marker in preamble, marker
        assert _looks_like_scaffold_prompt(prompt)

    def test_bare_mock_still_scaffolds_end_to_end(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reordered prompt doesn't break offline `--mock`: a bare mock
        run (no MOVATE_MOCK_RESPONSE) still writes a loadable agent."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "reordered-mock-agent",
                "--llm",
                "An assistant that summarizes incoming support tickets briefly",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        target = tmp_path / "reordered-mock-agent"
        assert (target / "agent.yaml").is_file()
        from movate.core.loader import load_agent  # noqa: PLC0415

        bundle = load_agent(target)
        assert bundle.spec.name == "reordered-mock-agent"


# ---------------------------------------------------------------------------
# Part 2 — model named in the scaffold spinner
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSpinnerModelLabel:
    def test_mock_spinner_message_names_model_and_offline(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The spinner message names the model and flags the offline mock.

        The spinner itself is a no-op on non-TTY (CliRunner), so we capture
        the message string handed to `spinner(...)` to assert its shape
        without depending on terminal rendering."""
        import movate.cli._progress as progress_mod  # noqa: PLC0415

        captured: list[str] = []
        real_spinner = progress_mod.spinner

        from contextlib import contextmanager  # noqa: PLC0415

        @contextmanager
        def _recording_spinner(message: str, **kwargs: object):  # type: ignore[no-untyped-def]
            captured.append(message)
            with real_spinner(message, **kwargs):  # type: ignore[arg-type]
                yield

        monkeypatch.setattr(progress_mod, "spinner", _recording_spinner)

        _strip_all_provider_keys(monkeypatch)
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "spinner-agent",
                "--llm",
                "An assistant that answers product questions for customers",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert captured, "spinner was never invoked"
        first = captured[0]
        assert "spinner-agent" in first
        # Default mock model is the openai default; offline flagged.
        assert "openai/gpt-4o-mini-2024-07-18" in first
        assert "mock, offline" in first
