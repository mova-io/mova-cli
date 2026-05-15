"""``--at`` alias for ``--target`` + absolute-path next-steps polish.

Two ergonomic wins around project location:

1. ``mdk init --project foo --at ~/work`` works identically to
   ``--target ~/work``. The alias reads more naturally for the
   project case where ``target`` is the parent of where the project
   lives.

2. The success Panel's ``cd ...`` line shows the absolute path when
   the project is outside cwd, so the operator's copy-paste actually
   lands them in the right directory. The default (project in cwd)
   keeps the short bare name for readability.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.init import _cd_target
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# --at alias
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAtAlias:
    def test_at_alias_scaffolds_project_at_explicit_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--at <dir>`` is an alias for ``--target <dir>``."""
        elsewhere = tmp_path / "explicit-elsewhere"
        elsewhere.mkdir()
        # cwd is tmp_path (NOT inside elsewhere); --at points elsewhere.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--at",
                str(elsewhere),
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (elsewhere / "support-bot" / "movate.yaml").is_file()
        # cwd was unaffected — no support-bot/ in tmp_path itself.
        assert not (tmp_path / "support-bot").exists()

    def test_target_and_at_behave_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--at`` and ``--target`` should produce bit-for-bit identical
        project layouts when given the same path."""
        a_dir = tmp_path / "via-at"
        t_dir = tmp_path / "via-target"
        a_dir.mkdir()
        t_dir.mkdir()
        monkeypatch.chdir(tmp_path)

        r_at = runner.invoke(
            app,
            ["init", "--project", "p", "--skip-snapshot", "--at", str(a_dir)],
        )
        r_t = runner.invoke(
            app,
            ["init", "--project", "p", "--skip-snapshot", "--target", str(t_dir)],
        )
        assert r_at.exit_code == 0 and r_t.exit_code == 0

        # Same files exist in each.
        for fname in ("movate.yaml", ".env.example", ".gitignore"):
            assert (a_dir / "p" / fname).is_file()
            assert (t_dir / "p" / fname).is_file()
            # Same content (the movate.yaml has a project-name header
            # that depends on the name only, which matches both runs).
            assert (a_dir / "p" / fname).read_text() == (t_dir / "p" / fname).read_text()


# ---------------------------------------------------------------------------
# Absolute-path cd line in the success Panel
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCdTargetHelper:
    def test_cd_target_returns_name_when_inside_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default flow — project at ``./support-bot/`` → cd line says
        ``cd support-bot`` (short, copy-paste-friendly)."""
        monkeypatch.chdir(tmp_path)
        project_root = tmp_path / "support-bot"
        project_root.mkdir()
        assert _cd_target(project_root) == "support-bot"

    def test_cd_target_returns_abs_path_when_outside_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--at ~/elsewhere`` flow — project_root is outside cwd, so
        the cd line MUST use the absolute path or it won't work."""
        cwd_dir = tmp_path / "cwd"
        elsewhere = tmp_path / "elsewhere" / "support-bot"
        cwd_dir.mkdir()
        elsewhere.mkdir(parents=True)
        monkeypatch.chdir(cwd_dir)
        assert _cd_target(elsewhere) == str(elsewhere)

    def test_cd_target_returns_abs_when_in_place_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-place bootstrap (project_root == cwd) — `cd .` is nonsense;
        fall back to absolute path."""
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.chdir(proj)
        # _cd_target's relative_to gives ".", so it returns absolute.
        assert _cd_target(proj) == str(proj)


@pytest.mark.unit
class TestSuccessPanelUsesAbsPath:
    def test_panel_uses_absolute_path_when_target_outside_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The success Panel's ``cd`` line uses the absolute path when
        ``--at`` points outside cwd, so the suggestion is actionable
        regardless of where the operator invoked ``mdk init`` from."""
        cwd_dir = tmp_path / "cwd"
        elsewhere = tmp_path / "elsewhere"
        cwd_dir.mkdir()
        elsewhere.mkdir()
        monkeypatch.chdir(cwd_dir)

        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--at",
                str(elsewhere),
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # The absolute path appears in the cd line.
        expected_abs = str(elsewhere / "support-bot")
        assert expected_abs in result.stdout
        # And the bare-name short form does NOT appear as a cd target —
        # we look for "cd support-bot" (no leading slash) to confirm
        # the panel didn't lazily use just the name.
        # NOTE: the project name might appear elsewhere in the body
        # (Project:, Path:), so we narrow to "cd ".
        bad_cd_lines = [
            line
            for line in result.stdout.splitlines()
            if "cd support-bot" in line and "cd " in line
        ]
        # Only acceptable form: `cd <abs-path>/support-bot`.
        for line in bad_cd_lines:
            assert expected_abs in line, f"cd line should use absolute path, got: {line}"

    def test_panel_uses_short_name_when_target_inside_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default flow — no --at / --target. cd line uses the short
        bare name (back-compat with existing behavior + tests)."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "support-bot", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # "cd support-bot" appears verbatim — no absolute path.
        assert "cd support-bot" in result.stdout

    def test_panel_uses_abs_path_with_at_in_combined_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The combined Workspace Panel (with --with-agents) ALSO uses
        the absolute cd path when --at points outside cwd."""
        cwd_dir = tmp_path / "cwd"
        elsewhere = tmp_path / "elsewhere"
        cwd_dir.mkdir()
        elsewhere.mkdir()
        monkeypatch.chdir(cwd_dir)

        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--at",
                str(elsewhere),
                "--with-agents",
                "rag-qa",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Combined Workspace Panel title fires.
        assert "Workspace ready" in result.stdout
        # Absolute path appears in the cd suggestion.
        expected_abs = str(elsewhere / "support-bot")
        assert expected_abs in result.stdout
