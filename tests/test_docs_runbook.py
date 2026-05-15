"""Sprint P — `mdk docs runbook` tests.

Three layers:

1. **Context builder** — :func:`build_context` correctly reads
   movate.yaml, walks agents/, splits env vars into required vs
   optional.
2. **Generator** — :func:`generate_runbook` is pure and produces
   valid markdown for all states (empty / partial / full project).
3. **CLI** — `mdk docs runbook` writes the file, --dry-run prints
   without writing, refuses overwrite without --force.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.docs import (
    AgentEntry,
    RunbookContext,
    build_context,
    generate_runbook,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_project(tmp_path: Path) -> Path:
    """Just an empty directory — no movate.yaml, no agents."""
    return tmp_path


@pytest.fixture
def basic_project(tmp_path: Path) -> Path:
    """Project with movate.yaml + one agent + .env.example."""
    (tmp_path / "movate.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Project\n"
        "name: test-proj\n"
        "version: 0.1.0\n"
        "description: A test project for runbook generation.\n"
    )

    agent_dir = tmp_path / "agents" / "triage"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: triage\n"
        "description: Classifies support tickets.\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
    )
    (agent_dir / "prompt.md").write_text("Classify this ticket.\n")

    (tmp_path / ".env.example").write_text(
        "OPENAI_API_KEY=\n# ANTHROPIC_API_KEY=\n# LANGFUSE_PUBLIC_KEY=\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildContext:
    def test_empty_project_returns_minimal_context(self, empty_project: Path) -> None:
        ctx = build_context(empty_project)
        assert ctx.has_movate_yaml is False
        assert ctx.agents == ()
        assert ctx.required_env_vars == ()

    def test_basic_project_reads_metadata(self, basic_project: Path) -> None:
        ctx = build_context(basic_project)
        assert ctx.has_movate_yaml is True
        assert ctx.project_name == "test-proj"
        assert ctx.project_version == "0.1.0"
        assert "test project" in ctx.project_description.lower()

    def test_finds_agents(self, basic_project: Path) -> None:
        ctx = build_context(basic_project)
        assert len(ctx.agents) == 1
        agent = ctx.agents[0]
        assert agent.name == "triage"
        assert agent.description == "Classifies support tickets."
        assert "openai" in agent.model_provider
        assert agent.has_prompt is True

    def test_splits_env_vars_into_required_optional(self, basic_project: Path) -> None:
        ctx = build_context(basic_project)
        assert "OPENAI_API_KEY" in ctx.required_env_vars
        # Commented-out lines → optional
        assert "ANTHROPIC_API_KEY" in ctx.optional_env_vars
        assert "LANGFUSE_PUBLIC_KEY" in ctx.optional_env_vars

    def test_malformed_agent_yaml_does_not_crash(self, basic_project: Path) -> None:
        """Broken agent.yaml should fall through to empty fields, not raise."""
        bad = basic_project / "agents" / "broken"
        bad.mkdir()
        (bad / "agent.yaml").write_text("not: : valid: :")
        ctx = build_context(basic_project)
        # The good agent is still there; the broken one is recorded but with
        # empty fields rather than blowing up the whole walk.
        names = [a.name for a in ctx.agents]
        assert "triage" in names
        assert "broken" in names

    def test_project_name_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        """No movate.yaml.name → use the directory name."""
        ctx = build_context(tmp_path)
        assert ctx.project_name == tmp_path.name


# ---------------------------------------------------------------------------
# Generator (pure function)
# ---------------------------------------------------------------------------


def _ctx(**overrides: object) -> RunbookContext:
    """Tiny helper — builds a context with default empty fields."""
    defaults: dict[str, object] = {
        "project_name": "test",
        "project_root": Path("/fake"),
    }
    defaults.update(overrides)
    return RunbookContext(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
class TestGenerateRunbook:
    def test_returns_markdown_string(self) -> None:
        out = generate_runbook(_ctx())
        assert isinstance(out, str)
        assert out.startswith("# Runbook")
        # Final newline guaranteed
        assert out.endswith("\n")

    def test_includes_project_name_in_header(self) -> None:
        out = generate_runbook(_ctx(project_name="my-proj"))
        assert "# Runbook — my-proj" in out

    def test_empty_agents_renders_helpful_message(self) -> None:
        out = generate_runbook(_ctx())
        assert "## Agents" in out
        assert "no agents yet" in out.lower() or "mdk init" in out.lower()

    def test_agent_section_includes_run_recipe(self) -> None:
        agent = AgentEntry(name="triage", description="classifies tickets")
        out = generate_runbook(_ctx(agents=(agent,)))
        assert "### `triage`" in out
        assert "mdk run triage" in out

    def test_env_section_separates_required_from_optional(self) -> None:
        out = generate_runbook(
            _ctx(
                required_env_vars=("OPENAI_API_KEY",),
                optional_env_vars=("LANGFUSE_PUBLIC_KEY",),
            )
        )
        # Both subsections appear
        assert "### Required" in out
        assert "### Optional" in out
        # Each var is listed
        assert "`OPENAI_API_KEY`" in out
        assert "`LANGFUSE_PUBLIC_KEY`" in out

    def test_state_cluster_section_lists_commands(self) -> None:
        out = generate_runbook(_ctx())
        # Each state-cluster command should appear
        for cmd in (
            "mdk snapshot create",
            "mdk diff",
            "mdk rollback",
            "mdk migrate",
            "mdk promote",
            "mdk audit",
        ):
            assert cmd in out

    def test_troubleshooting_section_has_exit_code_legend(self) -> None:
        out = generate_runbook(_ctx())
        assert "Exit codes" in out
        # All three codes documented
        for code in ("`0`", "`1`", "`2`"):
            assert code in out

    def test_is_pure_function(self) -> None:
        """Two calls with the same context produce the same output."""
        ctx = _ctx(
            project_name="foo",
            agents=(AgentEntry(name="a"),),
            required_env_vars=("X",),
        )
        assert generate_runbook(ctx) == generate_runbook(ctx)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_runbook_writes_to_default_location(basic_project: Path) -> None:
    result = runner.invoke(app, ["docs", "runbook", "--project-root", str(basic_project)])
    assert result.exit_code == 0, result.stdout + result.stderr
    expected = basic_project / "docs" / "RUNBOOK.md"
    assert expected.is_file()
    text = expected.read_text()
    # Sanity: real content was generated
    assert "# Runbook" in text
    assert "test-proj" in text


@pytest.mark.unit
def test_cli_runbook_writes_to_custom_output(basic_project: Path) -> None:
    out = basic_project / "ops" / "runbook.md"
    result = runner.invoke(
        app,
        [
            "docs",
            "runbook",
            str(out),
            "--project-root",
            str(basic_project),
        ],
    )
    assert result.exit_code == 0
    assert out.is_file()


@pytest.mark.unit
def test_cli_runbook_dry_run_prints_does_not_write(basic_project: Path) -> None:
    result = runner.invoke(
        app,
        ["docs", "runbook", "--dry-run", "--project-root", str(basic_project)],
    )
    assert result.exit_code == 0
    # Output contains the markdown
    assert "# Runbook" in result.stdout
    # Default path not written
    assert not (basic_project / "docs" / "RUNBOOK.md").exists()


@pytest.mark.unit
def test_cli_runbook_refuses_existing_without_force(basic_project: Path) -> None:
    target = basic_project / "docs" / "RUNBOOK.md"
    target.parent.mkdir(parents=True)
    target.write_text("don't lose me\n")

    result = runner.invoke(app, ["docs", "runbook", "--project-root", str(basic_project)])
    assert result.exit_code == 2
    # File untouched
    assert target.read_text() == "don't lose me\n"


@pytest.mark.unit
def test_cli_runbook_force_overwrites(basic_project: Path) -> None:
    target = basic_project / "docs" / "RUNBOOK.md"
    target.parent.mkdir(parents=True)
    target.write_text("old\n")

    result = runner.invoke(
        app,
        ["docs", "runbook", "--force", "--project-root", str(basic_project)],
    )
    assert result.exit_code == 0
    assert "# Runbook" in target.read_text()


@pytest.mark.unit
def test_cli_runbook_on_empty_project_succeeds(empty_project: Path) -> None:
    """Empty project should still produce a useful runbook."""
    result = runner.invoke(
        app,
        ["docs", "runbook", "--project-root", str(empty_project)],
    )
    assert result.exit_code == 0
    text = (empty_project / "docs" / "RUNBOOK.md").read_text()
    assert "no agents yet" in text.lower() or "mdk init" in text.lower()
