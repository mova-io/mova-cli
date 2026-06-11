"""``mdk validate`` errors on ambiguous cross-workflow skill names (#853).

A skill DIRECTORY name is a process-global Python module name on the shared
worker — the python backend resolves ``<dir>.impl`` once per process. Two
deployed workflows shipping same-named skill dirs with DIFFERENT impls means
one workflow executes the other's code live (the ``sim-remediate`` failure).
``mdk validate <workflow>`` now scans sibling workflows under the same
workflows/ root and fails HARD on a differing same-named pair; byte-identical
duplicates (the ``redact-pii`` convention) stay allowed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app

runner = CliRunner(mix_stderr=False)


def _make_agent(agent_dir: Path) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "echo-agent",
                "version": "0.1.0",
                "description": "echoes text",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo {{ input.text }}")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            }
        )
    )


def _make_workflow(workflow_dir: Path) -> None:
    """A minimal single-agent workflow that passes compile + validate."""
    _make_agent(workflow_dir / "agents" / "one")
    (workflow_dir / "state.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {"text": {"type": "string"}, "summary": {"type": "string"}},
            }
        )
    )
    (workflow_dir / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": workflow_dir.name,
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "one",
                "nodes": [{"id": "one", "type": "agent", "ref": "./agents/one"}],
                "edges": [],
            }
        )
    )


def _make_skill(workflow_dir: Path, name: str, *, impl_body: str) -> Path:
    skill_dir = workflow_dir / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Skill",
                "name": name,
                "version": "0.1.0",
                "description": f"sim skill {name}",
                "implementation": {"kind": "python", "module": "impl"},
                "schema": {"input": {"type": "object"}, "output": {"type": "object"}},
            }
        )
    )
    (skill_dir / "impl.py").write_text(impl_body)
    return skill_dir


def _tree(tmp_path: Path, *, sibling_impl: str) -> Path:
    """workflows/ root with wf-a + wf-b both shipping skills/sim-thing."""
    root = tmp_path / "workflows"
    wf_a = root / "wf-a"
    wf_b = root / "wf-b"
    _make_workflow(wf_a)
    _make_workflow(wf_b)
    _make_skill(wf_a, "sim-thing", impl_body="def run(payload, ctx):\n    return {'a': 1}\n")
    _make_skill(wf_b, "sim-thing", impl_body=sibling_impl)
    return wf_a


@pytest.mark.unit
def test_colliding_skill_names_fail_validation(tmp_path: Path) -> None:
    """Same dir name, different impl bytes → hard ERROR naming both paths."""
    wf_a = _tree(
        tmp_path, sibling_impl="def run(payload, ctx):\n    return {'b': 2}  # different\n"
    )
    result = runner.invoke(cli_app, ["validate", str(wf_a)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert "ambiguous cross-workflow skill name" in combined
    assert "sim-thing" in combined
    assert "wf-a" in combined and "wf-b" in combined  # both paths named
    assert "rename" in combined.lower()


@pytest.mark.unit
def test_byte_identical_duplicate_passes(tmp_path: Path) -> None:
    """The redact-pii convention: byte-identical duplicates are interchangeable."""
    wf_a = _tree(tmp_path, sibling_impl="def run(payload, ctx):\n    return {'a': 1}\n")
    result = runner.invoke(cli_app, ["validate", str(wf_a)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "ambiguous" not in combined


@pytest.mark.unit
def test_pycache_noise_does_not_break_identity(tmp_path: Path) -> None:
    """__pycache__ artifacts in one copy must not turn identical dirs ambiguous."""
    wf_a = _tree(tmp_path, sibling_impl="def run(payload, ctx):\n    return {'a': 1}\n")
    cache = wf_a / "skills" / "sim-thing" / "__pycache__"
    cache.mkdir()
    (cache / "impl.cpython-311.pyc").write_bytes(b"\x00compiled")
    result = runner.invoke(cli_app, ["validate", str(wf_a)])
    assert result.exit_code == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_workflow_without_skills_is_untouched(tmp_path: Path) -> None:
    root = tmp_path / "workflows"
    wf_a = root / "wf-a"
    _make_workflow(wf_a)
    _make_workflow(root / "wf-b")
    _make_skill(root / "wf-b", "sim-thing", impl_body="def run(p, c):\n    return {}\n")
    result = runner.invoke(cli_app, ["validate", str(wf_a)])
    assert result.exit_code == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_non_workflow_sibling_dir_is_ignored(tmp_path: Path) -> None:
    """A sibling dir without workflow.yaml (docs, scratch) is not a collision."""
    root = tmp_path / "workflows"
    wf_a = root / "wf-a"
    _make_workflow(wf_a)
    _make_skill(wf_a, "sim-thing", impl_body="def run(p, c):\n    return {'a': 1}\n")
    scratch = root / "scratch"
    (scratch / "skills" / "sim-thing").mkdir(parents=True)
    (scratch / "skills" / "sim-thing" / "impl.py").write_text("def run(p, c):\n    return {}\n")
    result = runner.invoke(cli_app, ["validate", str(wf_a)])
    assert result.exit_code == 0, result.stdout + result.stderr
