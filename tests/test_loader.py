"""Loader tests: agent dir → AgentBundle, with strict early failures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from movate.core.loader import AgentLoadError, load_agent

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


def _scaffold_agent(dst: Path, name: str = "test-agent") -> Path:
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.mark.unit
def test_load_template_agent(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    assert bundle.spec.name == "demo"
    assert bundle.prompt_hash  # sha256 hex
    assert bundle.input_schema["required"] == ["text"]
    assert bundle.output_schema["required"] == ["message"]


@pytest.mark.unit
def test_render_prompt_with_input(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    rendered = bundle.render_prompt({"text": "ping"})
    assert "ping" in rendered


@pytest.mark.unit
def test_render_prompt_undefined_variable_fails(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    # StrictUndefined → missing namespace raises.
    with pytest.raises(Exception):
        bundle.render_prompt({})


@pytest.mark.unit
def test_load_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(AgentLoadError, match="not a directory"):
        load_agent(tmp_path / "does-not-exist")


@pytest.mark.unit
def test_load_missing_agent_yaml(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(AgentLoadError, match=r"agent\.yaml not found"):
        load_agent(tmp_path / "empty")


@pytest.mark.unit
def test_load_invalid_yaml(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "agent.yaml").write_text("this: is: not: yaml")
    with pytest.raises(AgentLoadError):
        load_agent(agent_dir)


@pytest.mark.unit
def test_load_validation_error_surfaces(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("0.1.0", "not-a-version"))
    with pytest.raises(AgentLoadError, match=r"agent\.yaml validation failed"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_load_missing_prompt(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "prompt.md").unlink()
    with pytest.raises(AgentLoadError, match="prompt file not found"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_load_invalid_input_schema(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "potato"})
    )
    with pytest.raises(AgentLoadError, match="invalid JSON schema"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_prompt_hash_is_stable(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    a = load_agent(agent_dir)
    b = load_agent(agent_dir)
    assert a.prompt_hash == b.prompt_hash


@pytest.mark.unit
def test_prompt_hash_changes_when_prompt_changes(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    before = load_agent(agent_dir).prompt_hash
    (agent_dir / "prompt.md").write_text("changed")
    after = load_agent(agent_dir).prompt_hash
    assert before != after
