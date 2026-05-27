"""Loader tests: agent dir → AgentBundle, with strict early failures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from movate.core.config import AgentDefaults
from movate.core.loader import AgentLoadError, load_agent

# Empty defaults → bypass any project policy.yaml so these loader tests run
# pristine regardless of the invoking cwd (same escape hatch the layered-
# defaults tests use).
_NO_DEFAULTS = AgentDefaults()

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
    # ADR 026 D2: the loader's missing-dir message reads as a clear
    # diagnostic; CLI commands intercept the bare-name case earlier with a
    # command-aware hint (see movate.cli._resolve.resolve_agent_arg).
    with pytest.raises(AgentLoadError, match="no agent directory at"):
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


# ---------------------------------------------------------------------------
# Friendly validation errors (PR: name unknown fields + allowed set +
# did-you-mean). The schema stays strict-by-design (extra="forbid"); only
# the MESSAGE improves so a human / LLM can self-correct.
# ---------------------------------------------------------------------------


def _write_agent_yaml(agent_dir: Path, *, extra_block: str) -> Path:
    """Minimal valid agent.yaml + an injected `extra_block` (raw YAML)."""
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: friendly-errors\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    message: string\n"
        "  output:\n"
        "    response: string\n"
        f"{extra_block}"
    )
    (agent_dir / "prompt.md").write_text("p\n\n{{ input.message }}")
    return agent_dir


@pytest.mark.unit
def test_unknown_nested_field_lists_allowed(tmp_path: Path) -> None:
    """An unknown key under `metadata:` names the key + lists AgentMetadata's
    allowed fields (the reported `category` case)."""
    agent_dir = _write_agent_yaml(tmp_path / "demo", extra_block="metadata:\n  category: x\n")
    with pytest.raises(AgentLoadError) as exc:
        load_agent(agent_dir, defaults=_NO_DEFAULTS)
    msg = str(exc.value)
    assert "unknown field 'category'" in msg
    assert "in 'metadata'" in msg
    assert "allowed fields here:" in msg
    # AgentMetadata's full field set must be listed so the user can self-correct.
    for field_name in ("persona", "role", "capabilities", "tags", "examples", "owner"):
        assert field_name in msg


@pytest.mark.unit
def test_unknown_nested_field_did_you_mean(tmp_path: Path) -> None:
    """A near-miss typo gets a difflib did-you-mean suggestion."""
    agent_dir = _write_agent_yaml(tmp_path / "demo", extra_block="metadata:\n  capabilites: []\n")
    with pytest.raises(AgentLoadError) as exc:
        load_agent(agent_dir, defaults=_NO_DEFAULTS)
    msg = str(exc.value)
    assert "unknown field 'capabilites'" in msg
    assert "Did you mean 'capabilities'?" in msg


@pytest.mark.unit
def test_unknown_top_level_field_names_top_level(tmp_path: Path) -> None:
    """A top-level extra key is reported as 'agent.yaml top level' and lists
    AgentSpec's allowed fields."""
    agent_dir = _write_agent_yaml(tmp_path / "demo", extra_block="foo: bar\n")
    with pytest.raises(AgentLoadError) as exc:
        load_agent(agent_dir, defaults=_NO_DEFAULTS)
    msg = str(exc.value)
    assert "unknown field 'foo'" in msg
    assert "agent.yaml top level" in msg
    # A couple of AgentSpec fields prove the allowed list is the top-level one.
    assert "model" in msg
    assert "prompt" in msg


@pytest.mark.unit
def test_non_extra_error_renders_clean_line(tmp_path: Path) -> None:
    """A non-extra error (wrong type) still renders a clean `<loc>: <msg>`
    line — no crash, no pydantic URL noise."""
    agent_dir = _write_agent_yaml(
        tmp_path / "demo", extra_block="timeouts:\n  call_ms: not-an-int\n"
    )
    with pytest.raises(AgentLoadError) as exc:
        load_agent(agent_dir, defaults=_NO_DEFAULTS)
    msg = str(exc.value)
    assert "agent.yaml validation failed" in msg
    assert "timeouts.call_ms:" in msg
    # The friendly formatter strips the "For further information visit
    # https://errors.pydantic.dev/..." trailer.
    assert "errors.pydantic.dev" not in msg


@pytest.mark.unit
def test_load_missing_prompt(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "prompt.md").unlink()
    with pytest.raises(AgentLoadError, match="prompt file not found"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_load_invalid_input_schema(tmp_path: Path) -> None:
    # The init template uses inline-shorthand schemas now, so to test
    # the path-form's JSON Schema validation we build the agent.yaml
    # explicitly with file paths + drop a malformed input.json in.
    agent_dir = _write_agent(
        tmp_path / "demo",
        schema_block=("schema:\n  input: ./schema/input.json\n  output: ./schema/output.json\n"),
    )
    (agent_dir / "schema").mkdir()
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "potato"})
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            }
        )
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


# ---------------------------------------------------------------------------
# Layered defaults (v0.6+) — policy.yaml: defaults: fills gaps in agent.yaml
# ---------------------------------------------------------------------------


def test_loader_applies_project_defaults(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: a policy.yaml with project defaults causes load_agent
    to produce a spec with those defaults filled in for keys the agent
    didn't specify. Concrete: project sets temperature=0.0, agent
    omits it, resolved spec has temperature=0.0."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(
        "defaults:\n"
        "  model:\n"
        "    params:\n"
        "      temperature: 0.0\n"
        "      max_tokens: 1024\n"
        "  timeouts:\n"
        "    call_ms: 15000\n"
        "  budget:\n"
        "    max_cost_usd_per_run: 0.50\n"
    )
    agent_dir = _write_agent(
        tmp_path / "agent-needs-defaults",
        schema_block=("schema:\n  input:\n    message: string\n  output:\n    response: string\n"),
        name="needs-defaults",
    )
    bundle = load_agent(agent_dir)
    # Defaults filled in for params, timeouts, budget.
    assert bundle.spec.model.params["temperature"] == 0.0
    assert bundle.spec.model.params["max_tokens"] == 1024
    assert bundle.spec.timeouts.call_ms == 15000
    assert bundle.spec.budget.max_cost_usd_per_run == 0.50


def test_loader_agent_overrides_project_defaults(tmp_path: Path, monkeypatch) -> None:
    """Agent.yaml always wins per-key. Project default temperature=0.0
    is shadowed when the agent declares temperature=0.5."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(
        "defaults:\n  model:\n    params:\n      temperature: 0.0\n      max_tokens: 1024\n"
    )
    agent_dir = tmp_path / "agent-overrides"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: overrides-defaults\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "  params:\n"
        "    temperature: 0.5\n"  # explicit — wins over default 0.0
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    message: string\n"
        "  output:\n"
        "    response: string\n"
    )
    (agent_dir / "prompt.md").write_text("p\n\n{{ input.message }}")
    bundle = load_agent(agent_dir)
    # Agent's explicit value survives.
    assert bundle.spec.model.params["temperature"] == 0.5
    # Default that the agent DIDN'T override still fills.
    assert bundle.spec.model.params["max_tokens"] == 1024


def test_loader_explicit_empty_defaults_bypasses_policy(tmp_path: Path, monkeypatch) -> None:
    """Passing ``defaults=AgentDefaults()`` explicitly bypasses the
    project config — needed by tests and library callers that want
    a pristine agent.yaml."""
    from movate.core.config import AgentDefaults  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(
        "defaults:\n  model:\n    params:\n      temperature: 0.0\n"
    )
    agent_dir = _write_agent(
        tmp_path / "pristine",
        schema_block=("schema:\n  input:\n    message: string\n  output:\n    response: string\n"),
        name="pristine",
    )
    # Default arg → defaults applied:
    with_defaults = load_agent(agent_dir)
    assert with_defaults.spec.model.params.get("temperature") == 0.0
    # Explicit empty defaults → pristine:
    pristine = load_agent(agent_dir, defaults=AgentDefaults())
    assert "temperature" not in pristine.spec.model.params


def test_loader_no_policy_yaml_loads_pristine(tmp_path: Path, monkeypatch) -> None:
    """When there's no policy.yaml in cwd, load_agent's default-resolution
    is a no-op — agent.yaml loads exactly as-is."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(
        tmp_path / "nodefaults",
        schema_block=("schema:\n  input:\n    message: string\n  output:\n    response: string\n"),
        name="nodefaults",
    )
    bundle = load_agent(agent_dir)
    # Pydantic's built-in default for call_ms is 30000, not the 15000
    # an absent policy.yaml might be confused into supplying.
    assert bundle.spec.timeouts.call_ms == 30000
    # No project default → params is empty (Pydantic default).
    assert bundle.spec.model.params == {}
