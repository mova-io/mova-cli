"""Tests for the core-command polish batch (post-MVP cleanup).

Covers six small fixes folded into one PR rather than spawned as
individual sessions:

1. ``mdk validate`` (no args) inside a project defaults to ``--all``.
2. ``mdk init -t <template>`` next-steps use ``mdk`` (canonical),
   not the legacy ``movate`` binary name.
3. ``mdk add <bad-template>`` fuzzy-matches via difflib edit-distance
   (not just substring).
4. ``mdk run`` emits a greppable ``mdk_run_summary:`` line; same for
   ``mdk deploy --dry-run`` (``mdk_deploy_summary:``).
5. ``mdk templates list`` subcommand exists and renders every template.
6. Typer's unknown-command path injects a fuzzy "Did you mean" hint
   for typos Click's built-in didn't catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import TEMPLATES

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# #1 — validate (no args) inside a project defaults to --all
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_no_args_inside_project_defaults_to_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk validate` (no path, no --all) inside a project sweeps all
    agents + workflows instead of erroring with "path required."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    # No agents yet — sweep should still succeed (vacuous-pass)
    result = runner.invoke(app, ["validate"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # Greppable summary should fire (project-level validate path).
    assert "mdk_validate_summary:" in result.stdout
    # The "no path; defaulting to --all" hint should appear too.
    assert "defaulting to" in result.stdout and "--all" in result.stdout


@pytest.mark.unit
def test_validate_no_args_outside_project_still_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside any project, no-args validate errors with a hint that
    points at the missing project marker — we have nothing to sweep."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "not inside a movate project" in combined.lower()


# ---------------------------------------------------------------------------
# #2 — init -t <template> next-steps say mdk, not movate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_template_mode_next_steps_use_mdk_not_movate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk init my-agent -t faq` next-steps print `mdk validate` /
    `mdk run`, not `movate validate` / `movate run`. Mixing binary
    names in user-facing strings is confusing even when both work."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "my-agent", "-t", "faq"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # `mdk validate <path>` should appear.
    assert "mdk validate" in result.stdout
    assert "mdk run" in result.stdout
    # The legacy "movate validate" / "movate run" strings should NOT.
    # (Substring is fine — the agent's path itself doesn't contain
    # "movate validate".)
    assert "movate validate" not in result.stdout
    assert "movate run" not in result.stdout


# ---------------------------------------------------------------------------
# #5 — mdk add fuzzy-matches unknown template via edit distance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAddFuzzyTemplateMatch:
    def _bootstrap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        monkeypatch.chdir(tmp_path / "proj")

    def test_typo_with_missing_chars_still_suggests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk add rqa` (missing 'ag-') should suggest rag-qa via
        edit-distance fallback — substring alone misses this."""
        self._bootstrap(tmp_path, monkeypatch)
        result = runner.invoke(app, ["add", "rqa"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "rag-qa" in combined.lower()

    def test_typo_with_swapped_chars_suggests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk add chtabot` (swapped a/t) should still find chatbot."""
        self._bootstrap(tmp_path, monkeypatch)
        result = runner.invoke(app, ["add", "chtabot"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "chatbot" in combined.lower()

    def test_unrelated_word_does_not_suggest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk add deploy` should NOT suggest a template — fall back
        to "available: ..." list rather than a false-positive match."""
        self._bootstrap(tmp_path, monkeypatch)
        result = runner.invoke(app, ["add", "totally-unrelated-name"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        # Should fall back to listing available templates, not a fuzzy guess.
        assert "available:" in combined.lower()


# ---------------------------------------------------------------------------
# #6 — run + deploy emit greppable summary lines
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_emits_mdk_run_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk run <agent> --mock '{...}'` writes mdk_run_summary: to
    stderr — CI workflows can scrape ok=true|false from there."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    result = runner.invoke(
        app,
        ["run", "./agents/faq", "--mock", '{"question": "test?"}'],
        env={"COLUMNS": "200"},
    )
    # Summary line goes to stderr (so stdout JSON stays clean).
    combined = result.stdout + result.stderr
    assert "mdk_run_summary:" in combined
    assert "kind=agent" in combined
    assert "agent=faq" in combined


@pytest.mark.unit
def test_deploy_dry_run_emits_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk deploy --target X --dry-run` should emit
    mdk_deploy_summary: with dry_run=true ok=true so CI workflows can
    confirm the plan parsed without actually deploying."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    # Register a fake target with the minimum required fields.
    monkeypatch.chdir(tmp_path)
    # Post-PR-#94: deploy preflight requires a Dockerfile in cwd. Touch
    # one so the preflight passes for this dry-run-shape test.
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    result = runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
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
    result = runner.invoke(
        app,
        ["deploy", "--target", "fake", "--dry-run"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "mdk_deploy_summary:" in combined
    assert "dry_run=true" in combined
    assert "ok=true" in combined


# ---------------------------------------------------------------------------
# #8 — `mdk templates list` exists + renders every template
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_templates_list_shows_every_template() -> None:
    """`mdk templates list` should render a row for every entry in
    the TEMPLATES registry — operators discover available scaffolds
    without grepping the source or reading --help."""
    result = runner.invoke(app, ["templates", "list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # Each template name should appear in the rendered table.
    for name in TEMPLATES:
        assert name in result.stdout, f"template {name!r} missing from list"
    # The footer hint should point operators at the natural next step.
    assert "mdk init" in result.stdout
    assert "mdk add" in result.stdout


# ---------------------------------------------------------------------------
# #11 — Typer unknown-command fuzzy suggestion
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFuzzyCommandSuggestion:
    def test_compound_typo_gets_suggestion(self) -> None:
        """`mdk init-stuff` should suggest `init` — Click's built-in
        misses this (tighter threshold) but our fallback catches it."""
        result = runner.invoke(app, ["init-stuff"], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        combined = result.stdout + result.stderr
        assert "init" in combined.lower()
        # Either Click's "Did you mean" or ours — the exact phrasing
        # doesn't matter, just that A suggestion fires.
        assert "did you mean" in combined.lower()

    def test_unrelated_command_does_not_double_suggest(self) -> None:
        """When Click's built-in already proposed a match, we shouldn't
        layer a second 'Did you mean: ...' line on top."""
        # `delpoy` is a high-confidence typo for `deploy` — Click
        # ships a suggestion for this one. Confirm only ONE "Did you
        # mean" line appears in the output.
        result = runner.invoke(app, ["delpoy"], env={"COLUMNS": "200"})
        combined = result.stdout + result.stderr
        assert combined.lower().count("did you mean") == 1
