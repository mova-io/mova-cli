"""``mdk validate`` surfaces the chained-agent state-threading lint (authoring slice 1).

Workflow nodes share one state dict, threaded in topological order. The silent
footgun is a node whose required INPUT key is neither an external initial-state
input nor produced by an UPSTREAM node — at runtime it just receives nothing.
`mdk validate` now flags that at author time (advisory; it never fails the
build). A correctly-threaded workflow stays silent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app

runner = CliRunner(mix_stderr=False)


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": f"reads {input_key}, writes {output_key}",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo {{ input." + input_key + " }} as " + output_key)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "required": [input_key],
                "properties": {input_key: {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [output_key],
                "properties": {output_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )


def _write_workflow(root: Path, *, second_input_key: str) -> Path:
    """A two-agent chain: first(text→step1) → second(<second_input_key>→step2).

    With ``second_input_key="step1"`` the chain threads cleanly; with ``"query"``
    the second agent requires a key no upstream node produces (the footgun).
    """
    wf = root / "wf"
    _make_agent(wf / "agents" / "first", name="first-agent", input_key="text", output_key="step1")
    _make_agent(
        wf / "agents" / "second",
        name="second-agent",
        input_key=second_input_key,
        output_key="step2",
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
                "name": "thread-demo",
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
def test_validate_flags_state_threading_gap(tmp_path: Path) -> None:
    # second-agent requires `query`, but nothing upstream produces it and it's
    # not a declared initial-state input → the lint warns (advisory, exit 0).
    wf = _write_workflow(tmp_path, second_input_key="query")
    result = runner.invoke(cli_app, ["validate", str(wf)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined  # advisory — never fails the build
    assert "state-threading" in combined
    assert "query" in combined


@pytest.mark.unit
def test_validate_silent_when_threaded_cleanly(tmp_path: Path) -> None:
    # first(text→step1) → second(step1→step2): every required input is produced
    # upstream or is an initial input → no state-threading warning.
    wf = _write_workflow(tmp_path, second_input_key="step1")
    result = runner.invoke(cli_app, ["validate", str(wf)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "state-threading" not in combined
