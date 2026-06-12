"""``mdk validate`` warns on path-exclusive prompt keys (the B2 live-failure class).

Prompts render with Jinja ``StrictUndefined``: a converging workflow where
``{{ input.X }}`` is only produced on SOME paths into a node crashes at
runtime on the path that omits X. The lint computes, per agent node, the keys
guaranteed on EVERY entrypoint→node path (external initial inputs + the
outputs of the node's graph dominators) and warns — advisory, never exit-2 —
on an unguarded reference outside that set. Both guard idioms
(``{% if input.X is defined %}`` and ``| default(...)``) silence it, and a
linear chain whose keys all thread cleanly stays silent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app

runner = CliRunner(mix_stderr=False)


def _make_agent(
    agent_dir: Path,
    *,
    name: str,
    prompt: str,
    required_input: str,
    output_keys: list[str],
) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": f"agent {name}",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(prompt)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "required": [required_input],
                "properties": {required_input: {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": output_keys,
                "properties": {k: {"type": "string"} for k in output_keys},
            }
        )
    )


def _write_convergence_workflow(root: Path, *, final_prompt: str) -> Path:
    """entry → decision → (branch-a | branch-b) → final (exclusive OR-merge).

    Both branches read ``triage`` (produced by entry, so it is on every path)
    and write ``summary`` — but only branch-a writes ``extra``. A final-node
    prompt referencing ``{{ input.extra }}`` is therefore path-exclusive:
    fine via branch-a, a StrictUndefined crash via branch-b.
    """
    wf = root / "wf"
    _make_agent(
        wf / "agents" / "entry",
        name="entry-agent",
        prompt="triage {{ input.text }}",
        required_input="text",
        output_keys=["triage"],
    )
    _make_agent(
        wf / "agents" / "branch-a",
        name="branch-a-agent",
        prompt="deep-dive {{ input.triage }}",
        required_input="triage",
        output_keys=["summary", "extra"],
    )
    _make_agent(
        wf / "agents" / "branch-b",
        name="branch-b-agent",
        prompt="quick-pass {{ input.triage }}",
        required_input="triage",
        output_keys=["summary"],
    )
    _make_agent(
        wf / "agents" / "final",
        name="final-agent",
        prompt=final_prompt,
        required_input="summary",
        output_keys=["report"],
    )
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "state.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "text": {"type": "string"},
                    "triage": {"type": "string"},
                    "summary": {"type": "string"},
                    "extra": {"type": "string"},
                    "report": {"type": "string"},
                },
            }
        )
    )
    (wf / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "convergence-demo",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "entry",
                "nodes": [
                    {"id": "entry", "type": "agent", "ref": "./agents/entry"},
                    {
                        "id": "route",
                        "type": "decision",
                        "cases": [
                            {
                                "when": {"field": "triage", "op": "eq", "value": "deep"},
                                "to": "branch-a",
                            }
                        ],
                        "default": "branch-b",
                    },
                    {"id": "branch-a", "type": "agent", "ref": "./agents/branch-a"},
                    {"id": "branch-b", "type": "agent", "ref": "./agents/branch-b"},
                    {"id": "final", "type": "agent", "ref": "./agents/final"},
                ],
                "edges": [
                    {"from": "entry", "to": "route"},
                    {"from": "branch-a", "to": "final"},
                    {"from": "branch-b", "to": "final"},
                ],
            }
        )
    )
    return wf


def _write_linear_workflow(root: Path) -> Path:
    """first(text→step1) → second(step1→step2): every key threads cleanly."""
    wf = root / "wf"
    _make_agent(
        wf / "agents" / "first",
        name="first-agent",
        prompt="echo {{ input.text }}",
        required_input="text",
        output_keys=["step1"],
    )
    _make_agent(
        wf / "agents" / "second",
        name="second-agent",
        prompt="refine {{ input.step1 }}",
        required_input="step1",
        output_keys=["step2"],
    )
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "state.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "text": {"type": "string"},
                    "step1": {"type": "string"},
                    "step2": {"type": "string"},
                },
            }
        )
    )
    (wf / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "linear-demo",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "first",
                "nodes": [
                    {"id": "first", "type": "agent", "ref": "./agents/first"},
                    {"id": "second", "type": "agent", "ref": "./agents/second"},
                ],
                "edges": [{"from": "first", "to": "second"}],
            }
        )
    )
    return wf


@pytest.mark.unit
def test_warns_on_path_exclusive_key(tmp_path: Path) -> None:
    """`extra` is produced only on the branch-a path → advisory warning, exit 0."""
    wf = _write_convergence_workflow(
        tmp_path, final_prompt="report on {{ input.summary }} with {{ input.extra }}"
    )
    result = runner.invoke(cli_app, ["validate", str(wf)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined  # advisory — never fails the build
    assert "path-exclusive" in combined
    assert "final" in combined and "extra" in combined
    # the two guard idioms are named in the fix line
    assert "is defined" in combined
    assert "default(" in combined


@pytest.mark.unit
def test_default_filter_silences_the_warning(tmp_path: Path) -> None:
    wf = _write_convergence_workflow(
        tmp_path,
        final_prompt="report on {{ input.summary }} with {{ input.extra | default('') }}",
    )
    result = runner.invoke(cli_app, ["validate", str(wf)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "path-exclusive" not in combined


@pytest.mark.unit
def test_is_defined_guard_silences_the_warning(tmp_path: Path) -> None:
    wf = _write_convergence_workflow(
        tmp_path,
        final_prompt=(
            "report on {{ input.summary }}"
            "{% if input.extra is defined %} with {{ input.extra }}{% endif %}"
        ),
    )
    result = runner.invoke(cli_app, ["validate", str(wf)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "path-exclusive" not in combined


@pytest.mark.unit
def test_key_produced_on_every_path_is_silent(tmp_path: Path) -> None:
    """Keys guaranteed on every path must not warn: `triage` (written by the
    shared entry node) and `summary` (written by BOTH branches — different
    producers, but present whichever leg runs)."""
    wf = _write_convergence_workflow(
        tmp_path, final_prompt="report on {{ input.triage }} and {{ input.summary }}"
    )
    result = runner.invoke(cli_app, ["validate", str(wf)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "path-exclusive" not in combined


@pytest.mark.unit
def test_linear_workflow_is_silent(tmp_path: Path) -> None:
    """A cleanly-threaded linear chain (upstream output + external input refs)."""
    wf = _write_linear_workflow(tmp_path)
    result = runner.invoke(cli_app, ["validate", str(wf)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "path-exclusive" not in combined
