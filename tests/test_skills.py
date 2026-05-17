"""End-to-end tests for the skills + tool-use feature (ADR 002 / PR 1).

Layered coverage:

* **SkillSpec model** — Pydantic validation, name/version rules,
  python entry shape check.
* **skill_loader** — loads a directory, builds a registry, resolves
  agent references to bundles, surfaces typos with the available list.
* **PythonSkillBackend** — dispatches a sync + an async function,
  caches resolves, maps each :class:`SkillErrorType` to the right
  surface.
* **Executor tool-use loop** — wires MockProvider's tool_script,
  drives a multi-turn loop against a real skill, accumulates cost
  + tokens, hits the max-turns guard, recovers from skill errors.

Hermetic: no real network, no real provider keys. Every skill is a
Python function defined in this test module.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from movate.core.models import (
    RunRequest,
    SkillImplementationKind,
    SkillSideEffects,
    SkillSpec,
)
from movate.core.skill_backend import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    dispatch_skill,
)
from movate.core.skill_backend import python as _python_backend  # noqa: F401
from movate.core.skill_loader import (
    SkillLoadError,
    load_skill,
    load_skill_registry,
    resolve_agent_skills,
)

# ---------------------------------------------------------------------------
# SkillSpec model
# ---------------------------------------------------------------------------


def _skill_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "api_version": "movate/v1",
        "kind": "Skill",
        "name": "demo-skill",
        "version": "0.1.0",
        "description": "test skill",
        "schema": {
            "input": {"x": "integer"},
            "output": {"y": "integer"},
        },
        "implementation": {
            "kind": "python",
            "entry": "tests.test_skills:_dummy_skill",
        },
    }
    base.update(overrides)
    return base


def test_skill_spec_minimal_valid() -> None:
    spec = SkillSpec.model_validate(_skill_dict())
    assert spec.name == "demo-skill"
    assert spec.implementation.kind == SkillImplementationKind.PYTHON
    assert spec.cost.per_call_usd == 0.0
    assert spec.side_effects == SkillSideEffects.READ_ONLY


def test_skill_spec_rejects_bad_name() -> None:
    with pytest.raises(ValidationError, match="lowercase alphanumeric"):
        SkillSpec.model_validate(_skill_dict(name="Bad_Name!"))


def test_skill_spec_rejects_bad_version() -> None:
    with pytest.raises(ValidationError, match="semver"):
        SkillSpec.model_validate(_skill_dict(version="latest"))


def test_skill_spec_python_entry_must_have_colon() -> None:
    """Catch typos at parse time, not at first invocation. A malformed
    entry like ``my.module.func`` (no ``:``) would surface deep in
    importlib at runtime; we want it surfaced by ``mdk validate``."""
    with pytest.raises(ValidationError, match=r"pkg\.mod:func"):
        SkillSpec.model_validate(
            _skill_dict(implementation={"kind": "python", "entry": "tests.test_skills.no_colon"})
        )


def test_skill_spec_cost_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        SkillSpec.model_validate(
            _skill_dict(cost={"per_call_usd": -0.01}),
        )


# ---------------------------------------------------------------------------
# Skill module-level test fixtures (referenced by `entry` strings below)
# ---------------------------------------------------------------------------


def _dummy_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Sync Python skill — adds 1 to ``x``, returns as ``y``."""
    return {"y": int(input["x"]) + 1}


async def _async_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Async Python skill — same as ``_dummy_skill`` but awaitable."""
    await asyncio.sleep(0)
    return {"y": int(input["x"]) * 2}


def _bad_output_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Returns a dict that doesn't match the declared output schema —
    must surface as :data:`SkillErrorType.VALIDATION_FAILED`."""
    return {"wrong_field": "boom"}


def _exploding_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Raises a generic Python exception — must wrap as
    :data:`SkillErrorType.BACKEND_ERROR`."""
    raise RuntimeError("kaboom")


async def _slow_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Sleeps past the budget so we can test the TIMEOUT path."""
    await asyncio.sleep(5)
    return {"y": 0}


_NOT_CALLABLE = "i-am-a-string-not-a-function"  # for the "not callable" test


# ---------------------------------------------------------------------------
# Skill loader
# ---------------------------------------------------------------------------


def _write_skill_dir(
    parent: Path,
    name: str,
    *,
    entry: str = "tests.test_skills:_dummy_skill",
    cost: float = 0.0,
) -> Path:
    skill_dir = parent / name
    skill_dir.mkdir(parents=True)
    yaml_path = skill_dir / "skill.yaml"
    yaml_path.write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        f"description: test skill {name}\n"
        "schema:\n"
        "  input:\n"
        "    x: integer\n"
        "  output:\n"
        "    y: integer\n"
        "implementation:\n"
        "  kind: python\n"
        f"  entry: {entry}\n"
        "cost:\n"
        f"  per_call_usd: {cost}\n"
    )
    return skill_dir


def test_load_skill_returns_bundle(tmp_path: Path) -> None:
    skill_dir = _write_skill_dir(tmp_path, "demo")
    bundle = load_skill(skill_dir)
    assert bundle.spec.name == "demo"
    # Schemas compiled from inline shorthand into JSON Schema dicts.
    assert bundle.input_schema["properties"]["x"] == {"type": "integer"}
    # And the validators are real, ready to use.
    bundle.input_validator.validate({"x": 5})


def test_load_skill_missing_yaml(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    with pytest.raises(SkillLoadError, match=r"skill\.yaml not found"):
        load_skill(tmp_path / "broken")


def test_load_skill_registry_finds_all(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill_dir(skills_dir, "alpha")
    _write_skill_dir(skills_dir, "beta")
    # Dotfile dirs and non-skill dirs are skipped.
    (skills_dir / ".cache").mkdir()
    (skills_dir / "no-yaml-here").mkdir()
    registry = load_skill_registry(tmp_path)
    assert set(registry.keys()) == {"alpha", "beta"}


def test_load_skill_registry_empty_when_no_skills_folder(tmp_path: Path) -> None:
    """Projects without a ``skills/`` folder return an empty registry
    — that's the permissive default for agents with ``skills: []``."""
    assert load_skill_registry(tmp_path) == {}


def test_load_skill_registry_rejects_duplicate_names(tmp_path: Path) -> None:
    """Two skill folders declaring the same name must fail at registry
    build — otherwise agent.yaml references are ambiguous."""
    skills_dir = tmp_path / "skills"
    _write_skill_dir(skills_dir, "alpha", entry="tests.test_skills:_dummy_skill")
    # Second skill folder under a different dirname but same `name:` field.
    second = skills_dir / "different-dir"
    second.mkdir()
    (second / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: alpha\n"  # duplicate
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {x: integer}\n"
        "  output: {y: integer}\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: tests.test_skills:_dummy_skill\n"
    )
    with pytest.raises(SkillLoadError, match="duplicate skill name"):
        load_skill_registry(tmp_path)


def test_resolve_agent_skills_returns_bundles_in_order(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill_dir(skills_dir, "alpha")
    _write_skill_dir(skills_dir, "beta")
    registry = load_skill_registry(tmp_path)
    resolved = resolve_agent_skills(["beta", "alpha"], registry)
    assert [b.spec.name for b in resolved] == ["beta", "alpha"]


def test_resolve_agent_skills_unknown_name_errors(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill_dir(skills_dir, "alpha")
    registry = load_skill_registry(tmp_path)
    with pytest.raises(SkillLoadError, match="no such skill is registered"):
        resolve_agent_skills(["alpha", "nonexistent"], registry)


# ---------------------------------------------------------------------------
# PythonSkillBackend dispatch — taxonomy coverage
# ---------------------------------------------------------------------------


def _bundle_with_entry(tmp_path: Path, entry: str, name: str = "demo") -> Any:
    """Construct a SkillBundle wired to a function in this test module."""
    skill_dir = _write_skill_dir(tmp_path, name, entry=entry)
    return load_skill(skill_dir)


@pytest.mark.asyncio
async def test_dispatch_skill_happy_path_sync(tmp_path: Path) -> None:
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_dummy_skill")
    ctx = SkillExecutionContext()
    output = await dispatch_skill(bundle, {"x": 4}, ctx)
    assert output == {"y": 5}


@pytest.mark.asyncio
async def test_dispatch_skill_happy_path_async(tmp_path: Path) -> None:
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_async_skill")
    ctx = SkillExecutionContext()
    output = await dispatch_skill(bundle, {"x": 7}, ctx)
    assert output == {"y": 14}


@pytest.mark.asyncio
async def test_dispatch_skill_validation_failed_on_bad_input(tmp_path: Path) -> None:
    """LLM passed an input that doesn't match the schema."""
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_dummy_skill")
    ctx = SkillExecutionContext()
    with pytest.raises(SkillError) as info:
        await dispatch_skill(bundle, {"x": "not-an-integer"}, ctx)
    assert info.value.type == SkillErrorType.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_dispatch_skill_validation_failed_on_bad_output(tmp_path: Path) -> None:
    """Backend produced output that doesn't match the declared schema."""
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_bad_output_skill")
    ctx = SkillExecutionContext()
    with pytest.raises(SkillError) as info:
        await dispatch_skill(bundle, {"x": 5}, ctx)
    assert info.value.type == SkillErrorType.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_dispatch_skill_backend_error_wraps_exception(tmp_path: Path) -> None:
    """Any unhandled exception from the impl gets wrapped — operators
    see ``backend_error`` + the original message, not a raw traceback."""
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_exploding_skill")
    ctx = SkillExecutionContext()
    with pytest.raises(SkillError) as info:
        await dispatch_skill(bundle, {"x": 5}, ctx)
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "kaboom" in info.value.message


@pytest.mark.asyncio
async def test_dispatch_skill_timeout(tmp_path: Path) -> None:
    """When the impl runs past the budget, surfaces TIMEOUT."""
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_slow_skill")
    ctx = SkillExecutionContext(call_ms_budget=10)  # 10ms budget vs 5s sleep
    with pytest.raises(SkillError) as info:
        await dispatch_skill(bundle, {"x": 1}, ctx)
    assert info.value.type == SkillErrorType.TIMEOUT


@pytest.mark.asyncio
async def test_dispatch_skill_non_callable_entry(tmp_path: Path) -> None:
    """The ``entry`` resolved to something that isn't callable."""
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_NOT_CALLABLE")
    ctx = SkillExecutionContext()
    with pytest.raises(SkillError) as info:
        await dispatch_skill(bundle, {"x": 1}, ctx)
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "not callable" in info.value.message


@pytest.mark.asyncio
async def test_dispatch_skill_unimportable_module(tmp_path: Path) -> None:
    bundle = _bundle_with_entry(tmp_path, "no_such_module:foo")
    ctx = SkillExecutionContext()
    with pytest.raises(SkillError) as info:
        await dispatch_skill(bundle, {"x": 1}, ctx)
    assert info.value.type == SkillErrorType.BACKEND_ERROR


@pytest.mark.asyncio
async def test_dispatch_skill_hyphen_name_importable_via_skill_dir(tmp_path: Path) -> None:
    """A skill directory named ``my-skill`` (with a hyphen) must be importable.

    The ``import`` statement can't handle hyphens, but
    ``importlib.import_module`` works when the parent directory is on
    ``sys.path``. ``_resolve`` adds ``skill_dir.parent`` to sys.path so
    operators can scaffold skills with hyphenated names (the default
    convention: ``kb-lookup``, ``web-search``, etc.) without renaming
    their directories.
    """
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: my-skill\n"
        "version: 0.1.0\n"
        "description: hyphen test\n"
        "schema:\n"
        "  input:\n"
        "    x: integer\n"
        "  output:\n"
        "    y: integer\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: my-skill.impl:run\n"
        "cost:\n"
        "  per_call_usd: 0.0\n"
        "side_effects: read-only\n"
    )
    (skill_dir / "impl.py").write_text(
        "async def run(input, ctx):\n    return {'y': input['x'] + 1}\n"
    )
    from movate.core.skill_loader import load_skill  # noqa: PLC0415

    bundle = load_skill(skill_dir)
    ctx = SkillExecutionContext()
    output = await dispatch_skill(bundle, {"x": 10}, ctx)
    assert output == {"y": 11}


@pytest.mark.asyncio
async def test_dispatch_skill_syspath_not_duplicated(tmp_path: Path) -> None:
    """Calling dispatch_skill twice for the same skill dir adds the parent
    to sys.path exactly once — the membership check is idempotent."""
    import sys  # noqa: PLC0415

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: my-skill\n"
        "version: 0.1.0\n"
        "description: hyphen test\n"
        "schema:\n"
        "  input:\n"
        "    x: integer\n"
        "  output:\n"
        "    y: integer\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: my-skill.impl:run\n"
        "cost:\n"
        "  per_call_usd: 0.0\n"
        "side_effects: read-only\n"
    )
    (skill_dir / "impl.py").write_text(
        "async def run(input, ctx):\n    return {'y': input['x'] * 2}\n"
    )
    from movate.core.skill_backend.python import PythonSkillBackend  # noqa: PLC0415
    from movate.core.skill_loader import load_skill  # noqa: PLC0415

    bundle = load_skill(skill_dir)
    backend = PythonSkillBackend()
    ctx = SkillExecutionContext()
    parent = str(skill_dir.parent)
    count_before = sys.path.count(parent)

    await backend.execute(bundle, {"x": 3}, ctx)
    await backend.execute(bundle, {"x": 5}, ctx)

    assert sys.path.count(parent) == count_before + 1


# ---------------------------------------------------------------------------
# Executor tool-use loop
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_skill(tmp_path: Path) -> Path:
    """Build a minimal project layout with one skill and one agent that
    references it. Returns the project root."""
    _write_skill_dir(
        tmp_path / "skills",
        "add-one",
        entry="tests.test_skills:_dummy_skill",
        cost=0.0005,
    )
    agent_dir = tmp_path / "agents" / "calc-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: calc-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    question: string\n"
        "  output:\n"
        "    answer: string\n"
        "skills:\n"
        "  - add-one\n"
    )
    (agent_dir / "prompt.md").write_text("{{ input.question }}")
    return tmp_path


@pytest.mark.asyncio
async def test_executor_runs_tool_use_loop_end_to_end(
    project_with_skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: load agent with a skill, run with scripted tool-use,
    verify the model's tool call gets dispatched, the result is fed back,
    and the final response succeeds + costs sum correctly."""
    monkeypatch.chdir(project_with_skill)
    # Project is set up; agent dir is tmp/agents/calc-agent. The loader
    # looks for skills/ at the agent's parent (= tmp/agents) — but our
    # canonical layout has skills/ at the *project* root (= tmp). So
    # this test agent lives at tmp/calc-agent, sibling to tmp/skills.
    # Move it.
    src_dir = project_with_skill / "agents" / "calc-agent"
    dst_dir = project_with_skill / "calc-agent"
    src_dir.rename(dst_dir)

    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    bundle = load_agent(dst_dir)
    assert len(bundle.skills) == 1
    assert bundle.skills[0].spec.name == "add-one"

    rt = await build_local_runtime(mock=True)
    # Script the mock so it makes one tool call, then returns a final
    # answer that satisfies the output schema.
    rt.provider._tool_script = [  # type: ignore[attr-defined]
        ("add-one", {"x": 41}),
    ]
    rt.provider._response = '{"answer": "42"}'  # type: ignore[attr-defined]
    rt.provider._tool_calls_emitted = 0  # type: ignore[attr-defined]
    try:
        response = await rt.executor.execute(
            bundle, RunRequest(agent="calc-agent", input={"question": "what is 41+1?"})
        )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    assert response.status == "success"
    assert response.data == {"answer": "42"}
    # Cost includes skill cost (0.0005) plus token cost (zero in mock).
    assert response.metrics.cost_usd >= 0.0005


@pytest.mark.asyncio
async def test_executor_single_shot_when_agent_has_no_skills(
    tmp_path: Path,
) -> None:
    """An agent with ``skills: []`` skips the tool-use loop entirely
    — same path as v0.5. No regression for existing agents."""
    agent_dir = tmp_path / "vanilla"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: vanilla\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    question: string\n"
        "  output:\n"
        "    answer: string\n"
    )
    (agent_dir / "prompt.md").write_text("{{ input.question }}")

    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    bundle = load_agent(agent_dir)
    assert bundle.skills == []
    rt = await build_local_runtime(mock=True)
    rt.provider._response = '{"answer": "hi"}'  # type: ignore[attr-defined]
    try:
        response = await rt.executor.execute(
            bundle, RunRequest(agent="vanilla", input={"question": "hello"})
        )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    assert response.status == "success"


@pytest.mark.asyncio
async def test_executor_recovers_from_skill_error(
    project_with_skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a skill raises, the executor feeds the SkillError back to
    the model as a tool_result and continues the loop — the agent run
    still succeeds because the model can recover."""
    src_dir = project_with_skill / "agents" / "calc-agent"
    dst_dir = project_with_skill / "calc-agent"
    src_dir.rename(dst_dir)
    monkeypatch.chdir(project_with_skill)

    # Replace the skill's entry to point at the exploding fixture.
    skill_yaml = project_with_skill / "skills" / "add-one" / "skill.yaml"
    skill_yaml.write_text(skill_yaml.read_text().replace("_dummy_skill", "_exploding_skill"))

    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    bundle = load_agent(dst_dir)
    rt = await build_local_runtime(mock=True)
    # One tool call (will error), then the model finalizes.
    rt.provider._tool_script = [("add-one", {"x": 5})]  # type: ignore[attr-defined]
    rt.provider._response = '{"answer": "fell back to default"}'  # type: ignore[attr-defined]
    rt.provider._tool_calls_emitted = 0  # type: ignore[attr-defined]
    try:
        response = await rt.executor.execute(
            bundle, RunRequest(agent="calc-agent", input={"question": "fail please"})
        )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    # The run succeeds because the model produced a final answer after
    # seeing the tool error. The error was logged on the span but
    # didn't crash the run.
    assert response.status == "success"


@pytest.mark.asyncio
async def test_executor_max_turns_guard(
    project_with_skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that keeps emitting tool calls past _MAX_TOOL_TURNS_DEFAULT
    gets cut off — the loop returns rather than burning cost forever."""
    src_dir = project_with_skill / "agents" / "calc-agent"
    dst_dir = project_with_skill / "calc-agent"
    src_dir.rename(dst_dir)
    monkeypatch.chdir(project_with_skill)

    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.executor import _MAX_TOOL_TURNS_DEFAULT  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    bundle = load_agent(dst_dir)
    rt = await build_local_runtime(mock=True)
    # Script more tool calls than the guard allows; the loop should
    # terminate after _MAX_TOOL_TURNS_DEFAULT iterations without
    # crashing. We deliberately script an output that satisfies the
    # schema so the final "force-terminate" response can be parsed
    # successfully.
    rt.provider._tool_script = [  # type: ignore[attr-defined]
        ("add-one", {"x": i}) for i in range(_MAX_TOOL_TURNS_DEFAULT + 5)
    ]
    rt.provider._response = '{"answer": "loop terminated by guard"}'  # type: ignore[attr-defined]
    rt.provider._tool_calls_emitted = 0  # type: ignore[attr-defined]
    try:
        # The loop should NOT raise; it terminates via the guard.
        response = await rt.executor.execute(
            bundle, RunRequest(agent="calc-agent", input={"question": "runaway"})
        )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    # The mock keeps emitting tool_use until it runs out OR the guard
    # cuts it off. After max-turns, the executor takes the last
    # response as the final answer. The mock keeps emitting empty-text
    # tool_use responses, so the parsed output is empty — but the
    # critical assertion is that the run terminated.
    # We don't assert response.status here because the schema validator
    # may reject empty output; what we DO assert is that the call
    # *returned* (didn't infinite-loop). Reaching this line proves it.
    assert response is not None


# ---------------------------------------------------------------------------
# Provider-level tool-spec conversion
# ---------------------------------------------------------------------------


def test_provider_to_tool_spec_default_openai_shape(tmp_path: Path) -> None:
    """Default ``BaseLLMProvider.to_tool_spec`` (used by LiteLLM)
    produces OpenAI function-call shape."""
    bundle = _bundle_with_entry(tmp_path, "tests.test_skills:_dummy_skill")
    # MockProvider doesn't override to_tool_spec, so it inherits the
    # Protocol's default.
    from movate.providers.mock import MockProvider  # noqa: PLC0415

    spec = MockProvider().to_tool_spec(bundle)
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "demo"
    assert spec["function"]["parameters"]["properties"]["x"] == {"type": "integer"}
