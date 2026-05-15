"""Smoke tests for the 10 role-agent templates.

Every template must:
1. Be registered in ``movate.templates.TEMPLATES``.
2. Scaffold cleanly via ``mdk init <name> -t <template>``.
3. Pass ``load_agent()`` on the scaffolded directory (every field
   typechecks, schemas compile, prompt renders).
4. Have a non-empty ``evals/dataset.jsonl`` so ``mdk audit`` doesn't
   flag missing-evals immediately.
5. The scaffolded agent's `agent.yaml` resolves to ``name = <CLI arg>``
   (the loader applies the ``__AGENT_NAME__`` substitution).

These tests run against `MockProvider` indirectly — they don't invoke
the model, just verify load_agent + the scaffold layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent
from movate.templates import TEMPLATES

runner = CliRunner(mix_stderr=False)


# The 10 role-based templates added in this PR. Pinned here (rather
# than read from TEMPLATES at runtime) so a future template addition
# requires a deliberate test-list update — keeps the contract explicit.
ROLE_TEMPLATES: list[str] = [
    "rag-qa",
    "ticket-triager",
    "email-responder",
    "sql-writer",
    "code-reviewer",
    "lead-qualifier",
    "meeting-summarizer",
    "resume-screener",
    "compliance-checker",
    "research-agent",
]


@pytest.mark.unit
class TestRegistry:
    @pytest.mark.parametrize("name", ROLE_TEMPLATES)
    def test_template_is_registered(self, name: str) -> None:
        assert name in TEMPLATES, (
            f"Role template {name!r} not in TEMPLATES. "
            f"Did you forget to update src/movate/templates/__init__.py?"
        )


@pytest.mark.unit
class TestScaffoldAndLoad:
    """For each role template:

    * `mdk init <agent> -t <template>` succeeds.
    * The resulting directory loads via `load_agent` without error.
    * `__AGENT_NAME__` was substituted with the CLI argument.
    * `evals/dataset.jsonl` exists and has at least one entry.
    """

    @pytest.mark.parametrize("template", ROLE_TEMPLATES)
    def test_template_scaffolds_and_loads(self, template: str, tmp_path: Path) -> None:
        agent_name = template + "-smoke"
        result = runner.invoke(
            app,
            ["init", agent_name, "-t", template, "--target", str(tmp_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        agent_dir = tmp_path / agent_name
        assert (agent_dir / "agent.yaml").is_file()
        assert (agent_dir / "prompt.md").is_file()
        assert (agent_dir / "schema" / "input.json").is_file()
        assert (agent_dir / "schema" / "output.json").is_file()
        assert (agent_dir / "evals" / "dataset.jsonl").is_file()

        # load_agent must succeed end-to-end. This catches:
        # - YAML errors
        # - missing required AgentSpec fields
        # - bad JSON Schema in schema/{input,output}.json
        # - Jinja syntax errors in prompt.md (caught at render time —
        #   tested in a separate render check below)
        bundle = load_agent(agent_dir)
        assert bundle.spec.name == agent_name, (
            f"__AGENT_NAME__ substitution didn't apply for {template} — "
            f"got name={bundle.spec.name!r} instead of {agent_name!r}"
        )

        # Dataset has at least one row of valid JSONL.
        dataset = (agent_dir / "evals" / "dataset.jsonl").read_text()
        rows = [json.loads(line) for line in dataset.splitlines() if line.strip()]
        assert len(rows) >= 1, f"{template} dataset has no rows"
        for row in rows:
            assert "input" in row and "expected" in row, (
                f"{template} dataset row missing 'input' or 'expected': {row!r}"
            )


@pytest.mark.unit
class TestPromptRenders:
    """Render the prompt against the first dataset row's input — confirms
    Jinja syntax + that the schema and prompt agree on field names."""

    @pytest.mark.parametrize("template", ROLE_TEMPLATES)
    def test_prompt_renders_with_first_dataset_row(self, template: str, tmp_path: Path) -> None:
        agent_name = template + "-render"
        result = runner.invoke(
            app,
            ["init", agent_name, "-t", template, "--target", str(tmp_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        agent_dir = tmp_path / agent_name
        bundle = load_agent(agent_dir)

        # Pull the first eval row and validate input shape, then render.
        dataset_path = agent_dir / "evals" / "dataset.jsonl"
        first_row = json.loads(dataset_path.read_text().splitlines()[0])
        bundle.input_validator.validate(first_row["input"])
        # Render — Jinja must resolve every {{ input.X }} reference.
        rendered = bundle.render_prompt(first_row["input"])
        assert rendered, f"{template} prompt rendered to empty string"
