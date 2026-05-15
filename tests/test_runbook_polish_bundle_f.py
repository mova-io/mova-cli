"""Bundle F — runbook polish.

Six items that smooth the end-to-end test flow:

1. ``mdk run <agent>`` with no input auto-surfaces the first dataset row.
2. ``mdk auth status`` shows Telegram + webhook rows under Notifications.
3. ``mdk add --list`` marks already-installed templates with ✓.
4. ``mdk init --project --with-agents`` shows forward-looking next-steps.
5. Fuzzy-match for typo'd agent names (``mdk run ragqa`` → "did you mean rag-qa?").
6. ``mdk init --project`` (no --with-agents) suggests --with-agents in next-steps.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli._resolve import list_project_agents, suggest_similar_agent
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Same fixture pattern as the rest of the suite."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


def _scaffold_test_agent(proj: Path, name: str = "test-agent") -> Path:
    """Drop a minimal but loader-valid agent under proj/agents/<name>/."""
    agent_dir = proj / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "schema").mkdir()
    (agent_dir / "evals").mkdir()
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo: {{ input.text }}")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["message"],
                "properties": {"message": {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"text": "hello"}, "expected": {"message": "ok"}}) + "\n"
    )
    return agent_dir


# ---------------------------------------------------------------------------
# Item 1: auto-example for `mdk run` with no input
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoExampleInput:
    def test_no_input_surfaces_dataset_example(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="echo-bot")
        monkeypatch.chdir(proj)

        result = runner.invoke(app, ["run", "echo-bot"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        # `mdk run`'s console writes to stderr. Combine both for safety.
        out = result.stdout + result.stderr
        assert "provide input" in out.lower()
        assert "try the first example" in out.lower()
        assert "mdk run echo-bot" in out
        assert "hello" in out

    def test_no_input_no_dataset_falls_back_to_show_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the agent has no dataset (or it's empty), the hint
        points at `mdk show` instead of a sample command."""
        proj = _bootstrap_project(tmp_path)
        agent_dir = _scaffold_test_agent(proj, name="no-data-bot")
        # Empty the dataset.
        (agent_dir / "evals" / "dataset.jsonl").write_text("")
        monkeypatch.chdir(proj)

        result = runner.invoke(app, ["run", "no-data-bot"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        out = result.stdout + result.stderr
        assert "no dataset sample available" in out.lower()
        assert "mdk show" in out


# ---------------------------------------------------------------------------
# Item 2: mdk auth status shows Telegram + webhook rows
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthStatusNotifications:
    def test_status_includes_telegram_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds"))
        # Strip all notification env vars so the rows render as "not set".
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MOVATE_DEPLOY_WEBHOOK"):
            monkeypatch.delenv(key, raising=False)

        result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # The Notifications section appears, with all three env vars.
        assert "Notifications" in result.stdout
        assert "TELEGRAM_BOT_TOKEN" in result.stdout
        assert "TELEGRAM_CHAT_ID" in result.stdout
        assert "MOVATE_DEPLOY_WEBHOOK" in result.stdout
        # The hint points at `mdk auth login telegram`.
        assert "mdk auth login telegram" in result.stdout

    def test_status_summary_counts_include_notifications(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The greppable summary line should count notification keys too."""
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds"))
        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "LYZR_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "MOVATE_DEPLOY_WEBHOOK",
        ):
            monkeypatch.delenv(key, raising=False)
        result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # 5 provider env vars + 3 notification env vars = 8 unset.
        assert "set=0" in result.stdout
        assert "unset=8" in result.stdout


# ---------------------------------------------------------------------------
# Item 3: mdk add --list marks already-installed templates
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListInstalledMarker:
    def test_installed_template_marked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        # Scaffold one role agent via the real `mdk add` so the file
        # layout matches what _installed_templates() looks for.
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})

        result = runner.invoke(app, ["add", "--list"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # The installed agent's row has the ✓ installed marker.
        # Defensive: find the line containing rag-qa and check it.
        rag_qa_lines = [
            line for line in result.stdout.splitlines() if "rag-qa" in line
        ]
        assert rag_qa_lines, "rag-qa row missing from --list output"
        assert any("installed" in line.lower() for line in rag_qa_lines), (
            f"rag-qa row should be marked installed, got: {rag_qa_lines}"
        )

    def test_uninstalled_templates_not_marked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})

        result = runner.invoke(app, ["add", "--list"], env={"COLUMNS": "200"})
        # ticket-triager wasn't installed — its row should NOT show
        # the installed marker.
        ticket_lines = [
            line for line in result.stdout.splitlines() if "ticket-triager" in line
        ]
        assert ticket_lines
        # Specifically: no "installed" marker on this row.
        for line in ticket_lines:
            assert "installed" not in line.lower(), (
                f"ticket-triager should not be marked installed: {line}"
            )

    def test_outside_project_no_markers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Catalog rendered outside a project never shows markers
        (there's no project to detect installation in)."""
        monkeypatch.chdir(tmp_path)  # no movate.yaml here
        result = runner.invoke(app, ["add", "--list"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # No row should be tagged installed.
        assert "installed" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# Item 4: smarter next-steps after --with-agents
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSmartNextStepsAfterWithAgents:
    def test_with_agents_shows_forward_looking_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        # The next-steps section should mention the run/eval/doctor commands.
        assert "mdk doctor agent" in result.stdout
        assert "mdk run rag-qa" in result.stdout
        assert "mdk eval rag-qa" in result.stdout
        # And should NOT mention the `mdk add --list` step — agents
        # are already there.
        # (The workspace-summary panel from PR #68 still says "mdk ci
        # eval" + "mdk deploy"; that's separate. The PROJECT panel
        # specifically should not re-suggest `mdk add --list`.)
        # Check the project Panel content by looking for the
        # "Project initialized" title with no `mdk add --list` in
        # the body. Defensive: look for the substring narrowing to
        # the panel between "Project initialized" and the next panel.
        proj_panel_start = result.stdout.find("Project initialized")
        next_panel_start = result.stdout.find("Added rag-qa")
        if proj_panel_start >= 0 and next_panel_start > proj_panel_start:
            proj_panel = result.stdout[proj_panel_start:next_panel_start]
            assert "mdk add --list" not in proj_panel, (
                "Project Panel should not suggest mdk add --list after --with-agents"
            )

    def test_no_with_agents_shows_add_list_tip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "fresh", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # The traditional path shows the add catalog AND the tip.
        assert "mdk add --list" in result.stdout
        # Tip about --with-agents next time:
        assert "--with-agents" in result.stdout


# ---------------------------------------------------------------------------
# Item 5: fuzzy-match for unknown agent names
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFuzzyMatch:
    def test_suggest_similar_agent_finds_typo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="rag-qa")
        monkeypatch.chdir(proj)
        # "ragqa" → "rag-qa" (1-char Levenshtein distance).
        assert suggest_similar_agent("ragqa") == "rag-qa"

    def test_suggest_returns_none_outside_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert suggest_similar_agent("anything") is None

    def test_suggest_returns_none_for_unrelated_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="rag-qa")
        monkeypatch.chdir(proj)
        # "xyzzy" has no fuzzy match in the project.
        assert suggest_similar_agent("xyzzy") is None

    def test_list_project_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="alpha")
        _scaffold_test_agent(proj, name="beta")
        monkeypatch.chdir(proj)
        assert list_project_agents() == ["alpha", "beta"]

    def test_doctor_agent_typo_suggests_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk doctor agent ragqa` → "Did you mean rag-qa?" hint."""
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="rag-qa")
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["doctor", "agent", "ragqa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        assert "did you mean" in result.stderr.lower()
        assert "rag-qa" in result.stderr

    def test_run_typo_suggests_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk run ragqa` → fuzzy-match hint AFTER the load-failed error."""
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="rag-qa")
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["run", "ragqa", "--mock", '{"text":"hi"}'], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        # The fuzzy hint surfaces in the same output (stdout for run).
        combined = result.stdout + result.stderr
        assert "did you mean" in combined.lower()
        assert "rag-qa" in combined

    def test_full_path_no_fuzzy_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the operator passes a full path that doesn't exist,
        DON'T show a fuzzy hint — they passed an explicit path and
        the error is more likely a real filesystem issue than a typo
        in a bare name."""
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="rag-qa")
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app,
            ["run", "./nonexistent/path", "--mock", '{"text":"hi"}'],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        # The error fires but no fuzzy hint follows.
        combined = result.stdout + result.stderr
        assert "did you mean" not in combined.lower()


# ---------------------------------------------------------------------------
# Item 6: mdk init --project (no --with-agents) suggests it
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWithAgentsDiscoverabilityTip:
    def test_vanilla_init_shows_with_agents_tip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "vanilla", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # The tip references --with-agents explicitly.
        assert "--with-agents" in result.stdout
        # And shows a copy-pasteable example.
        assert "mdk init --project" in result.stdout

    def test_with_agents_init_omits_the_tip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When --with-agents was already used, don't re-suggest it."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "smart",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # Look for the tip phrase that ONLY fires in the no-with-agents
        # path. The project Panel shouldn't contain it. (The workspace
        # summary Panel might mention --with-agents in its agent list,
        # but the "Tip:" wording is unique to the vanilla path.)
        # Defensive: the project Panel ends before the first agent
        # success Panel begins.
        project_panel_end = result.stdout.find("Added rag-qa")
        project_panel = (
            result.stdout[:project_panel_end] if project_panel_end > 0 else result.stdout
        )
        assert "Tip:" not in project_panel, (
            "vanilla-only tip should not fire after --with-agents"
        )
