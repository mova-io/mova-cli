"""LLM-bootstrapped project planner (Phase J-3) — module + CLI tests.

Layered coverage:

1. **Module-level**: :func:`parse_plan` accepts/rejects various shapes;
   :func:`call_planner` returns a structured plan from a mock provider.
2. **CLI-level**: ``mdk plan`` dry-run renders, ``--apply`` scaffolds
   to disk + creates a loadable project, error paths exit cleanly.

Real provider calls are out of scope — :class:`MockProvider` with
:envvar:`MDK_MOCK_PLAN_RESPONSE` substitutes for any actual LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.planner import (
    PlannedAgent,
    PlanParseError,
    ProjectPlan,
    build_planner_prompt,
    call_planner,
    parse_plan,
)
from movate.providers.mock import MockProvider

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Module: build_planner_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_planner_prompt_lists_every_role_template() -> None:
    """Catalog inlined into the prompt must mention every role template
    so the planner has the full vocabulary to choose from. Without
    this, the planner could pick a template that doesn't exist."""
    prompt = build_planner_prompt("anything")
    from movate.templates import ROLE_TEMPLATES  # noqa: PLC0415

    for name in ROLE_TEMPLATES:
        assert name in prompt, f"role {name!r} missing from planner prompt"


@pytest.mark.unit
def test_build_planner_prompt_includes_user_description() -> None:
    desc = "Contract evaluation against a 12-item checklist"
    prompt = build_planner_prompt(desc)
    assert desc in prompt


# ---------------------------------------------------------------------------
# Module: parse_plan
# ---------------------------------------------------------------------------


_GOOD_PLAN_JSON = json.dumps(
    {
        "project_name": "contract-eval",
        "description": "Evaluate contracts against a checklist",
        "agents": [
            {
                "name": "parser",
                "template": "document-summarizer",
                "purpose": "extract structure",
            },
            {
                "name": "grader",
                "template": "text-classifier",
                "purpose": "grade items",
            },
        ],
        "workflow": ["parser", "grader"],
    }
)


@pytest.mark.unit
class TestParsePlan:
    def test_happy_path(self) -> None:
        plan = parse_plan(_GOOD_PLAN_JSON)
        assert plan.project_name == "contract-eval"
        assert len(plan.agents) == 2
        assert plan.agents[0].name == "parser"
        assert plan.agents[0].template == "document-summarizer"
        assert plan.workflow == ("parser", "grader")

    def test_strips_markdown_code_fences(self) -> None:
        """Planners sometimes wrap in ```json — accept it."""
        wrapped = f"```json\n{_GOOD_PLAN_JSON}\n```"
        plan = parse_plan(wrapped)
        assert plan.project_name == "contract-eval"

    def test_rejects_unknown_template(self) -> None:
        bad = json.loads(_GOOD_PLAN_JSON)
        bad["agents"][0]["template"] = "no-such-role"
        with pytest.raises(PlanParseError, match="not a known role"):
            parse_plan(json.dumps(bad))

    def test_rejects_workflow_referencing_undeclared_agent(self) -> None:
        bad = json.loads(_GOOD_PLAN_JSON)
        bad["workflow"] = ["parser", "missing"]
        with pytest.raises(PlanParseError, match="not in declared agents"):
            parse_plan(json.dumps(bad))

    def test_rejects_duplicate_agent_names(self) -> None:
        bad = json.loads(_GOOD_PLAN_JSON)
        bad["agents"][1]["name"] = "parser"  # collision
        with pytest.raises(PlanParseError, match="duplicates"):
            parse_plan(json.dumps(bad))

    def test_rejects_empty_agents_list(self) -> None:
        bad = json.loads(_GOOD_PLAN_JSON)
        bad["agents"] = []
        with pytest.raises(PlanParseError, match="non-empty"):
            parse_plan(json.dumps(bad))

    def test_rejects_missing_required_field(self) -> None:
        bad = json.loads(_GOOD_PLAN_JSON)
        del bad["workflow"]
        with pytest.raises(PlanParseError, match="missing required field"):
            parse_plan(json.dumps(bad))

    def test_rejects_non_json_response(self) -> None:
        with pytest.raises(PlanParseError, match="non-JSON"):
            parse_plan("I refuse to follow instructions")

    def test_rejects_non_dict_root(self) -> None:
        with pytest.raises(PlanParseError, match="JSON object"):
            parse_plan('["a", "b"]')


# ---------------------------------------------------------------------------
# Module: call_planner
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_planner_returns_structured_plan() -> None:
    """End-to-end module test: mock provider returns canned plan,
    call_planner parses + validates it."""
    provider = MockProvider(response=_GOOD_PLAN_JSON)
    plan = await call_planner(
        description="contract eval please",
        planner_model="anthropic/claude-haiku-4-5-20251001",
        provider=provider,
    )
    assert isinstance(plan, ProjectPlan)
    assert len(plan.agents) == 2
    assert all(isinstance(a, PlannedAgent) for a in plan.agents)


# ---------------------------------------------------------------------------
# CLI: mdk plan --mock (dry-run by default)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plan_dry_run_renders_plan_without_writing(tmp_path: Path) -> None:
    """Default invocation is dry-run: prints the plan, doesn't touch disk."""
    result = runner.invoke(
        app,
        ["plan", "Triage support tickets and reply", "--mock"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Plan rendered
    assert "demo-project" in result.stdout or "Plan:" in result.stdout
    assert "triage" in result.stdout.lower()
    # Hint about --apply at the end
    assert "--apply" in result.stdout
    # Nothing written under tmp_path (we didn't target it, but no
    # cwd writes either — sanity check that scaffold path didn't fire).
    assert not (tmp_path / "demo-project").exists()


@pytest.mark.unit
def test_plan_apply_scaffolds_loadable_project(tmp_path: Path) -> None:
    """--apply writes a project to disk; the result must be a valid
    MDK project — agents/<name>/agent.yaml must parse + load."""
    result = runner.invoke(
        app,
        [
            "plan",
            "Generic 2-agent flow",
            "--mock",
            "--apply",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    project = tmp_path / "demo-project"
    assert project.is_dir()
    assert (project / "movate.yaml").is_file()
    assert (project / "agents").is_dir()
    # Each planned agent landed.
    assert (project / "agents" / "triage" / "agent.yaml").is_file()
    assert (project / "agents" / "summary" / "agent.yaml").is_file()
    # Agent YAML has the right name stamped (no __AGENT_NAME__ leftover).
    triage_yaml = (project / "agents" / "triage" / "agent.yaml").read_text()
    assert "__AGENT_NAME__" not in triage_yaml
    spec = yaml.safe_load(triage_yaml)
    assert spec["name"] == "triage"


@pytest.mark.unit
def test_plan_apply_rejects_existing_target(tmp_path: Path) -> None:
    """If <target>/<project_name> already exists, --apply must error
    rather than silently overwriting."""
    # Pre-create the destination
    (tmp_path / "demo-project").mkdir()
    result = runner.invoke(
        app,
        ["plan", "...", "--mock", "--apply", "--target", str(tmp_path)],
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "already exists" in combined.lower()


@pytest.mark.unit
def test_plan_with_custom_mock_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom MDK_MOCK_PLAN_RESPONSE lets tests inject specific plans."""
    custom = json.dumps(
        {
            "project_name": "my-custom-proj",
            "description": "Custom",
            "agents": [
                {
                    "name": "agent-a",
                    "template": "sql-writer",
                    "purpose": "p",
                }
            ],
            "workflow": ["agent-a"],
        }
    )
    monkeypatch.setenv("MDK_MOCK_PLAN_RESPONSE", custom)
    result = runner.invoke(app, ["plan", "anything", "--mock"])
    assert result.exit_code == 0
    assert "my-custom-proj" in result.stdout
    assert "agent-a" in result.stdout


@pytest.mark.unit
def test_plan_with_malformed_mock_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planner returns garbage → user-friendly error + exit 1 (not crash)."""
    # MockProvider validates the JSON at __init__, but we want to test the
    # planner-call failure path. Set the mock to valid JSON of the wrong
    # SHAPE — that's what the planner module rejects.
    bad = json.dumps({"not_a_plan": True})
    monkeypatch.setenv("MDK_MOCK_PLAN_RESPONSE", bad)
    result = runner.invoke(app, ["plan", "anything", "--mock"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "unusable response" in combined.lower() or "missing" in combined.lower()
