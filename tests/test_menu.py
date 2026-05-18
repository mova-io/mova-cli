"""Sprint P bonus — `mdk menu` tests.

Three layers:

1. **status** — :func:`inspect_workspace` correctly reads filesystem
   state into a :class:`WorkspaceStatus`.
2. **actions** — :func:`build_actions` produces a sensibly-ordered
   list given various workspace states (empty / partial / complete).
3. **CLI** — ``mdk menu --dry-run`` and ``--auto N`` render and
   dispatch without hanging on the interactive prompt.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.menu import (
    Action,
    WorkspaceStatus,
    build_actions,
    inspect_workspace,
)
from movate.menu.status import AgentInfo, EnvVarStatus

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_project(tmp_path: Path) -> Path:
    """Empty directory — no movate.yaml, no agents."""
    return tmp_path


@pytest.fixture
def initialized_project(tmp_path: Path) -> Path:
    """Project with movate.yaml but no agents yet."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\n")
    return tmp_path


@pytest.fixture
def project_with_agents(tmp_path: Path) -> Path:
    """Project with movate.yaml + two agents."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\n")
    agents = tmp_path / "agents"
    (agents / "triage").mkdir(parents=True)
    (agents / "triage" / "agent.yaml").write_text("name: triage\n")
    (agents / "summary").mkdir(parents=True)
    (agents / "summary" / "agent.yaml").write_text("name: summary\n")
    return tmp_path


# ---------------------------------------------------------------------------
# status — inspect_workspace
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInspectWorkspace:
    def test_empty_directory_reports_no_yaml_no_agents(self, empty_project: Path) -> None:
        status = inspect_workspace(empty_project)
        assert status.has_movate_yaml is False
        assert status.has_agents is False
        assert status.movate_yaml_version is None
        assert status.snapshot_count == 0

    def test_initialized_project_reads_api_version(self, initialized_project: Path) -> None:
        status = inspect_workspace(initialized_project)
        assert status.has_movate_yaml is True
        assert status.movate_yaml_version == "movate/v1"
        assert status.has_agents is False

    def test_finds_agents_sorted(self, project_with_agents: Path) -> None:
        status = inspect_workspace(project_with_agents)
        assert status.has_agents is True
        assert len(status.agents) == 2
        # Sorted by directory name
        names = [a.name for a in status.agents]
        assert names == ["summary", "triage"]

    def test_skips_non_dir_under_agents(self, project_with_agents: Path) -> None:
        """A stray file under agents/ shouldn't crash inspection."""
        (project_with_agents / "agents" / "README.md").write_text("hi\n")
        status = inspect_workspace(project_with_agents)
        # Still 2 agents — README is filtered out (not a dir)
        assert len(status.agents) == 2

    def test_skips_agent_dir_without_agent_yaml(self, project_with_agents: Path) -> None:
        """A subdir under agents/ that lacks agent.yaml is not an agent."""
        (project_with_agents / "agents" / "incomplete").mkdir()
        status = inspect_workspace(project_with_agents)
        assert len(status.agents) == 2

    def test_detects_dotenv_file(self, initialized_project: Path) -> None:
        (initialized_project / ".env").write_text("FOO=bar\n")
        status = inspect_workspace(initialized_project)
        assert status.has_dotenv_file is True

    def test_parses_env_example(self, initialized_project: Path) -> None:
        (initialized_project / ".env.example").write_text(
            "# Required keys\nOPENAI_API_KEY=\nLANGFUSE_PUBLIC_KEY=\n\n# Optional\nMOVATE_DEBUG=\n"
        )
        status = inspect_workspace(initialized_project)
        names = [v.name for v in status.env_vars]
        assert "OPENAI_API_KEY" in names
        assert "LANGFUSE_PUBLIC_KEY" in names
        assert "MOVATE_DEBUG" in names

    def test_falls_back_to_common_keys_without_env_example(self, initialized_project: Path) -> None:
        status = inspect_workspace(initialized_project)
        # No .env.example → falls back to the common provider keys
        names = [v.name for v in status.env_vars]
        assert "OPENAI_API_KEY" in names

    def test_missing_env_vars_property(
        self, initialized_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """missing_env_vars returns only required+unset entries."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (initialized_project / ".env.example").write_text("OPENAI_API_KEY=\n")
        status = inspect_workspace(initialized_project)
        missing_names = [v.name for v in status.missing_env_vars]
        assert "OPENAI_API_KEY" in missing_names

    def test_counts_snapshots(self, initialized_project: Path) -> None:
        snap_dir = initialized_project / ".movate" / "snapshots"
        (snap_dir / "abc1234").mkdir(parents=True)
        (snap_dir / "def5678").mkdir(parents=True)
        # A loose file in snapshots/ shouldn't count
        (snap_dir / "stray.txt").write_text("ignored\n")
        status = inspect_workspace(initialized_project)
        assert status.snapshot_count == 2


# ---------------------------------------------------------------------------
# actions — build_actions
# ---------------------------------------------------------------------------


def _make_status(**overrides: object) -> WorkspaceStatus:
    """Tiny helper — defaults to an empty-project status, override what matters."""
    defaults: dict[str, object] = {
        "project_root": Path("/fake"),
        "has_movate_yaml": False,
        "movate_yaml_version": None,
        "active_profile": None,
        "agents": (),
        "env_vars": (),
        "has_local_db": False,
        "snapshot_count": 0,
        "has_dotenv_file": False,
    }
    defaults.update(overrides)
    return WorkspaceStatus(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
class TestBuildActions:
    def test_empty_project_suggests_init_first(self) -> None:
        actions = build_actions(_make_status())
        # Top action must be init --project
        assert actions[0].argv == ("init", "--project")
        assert "Initialize" in actions[0].label

    def test_initialized_no_agents_suggests_scaffold(self) -> None:
        status = _make_status(has_movate_yaml=True, movate_yaml_version="movate/v1")
        actions = build_actions(status)
        labels = [a.label for a in actions]
        assert any("Scaffold" in label for label in labels)

    def test_agents_present_suggests_validate(self) -> None:
        status = _make_status(
            has_movate_yaml=True,
            movate_yaml_version="movate/v1",
            agents=(AgentInfo(name="triage", path=Path("/x")),),
        )
        actions = build_actions(status)
        assert any(a.argv == ("validate",) for a in actions)
        # Post-PR #102 the run action carries the dataset example as
        # its third argv element (or literal '{}' when no dataset
        # ships). The path /x doesn't exist so this falls back to '{}'.
        assert any(a.argv[:2] == ("run", "triage") for a in actions)

    def test_missing_env_var_surfaces_secrets_set_action(self) -> None:
        status = _make_status(
            has_movate_yaml=True,
            env_vars=(EnvVarStatus(name="OPENAI_API_KEY", set_in_env=False, required=True),),
        )
        actions = build_actions(status)
        # A "Set OPENAI_API_KEY" action should appear
        matching = [a for a in actions if a.argv == ("secrets", "set", "OPENAI_API_KEY")]
        assert len(matching) == 1
        # Priority should be high (lower number = surfaces earlier)
        assert matching[0].priority < 50

    def test_always_includes_doctor_and_help(self) -> None:
        actions = build_actions(_make_status())
        argv_set = {a.argv for a in actions}
        assert ("doctor",) in argv_set
        assert ("--help",) in argv_set

    def test_actions_are_sorted_by_priority(self) -> None:
        actions = build_actions(_make_status(has_movate_yaml=True))
        priorities = [a.priority for a in actions]
        assert priorities == sorted(priorities)

    def test_action_command_string_matches_argv(self) -> None:
        """The displayed command and the argv should be consistent."""
        actions = build_actions(_make_status(has_movate_yaml=True))
        for action in actions:
            # Command starts with "mdk "
            assert action.command.startswith("mdk ") or action.command.startswith("movate ")


# ---------------------------------------------------------------------------
# CLI: --dry-run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_menu_dry_run_exits_0(project_with_agents: Path) -> None:
    """--dry-run skips the prompt entirely."""
    result = runner.invoke(app, ["menu", "--dry-run", "--project-root", str(project_with_agents)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Status panel renders
    assert "Workspace" in result.stdout
    # At least one suggestion shows the literal `mdk` command
    assert "mdk " in result.stdout


@pytest.mark.unit
def test_cli_menu_dry_run_shows_status_marks(project_with_agents: Path) -> None:
    """The status panel uses ✓ / ⚠ / ✗ markers."""
    result = runner.invoke(app, ["menu", "--dry-run", "--project-root", str(project_with_agents)])
    assert result.exit_code == 0
    # At least one ✓ should appear (movate.yaml exists)
    assert "✓" in result.stdout


@pytest.mark.unit
def test_cli_menu_empty_project_recommends_init(empty_project: Path) -> None:
    result = runner.invoke(app, ["menu", "--dry-run", "--project-root", str(empty_project)])
    assert result.exit_code == 0
    # Init suggestion appears
    assert "init --project" in result.stdout or "Initialize" in result.stdout


# ---------------------------------------------------------------------------
# CLI: --auto N (test-friendly dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_menu_auto_too_high_exits_2(project_with_agents: Path) -> None:
    """--auto with N > len(actions) is a clean operator error."""
    result = runner.invoke(
        app, ["menu", "--auto", "99", "--project-root", str(project_with_agents)]
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_menu_auto_dispatches_help_action(project_with_agents: Path) -> None:
    """--auto picks an action by its 1-indexed slot.

    We pick the --help action specifically because it has no
    'needs_user_input' flag and dispatches without side effects.
    """
    # Capture subprocess.run to avoid actually invoking the binary.
    with mock.patch("movate.cli.menu_cmd.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0)
        status = inspect_workspace(project_with_agents)
        actions = build_actions(status)
        # Find the --help action's index
        help_idx = next(i for i, a in enumerate(actions, start=1) if a.argv == ("--help",))
        result = runner.invoke(
            app,
            [
                "menu",
                "--auto",
                str(help_idx),
                "--project-root",
                str(project_with_agents),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # subprocess.run was called with the right argv
        assert mock_run.call_count == 1
        called_argv = mock_run.call_args[0][0]
        assert called_argv[-1] == "--help"


@pytest.mark.unit
def test_cli_menu_needs_input_action_does_not_execute(
    project_with_agents: Path,
) -> None:
    """Actions flagged needs_user_input are surfaced as templates, not run.

    We pick the 'Run <agent>' action which is always needs_user_input=True
    (user must supply input JSON).
    """
    with mock.patch("movate.cli.menu_cmd.subprocess.run") as mock_run:
        status = inspect_workspace(project_with_agents)
        actions = build_actions(status)
        run_idx = next(i for i, a in enumerate(actions, start=1) if a.argv[:1] == ("run",))
        result = runner.invoke(
            app,
            [
                "menu",
                "--auto",
                str(run_idx),
                "--project-root",
                str(project_with_agents),
            ],
        )
        assert result.exit_code == 0
        # No subprocess fired — operator was shown the template instead
        assert mock_run.call_count == 0
        assert "needs additional input" in result.stdout.lower() or "copy" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Action dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_action_is_immutable() -> None:
    """Action is frozen — protects against accidental mutation."""
    a = Action(label="x", command="y", argv=("y",))
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        a.label = "z"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Action table rendering — visible numbers + headers + bordered layout
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_menu_action_table_renders_column_headers(
    project_with_agents: Path,
) -> None:
    """The action table renders ``#``, ``Step``, and ``Command`` as
    visible column headers so operators understand the number-row
    mapping at a glance (no need to guess that the first column
    drives the prompt)."""
    result = runner.invoke(
        app, ["menu", "--dry-run", "--project-root", str(project_with_agents)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Header row content
    assert "#" in result.stdout
    assert "Step" in result.stdout
    assert "Command" in result.stdout


@pytest.mark.unit
def test_menu_action_table_renders_plain_digit_numbers_not_brackets(
    project_with_agents: Path,
) -> None:
    """The number column shows ``1``, ``2``, ``3`` as plain digits
    (with the visible ``#`` header carrying the semantic). Previously
    we used ``[1]`` brackets inline, which buried the number among
    other punctuation. Pin the new layout."""
    result = runner.invoke(
        app, ["menu", "--dry-run", "--project-root", str(project_with_agents)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    # Plain digits + q row are present
    assert " 1 " in result.stdout or " 1\n" in result.stdout or "  1 " in result.stdout
    # The old `[1]` marker form must NOT regress
    assert "[1]" not in result.stdout
    assert "[q]" not in result.stdout


@pytest.mark.unit
def test_menu_action_table_renders_q_row_for_quit(
    project_with_agents: Path,
) -> None:
    """A ``q``-row at the bottom is the only way an operator quits
    without picking an action. Make sure it still surfaces in the
    new table layout."""
    result = runner.invoke(
        app, ["menu", "--dry-run", "--project-root", str(project_with_agents)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    assert "Quit" in result.stdout
    assert "exit menu" in result.stdout


@pytest.mark.unit
def test_menu_action_table_does_not_drop_number_column_for_long_commands(
    tmp_path: Path,
) -> None:
    """A long ``mdk run`` command (with embedded JSON) used to crowd
    the ``#`` and ``Step`` columns out of the rendered width — Rich
    would silently drop them to fit. The new table caps the Command
    column with overflow=ellipsis so the number is always visible."""
    # Scaffold a project + add an agent so the Run action surfaces
    # with its full dataset-derived command.
    (tmp_path / "project.yaml").write_text("api_version: movate/v1\nname: demo\n")
    agents = tmp_path / "agents" / "rag-qa"
    (agents / "evals").mkdir(parents=True)
    (agents / "agent.yaml").write_text("name: rag-qa\n")
    (agents / "evals" / "dataset.jsonl").write_text(
        '{"input": {"question": "x" * 500}, "expected": {}}\n'
    )

    result = runner.invoke(
        app, ["menu", "--dry-run", "--project-root", str(tmp_path)],
        env={"COLUMNS": "80"},  # narrow terminal — the failure mode
    )
    assert result.exit_code == 0
    # Header row must still render under 80 cols (regression guard).
    assert "#" in result.stdout
    assert "Step" in result.stdout
    # And the digit column must still surface — pick any of 1/2/3 since
    # the exact action ordering depends on workspace state.
    has_digit = any(
        f" {d} " in result.stdout or f"  {d} " in result.stdout for d in "12345"
    )
    assert has_digit, "no digit visible in the # column — Rich likely dropped it"
