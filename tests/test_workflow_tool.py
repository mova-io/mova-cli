"""First-class ``tool`` node (ADR 097) — unit + integration tests.

Covers:
 1. The pure mapping helpers (``core.workflow.tool``) — explicit input map
    (dotted paths, literals, missing-path omission, exclusivity), the default
    input-schema projection, and the raw-merge / ``output_key`` delta.
 2. Spec parsing — ``ToolNodeSpec`` accepts good nodes and rejects malformed
    input maps + extra keys at parse time.
 3. ``compile_workflow`` — registry-name resolution at COMPILE time
    (workflow-local first, then project root; fail-loud on a miss), metadata
    stamping (skill / side_effects / capabilities / timeout / map / key /
    source), and ``validate_linear`` admitting ``NodeType.TOOL``.
 4. Native runner — projection default, explicit map (exclusive), raw merge,
    ``output_key`` namespacing, NO RunRecord, skill failure → workflow ERROR
    at the node, SKILL policy deny → ERROR (ADR 097 D4/D5/D7).
 5. Temporal — ``_emit_skill_node`` passes the mapping as defaulted trailing
    args, a pure-tool workflow schedules no LLM activity, and the extended
    ``call_skill_activity`` (mapping activity-side via the SAME helpers,
    ``timeout_call_ms`` honored, SKILL gate enforced) routes/merges
    identically to native (parity) while the 4-arg call stays byte-identical.
 6. ``mdk validate`` lints — the state-threading tool arm and the
    runtime:temporal project-level-skill deploy warning (ADR 097 D2/D6).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

import movate.core.workflow.temporal_activities as ta
from movate.cli.main import app as cli_app
from movate.core.config import SkillPolicy
from movate.core.executor import Executor
from movate.core.failures import PolicyViolationError
from movate.core.models import SkillSideEffects, WorkflowStatus
from movate.core.skill_backend.base import SkillExecutionContext
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.ir import NodeType
from movate.core.workflow.spec import ToolNodeSpec
from movate.core.workflow.tool import build_skill_input, merge_tool_output
from movate.testing import InMemoryStorage, NullTracer

cli_runner = CliRunner(mix_stderr=False)

# ---------------------------------------------------------------------------
# Skill entrypoints (kind: python, entry: tests.test_workflow_tool:<fn>)
# ---------------------------------------------------------------------------


def _lookup_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Deterministic lookup: echoes its (validated) input back for assertions."""
    return {"order_status": "shipped", "echo": dict(input)}


def _boom_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_SKILL_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["order_id"],
    "properties": {
        "order_id": {"type": "string"},
        "include_history": {"type": "boolean"},
    },
}
_SKILL_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["order_status", "echo"],
    "properties": {"order_status": {"type": "string"}, "echo": {"type": "object"}},
}

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {"order_id": {"type": "string"}, "order": {"type": "object"}},
}


def _make_skill(
    parent: Path,
    *,
    name: str = "order-lookup",
    entry: str = "tests.test_workflow_tool:_lookup_skill",
    side_effects: str = "read-only",
    timeout_call_ms: int | None = None,
    deterministic: bool | None = None,
) -> Path:
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    spec: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Skill",
        "name": name,
        "version": "0.1.0",
        "schema": {"input": "./input.json", "output": "./output.json"},
        "implementation": {"kind": "python", "entry": entry},
        "side_effects": side_effects,
    }
    if timeout_call_ms is not None:
        spec["timeout_call_ms"] = timeout_call_ms
    if deterministic is not None:
        spec["capabilities"] = {"deterministic": deterministic}
    (skill_dir / "skill.yaml").write_text(yaml.safe_dump(spec))
    (skill_dir / "input.json").write_text(json.dumps(_SKILL_INPUT_SCHEMA))
    (skill_dir / "output.json").write_text(json.dumps(_SKILL_OUTPUT_SCHEMA))
    return skill_dir


def _make_tool_workflow(
    wf_dir: Path,
    *,
    skill: str = "order-lookup",
    input_map: dict[str, Any] | None = None,
    output_key: str | None = None,
    runtime: str | None = None,
    workflow_local_skill: bool = True,
    state_schema: dict[str, Any] | None = None,
    skill_kwargs: dict[str, Any] | None = None,
) -> Path:
    """One-node tool workflow. ``workflow_local_skill=True`` drops the skill at
    ``<wf>/skills/<name>/`` (tier 1); ``False`` leaves resolution to the
    project registry (the caller builds it)."""
    wf_dir.mkdir(parents=True, exist_ok=True)
    if workflow_local_skill:
        _make_skill(wf_dir / "skills", name=skill, **(skill_kwargs or {}))
    (wf_dir / "state.json").write_text(json.dumps(state_schema or _STATE_SCHEMA))
    node: dict[str, Any] = {"id": "fetch", "type": "tool", "skill": skill}
    if input_map is not None:
        node["input"] = input_map
    if output_key is not None:
        node["output_key"] = output_key
    doc: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "test-tool",
        "version": "0.1.0",
        "state_schema": "./state.json",
        "entrypoint": "fetch",
        "nodes": [node],
        "edges": [],
    }
    if runtime is not None:
        doc["runtime"] = runtime
    (wf_dir / "workflow.yaml").write_text(yaml.safe_dump(doc))
    return wf_dir / "workflow.yaml"


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture(autouse=True)
def _reset_activity_context() -> Any:
    ta._CONTEXT = None
    yield
    ta._CONTEXT = None


def _mock_executor() -> MagicMock:
    """A runner-facing executor double: real tracer calls recorded, the SKILL
    gate a permissive no-op (overridden per-test for the deny case)."""
    ex = MagicMock(spec=Executor)
    ex.govern_skill_dispatch = MagicMock(return_value=None)
    return ex


# ---------------------------------------------------------------------------
# 1. Pure helpers (core/workflow/tool.py — ADR 097 D3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_skill_input_projection_default() -> None:
    state = {"order_id": "o-1", "noise": 1}
    props = _SKILL_INPUT_SCHEMA["properties"]
    assert build_skill_input(state, None, props) == {"order_id": "o-1"}


@pytest.mark.unit
def test_build_skill_input_no_properties_passes_whole_state() -> None:
    state = {"a": 1, "b": 2}
    assert build_skill_input(state, None, None) == state
    assert build_skill_input(state, None, {}) == state
    # And it's a copy, not the same dict.
    assert build_skill_input(state, None, None) is not state


@pytest.mark.unit
def test_build_skill_input_explicit_map_is_exclusive() -> None:
    # Top-level order_id present, but the map points at the nested path —
    # only the mapped keys are sent (no implicit projection underneath).
    state = {"order_id": "top", "order": {"id": "nested"}}
    input_map = {"order_id": "order.id", "include_history": {"literal": True}}
    assert build_skill_input(state, input_map, _SKILL_INPUT_SCHEMA["properties"]) == {
        "order_id": "nested",
        "include_history": True,
    }


@pytest.mark.unit
def test_build_skill_input_missing_path_is_omitted() -> None:
    # A mapped path absent from state is omitted (the skill's `required`
    # schema then fails the dispatch loudly — ADR 097 D1).
    assert build_skill_input({}, {"order_id": "absent.path"}, None) == {}
    # A present-but-None value is NOT omitted (missing ≠ stored None).
    assert build_skill_input({"x": None}, {"order_id": "x"}, None) == {"order_id": None}


@pytest.mark.unit
def test_merge_tool_output_raw_and_namespaced() -> None:
    out = {"order_status": "shipped"}
    assert merge_tool_output(out, None) == out
    assert merge_tool_output(out, None) is not out  # a copy
    assert merge_tool_output(out, "lookup") == {"lookup": {"order_status": "shipped"}}


# ---------------------------------------------------------------------------
# 2. Spec validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_spec_accepts_valid_tool_node() -> None:
    node = ToolNodeSpec.model_validate(
        {
            "id": "fetch",
            "type": "tool",
            "skill": "order-lookup",
            "input": {"order_id": "order.id", "include_history": {"literal": True}},
            "output_key": "lookup",
        }
    )
    assert node.type == "tool"
    assert node.skill == "order-lookup"
    # Minimal form: just the skill name.
    minimal = ToolNodeSpec.model_validate({"id": "fetch", "type": "tool", "skill": "x-y"})
    assert minimal.input is None
    assert minimal.output_key is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_input",
    [
        {"order_id": 42},  # neither path string nor literal dict
        {"order_id": {"litteral": True}},  # misspelled wrapper key
        {"order_id": {"literal": True, "extra": 1}},  # extra key in wrapper
        {"order_id": "  "},  # empty dotted path
    ],
)
def test_spec_rejects_malformed_input_map(bad_input: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ToolNodeSpec.model_validate(
            {"id": "fetch", "type": "tool", "skill": "order-lookup", "input": bad_input}
        )


@pytest.mark.unit
def test_spec_rejects_extra_keys_and_missing_skill() -> None:
    with pytest.raises(ValidationError):
        ToolNodeSpec.model_validate({"id": "fetch", "type": "tool", "skill": "x", "ref": "./oops"})
    with pytest.raises(ValidationError):
        ToolNodeSpec.model_validate({"id": "fetch", "type": "tool"})


# ---------------------------------------------------------------------------
# 3. compile_workflow — resolution + metadata + validate_linear
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_resolves_workflow_local_skill_and_stamps_metadata(tmp_path: Path) -> None:
    yaml_path = _make_tool_workflow(
        tmp_path / "wf",
        input_map={"order_id": "order.id", "include_history": {"literal": True}},
        output_key="lookup",
        skill_kwargs={"timeout_call_ms": 1234, "deterministic": True},
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    node = graph.nodes["fetch"]
    assert node.type is NodeType.TOOL
    # ref = the absolute workflow-local skill dir (the activity contract).
    assert node.ref == str((parent / "skills" / "order-lookup").resolve())
    meta = node.metadata
    assert meta["skill"] == "order-lookup"
    assert meta["side_effects"] == "read-only"
    assert meta["capabilities"]["deterministic"] is True
    assert meta["timeout_call_ms"] == 1234
    assert meta["input_map"] == {"order_id": "order.id", "include_history": {"literal": True}}
    assert meta["output_key"] == "lookup"
    assert meta["skill_source"] == "workflow-local"
    # The linear phase gate admits NodeType.TOOL (ADR 097).
    validate_linear(graph)


@pytest.mark.unit
def test_compile_resolves_project_registry_skill(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.yaml").write_text("{}\n")  # marker for the walk-up
    _make_skill(proj / "skills")
    yaml_path = _make_tool_workflow(proj / "workflows" / "wf", workflow_local_skill=False)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    node = graph.nodes["fetch"]
    assert node.ref == str((proj / "skills" / "order-lookup").resolve())
    assert node.metadata["skill_source"] == "project"


@pytest.mark.unit
def test_compile_workflow_local_wins_over_project(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.yaml").write_text("{}\n")
    _make_skill(proj / "skills")  # project tier
    yaml_path = _make_tool_workflow(proj / "workflows" / "wf")  # + workflow-local tier
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    assert graph.nodes["fetch"].ref == str((parent / "skills" / "order-lookup").resolve())
    assert graph.nodes["fetch"].metadata["skill_source"] == "workflow-local"


@pytest.mark.unit
def test_compile_fails_loud_on_unknown_skill(tmp_path: Path) -> None:
    yaml_path = _make_tool_workflow(
        tmp_path / "wf", skill="no-such-skill", workflow_local_skill=False
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="skill 'no-such-skill' not found"):
        compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# 4. Native runner (ADR 097 D3/D4/D5/D7)
# ---------------------------------------------------------------------------


def _graph(yaml_path: Path) -> Any:
    spec, parent = load_workflow_spec(yaml_path)
    return compile_workflow(spec, parent)


@pytest.mark.unit
async def test_native_projection_default_and_raw_merge(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    graph = _graph(_make_tool_workflow(tmp_path / "wf"))
    runner = WorkflowRunner(executor=_mock_executor(), storage=storage)
    result = await runner.run(graph, initial_state={"order_id": "o-1", "noise": 1})

    assert result.status is WorkflowStatus.SUCCESS
    # Projection: the skill saw ONLY its input-schema keys, not `noise`.
    assert result.final_state["echo"] == {"order_id": "o-1"}
    # Raw merge (default): output keys land at top level; prior state retained.
    assert result.final_state["order_status"] == "shipped"
    assert result.final_state["noise"] == 1
    # No model ran ⇒ NO RunRecord (ADR 097 D7).
    assert result.runs == []


@pytest.mark.unit
async def test_native_input_map_is_exclusive_and_output_key_namespaces(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    graph = _graph(
        _make_tool_workflow(
            tmp_path / "wf",
            input_map={"order_id": "order.id", "include_history": {"literal": True}},
            output_key="lookup",
        )
    )
    runner = WorkflowRunner(executor=_mock_executor(), storage=storage)
    result = await runner.run(graph, initial_state={"order_id": "top", "order": {"id": "nested"}})

    assert result.status is WorkflowStatus.SUCCESS
    # Exclusive map: dotted path + literal, no projection underneath.
    assert result.final_state["lookup"]["echo"] == {
        "order_id": "nested",
        "include_history": True,
    }
    # output_key: the WHOLE output dict namespaced under one key — nothing
    # merged at top level, the original keys untouched.
    assert result.final_state["order_id"] == "top"
    assert "order_status" not in result.final_state


@pytest.mark.unit
async def test_native_skill_failure_fails_workflow_at_node(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    graph = _graph(
        _make_tool_workflow(
            tmp_path / "wf",
            skill_kwargs={"entry": "tests.test_workflow_tool:_boom_skill"},
        )
    )
    runner = WorkflowRunner(executor=_mock_executor(), storage=storage)
    result = await runner.run(graph, initial_state={"order_id": "o-1"})

    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "fetch"
    assert result.error is not None
    assert result.error.type == "backend_error"
    assert "boom" in result.error.message
    assert result.runs == []  # no synthetic RunRecord on failure either
    # The terminal WorkflowRunRecord is persisted with the failing node.
    rec = await storage.get_workflow_run(result.workflow_run_id, tenant_id="local")
    assert rec is not None
    assert rec.status is WorkflowStatus.ERROR
    assert rec.error_node_id == "fetch"


@pytest.mark.unit
async def test_native_missing_required_input_fails_validation(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    # Mapped path missing from state ⇒ key omitted ⇒ the skill's own
    # `required` contract fails the call loudly (ADR 097 D1).
    graph = _graph(_make_tool_workflow(tmp_path / "wf", input_map={"order_id": "absent.path"}))
    runner = WorkflowRunner(executor=_mock_executor(), storage=storage)
    result = await runner.run(graph, initial_state={})
    assert result.status is WorkflowStatus.ERROR
    assert result.error is not None
    assert result.error.type == "validation_failed"
    assert "order-lookup" in result.error.message


@pytest.mark.unit
async def test_native_policy_deny_fails_node(tmp_path: Path, storage: InMemoryStorage) -> None:
    graph = _graph(_make_tool_workflow(tmp_path / "wf"))
    executor = _mock_executor()
    executor.govern_skill_dispatch = MagicMock(
        side_effect=PolicyViolationError("skill 'order-lookup' denied by policy")
    )
    runner = WorkflowRunner(executor=executor, storage=storage)
    result = await runner.run(graph, initial_state={"order_id": "o-1"})

    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "fetch"
    assert result.error is not None
    assert result.error.type == "policy_violation"
    # The gate was given the loaded skill's identity, pre-dispatch.
    kwargs = executor.govern_skill_dispatch.call_args.kwargs
    assert kwargs["skill_name"] == "order-lookup"
    assert kwargs["side_effects"] is SkillSideEffects.READ_ONLY


@pytest.mark.unit
async def test_native_opens_workflow_tool_span(tmp_path: Path, storage: InMemoryStorage) -> None:
    graph = _graph(_make_tool_workflow(tmp_path / "wf"))
    executor = _mock_executor()
    runner = WorkflowRunner(executor=executor, storage=storage)
    await runner.run(graph, initial_state={"order_id": "o-1"})
    span_names = [c.args[0] for c in executor.tracer.start_span.call_args_list]
    assert "workflow.tool" in span_names
    idx = span_names.index("workflow.tool")
    attrs = executor.tracer.start_span.call_args_list[idx].args[1]
    assert attrs["workflow.node_id"] == "fetch"
    assert attrs["tool.skill"] == "order-lookup"


@pytest.mark.unit
async def test_native_timeout_call_ms_threaded_into_dispatch(
    tmp_path: Path, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = _graph(_make_tool_workflow(tmp_path / "wf", skill_kwargs={"timeout_call_ms": 1234}))
    captured: dict[str, Any] = {}

    async def _fake_dispatch(skill: Any, input: dict[str, Any], ctx: Any) -> dict[str, Any]:
        captured["call_ms_budget"] = ctx.call_ms_budget
        captured["mock"] = ctx.mock
        return {"order_status": "shipped", "echo": dict(input)}

    monkeypatch.setattr("movate.core.skill_backend.base.dispatch_skill", _fake_dispatch)
    runner = WorkflowRunner(executor=_mock_executor(), storage=storage)
    result = await runner.run(graph, initial_state={"order_id": "o-1"}, mock=True)
    assert result.status is WorkflowStatus.SUCCESS
    assert captured["call_ms_budget"] == 1234
    assert captured["mock"] is True  # --mock flows into the SkillExecutionContext


# ---------------------------------------------------------------------------
# Executor SKILL gate (the shared ADR 097 D5 enforcement point)
# ---------------------------------------------------------------------------


def _real_executor(skill_policy: SkillPolicy | None = None) -> Executor:
    return Executor(
        provider=object(),  # type: ignore[arg-type] — never invoked by the gate
        pricing=object(),  # type: ignore[arg-type]
        storage=InMemoryStorage(),
        tracer=NullTracer(),
        skill_policy=skill_policy,
    )


@pytest.mark.unit
def test_govern_skill_dispatch_permissive_noop() -> None:
    _real_executor().govern_skill_dispatch(
        skill_name="order-lookup", side_effects=SkillSideEffects.MUTATES_STATE
    )  # no raise


@pytest.mark.unit
def test_govern_skill_dispatch_denies_disallowed_side_effects() -> None:
    ex = _real_executor(SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY]))
    # Allowed class passes…
    ex.govern_skill_dispatch(skill_name="safe", side_effects=SkillSideEffects.READ_ONLY)
    # …a disallowed class raises the same PolicyViolationError execute() does.
    with pytest.raises(PolicyViolationError, match="mutates-state"):
        ex.govern_skill_dispatch(skill_name="evil", side_effects=SkillSideEffects.MUTATES_STATE)


# ---------------------------------------------------------------------------
# 5. Temporal — emission golden bits, activity semantics, parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_temporal_emits_skill_activity_with_mapping_args(tmp_path: Path) -> None:
    graph = _graph(
        _make_tool_workflow(
            tmp_path / "wf",
            input_map={"order_id": "order.id", "include_history": {"literal": True}},
            output_key="lookup",
        )
    )
    result = TemporalCompiler().compile(graph)
    src = result.module_source
    ast.parse(src)  # parses as Python
    # The mapping rides the activity args as defaulted trailing positions.
    assert "call_skill_activity," in src
    assert "'order_id': 'order.id'" in src
    assert "{'literal': True}" in src
    assert ", 'lookup']," in src
    # A pure-tool workflow schedules NO LLM activity (the no-LLM win).
    assert set(result.activity_names) == {
        "call_skill_activity",
        "persist_workflow_result_activity",
    }


@pytest.mark.unit
def test_temporal_emits_none_defaults_without_mapping(tmp_path: Path) -> None:
    graph = _graph(_make_tool_workflow(tmp_path / "wf"))
    src = TemporalCompiler().compile(graph).module_source
    ast.parse(src)
    assert "state, run_id, None, None]," in src


@pytest.mark.unit
def test_temporal_determinism_lint_fires_via_stamped_capabilities(tmp_path: Path) -> None:
    # D6: capabilities stamped at compile time light up the EXISTING lint —
    # zero lint changes needed for a nondeterministic skill behind a tool node.
    graph = _graph(_make_tool_workflow(tmp_path / "wf", skill_kwargs={"deterministic": False}))
    issues = TemporalCompiler().lint(graph)
    assert any(i.node_id == "fetch" and "deterministic" in i.message for i in issues), [
        i.message for i in issues
    ]


def _configure_activities(skill_policy: SkillPolicy | None = None) -> None:
    ta.configure_activities(
        storage=InMemoryStorage(),
        pricing=object(),  # type: ignore[arg-type] — never read by the gate/dispatch
        tracer=NullTracer(),
        provider=object(),  # type: ignore[arg-type]
        skill_policy=skill_policy if skill_policy is not None else SkillPolicy(),
    )


@pytest.mark.unit
async def test_call_skill_activity_four_arg_backward_compat(tmp_path: Path) -> None:
    """The old 4-arg call keeps its exact semantics: input-schema projection
    in, RAW output dict (no delta wrapper) out."""
    _configure_activities()
    skill_dir = _make_skill(tmp_path / "skills")
    out = await ta.call_skill_activity(
        "fetch", str(skill_dir), {"order_id": "o-1", "noise": 1}, "run-1"
    )
    assert out == {"order_status": "shipped", "echo": {"order_id": "o-1"}}


@pytest.mark.unit
async def test_call_skill_activity_applies_map_and_output_key(tmp_path: Path) -> None:
    _configure_activities()
    skill_dir = _make_skill(tmp_path / "skills")
    delta = await ta.call_skill_activity(
        "fetch",
        str(skill_dir),
        {"order": {"id": "nested"}},
        "run-1",
        {"order_id": "order.id", "include_history": {"literal": True}},
        "lookup",
    )
    # The activity returns the state DELTA — the workflow's state.update()
    # then produces the same final state native does.
    assert delta == {
        "lookup": {
            "order_status": "shipped",
            "echo": {"order_id": "nested", "include_history": True},
        }
    }


@pytest.mark.unit
async def test_call_skill_activity_honors_timeout_call_ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Latent-gap fix: the skill's declared timeout reaches the dispatch."""
    _configure_activities()
    skill_dir = _make_skill(tmp_path / "skills", timeout_call_ms=1234)
    captured: dict[str, Any] = {}

    async def _fake_dispatch(skill: Any, input: dict[str, Any], ctx: Any) -> dict[str, Any]:
        captured["call_ms_budget"] = ctx.call_ms_budget
        return {"order_status": "shipped", "echo": dict(input)}

    monkeypatch.setattr("movate.core.skill_backend.base.dispatch_skill", _fake_dispatch)
    await ta.call_skill_activity("fetch", str(skill_dir), {"order_id": "x"}, "r")
    assert captured["call_ms_budget"] == 1234


@pytest.mark.unit
async def test_call_skill_activity_enforces_skill_policy(tmp_path: Path) -> None:
    """Latent-gap fix: the activity now clears the SKILL gate pre-dispatch."""
    _configure_activities(SkillPolicy(allowed_side_effects=[]))
    skill_dir = _make_skill(tmp_path / "skills")
    with pytest.raises(PolicyViolationError, match="order-lookup"):
        await ta.call_skill_activity("fetch", str(skill_dir), {"order_id": "x"}, "r")


@pytest.mark.unit
async def test_call_skill_activity_failure_raises_named_runtime_error(
    tmp_path: Path,
) -> None:
    _configure_activities()
    skill_dir = _make_skill(tmp_path / "skills", entry="tests.test_workflow_tool:_boom_skill")
    with pytest.raises(RuntimeError) as ei:
        await ta.call_skill_activity("fetch", str(skill_dir), {"order_id": "x"}, "r")
    msg = str(ei.value)
    # Node, skill, and error type are all attributable in workflow history.
    assert "'fetch'" in msg
    assert "order-lookup" in msg
    assert "backend_error" in msg


@pytest.mark.unit
async def test_parity_native_and_temporal_reach_same_state(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """Same workflow, same initial state ⇒ native walk and the Temporal
    activity (with the compiler-emitted args) produce the SAME final state —
    both funnel through dispatch_skill + the shared tool.py helpers."""
    yaml_path = _make_tool_workflow(
        tmp_path / "wf",
        input_map={"order_id": "order.id", "include_history": {"literal": True}},
        output_key="lookup",
    )
    graph = _graph(yaml_path)
    initial = {"order_id": "top", "order": {"id": "nested"}}

    # Native walk.
    runner = WorkflowRunner(executor=_mock_executor(), storage=storage)
    native = await runner.run(graph, initial_state=dict(initial))
    assert native.status is WorkflowStatus.SUCCESS

    # Temporal path: the generated workflow calls the activity with
    # [node_id, ref, state, run_id, input_map, output_key] and merges the
    # returned delta — replay that contract directly.
    _configure_activities()
    node = graph.nodes["fetch"]
    state = dict(initial)
    delta = await ta.call_skill_activity(
        "fetch",
        node.ref,
        state,
        "run-parity",
        node.metadata["input_map"],
        node.metadata["output_key"],
    )
    state.update(delta)

    assert state == native.final_state


# ---------------------------------------------------------------------------
# 6. `mdk validate` lints (ADR 097 D2/D6)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_flags_tool_state_threading_gap(tmp_path: Path) -> None:
    # The map reads `missing.thing` — nothing upstream produces `missing` and
    # it's not a declared initial-state input → advisory warning, exit 0.
    _make_tool_workflow(tmp_path / "wf", input_map={"order_id": "missing.thing"})
    result = cli_runner.invoke(cli_app, ["validate", str(tmp_path / "wf")])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "state-threading" in combined
    assert "missing" in combined


@pytest.mark.unit
def test_validate_silent_when_tool_threading_clean(tmp_path: Path) -> None:
    # Default projection: the skill requires `order_id`, which IS a declared
    # initial-state input → silent.
    _make_tool_workflow(tmp_path / "wf")
    result = cli_runner.invoke(cli_app, ["validate", str(tmp_path / "wf")])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "state-threading" not in combined


@pytest.mark.unit
def test_validate_warns_on_temporal_project_level_skill(tmp_path: Path) -> None:
    # runtime: temporal + a PROJECT-level skill ⇒ the worker image bake
    # (COPY workflows/) won't include it → deploy warning (ADR 097 D2).
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.yaml").write_text("{}\n")
    _make_skill(proj / "skills")
    _make_tool_workflow(proj / "workflows" / "wf", workflow_local_skill=False, runtime="temporal")
    result = cli_runner.invoke(cli_app, ["validate", str(proj / "workflows" / "wf")])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "project-level" in combined
    assert "order-lookup" in combined


@pytest.mark.unit
def test_validate_no_warning_for_workflow_local_temporal_skill(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.yaml").write_text("{}\n")
    _make_tool_workflow(proj / "workflows" / "wf", runtime="temporal")
    result = cli_runner.invoke(cli_app, ["validate", str(proj / "workflows" / "wf")])
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "project-level" not in combined


@pytest.mark.unit
def test_validate_static_skill_policy_gate_blocks_tool_node(tmp_path: Path) -> None:
    # ADR 097 D5 static layer: a project allowlist that excludes the skill's
    # side-effects class fails `mdk validate` (exit 2) — caught before merge,
    # mirroring the agent-bundle check.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "policy.yaml").write_text("skills:\n  allowed_side_effects: []\n")
    _make_tool_workflow(proj / "workflows" / "wf")
    result = cli_runner.invoke(cli_app, ["validate", str(proj / "workflows" / "wf")])
    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert "skill policy violation" in combined
    assert "order-lookup" in combined
