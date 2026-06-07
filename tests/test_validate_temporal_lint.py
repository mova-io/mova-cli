"""``mdk validate`` surfaces the Temporal determinism lint (ADR 054 D5).

For a ``runtime: temporal`` workflow, the front-door ``mdk validate`` check
runs the compiler's determinism linter and surfaces a summary of any findings
(non-deterministic ``time/random/datetime`` primitives, etc.) — pointing the
author at ``mdk workflow lint --runtime temporal`` for the full report. A
``native`` workflow never triggers this surface.

This is a *complementary* surface to the dedicated ``mdk workflow lint``
subcommand: validate is the all-up front-door, so an author who only runs
``mdk validate`` still learns about determinism risks before compile/run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


def _write_workflow(
    dir_: Path,
    *,
    runtime: str,
    human_prompt: str,
) -> Path:
    """Write a minimal 2-node (agent → human) workflow.yaml + state.json.

    The agent ``ref`` is scaffolded on disk because ``compile_workflow``
    resolves + checks each agent node's ref path at compile time.
    """
    dir_.mkdir(parents=True, exist_ok=True)
    scaffold_agent(dir_ / "agents" / "start", name="start")
    (dir_ / "state.json").write_text(
        json.dumps({"type": "object", "properties": {}}),
        encoding="utf-8",
    )
    spec = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "lint-demo",
        "version": "0.1.0",
        "runtime": runtime,
        "state_schema": "./state.json",
        "entrypoint": "start",
        "nodes": [
            {"id": "start", "type": "agent", "ref": "./agents/start"},
            {
                "id": "approval",
                "type": "human",
                "prompt": human_prompt,
                "output_contract": ["decision"],
            },
        ],
        "edges": [{"from": "start", "to": "approval"}],
    }
    (dir_ / "workflow.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return dir_


@pytest.mark.unit
def test_validate_temporal_surfaces_determinism_lint(tmp_path: Path) -> None:
    """A ``runtime: temporal`` workflow whose node references a
    non-deterministic primitive → validate prints the determinism-lint
    summary, the finding's code, and a pointer to ``mdk workflow lint``."""
    wf = _write_workflow(
        tmp_path / "wf",
        runtime="temporal",
        human_prompt="Approve refund? recorded_at = datetime.now()",
    )
    result = runner.invoke(cli_app, ["validate", str(wf)], env={"COLUMNS": "200"})

    # Validate itself still succeeds (lint is advisory, not a hard failure).
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "temporal determinism lint" in out
    assert "TEMPORAL_NONDETERMINISTIC_TIME" in out
    assert "@approval" in out
    assert "mdk workflow lint --runtime temporal" in out


@pytest.mark.unit
def test_validate_temporal_clean_workflow_reports_clean(tmp_path: Path) -> None:
    """A deterministic ``runtime: temporal`` workflow → validate reports the
    temporal lint is clean (no findings, no detail pointer)."""
    wf = _write_workflow(
        tmp_path / "wf",
        runtime="temporal",
        human_prompt="Approve the refund request for this customer?",
    )
    result = runner.invoke(cli_app, ["validate", str(wf)], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "temporal lint: ✓ clean" in out
    assert "mdk workflow lint" not in out


@pytest.mark.unit
def test_validate_native_workflow_skips_temporal_lint(tmp_path: Path) -> None:
    """A ``native`` workflow never triggers the temporal-lint surface, even
    when a node references a non-deterministic primitive (it is only a risk
    on the Temporal replay backend)."""
    wf = _write_workflow(
        tmp_path / "wf",
        runtime="native",
        human_prompt="Approve refund? recorded_at = datetime.now()",
    )
    result = runner.invoke(cli_app, ["validate", str(wf)], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "temporal" not in out.lower()
