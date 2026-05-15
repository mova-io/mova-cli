"""Phase 1 of mdk init --llm: CLI surface only.

Phase 1 wires up the flags + mutual-exclusion guards + dispatch path
so Phase 2's generator can drop in without churning the CLI surface.
The generator itself is a stub today — invocation exits 2 with a
"not yet implemented" message.

These tests verify the CLI contract Phase 2 will rely on:

1. ``--llm`` is accepted as an option (parses cleanly).
2. ``--llm`` + ``--project`` errors with code 2 (mutually exclusive).
3. ``--llm`` + non-default ``--template`` warns but does not error.
4. ``--llm "..."`` invokes the stub and exits 2 with the expected
   captured-args block on stderr.
5. ``--llm ""`` (empty description) exits 2 with a clear error.
6. ``--dry-run`` without ``--llm`` warns but does not error.
7. Existing ``mdk init <name>`` flow (no ``--llm``) is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.init import _DEFAULT_LLM_MODEL
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Item 1: --llm flag parses
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_flag_appears_in_help() -> None:
    """The --llm option must surface in `mdk init --help` so operators
    can discover it. Phase 2 will keep the help text; Phase 1 just
    locks in that it's present."""
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--llm" in result.stdout
    assert "--llm-model" in result.stdout
    assert "--dry-run" in result.stdout


@pytest.mark.unit
def test_default_llm_model_is_cheap_openai() -> None:
    """The default --llm-model should be a cheap, JSON-mode-reliable
    model. If this changes, intentional review is required (cost +
    Phase 2 prompt tuning depend on this baseline)."""
    assert _DEFAULT_LLM_MODEL == "openai/gpt-4o-mini-2024-07-18"


# ---------------------------------------------------------------------------
# Item 2: --llm + --project mutual exclusion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_with_project_errors_with_pointer(tmp_path: Path) -> None:
    """`--llm` is for agent scaffolding, not project bootstrap. The
    error must point operators at the right two-step flow."""
    result = runner.invoke(
        app,
        [
            "init",
            "--project",
            "myproj",
            "--llm",
            "an agent that does things",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    # Stderr should contain the right pointer (two-step flow).
    assert "agent scaffolding" in result.stderr.lower()
    assert "mdk init --project" in result.stderr
    # And it should NOT have written anything (errored before dispatch).
    assert not (tmp_path / "myproj").exists()


# ---------------------------------------------------------------------------
# Item 3: --llm + --template warns (does not error)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_with_non_default_template_warns_but_invokes_stub(tmp_path: Path) -> None:
    """`--llm` + `--template chatbot` is legal — the template becomes
    a few-shot starting point. Phase 1 just warns; Phase 2 will use
    the template in the meta-prompt."""
    result = runner.invoke(
        app,
        [
            "init",
            "my-agent",
            "--llm",
            "a chatbot for customer support",
            "--template",
            "chatbot",
            "--target",
            str(tmp_path),
        ],
    )
    # Still exits 2 because the generator stub is the terminal state in
    # Phase 1, but the warning AND the Phase 2 capture both fire.
    assert result.exit_code == 2
    assert "Phase 2" in result.stderr
    assert "template" in result.stderr.lower()


@pytest.mark.unit
def test_llm_with_default_template_does_not_warn(tmp_path: Path) -> None:
    """When --template is left at the default, the LLM+template warning
    should NOT fire (it would be noise — operators didn't ask for the
    combination explicitly)."""
    result = runner.invoke(
        app,
        [
            "init",
            "my-agent",
            "--llm",
            "an agent",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    # The combination-warning text shouldn't appear.
    assert "--template default" not in result.stderr
    # But the Phase-1 stub message still does.
    assert "Phase 2" in result.stderr


# ---------------------------------------------------------------------------
# Item 4: --llm invokes the stub and prints captured args
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_invocation_prints_captured_args(tmp_path: Path) -> None:
    """The Phase 1 stub must echo every flag through to stderr so
    reviewers can verify end-to-end wiring without a real LLM call.
    Phase 2 replaces this block with the actual generator output."""
    result = runner.invoke(
        app,
        [
            "init",
            "faq-agent",
            "--llm",
            "FAQ agent for our SaaS pricing",
            "--llm-model",
            "anthropic/claude-sonnet-4",
            "--target",
            str(tmp_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 2
    # All the captured flags must appear in the Phase-2-capture block.
    assert "faq-agent" in result.stderr
    assert "FAQ agent for our SaaS pricing" in result.stderr
    assert "anthropic/claude-sonnet-4" in result.stderr
    assert "dry_run:     True" in result.stderr


@pytest.mark.unit
def test_llm_default_model_used_when_flag_omitted(tmp_path: Path) -> None:
    """When --llm-model isn't passed, the default must show up in the
    captured-args block. This is what Phase 2 will dispatch to the
    runtime."""
    result = runner.invoke(
        app,
        [
            "init",
            "agent",
            "--llm",
            "an agent",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert _DEFAULT_LLM_MODEL in result.stderr


# ---------------------------------------------------------------------------
# Item 5: empty --llm description
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_llm_description_errors_early(tmp_path: Path) -> None:
    """An empty description (whitespace-only included) must error
    before any LLM call. Phase 2 reuses this guard."""
    result = runner.invoke(
        app,
        [
            "init",
            "my-agent",
            "--llm",
            "   ",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "empty" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Item 6: --dry-run without --llm warns
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_without_llm_warns_but_succeeds(tmp_path: Path) -> None:
    """--dry-run is only meaningful with --llm today. Without --llm,
    we warn rather than error — muscle memory shouldn't break."""
    result = runner.invoke(
        app,
        [
            "init",
            "my-agent",
            "--target",
            str(tmp_path),
            "--dry-run",
        ],
    )
    # Template-copy path runs successfully even with the dry-run warning.
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "only meaningful" in result.stderr.lower()
    # And the agent directory was actually created (warning ≠ skip).
    assert (tmp_path / "my-agent").is_dir()


# ---------------------------------------------------------------------------
# Item 7: backwards compatibility — no --llm = unchanged behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_without_llm_unchanged(tmp_path: Path) -> None:
    """The original template-copy flow must remain bit-for-bit
    identical when --llm isn't passed."""
    result = runner.invoke(
        app,
        [
            "init",
            "my-agent",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Standard scaffold success markers — these are what existed before.
    assert (tmp_path / "my-agent" / "agent.yaml").is_file()
    assert (tmp_path / "my-agent" / "prompt.md").is_file()
    # No Phase 2 capture-args block appears (since --llm wasn't set).
    assert "Phase 2" not in result.stderr


@pytest.mark.unit
def test_init_project_without_llm_unchanged(tmp_path: Path) -> None:
    """Project bootstrap should also be unchanged when --llm isn't passed."""
    result = runner.invoke(
        app,
        [
            "init",
            "--project",
            "myproj",
            "--target",
            str(tmp_path),
            "--skip-snapshot",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "myproj" / "movate.yaml").is_file()
    assert (tmp_path / "myproj" / "agents").is_dir()
