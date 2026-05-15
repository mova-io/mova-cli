"""Sprint Q — `mdk inspect agent` tests.

Three layers:

1. **Path resolution** — bare names resolve under ``agents/<name>``;
   literal paths take precedence when both work; missing → exit 2.
2. **Rendering** — default (no --only) emits all sections; --only
   narrows; --json emits a machine-readable shape with the expected
   keys.
3. **Edge cases** — unknown section id rejected, agent without
   contexts / skills still renders cleanly.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold_agent(dst: Path, name: str = "demo") -> Path:
    """Copy the agent_init template + substitute __AGENT_NAME__."""
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.fixture
def project_with_agent(tmp_path: Path) -> Path:
    """Project root with one agent at agents/triage."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\nkind: Project\nname: t\n")
    _scaffold_agent(tmp_path / "agents" / "triage", name="triage")
    return tmp_path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolves_bare_name_under_agents(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        ["inspect", "agent", "triage", "--project-root", str(project_with_agent)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The agent's identity should appear in the output
    assert "triage" in result.stdout


@pytest.mark.unit
def test_resolves_literal_path(project_with_agent: Path) -> None:
    agent_dir = project_with_agent / "agents" / "triage"
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            str(agent_dir),
            "--project-root",
            str(project_with_agent),
        ],
    )
    assert result.exit_code == 0
    assert "triage" in result.stdout


@pytest.mark.unit
def test_unknown_agent_exits_2(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        ["inspect", "agent", "ghost", "--project-root", str(project_with_agent)],
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower() or "ghost" in combined


# ---------------------------------------------------------------------------
# Default rendering (all sections)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_shows_all_sections(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        ["inspect", "agent", "triage", "--project-root", str(project_with_agent)],
    )
    assert result.exit_code == 0
    # Each section header should appear (Identity, Model, schemas, etc.)
    assert "Identity" in result.stdout
    assert "Model" in result.stdout
    assert "Prompt" in result.stdout
    assert "Input schema" in result.stdout
    assert "Output schema" in result.stdout


@pytest.mark.unit
def test_default_includes_provider(project_with_agent: Path) -> None:
    """The resolved model.provider should appear (proves loader ran)."""
    result = runner.invoke(
        app,
        ["inspect", "agent", "triage", "--project-root", str(project_with_agent)],
    )
    assert result.exit_code == 0
    # The template's default provider — confirms `inspect` shows the
    # post-resolution value, not a placeholder.
    assert "openai" in result.stdout.lower()


# ---------------------------------------------------------------------------
# --only filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_only_narrows_to_one_section(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--only",
            "model",
            "--project-root",
            str(project_with_agent),
        ],
    )
    assert result.exit_code == 0
    assert "Model" in result.stdout
    # Other sections should NOT appear
    assert "Input schema" not in result.stdout
    assert "Prompt" not in result.stdout


@pytest.mark.unit
def test_only_accepts_multiple_sections(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--only",
            "model",
            "--only",
            "identity",
            "--project-root",
            str(project_with_agent),
        ],
    )
    assert result.exit_code == 0
    assert "Identity" in result.stdout
    assert "Model" in result.stdout
    # Other sections suppressed
    assert "Input schema" not in result.stdout


@pytest.mark.unit
def test_only_unknown_section_exits_2(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--only",
            "bogus",
            "--project-root",
            str(project_with_agent),
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "unknown section" in combined.lower() or "bogus" in combined


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_output_has_expected_keys(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--json",
            "--project-root",
            str(project_with_agent),
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    # All six top-level sections present
    assert set(data.keys()) == {
        "identity",
        "model",
        "prompt",
        "schemas",
        "skills",
        "contexts",
    }


@pytest.mark.unit
def test_json_identity_includes_resolved_fields(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--json",
            "--project-root",
            str(project_with_agent),
        ],
    )
    data = json.loads(result.stdout)
    assert data["identity"]["name"] == "triage"
    assert data["identity"]["prompt_hash"]  # non-empty
    # agent_dir resolves to the real path
    assert "agents/triage" in data["identity"]["agent_dir"]


@pytest.mark.unit
def test_json_schemas_are_fully_expanded(project_with_agent: Path) -> None:
    """Inline-shorthand schemas should be lifted to full JSON Schemas."""
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--json",
            "--project-root",
            str(project_with_agent),
        ],
    )
    data = json.loads(result.stdout)
    # Both schemas should have the JSON-Schema shape with `type`/`properties`
    assert data["schemas"]["input"].get("type") == "object"
    assert "properties" in data["schemas"]["input"]


@pytest.mark.unit
def test_json_only_filter_narrows_keys(project_with_agent: Path) -> None:
    """--only restricts JSON output to the named keys too."""
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--json",
            "--only",
            "model",
            "--project-root",
            str(project_with_agent),
        ],
    )
    data = json.loads(result.stdout)
    assert set(data.keys()) == {"model"}


# ---------------------------------------------------------------------------
# Agent without skills/contexts still renders cleanly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_without_skills_shows_single_shot_hint(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--only",
            "skills",
            "--project-root",
            str(project_with_agent),
        ],
    )
    assert result.exit_code == 0
    # Single-shot hint appears
    assert "single-shot" in result.stdout.lower() or "none" in result.stdout.lower()


@pytest.mark.unit
def test_agent_without_contexts_shows_as_is_hint(project_with_agent: Path) -> None:
    result = runner.invoke(
        app,
        [
            "inspect",
            "agent",
            "triage",
            "--only",
            "contexts",
            "--project-root",
            str(project_with_agent),
        ],
    )
    assert result.exit_code == 0
    assert "as-is" in result.stdout.lower() or "none" in result.stdout.lower()
