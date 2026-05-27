"""Phase 1 of mdk init --llm: CLI surface only.

These tests verify the CLI contract that Phase 2 relies on:

1. ``--llm`` / ``--llm-model`` / ``--dry-run`` flags are accepted.
2. ``--llm`` + ``--project`` errors with code 2 (mutually exclusive).
3. ``--llm ""`` (empty description) exits 2 BEFORE building any runtime.
4. ``--dry-run`` without ``--llm`` warns but does not error.
5. Existing ``mdk init <name>`` (no ``--llm``) flow is unchanged.

Tests that exercise the generator end-to-end (template-warning paths,
captured-args echo, MockProvider behavior) moved to
``test_init_llm_phase_2.py`` where the runtime is wired through.
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
    locks in that it's present.

    We strip ANSI escapes before substring-checking because CI runs
    with ``FORCE_COLOR=1``, which causes Rich to insert escape
    sequences *inside* the option name (``--`` and ``llm`` get
    styled as separate spans). A raw substring check on the styled
    output misses the flag entirely."""
    import re  # noqa: PLC0415

    result = runner.invoke(app, ["init", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", result.stdout)
    assert "--llm" in plain
    assert "--llm-model" in plain
    assert "--dry-run" in plain


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
            "--bare",
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
# Item 3: empty --llm description
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_llm_description_errors_early(tmp_path: Path) -> None:
    """An empty description (whitespace-only included) must error
    before any LLM call. Phase 2 reuses this guard."""
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
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
# Item 4: --dry-run without --llm warns
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_without_llm_warns_but_succeeds(tmp_path: Path) -> None:
    """--dry-run is only meaningful with --llm today. Without --llm,
    we warn rather than error — muscle memory shouldn't break.

    Uses `-t default` to opt into agent mode explicitly; the May-2026
    default-change makes bare `mdk init <name>` project-mode, so a
    template flag is required to exercise the agent-mode dry-run path.
    """
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "my-agent",
            "-t",
            "default",
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
# Item 5: backwards compatibility — no --llm = unchanged behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_without_llm_unchanged(tmp_path: Path) -> None:
    """The template-copy flow must remain identical when --llm isn't
    passed. Operator opts into agent mode via `-t default` (post the
    May-2026 default-change, bare `mdk init <name>` is project mode)."""
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "my-agent",
            "-t",
            "default",
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
            "--bare",
            "--project",
            "myproj",
            "--target",
            str(tmp_path),
            "--skip-snapshot",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "myproj" / "project.yaml").is_file()
    assert (tmp_path / "myproj" / "agents").is_dir()
