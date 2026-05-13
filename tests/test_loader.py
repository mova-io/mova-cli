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


# ---------------------------------------------------------------------------
# Inline shorthand schemas (v0.6+) — `input:` / `output:` may be a dict
# that the loader compiles into JSON Schema instead of pointing at a file.
# ---------------------------------------------------------------------------


def _write_agent(
    agent_dir: Path,
    *,
    schema_block: str,
    name: str = "shorthand-agent",
) -> Path:
    """Build a minimal agent dir with a custom `schema:` block."""
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        f"{schema_block}\n"
    )
    (agent_dir / "prompt.md").write_text("you are helpful\n\n{{ input.message }}")
    return agent_dir


@pytest.mark.unit
def test_loader_compiles_inline_dict_shorthand(tmp_path: Path) -> None:
    """`input:` / `output:` as dicts compile via the shorthand compiler.
    No `schema/` subfolder needed — common case for trivial schemas."""
    agent_dir = _write_agent(
        tmp_path / "inline",
        schema_block=(
            "schema:\n"
            "  input:\n"
            "    message: string\n"
            "  output:\n"
            "    response: string\n"
            "    confidence: number?\n"
        ),
    )
    bundle = load_agent(agent_dir)
    assert bundle.input_schema == {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    assert bundle.output_schema["properties"]["confidence"] == {"type": "number"}
    assert bundle.output_schema["required"] == ["response"]
    # Validators built from the compiled schemas accept conforming
    # payloads and reject non-conforming ones — proves the inline
    # form is a drop-in for the file form downstream.
    bundle.input_validator.validate({"message": "hi"})
    bundle.output_validator.validate({"response": "ok"})
    with pytest.raises(Exception):
        bundle.input_validator.validate({})


@pytest.mark.unit
def test_loader_path_form_still_works(tmp_path: Path) -> None:
    """Existing agents using `schema: { input: ./schema/x.json }` keep
    loading — this PR is purely additive for the inline form."""
    agent_dir = _write_agent(
        tmp_path / "pathy",
        schema_block=("schema:\n  input: ./schema/input.json\n  output: ./schema/output.json\n"),
    )
    (agent_dir / "schema").mkdir()
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ["response"],
                "additionalProperties": False,
            }
        )
    )
    bundle = load_agent(agent_dir)
    assert bundle.input_schema["required"] == ["message"]
    assert bundle.output_schema["required"] == ["response"]


@pytest.mark.unit
def test_loader_mixed_inline_and_path_works(tmp_path: Path) -> None:
    """One side inline, the other side a file — both legal; useful when
    input is trivial but output has a complex contract (or vice versa)."""
    agent_dir = _write_agent(
        tmp_path / "mixed",
        schema_block=("schema:\n  input:\n    message: string\n  output: ./schema/output.json\n"),
    )
    (agent_dir / "schema").mkdir()
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                "required": ["a"],
                "additionalProperties": False,
            }
        )
    )
    bundle = load_agent(agent_dir)
    assert bundle.input_schema["properties"]["message"] == {"type": "string"}
    assert bundle.output_schema["required"] == ["a"]


@pytest.mark.unit
def test_loader_bad_inline_shorthand_surfaces_field_path(tmp_path: Path) -> None:
    """A typo in the shorthand surfaces as an AgentLoadError whose
    message includes the offending field's key path — operators see
    'input.message' in the error, not just 'invalid schema'."""
    agent_dir = _write_agent(
        tmp_path / "bad",
        schema_block=("schema:\n  input:\n    message: strng\n  output:\n    response: string\n"),
    )
    with pytest.raises(AgentLoadError, match=r"input\.message"):
        load_agent(agent_dir)
