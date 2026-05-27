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
    def test_template_scaffolds_and_loads(
        self, template: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use the realistic `mdk init --project + mdk add` flow rather
        # than the bare `mdk init -t` path. Role templates that declare
        # skills or contexts need a project context for the
        # auto-scaffold mechanisms (`_maybe_scaffold_declared_skills`,
        # `_maybe_copy_template_contexts`) to fire — without them
        # `load_agent` errors out on missing skill / context refs.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--project", "proj", "--skip-snapshot"])
        assert result.exit_code == 0, result.stdout + result.stderr
        project = tmp_path / "proj"
        monkeypatch.chdir(project)

        # `mdk add` keeps the template name as the agent name by default.
        result = runner.invoke(app, ["add", template])
        assert result.exit_code == 0, result.stdout + result.stderr

        agent_dir = project / "agents" / template
        assert (agent_dir / "agent.yaml").is_file()
        assert (agent_dir / "prompt.md").is_file()
        # Canonical layout (#127): `mdk add` produces schema/*.yaml files
        # (never .json) plus an evals/judge.yaml.example. The loader still
        # accepts inline / JSON schema for hand-authored agents, but the
        # SHIPPED templates standardize on canonical YAML.
        assert (agent_dir / "schema" / "input.yaml").is_file(), (
            f"{template}: missing schema/input.yaml"
        )
        assert (agent_dir / "schema" / "output.yaml").is_file(), (
            f"{template}: missing schema/output.yaml"
        )
        assert not (agent_dir / "schema" / "input.json").exists(), (
            f"{template}: stray schema/input.json — should be canonical YAML"
        )
        assert not (agent_dir / "schema" / "output.json").exists(), (
            f"{template}: stray schema/output.json — should be canonical YAML"
        )
        assert (agent_dir / "evals" / "dataset.jsonl").is_file()
        assert (agent_dir / "evals" / "judge.yaml.example").is_file(), (
            f"{template}: missing evals/judge.yaml.example"
        )

        # load_agent must succeed end-to-end. This catches:
        # - YAML errors
        # - missing required AgentSpec fields
        # - bad JSON Schema in schema/{input,output}.json
        # - Jinja syntax errors in prompt.md (caught at render time —
        #   tested in a separate render check below)
        # - Missing skill / context references (caught by the loader)
        bundle = load_agent(agent_dir)
        assert bundle.spec.name == template, (
            f"__AGENT_NAME__ substitution didn't apply for {template} — "
            f"got name={bundle.spec.name!r} instead of {template!r}"
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
    def test_prompt_renders_with_first_dataset_row(
        self, template: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same shape as test_template_scaffolds_and_loads — use
        # `mdk init --project + mdk add` so the skill / context
        # auto-scaffold mechanisms fire for templates that declare
        # them. Without that, role templates with `skills:` blocks
        # error at load time (no skill registry in a flat dir).
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--project", "proj", "--skip-snapshot"])
        assert result.exit_code == 0, result.stdout + result.stderr
        project = tmp_path / "proj"
        monkeypatch.chdir(project)
        result = runner.invoke(app, ["add", template])
        assert result.exit_code == 0, result.stdout + result.stderr

        agent_dir = project / "agents" / template
        bundle = load_agent(agent_dir)

        # Pull the first eval row and validate input shape, then render.
        dataset_path = agent_dir / "evals" / "dataset.jsonl"
        first_row = json.loads(dataset_path.read_text().splitlines()[0])
        bundle.input_validator.validate(first_row["input"])
        # Render — Jinja must resolve every {{ input.X }} reference.
        rendered = bundle.render_prompt(first_row["input"])
        assert rendered, f"{template} prompt rendered to empty string"
