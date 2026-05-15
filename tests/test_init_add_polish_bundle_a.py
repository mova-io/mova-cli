"""Polish Bundle A — five UX improvements to `mdk init` + `mdk add`.

1. **Batch-add**: `mdk add rag-qa ticket-triager code-reviewer` scaffolds
   all three in one command. Existing single-template + rename form
   still works via heuristic (second arg is NOT a valid template).
2. **Auto-validate after scaffold**: every `mdk add` success runs
   `load_agent()` on the result and surfaces the outcome in both
   the Panel and the `mdk_add_summary:` line. `--no-validate` opts out.
3. **`mdk add --list --search "<term>"`**: substring filter against
   template name + description + feature highlight.
4. **`mdk init` empty-cwd hint**: error message when no args + not
   inside a project points operators at `mdk init --project`.
5. **Grouped `--list` output**: role catalog renders with a Use-case
   column (Support / Sales / Engineering / Knowledge / HR / Compliance)
   instead of flat alphabetical.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Same fixture pattern as test_add_cmd.py — minimal valid project."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


# ---------------------------------------------------------------------------
# Item 1: batch-add
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBatchAdd:
    def test_batch_adds_three_agents_in_one_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app,
            ["add", "rag-qa", "ticket-triager", "code-reviewer"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # All three landed.
        assert (proj / "agents" / "rag-qa" / "agent.yaml").is_file()
        assert (proj / "agents" / "ticket-triager" / "agent.yaml").is_file()
        assert (proj / "agents" / "code-reviewer" / "agent.yaml").is_file()
        # Three summary lines emitted, one per agent.
        assert result.stdout.count("mdk_add_summary:") == 3

    def test_single_template_with_rename_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backwards compat: `mdk add rag-qa pricing-qa` must continue
        to mean 'add rag-qa, rename to pricing-qa' — not 'add two
        templates rag-qa and pricing-qa' (which would fail because
        pricing-qa isn't a template)."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "rag-qa", "pricing-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        # The agent landed under the rename.
        assert (proj / "agents" / "pricing-qa" / "agent.yaml").is_file()
        # And NOT under the template name.
        assert not (proj / "agents" / "rag-qa").exists()

    def test_batch_rejects_rename_with_multiple_templates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--name` plus multiple positional templates is incoherent
        (all renamed to the same dir). Reject loudly."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app,
            ["add", "rag-qa", "ticket-triager", "--name", "foo"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        assert "cannot rename" in result.stderr.lower()

    def test_batch_validates_all_templates_up_front(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo in the third slot must error BEFORE the first two
        templates get scaffolded — partial-success is worse than
        no-success for a batch command."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app,
            ["add", "rag-qa", "ticket-triager", "bogus-template"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        # Nothing should have been written.
        assert not (proj / "agents" / "rag-qa").exists()
        assert not (proj / "agents" / "ticket-triager").exists()
        assert "unknown template" in result.stderr.lower()
        assert "bogus-template" in result.stderr

    def test_name_flag_works_as_explicit_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new `--name` flag is the unambiguous rename form
        (positional rename only kicks in when there are exactly 2
        args). Useful in scripts where the rename target might
        coincidentally be a template name."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["add", "rag-qa", "--name", "qa-engine"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (proj / "agents" / "qa-engine" / "agent.yaml").is_file()

    def test_name_conflicts_with_positional_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app,
            ["add", "rag-qa", "x", "--name", "y"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        assert "--name" in result.stderr


# ---------------------------------------------------------------------------
# Item 2: auto-validate after scaffold
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoValidate:
    def test_panel_includes_validates_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        # The success Panel should include a "Validates:" row with ✓ ok.
        assert "Validates:" in result.stdout
        assert "✓ ok" in result.stdout

    def test_summary_line_validates_field_true_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "validates=true" in result.stdout

    def test_no_validate_flag_skips_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["add", "rag-qa", "--no-validate"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        # No Validates: row in the Panel
        assert "Validates:" not in result.stdout
        # Summary line marks it skipped.
        assert "validates=skipped" in result.stdout


# ---------------------------------------------------------------------------
# Item 3: --search filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSearchFilter:
    def test_search_filters_role_templates(self) -> None:
        result = runner.invoke(
            app, ["add", "--list", "--search", "support"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        # Filtered title surfaces the search term.
        assert "filtered by 'support'" in result.stdout
        # Support-bucket templates appear.
        assert "ticket-triager" in result.stdout
        # Other roles do NOT appear when filtering for "support".
        # rag-qa is "knowledge work" not "support" — its description
        # doesn't contain "support", so it should be filtered out.
        # (Defensive: if rag-qa's description ever mentions "support",
        # adjust the search term in this test.)
        lines = result.stdout
        # Code-reviewer is engineering — should NOT match "support".
        assert "code-reviewer" not in lines

    def test_search_no_matches_renders_hint(self) -> None:
        result = runner.invoke(
            app,
            ["add", "--list", "--search", "definitely-no-match-xyz"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # Both tiers should report no matches.
        assert "no role templates match" in result.stdout.lower()
        assert "no core templates match" in result.stdout.lower()

    def test_search_matches_description_substring(self) -> None:
        """Search should hit the description, not just the name.
        `--search citation` finds rag-qa even though 'citation' isn't
        in the template name."""
        result = runner.invoke(
            app, ["add", "--list", "--search", "citation"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        assert "rag-qa" in result.stdout


# ---------------------------------------------------------------------------
# Item 4: `mdk init` empty-cwd hint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitEmptyCwdHint:
    def test_no_args_outside_project_suggests_project_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        # The empty-cwd hint variant fires.
        assert "not in a movate project" in result.stderr.lower()
        assert "mdk init --project" in result.stderr

    def test_no_args_inside_project_keeps_old_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If you're inside a project AND forget the agent name, the
        empty-cwd hint should NOT fire (you're not in an empty dir —
        you just forgot an argument)."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["init"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        # Generic hint instead.
        assert "not in a movate project" not in result.stderr.lower()
        assert "mdk init --help" in result.stderr


# ---------------------------------------------------------------------------
# Item 5: grouped --list output
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroupedList:
    def test_list_has_use_case_column(self) -> None:
        result = runner.invoke(app, ["add", "--list"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # New "Use case" column header surfaces.
        assert "Use case" in result.stdout
        # Each group label appears.
        for group in (
            "Support",
            "Sales / GTM",
            "Engineering",
            "Knowledge work",
            "HR / Recruiting",
            "Compliance / Ops",
        ):
            assert group in result.stdout, f"expected group {group!r} in --list output"

    def test_list_still_includes_core_table(self) -> None:
        """Grouping applies to the role tier; the core templates table
        should still render (alphabetical, no use-case column)."""
        result = runner.invoke(app, ["add", "--list"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # Core tier header remains.
        assert "Core templates" in result.stdout
        # Core templates appear.
        assert "faq" in result.stdout
        assert "summarizer" in result.stdout
