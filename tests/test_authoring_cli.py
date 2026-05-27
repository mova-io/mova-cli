"""ADR 025 PR1 — thin ``mdk authoring`` CLI smoke tests.

Asserts the scriptable surface that PR2's ``AGENTS.md`` will document actually
exists and behaves: ``list`` (text + json), ``plan`` (no writes), ``apply``
(--fast for additive, refuses gated without --yes), ``history``, ``undo``.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: greeter
version: 0.1.0
description: A test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
  params:
    temperature: 0.0
prompt: ./prompt.md
schema:
  input:
    text: string
  output:
    message: string
evals:
  dataset: ./evals/dataset.jsonl
"""


def _make_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("# test\n")
    agent_dir = root / "agents" / "greeter"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML)
    (agent_dir / "prompt.md").write_text("You are a greeter.\n")
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "hello"}}\n'
    )
    return root


def test_authoring_list_text() -> None:
    result = runner.invoke(app, ["authoring", "list"])
    assert result.exit_code == 0
    assert "add-context" in result.stdout
    assert "ingest-kb" in result.stdout


def test_authoring_list_json() -> None:
    result = runner.invoke(app, ["authoring", "list", "-o", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {e["name"] for e in payload}
    assert "add-context" in names
    assert all("args_schema" in e for e in payload)


def test_authoring_plan_no_writes(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    before = sorted(p.name for p in (root / "agents" / "greeter").rglob("*"))
    result = runner.invoke(
        app,
        [
            "authoring",
            "plan",
            "add-context",
            "--args",
            json.dumps({"agent": "greeter", "name": "tone", "body": "# Tone\n"}),
            "--project",
            str(root),
        ],
    )
    assert result.exit_code == 0
    assert "+# Tone" in result.stdout
    after = sorted(p.name for p in (root / "agents" / "greeter").rglob("*"))
    assert after == before  # plan wrote nothing


def test_authoring_apply_fast_then_history_then_undo(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    apply_res = runner.invoke(
        app,
        [
            "authoring",
            "apply",
            "add-context",
            "--args",
            json.dumps({"agent": "greeter", "name": "tone", "body": "# Tone\n"}),
            "--project",
            str(root),
            "--fast",
        ],
    )
    assert apply_res.exit_code == 0, apply_res.stderr
    assert (root / "agents" / "greeter" / "contexts" / "tone.md").is_file()

    hist = runner.invoke(app, ["authoring", "history", "--project", str(root), "-o", "json"])
    assert hist.exit_code == 0
    entries = json.loads(hist.stdout)
    assert len(entries) == 1
    assert entries[0]["action"] == "add-context"

    undo = runner.invoke(app, ["authoring", "undo", "--project", str(root)])
    assert undo.exit_code == 0
    assert not (root / "agents" / "greeter" / "contexts" / "tone.md").is_file()


def test_authoring_apply_gated_refuses_without_yes(tmp_path: Path) -> None:
    """A cost-incurring action (set-model) is declined when the prompt says no."""
    root = _make_project(tmp_path / "proj")
    result = runner.invoke(
        app,
        [
            "authoring",
            "apply",
            "set-model",
            "--args",
            json.dumps({"agent": "greeter", "provider": "anthropic/claude-sonnet-4-6"}),
            "--project",
            str(root),
        ],
        input="n\n",  # decline the confirmation prompt
    )
    assert result.exit_code == 1
    # agent.yaml unchanged.
    import yaml  # noqa: PLC0415

    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert data["model"]["provider"] == "openai/gpt-4o-mini-2024-07-18"


def test_authoring_apply_gated_proceeds_with_yes(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    result = runner.invoke(
        app,
        [
            "authoring",
            "apply",
            "set-model",
            "--args",
            json.dumps({"agent": "greeter", "provider": "anthropic/claude-sonnet-4-6"}),
            "--project",
            str(root),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.stderr
    import yaml  # noqa: PLC0415

    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert data["model"]["provider"] == "anthropic/claude-sonnet-4-6"
