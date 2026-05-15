"""Elegant init summary + ``mdk validate --all`` — bundle following
Bundle F.

Two ergonomic improvements to the demo flow:

1. ``mdk init --project foo --with-agents X,Y,Z`` now renders ONE
   combined Panel summarizing the workspace instead of three blocks
   (project Panel + per-agent legacy text + per-agent Panel + batch
   Panel) totaling ~80 lines for a 3-agent workspace. The greppable
   ``mdk_add_summary:`` lines still fire so CI keeps working.

2. ``mdk validate --all`` walks ``./agents/`` and ``./workflows/`` and
   validates every item in one shot, with a summary table + greppable
   ``mdk_validate_summary:`` line. Pairs with ``--with-agents`` as the
   natural next command.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Elegant combined summary for --with-agents
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestElegantInitSummary:
    def test_single_combined_panel_replaces_per_agent_panels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The combined Panel title fires; the old per-agent
        "Added X agent" Panels do NOT."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa,ticket-triager,code-reviewer",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # One combined Panel — title contains "Workspace ready".
        assert "Workspace ready (3 agents)" in result.stdout
        # Old per-agent Panel titles ("Added X agent") MUST NOT appear.
        assert "Added rag-qa agent" not in result.stdout
        assert "Added ticket-triager agent" not in result.stdout
        assert "Added code-reviewer agent" not in result.stdout

    def test_combined_panel_lists_all_agents_with_descriptions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each agent's role description appears in the Panel body —
        operators see WHAT each agent does without re-grepping the
        catalog."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa,ticket-triager",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Each agent name appears.
        assert "rag-qa" in result.stdout
        assert "ticket-triager" in result.stdout
        # The role descriptions from _ROLE_DESCRIPTIONS appear too.
        assert "Grounded Q&A" in result.stdout
        assert "Support ticket" in result.stdout

    def test_combined_panel_suggests_validate_all_in_next_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate --all` is THE natural next command after
        --with-agents — it must appear in the next-steps block."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert "mdk validate --all" in result.stdout

    def test_project_init_panel_suppressed_when_with_agents_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The standalone "Project initialized" Panel must NOT render
        when --with-agents is set — that information is folded into
        the combined Workspace Panel."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert "Project initialized" not in result.stdout

    def test_project_init_panel_still_renders_without_with_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When --with-agents isn't set, the original Project Panel
        keeps rendering bit-for-bit."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "support-bot", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert "Project initialized" in result.stdout

    def test_greppable_summary_lines_still_fire_per_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk_add_summary:` lines (one per agent) MUST still fire
        even in quiet mode — they're machine-readable, not visual
        clutter, and CI parses them."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa,ticket-triager",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        summary_lines = [line for line in result.stdout.splitlines() if "mdk_add_summary:" in line]
        assert len(summary_lines) == 2, (
            f"expected 2 summary lines, got {len(summary_lines)}: {summary_lines}"
        )
        # Each summary line carries the template name.
        joined = "\n".join(summary_lines)
        assert "template=rag-qa" in joined
        assert "template=ticket-triager" in joined


# ---------------------------------------------------------------------------
# mdk validate --all
# ---------------------------------------------------------------------------


def _bootstrap_with_agents(
    tmp_path: Path, agents_csv: str, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Helper: build a project + scaffold the named agents."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--project",
            "proj",
            "--skip-snapshot",
            "--with-agents",
            agents_csv,
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    project = tmp_path / "proj"
    monkeypatch.chdir(project)
    return project


@pytest.mark.unit
class TestValidateAll:
    def test_validate_all_passes_for_clean_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A freshly-scaffolded project's agents all load cleanly →
        --all exits 0 with every row marked ✓."""
        _bootstrap_with_agents(tmp_path, "rag-qa,ticket-triager", monkeypatch)
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        # Summary line present + reports ok=true.
        summary = next(
            (line for line in result.stdout.splitlines() if "mdk_validate_summary:" in line),
            None,
        )
        assert summary is not None, result.stdout
        assert "ok=true" in summary
        assert "passed=2" in summary
        assert "failed=0" in summary

    def test_validate_all_fails_when_any_agent_breaks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Break one agent's agent.yaml → --all exits 2 with failed≥1."""
        project = _bootstrap_with_agents(tmp_path, "rag-qa,ticket-triager", monkeypatch)
        # Corrupt one agent's spec — invalid YAML / missing required field.
        broken_yaml = project / "agents" / "rag-qa" / "agent.yaml"
        broken_yaml.write_text("name: rag-qa\n# missing api_version, kind, etc.\n")

        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        summary = next(
            (line for line in result.stdout.splitlines() if "mdk_validate_summary:" in line),
            None,
        )
        assert summary is not None
        assert "ok=false" in summary
        assert "failed=1" in summary

    def test_validate_all_outside_project_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No movate.yaml anywhere up the tree → exit 2 with a hint."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        # The error message points operators at `mdk init --project`.
        combined = result.stdout + result.stderr
        assert "not inside a movate project" in combined.lower()

    def test_validate_all_empty_project_warns_not_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty project (no agents, no workflows) is NOT a failure —
        operator just bootstrapped. Exit 0 with a warning + zeroed
        summary line."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "empty", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        monkeypatch.chdir(tmp_path / "empty")
        result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        summary = next(
            (line for line in result.stdout.splitlines() if "mdk_validate_summary:" in line),
            None,
        )
        assert summary is not None
        assert "agents_total=0" in summary
        assert "workflows_total=0" in summary

    def test_validate_all_rejects_path_and_all_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate some-path --all` is almost certainly a typo;
        reject explicitly rather than picking one silently."""
        project = _bootstrap_with_agents(tmp_path, "rag-qa", monkeypatch)
        result = runner.invoke(
            app,
            ["validate", str(project / "agents" / "rag-qa"), "--all"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "mutually exclusive" in combined.lower()
