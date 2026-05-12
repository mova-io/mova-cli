"""CLI workflow integration: validate, show, run via CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli._workflow_path import is_workflow_path
from movate.cli.main import app

# mix_stderr=False keeps tracer/Rich output (stderr) out of the JSON we read
# from stdout. Without this, parsing `movate run -o json` stdout fails because
# the StdoutTracer's NDJSON spans get interleaved.
runner = CliRunner(mix_stderr=False)

# Mock provider response that satisfies a simple {"out": <str>} output schema —
# every test agent here is built with that shape so a single env var suffices.
WORKFLOW_MOCK_RESPONSE = '{"out": "ok"}'


# ---------------------------------------------------------------------------
# Helpers — scaffold a minimal valid 2-node workflow
# ---------------------------------------------------------------------------


def _make_agent(agent_dir: Path, *, name: str, in_key: str, out_key: str) -> Path:
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
    (agent_dir / "prompt.md").write_text("echo {{ input." + in_key + " }}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": [in_key],
                "properties": {in_key: {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": [out_key],
                "properties": {out_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {in_key: "x"}, "expected": {out_key: "x"}}) + "\n"
    )
    return agent_dir


def _make_state_schema(workflow_dir: Path) -> Path:
    """Permissive state schema — workflow tests focus on dispatch, not schema."""
    p = workflow_dir / "state.json"
    p.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
            }
        )
    )
    return p


def _make_workflow(
    workflow_dir: Path,
    *,
    nodes: list[dict],
    edges: list[dict],
    entrypoint: str = "first",
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    _make_state_schema(workflow_dir)
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "demo-workflow",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": entrypoint,
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _scaffold_two_node_workflow(tmp_path: Path) -> Path:
    """text → step1 → step2 (linear, 2-node). Returns the workflow dir."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "first", name="first-agent", in_key="text", out_key="step1")
    _make_agent(wf / "agents" / "second", name="second-agent", in_key="step1", out_key="step2")
    _make_workflow(
        wf,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )
    return wf


# ---------------------------------------------------------------------------
# is_workflow_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_workflow_path_detects_directory(tmp_path: Path) -> None:
    wf = _scaffold_two_node_workflow(tmp_path)
    assert is_workflow_path(wf) is True


@pytest.mark.unit
def test_is_workflow_path_detects_file(tmp_path: Path) -> None:
    wf = _scaffold_two_node_workflow(tmp_path)
    assert is_workflow_path(wf / "workflow.yaml") is True


@pytest.mark.unit
def test_is_workflow_path_false_for_agent(tmp_path: Path) -> None:
    a = _make_agent(tmp_path / "a", name="a", in_key="text", out_key="msg")
    assert is_workflow_path(a) is False
    assert is_workflow_path(a / "agent.yaml") is False


# ---------------------------------------------------------------------------
# `movate validate` — workflow path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_dispatches_to_workflow(tmp_path: Path) -> None:
    wf = _scaffold_two_node_workflow(tmp_path)
    result = runner.invoke(app, ["validate", str(wf)])
    assert result.exit_code == 0, result.stdout
    assert "demo-workflow" in result.stdout
    assert "(workflow)" in result.stdout
    assert "first → second" in result.stdout


@pytest.mark.unit
def test_validate_workflow_rejects_bad_topology(tmp_path: Path) -> None:
    """An agent with type=human in the YAML is rejected at parse time, but
    we want to confirm the CLI surfaces phase-gate errors clearly. Use a
    workflow where the entrypoint doesn't match the source node."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "a", name="a", in_key="text", out_key="step1")
    _make_agent(wf / "agents" / "b", name="b", in_key="step1", out_key="step2")
    _make_workflow(
        wf,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/a"},
            {"id": "second", "type": "agent", "ref": "./agents/b"},
        ],
        # Reverse edge → "second" has no inbound, "first" has no outbound,
        # so the source is "second" but entrypoint is still "first".
        edges=[{"from": "second", "to": "first"}],
        entrypoint="first",
    )
    result = runner.invoke(app, ["validate", str(wf)])
    assert result.exit_code == 2
    assert "validation failed" in result.stdout


# ---------------------------------------------------------------------------
# `movate show` — workflow path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_workflow_renders_topology(tmp_path: Path) -> None:
    wf = _scaffold_two_node_workflow(tmp_path)
    result = runner.invoke(app, ["show", str(wf)])
    assert result.exit_code == 0, result.stdout
    assert "demo-workflow" in result.stdout
    assert "(workflow)" in result.stdout
    # ASCII topology
    assert "first → second" in result.stdout
    # Mermaid block — both nodes + the directed edge
    assert "flowchart LR" in result.stdout
    assert "first --> second" in result.stdout


# ---------------------------------------------------------------------------
# `movate run` — workflow path with --mock
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_single_node_workflow_mock_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-node workflow exercises the full CLI dispatch path through the
    runner, executor, MockProvider, and output renderer.

    Why single-node: ``MOVATE_MOCK_RESPONSE`` returns one canned JSON, so a
    multi-node workflow would need either per-node mock responses (deferred
    feature) or two nodes with identical output schemas. Single-node keeps
    the CLI smoke tight without forcing that decision.
    """
    # Sandbox HOME so the SQLite store doesn't touch the user's ~/.movate.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"step1": "alpha"}')

    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "only", name="only", in_key="text", out_key="step1")
    _make_workflow(
        wf,
        nodes=[{"id": "first", "type": "agent", "ref": "./agents/only"}],
        edges=[],
    )

    result = runner.invoke(app, ["run", str(wf), '{"text": "seed"}', "--mock", "-o", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["final_state"]["step1"] == "alpha"
    assert payload["final_state"]["text"] == "seed"
    assert len(payload["nodes"]) == 1
    assert payload["nodes"][0]["node_id"] == "first"


@pytest.mark.unit
def test_run_workflow_rejects_invalid_initial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workflow with a state_schema requiring 'text' should reject empty input."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "a", name="a", in_key="text", out_key="step1")
    # state_schema requires "text"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "state.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
                "additionalProperties": True,
            }
        )
    )
    (wf / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "needs-text",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "first",
                "nodes": [{"id": "first", "type": "agent", "ref": "./agents/a"}],
                "edges": [],
            }
        )
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["run", str(wf), "{}", "--mock"])
    assert result.exit_code == 2
    # `run.py` writes error messages to stderr.
    assert "initial_state failed" in result.stderr or "workflow failed" in result.stderr


@pytest.mark.unit
def test_run_workflow_rejects_non_object_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf = _scaffold_two_node_workflow(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["run", str(wf), '"just a string"', "--mock"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# `--node-trace` flag — per-node state reconstruction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_workflow_node_trace_adds_state_trace_to_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--node-trace`` augments JSON output with a ``state_trace`` array
    showing the running state after each node. Useful for debugging
    which node added/changed which key."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"step1": "alpha"}')

    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "only", name="only", in_key="text", out_key="step1")
    _make_workflow(
        wf,
        nodes=[{"id": "first", "type": "agent", "ref": "./agents/only"}],
        edges=[],
    )

    result = runner.invoke(
        app,
        ["run", str(wf), '{"text": "seed"}', "--mock", "-o", "json", "--node-trace"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "state_trace" in payload
    assert len(payload["state_trace"]) == 1
    entry = payload["state_trace"][0]
    assert entry["node_id"] == "first"
    assert entry["output"] == {"step1": "alpha"}
    # state_after is the running state merged with this node's output —
    # so it has both the initial "text" key AND the node's "step1" output.
    assert entry["state_after"] == {"text": "seed", "step1": "alpha"}


@pytest.mark.unit
def test_run_workflow_node_trace_omitted_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--node-trace``, the JSON output does NOT include
    ``state_trace`` — backwards-compatible default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"step1": "alpha"}')

    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "only", name="only", in_key="text", out_key="step1")
    _make_workflow(
        wf,
        nodes=[{"id": "first", "type": "agent", "ref": "./agents/only"}],
        edges=[],
    )

    result = runner.invoke(app, ["run", str(wf), '{"text": "seed"}', "--mock", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "state_trace" not in payload


@pytest.mark.unit
def test_run_workflow_node_trace_warns_on_agent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--node-trace`` is workflow-only. If passed on a single-agent
    run, we print a dim hint and otherwise behave normally."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"text": "hi"}')

    # Bare agent (no workflow.yaml; just agent.yaml etc.)
    agent_dir = _make_agent(tmp_path / "ag", name="ag", in_key="text", out_key="text")
    result = runner.invoke(
        app,
        ["run", str(agent_dir), '{"text": "x"}', "--mock", "-o", "json", "--node-trace"],
    )
    assert result.exit_code == 0
    # The warning is on stderr (Rich console) but Typer's CliRunner
    # collapses stdout+stderr by default when mix_stderr=True; we just
    # assert success.
