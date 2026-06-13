"""CLI: ``mdk workflow lint`` — surface the Temporal determinism linter
(ADR 054 D5) to workflow authors.

Mirrors the prompt-linter CLI tests (``--strict`` promotes warnings to
errors, ``--no-lint`` skips). The command is a thin surface over
``lint_temporal``; these tests pin the CLI contract, not the lint logic
(which is covered by ``test_temporal_compiler.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.workflow.compilers.temporal import (
    LINT_HUMAN_NODE_PHASE2,
    LINT_UNBOUNDED_LOOP,
)
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {"text": {"type": "string"}},
}


def _write_workflow(
    workflow_dir: Path,
    *,
    nodes: list[dict],
    edges: list[dict],
    entrypoint: str,
) -> Path:
    """Write a workflow.yaml + state schema under ``workflow_dir``."""
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    spec = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "lint-demo",
        "version": "0.1.0",
        "description": "Temporal lint fixture",
        "state_schema": "./state.json",
        "entrypoint": entrypoint,
        "nodes": nodes,
        "edges": edges,
    }
    (workflow_dir / "workflow.yaml").write_text(yaml.safe_dump(spec))
    return workflow_dir


def _linear_workflow(tmp_path: Path) -> Path:
    """A clean, deterministic two-agent linear workflow (no lint findings)."""
    wf = tmp_path / "wf"
    scaffold_agent(wf / "agents" / "first", name="first-agent")
    scaffold_agent(wf / "agents" / "second", name="second-agent")
    return _write_workflow(
        wf,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
        entrypoint="first",
    )


def _human_node_workflow(tmp_path: Path) -> Path:
    """An agent → HUMAN-gate workflow (triggers TEMPORAL_HUMAN_NODE_PHASE2)."""
    wf = tmp_path / "wf"
    scaffold_agent(wf / "agents" / "first", name="first-agent")
    return _write_workflow(
        wf,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "approve", "type": "human", "prompt": "Approve?"},
        ],
        edges=[{"from": "first", "to": "approve"}],
        entrypoint="first",
    )


def _looping_workflow(tmp_path: Path) -> Path:
    """A back-edge with no max_iterations bound (triggers TEMPORAL_UNBOUNDED_LOOP)."""
    wf = tmp_path / "wf"
    scaffold_agent(wf / "agents" / "a", name="a-agent")
    scaffold_agent(wf / "agents" / "b", name="b-agent")
    return _write_workflow(
        wf,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
        ],
        edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
        entrypoint="a",
    )


# ---------------------------------------------------------------------------
# Clean workflow
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lint_clean_workflow_passes(tmp_path: Path) -> None:
    wf = _linear_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--runtime", "temporal"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no temporal determinism issues" in result.stdout


@pytest.mark.unit
def test_lint_defaults_to_temporal_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No path + no --runtime → lints the cwd workflow under the temporal rules."""
    wf = _linear_workflow(tmp_path)
    monkeypatch.chdir(wf)
    result = runner.invoke(app, ["workflow", "lint"])
    assert result.exit_code == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Warning findings + --strict parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lint_human_node_warns_but_exits_zero(tmp_path: Path) -> None:
    wf = _human_node_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf)])
    # Phase 1: warnings only — non-strict run still exits 0.
    assert result.exit_code == 0, result.stdout + result.stderr
    assert LINT_HUMAN_NODE_PHASE2 in result.stdout
    assert "approve" in result.stdout  # node id surfaced


@pytest.mark.unit
def test_lint_strict_promotes_warning_to_error(tmp_path: Path) -> None:
    wf = _human_node_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--strict"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert LINT_HUMAN_NODE_PHASE2 in result.stdout


@pytest.mark.unit
def test_lint_unbounded_loop_warns(tmp_path: Path) -> None:
    """Cyclic workflows compile (allow_cycles) so the unbounded-loop lint fires."""
    wf = _looping_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert LINT_UNBOUNDED_LOOP in result.stdout


# ---------------------------------------------------------------------------
# --no-lint parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lint_no_lint_skips_linter(tmp_path: Path) -> None:
    wf = _human_node_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--no-lint"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert LINT_HUMAN_NODE_PHASE2 not in result.stdout
    assert "lint skipped" in result.stdout


@pytest.mark.unit
def test_lint_no_lint_strict_does_not_fail(tmp_path: Path) -> None:
    """--no-lint wins over --strict: no findings collected → nothing to promote."""
    wf = _human_node_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--no-lint", "--strict"])
    assert result.exit_code == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# --json output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lint_json_output_shape(tmp_path: Path) -> None:
    wf = _human_node_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["kind"] == "workflow-lint"
    assert payload["runtime"] == "temporal"
    assert payload["ok"] is True
    assert payload["counts"]["warnings"] >= 1
    codes = {i["code"] for i in payload["issues"]}
    assert LINT_HUMAN_NODE_PHASE2 in codes


@pytest.mark.unit
def test_lint_json_strict_marks_not_ok_and_exits_two(tmp_path: Path) -> None:
    wf = _human_node_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--json", "--strict"])
    assert result.exit_code == 2, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False


@pytest.mark.unit
def test_lint_clean_json_no_issues(tmp_path: Path) -> None:
    wf = _linear_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["issues"] == []
    assert payload["counts"] == {"errors": 0, "warnings": 0}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lint_rejects_non_temporal_runtime(tmp_path: Path) -> None:
    wf = _linear_workflow(tmp_path)
    result = runner.invoke(app, ["workflow", "lint", str(wf), "--runtime", "native"])
    assert result.exit_code == 2
    assert "temporal" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_lint_missing_workflow_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["workflow", "lint", str(tmp_path / "nope")])
    assert result.exit_code == 2
