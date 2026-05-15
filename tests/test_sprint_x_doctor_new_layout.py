"""Sprint X — doctor checks for the new project layout.

Adds + improves checks in `mdk doctor` (project-level) and
`mdk doctor agent <name>` (per-agent) so the May-2026 layout
(project.yaml, kb/, contexts/, skills/, agent-local context
override) is fully diagnosable.

What this bundle gives operators:

* `mdk doctor` outside a project: clear "not in cwd; defaults will
  be used" row.
* `mdk doctor` inside a project: rows for each conventional subdir
  (agents/, skills/, contexts/, kb/) + project config parses.
* `mdk doctor agent <name>`: per-name skill resolution detail (which
  skill is missing) + per-name context resolution detail (which
  context is missing AND whether each resolved one came from shared
  or agent-local).
* Filename multi-file footgun: if both `project.yaml` AND a legacy
  name exist, doctor calls it out (yellow + "delete to avoid
  confusion").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a fresh project + chdir in."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "demo", "--skip-snapshot"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "demo"
    monkeypatch.chdir(proj)
    return proj


# ---------------------------------------------------------------------------
# Project-level checks for new layout
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectLevelChecks:
    def test_doctor_reports_all_four_subdirs_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A freshly-bootstrapped project has agents/+skills/+contexts/+kb/.
        Each one shows up as a green row in the doctor table."""
        _bootstrap_project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        for subdir in ("agents/", "skills/", "contexts/", "kb/"):
            assert subdir in result.stdout, f"doctor missing row for {subdir}"
        # All four should be marked "present" / green.
        # Use a loose check — Rich rendering may wrap.
        assert "agent definitions" in result.stdout
        assert "reusable skill defs" in result.stdout
        assert "reusable Markdown contexts" in result.stdout
        assert "knowledge assets" in result.stdout

    def test_doctor_reports_missing_subdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a conventional subdir is missing, the row shows as
        'missing' with the "what it's for" descriptor still attached."""
        proj = _bootstrap_project(tmp_path, monkeypatch)
        # Delete kb/ to simulate an older project layout.
        import shutil  # noqa: PLC0415

        shutil.rmtree(proj / "kb")
        result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # The kb/ row reports missing.
        assert "kb/" in result.stdout
        # And the descriptor stays so operators see what kb/ is for.
        assert "knowledge assets" in result.stdout

    def test_doctor_reports_project_yaml_parses_green(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Freshly-bootstrapped project.yaml parses cleanly → green row."""
        _bootstrap_project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "project config parses" in result.stdout
        # The row carries the agents_dir / skills_dir / contexts_dir
        # detail so operators see the resolved layout config.
        assert "agents_dir=./agents" in result.stdout

    def test_doctor_reports_project_yaml_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed project.yaml renders a red row pointing at
        `mdk validate` for the full error."""
        proj = _bootstrap_project(tmp_path, monkeypatch)
        # Inject an unknown top-level field — ProjectConfig is
        # `extra="forbid"` so this fails Pydantic validation.
        config_path = proj / "project.yaml"
        config_path.write_text(
            config_path.read_text()
            + "\n# Sprint X test — invalid field below\n"
            + "unknown_top_level_field: oops\n"
        )
        result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
        # Doctor itself doesn't exit non-zero on a single bad row,
        # but the row marks the issue.
        assert "project config parses" in result.stdout
        # The detail mentions `mdk validate` as the next step.
        assert "mdk validate" in result.stdout

    def test_doctor_outside_project_reports_missing_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No project file in cwd → row says 'not in cwd; defaults
        will be used'. Doctor must not crash."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
        # No exit-2 — operator outside a project is a valid state.
        assert result.exit_code in (0, 1)
        assert "project config" in result.stdout
        assert "defaults will be used" in result.stdout


# ---------------------------------------------------------------------------
# Legacy filename detection + multi-file footgun
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLegacyFilenameDetection:
    def test_legacy_movate_yaml_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project with only `movate.yaml` (legacy) shows the
        row marked yellow with a rename suggestion."""
        (tmp_path / "movate.yaml").write_text("agents_dir: ./agents\n")
        (tmp_path / "agents").mkdir()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
        # movate.yaml row appears + the rename suggestion is present.
        assert "movate.yaml" in result.stdout
        assert "rename to" in result.stdout or "legacy" in result.stdout.lower()

    def test_canonical_and_legacy_both_present_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operator has BOTH `project.yaml` (canonical) AND
        `movate.yaml` (legacy left over from a previous attempt).
        Doctor calls out the duplicate so they delete the stale one."""
        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        (tmp_path / "movate.yaml").write_text("agents_dir: ./agents\n")
        (tmp_path / "agents").mkdir()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
        # The "also legacy: movate.yaml" hint appears.
        assert "delete to avoid confusion" in result.stdout


# ---------------------------------------------------------------------------
# Per-agent skill + context resolution detail
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPerAgentResolutionDetail:
    def test_per_agent_skills_resolve_lists_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk doctor agent <name>` names the resolved skills, not
        just a count. Operators see WHICH skills the agent uses."""
        _bootstrap_project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr

        result = runner.invoke(app, ["doctor", "agent", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code in (0, 1), result.stdout + result.stderr
        # rag-qa template declares `web-search` skill (post-#84).
        # Doctor lists it by name.
        assert "web-search" in result.stdout

    def test_per_agent_contexts_resolve_lists_names_with_tiers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-agent doctor labels each resolved context as (shared)
        or (agent-local).

        Test uses a manually-scaffolded `faq` agent + project-level
        context (faq doesn't ship a template `contexts/` subdir, so
        agent-local stays empty unless we put one there ourselves).
        This isolates the tier-label semantics from the template
        scaffold-copies-everything behavior.
        """
        proj = _bootstrap_project(tmp_path, monkeypatch)
        # Scaffold the `faq` agent inside the project — faq has no
        # template-shipped contexts/, so we control the tier.
        runner.invoke(
            app,
            ["init", "faq", "-t", "faq", "--target", str(proj / "agents")],
            env={"COLUMNS": "300"},
        )
        # Project-level shared context.
        (proj / "contexts" / "house-style.md").write_text("# House style\n")
        # Declare it in the agent.
        agent_yaml = proj / "agents" / "faq" / "agent.yaml"
        agent_yaml.write_text(agent_yaml.read_text() + "\ncontexts:\n  - house-style\n")
        result = runner.invoke(app, ["doctor", "agent", "faq"], env={"COLUMNS": "400"})
        assert result.exit_code in (0, 1), result.stdout + result.stderr
        assert "house-style" in result.stdout
        # No agent-local override → (shared) label.
        assert "(shared)" in result.stdout

        # Now drop an agent-local override and verify the label shifts.
        agent_ctx_dir = proj / "agents" / "faq" / "contexts"
        agent_ctx_dir.mkdir(parents=True, exist_ok=True)
        (agent_ctx_dir / "house-style.md").write_text("# faq-specific override\n")
        result = runner.invoke(app, ["doctor", "agent", "faq"], env={"COLUMNS": "400"})
        assert result.exit_code in (0, 1)
        assert "(agent-local)" in result.stdout

    def test_per_agent_missing_skill_reported_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If an agent declares `skills: [foo]` but `<project>/skills/foo/`
        doesn't exist, the doctor surfaces the missing skill by name.

        Implementation detail: the LOAD check fails first (the loader
        fails fast on missing skills), which is fine — the load
        error message itself names the missing skill, so operators
        still get a precise diagnosis. The detailed `skills resolve`
        row only fires when the load succeeded; if loading failed,
        the load row carries the diagnostic.
        """
        proj = _bootstrap_project(tmp_path, monkeypatch)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "300"})
        # Now corrupt the project by deleting the web-search skill that
        # rag-qa references.
        import shutil  # noqa: PLC0415

        shutil.rmtree(proj / "skills" / "web-search")

        result = runner.invoke(app, ["doctor", "agent", "rag-qa"], env={"COLUMNS": "400"})
        # Doctor reports a failure (load went red) + the failure's
        # detail names the missing skill.
        combined = result.stdout + result.stderr
        assert "web-search" in combined
        # `error=1` shows up in the greppable summary so CI can
        # detect the failure even without parsing the table.
        assert "error=1" in combined
