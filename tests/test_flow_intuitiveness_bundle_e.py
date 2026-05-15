"""Bundle E — flow intuitiveness polish.

Six items targeting the customer demo flow (project → add agents →
validate → eval → deploy with Telegram):

1. ``mdk init --project foo --with-agents X,Y,Z`` — combines bootstrap
   + batch-add into one command.
2. Agent-name resolution in ``mdk run`` / ``mdk eval`` / ``mdk validate``
   — bare names resolve under ``./agents/<name>`` when inside a project.
3. ``mdk auth login telegram`` — guided Telegram-bot setup for the
   deploy-notification flow.
4. ``mdk add`` end-of-batch summary Panel — workspace-level next-steps
   after batch-add (mdk ci eval, mdk deploy --notify).
5. ``mdk doctor`` empty-project hint — when 0 agents present, surface
   the next command (``mdk add --list``).
6. ``mdk init --project`` copy-paste-friendly next steps — combined
   ``cd && mdk add --list`` line replaces the obsolete ``cp .env`` step.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli._resolve import resolve_agent_or_workflow_arg
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Minimal valid project fixture — same shape used across the
    existing add/init test suites."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


# ---------------------------------------------------------------------------
# Item 1: --with-agents combines bootstrap + batch-add
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitWithAgents:
    def test_with_agents_scaffolds_each_template(
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
                "rag-qa,ticket-triager,code-reviewer",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Project shell exists.
        assert (tmp_path / "support-bot" / "movate.yaml").is_file()
        # All three agents scaffolded.
        for name in ("rag-qa", "ticket-triager", "code-reviewer"):
            assert (
                tmp_path / "support-bot" / "agents" / name / "agent.yaml"
            ).is_file(), f"missing agent: {name}"

    def test_with_agents_unknown_template_errors_before_partial_scaffold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo in slot 3 must error BEFORE slots 1 and 2 scaffold."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "proj",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa,bogus-template",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        assert "unknown template" in result.stderr.lower()
        # The project itself was bootstrapped (that happens FIRST), but
        # the partial agent slot is NOT populated.
        assert (tmp_path / "proj" / "movate.yaml").is_file()
        assert not (tmp_path / "proj" / "agents" / "rag-qa").exists()

    def test_without_with_agents_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new flag is opt-in — existing `mdk init --project foo`
        behavior is bit-for-bit unchanged when --with-agents isn't passed."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "noagents", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert (tmp_path / "noagents" / "movate.yaml").is_file()
        # agents/ dir exists but is empty (apart from .gitkeep).
        agents_dir = tmp_path / "noagents" / "agents"
        contents = [p for p in agents_dir.iterdir() if not p.name.startswith(".")]
        assert contents == []


# ---------------------------------------------------------------------------
# Item 2: agent-name resolution in run / eval / validate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAgentNameResolution:
    def test_resolver_passes_through_urls(self) -> None:
        assert (
            resolve_agent_or_workflow_arg("https://example.com")
            == "https://example.com"
        )

    def test_resolver_passes_through_existing_paths(
        self, tmp_path: Path
    ) -> None:
        existing = tmp_path / "existing-dir"
        existing.mkdir()
        assert resolve_agent_or_workflow_arg(str(existing)) == str(existing)

    def test_resolver_finds_agent_under_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        # Scaffold an agent.
        agent_dir = proj / "agents" / "rag-qa"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text("name: rag-qa\n")
        monkeypatch.chdir(proj)

        result = resolve_agent_or_workflow_arg("rag-qa")
        assert Path(result) == agent_dir

    def test_resolver_falls_through_when_no_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        # No agent of this name exists.
        result = resolve_agent_or_workflow_arg("nonexistent-agent")
        assert result == "nonexistent-agent"

    def test_resolver_outside_project_returns_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # No movate.yaml anywhere — bare names pass through.
        assert (
            resolve_agent_or_workflow_arg("some-name") == "some-name"
        )

    def test_resolver_finds_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        wf_dir = proj / "workflows" / "support"
        wf_dir.mkdir(parents=True)
        (wf_dir / "workflow.yaml").write_text("name: support\n")
        monkeypatch.chdir(proj)

        result = resolve_agent_or_workflow_arg("support")
        assert Path(result) == wf_dir


# ---------------------------------------------------------------------------
# Item 3: mdk auth login telegram
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthLoginTelegram:
    def test_telegram_listed_as_valid_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing a bogus provider must mention telegram in the valid
        list (it was added as a special-case provider)."""
        monkeypatch.setenv(
            "MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds")
        )
        result = runner.invoke(
            app,
            ["auth", "login", "bogus-name", "--key", "x", "--no-verify"],
        )
        assert result.exit_code == 2
        assert "telegram" in result.stderr.lower()

    def test_telegram_with_key_flag_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--key doesn't apply to telegram (needs TWO values). Reject
        with a helpful error pointing at the interactive flow."""
        monkeypatch.setenv(
            "MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds")
        )
        result = runner.invoke(
            app,
            ["auth", "login", "telegram", "--key", "fake-token"],
        )
        assert result.exit_code == 2
        assert "telegram" in result.stderr.lower()
        assert "two values" in result.stderr.lower() or "bot token" in result.stderr.lower()

    def test_telegram_interactive_flow_persists_both_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end interactive flow with --no-verify (so we don't
        hit Telegram's API in tests). Both TELEGRAM_BOT_TOKEN and
        TELEGRAM_CHAT_ID should land in the credentials store."""
        from movate.credentials import CredentialsStore  # noqa: PLC0415

        monkeypatch.setenv(
            "MOVATE_CREDENTIALS_PATH", str(tmp_path / "creds")
        )
        result = runner.invoke(
            app,
            ["auth", "login", "telegram", "--no-verify"],
            input="test-bot-token\n12345\n",
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        store = CredentialsStore()
        assert store.get("TELEGRAM_BOT_TOKEN") == "test-bot-token"
        assert store.get("TELEGRAM_CHAT_ID") == "12345"


# ---------------------------------------------------------------------------
# Item 4: mdk add end-of-batch summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBatchSummary:
    def test_batch_add_renders_workspace_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app,
            ["add", "rag-qa", "ticket-triager"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Workspace-level summary fires only for batch (>1 templates).
        assert "Workspace ready" in result.stdout
        # Both agent names appear in the summary.
        assert "rag-qa" in result.stdout
        assert "ticket-triager" in result.stdout
        # Hints for the workspace-level next commands.
        assert "mdk ci eval" in result.stdout
        assert "mdk deploy" in result.stdout

    def test_single_add_no_workspace_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One-template add doesn't render the workspace summary —
        the per-agent Panel already has the right next-steps for a
        single agent."""
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["add", "rag-qa"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        # Single-add Panel still renders.
        assert "Added rag-qa agent" in result.stdout
        # But the workspace-level Panel does NOT.
        assert "Workspace ready" not in result.stdout


# ---------------------------------------------------------------------------
# Item 5: mdk doctor empty-project hint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDoctorEmptyProjectHint:
    def test_empty_project_shows_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Hint references mdk add --list (the natural next command).
        assert "mdk add --list" in result.stdout
        assert "no agents" in result.stdout.lower()

    def test_project_with_agents_no_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the project already has agents, the empty-project hint
        is silent. Otherwise it would be noise."""
        proj = _bootstrap_project(tmp_path)
        (proj / "agents" / "existing-agent").mkdir()
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        # No empty-project hint fires.
        assert "no agents yet" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# Item 6: mdk init --project copy-paste-friendly next steps
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitProjectNextSteps:
    def test_combined_cd_command_appears(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new next-steps line is a copy-paste-friendly
        `cd <name> && mdk add --list` instead of a multi-line
        sequence."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "smooth-test", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Combined cd + first action.
        assert "cd smooth-test && mdk add --list" in result.stdout

    def test_obsolete_env_step_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old `cp .env.example .env` step no longer appears as a
        next-step (machine-global credentials make it redundant for
        first-touch). The .env file itself still ships — it's just
        not the highlighted next step anymore."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "cred-test", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # The exact pre-PR string is gone.
        assert "cp .env.example .env" not in result.stdout
        # The .env.example file STILL exists in the project though.
        assert (tmp_path / "cred-test" / ".env.example").is_file()
