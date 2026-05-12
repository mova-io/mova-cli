"""Conditional edges — DSL parser + structural validator + end-to-end routing.

Three layers of coverage:

1. **DSL parser/evaluator** — operator-authored expressions parse and
   evaluate predictably. Each operator gets at least one positive +
   negative case; common error paths surface as ``ConditionParseError``
   at compile-time and ``ConditionEvalError`` at runtime.
2. **Structural validator** — ``validate_conditional`` enforces the
   "one default per source, default-last, no mixed kinds" rules that
   the compiler relies on to emit a clean routing function.
3. **End-to-end** — a 3-branch workflow routes correctly under
   ``runtime: langgraph`` for each branch (low score → review,
   high score → approve, anything else → default).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("langgraph")

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import (
    WorkflowCompileError,
    compile_workflow,
    load_workflow_spec,
    validate_conditional,
)
from movate.core.workflow.condition_dsl import (
    ConditionEvalError,
    ConditionParseError,
    parse_condition,
)
from movate.core.workflow.ir import EdgeKind
from movate.core.workflow.runner import WorkflowRunner
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# DSL — parser + evaluator
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "expr,state,expected",
    [
        # Comparisons
        ("$.score < 0.7", {"score": 0.5}, True),
        ("$.score < 0.7", {"score": 0.9}, False),
        ("$.score >= 0.7", {"score": 0.7}, True),
        ("$.score > 0.7", {"score": 0.7}, False),
        ("$.score == 0", {"score": 0}, True),
        ("$.score != 0", {"score": 0}, False),
        # Strings
        ('$.decision == "auto_approve"', {"decision": "auto_approve"}, True),
        ("$.decision == 'auto_reject'", {"decision": "auto_reject"}, True),
        # Booleans + null
        ("$.flag == true", {"flag": True}, True),
        ("$.flag == false", {"flag": False}, True),
        ("$.absent == null", {}, True),
        # Boolean ops with short-circuit
        ("$.a == 1 && $.b == 2", {"a": 1, "b": 2}, True),
        ("$.a == 1 && $.b == 2", {"a": 1, "b": 3}, False),
        ("$.a == 1 || $.b == 2", {"a": 9, "b": 2}, True),
        ("$.a == 1 || $.b == 2", {"a": 9, "b": 9}, False),
        # NOT + grouping
        ("!($.score < 0.7)", {"score": 0.9}, True),
        ("!($.score < 0.7)", {"score": 0.5}, False),
        # Membership
        ('$.tier in ["gold", "platinum"]', {"tier": "gold"}, True),
        ('$.tier in ["gold", "platinum"]', {"tier": "bronze"}, False),
        ("$.count in [1, 2, 3]", {"count": 2}, True),
        # Nested JSONPath
        ("$.user.score >= 0.5", {"user": {"score": 0.6}}, True),
        ("$.user.score >= 0.5", {"user": {"score": 0.4}}, False),
        # Precedence: AND binds tighter than OR
        ("$.a == 1 && $.b == 2 || $.c == 3", {"a": 9, "b": 9, "c": 3}, True),
        ("$.a == 1 && $.b == 2 || $.c == 3", {"a": 1, "b": 2, "c": 9}, True),
        ("$.a == 1 && $.b == 2 || $.c == 3", {"a": 9, "b": 2, "c": 9}, False),
    ],
)
def test_dsl_evaluates_correctly(
    expr: str,
    state: dict,
    expected: bool,
) -> None:
    cc = parse_condition(expr)
    assert cc.evaluate(state) is expected


@pytest.mark.unit
def test_dsl_missing_path_returns_none() -> None:
    """`$.foo.bar.baz` on a state without `foo` evaluates to None — letting
    operators write `$.absent == null` rather than needing exception
    handling for missing keys."""
    cc = parse_condition("$.foo == null")
    assert cc.evaluate({}) is True
    cc2 = parse_condition("$.foo.bar == null")
    assert cc2.evaluate({"foo": "not-a-dict"}) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "expr",
    [
        "",  # empty
        "$.score @@ 1",  # unknown operator
        "$.score <",  # missing right operand
        "$.score < ",  # ditto
        "$.score < 1 &&",  # trailing op
        "(",  # unbalanced
        "($.a == 1",  # unbalanced
        "$.a in $.b",  # in must take a literal list
        "$.a in 1",  # ditto
        "1 == 1 extra",  # trailing tokens
    ],
)
def test_dsl_rejects_malformed_expressions(expr: str) -> None:
    with pytest.raises(ConditionParseError):
        parse_condition(expr)


@pytest.mark.unit
def test_dsl_typed_comparison_failure_raises_eval_error() -> None:
    """Comparing a string to a number raises a typed runtime error
    rather than a raw TypeError. Workflow runner surfaces this in
    the FailureRecord instead of crashing the process."""
    cc = parse_condition("$.tier < 0.5")
    with pytest.raises(ConditionEvalError, match="type error"):
        cc.evaluate({"tier": "gold"})


# ---------------------------------------------------------------------------
# Structural validator
# ---------------------------------------------------------------------------


_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "decision": {"type": "string"},
        "reviewed": {"type": "boolean"},
        "approved": {"type": "boolean"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, output_key: str) -> Path:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompt.md").write_text(
        f"Emit JSON with {output_key}: true. Score is {{{{ input.score }}}}.\n"
    )
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"score": {"type": "number"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": [output_key],
                "additionalProperties": False,
                "properties": {output_key: {"type": "boolean"}},
            }
        )
    )
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "lifecycle": "validated",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
            }
        )
    )
    return agent_dir


def _make_workflow(
    workflow_dir: Path,
    *,
    edges: list[dict],
    nodes: list[dict] | None = None,
    runtime: str = "langgraph",
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    nodes = nodes or [
        {"id": "classify", "type": "agent", "ref": "./agents/classify"},
        {"id": "review", "type": "agent", "ref": "./agents/review"},
        {"id": "approve", "type": "agent", "ref": "./agents/approve"},
        {"id": "fallback", "type": "agent", "ref": "./agents/fallback"},
    ]
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "branch-test",
                "version": "0.1.0",
                "runtime": runtime,
                "state_schema": "./state.json",
                "entrypoint": "classify",
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _scaffold_three_branch_workflow(tmp_path: Path, *, runtime: str = "langgraph") -> Path:
    workflow_dir = tmp_path / f"wf-cond-{runtime}"
    _make_agent(workflow_dir / "agents" / "classify", name="classify", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "review", name="review", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "approve", name="approve", output_key="approved")
    _make_agent(workflow_dir / "agents" / "fallback", name="fallback", output_key="approved")
    return _make_workflow(
        workflow_dir,
        runtime=runtime,
        edges=[
            {"from": "classify", "to": "review", "kind": "conditional", "when": "$.score < 0.7"},
            {
                "from": "classify",
                "to": "approve",
                "kind": "conditional",
                "when": "$.score >= 0.9",
            },
            {"from": "classify", "to": "fallback", "kind": "conditional", "when": None},
        ],
    )


@pytest.mark.unit
def test_validator_accepts_well_formed_conditional_fan_out(tmp_path: Path) -> None:
    yaml_path = _scaffold_three_branch_workflow(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    # Should not raise.
    validate_conditional(graph)


@pytest.mark.unit
def test_validator_rejects_mixed_kinds_per_source(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf-mixed"
    _make_agent(workflow_dir / "agents" / "classify", name="classify", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "review", name="review", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "approve", name="approve", output_key="approved")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "classify", "type": "agent", "ref": "./agents/classify"},
            {"id": "review", "type": "agent", "ref": "./agents/review"},
            {"id": "approve", "type": "agent", "ref": "./agents/approve"},
        ],
        edges=[
            {"from": "classify", "to": "review", "kind": "sequential"},
            {
                "from": "classify",
                "to": "approve",
                "kind": "conditional",
                "when": "$.score >= 0.9",
            },
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="mixes sequential and conditional"):
        validate_conditional(graph)


@pytest.mark.unit
def test_validator_rejects_no_default(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf-no-default"
    _make_agent(workflow_dir / "agents" / "classify", name="classify", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "review", name="review", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "approve", name="approve", output_key="approved")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "classify", "type": "agent", "ref": "./agents/classify"},
            {"id": "review", "type": "agent", "ref": "./agents/review"},
            {"id": "approve", "type": "agent", "ref": "./agents/approve"},
        ],
        edges=[
            {"from": "classify", "to": "review", "kind": "conditional", "when": "$.score < 0.7"},
            {
                "from": "classify",
                "to": "approve",
                "kind": "conditional",
                "when": "$.score >= 0.9",
            },
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="no default"):
        validate_conditional(graph)


@pytest.mark.unit
def test_validator_rejects_default_not_last(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf-default-mid"
    _make_agent(workflow_dir / "agents" / "classify", name="classify", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "review", name="review", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "approve", name="approve", output_key="approved")
    _make_agent(workflow_dir / "agents" / "fallback", name="fallback", output_key="approved")
    yaml_path = _make_workflow(
        workflow_dir,
        edges=[
            # Default in the middle, not last
            {"from": "classify", "to": "review", "kind": "conditional", "when": "$.score < 0.7"},
            {"from": "classify", "to": "fallback", "kind": "conditional", "when": None},
            {
                "from": "classify",
                "to": "approve",
                "kind": "conditional",
                "when": "$.score >= 0.9",
            },
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="must appear LAST"):
        validate_conditional(graph)


@pytest.mark.unit
def test_yaml_validator_rejects_sequential_with_when(tmp_path: Path) -> None:
    """`kind: sequential` + `when:` is a user error worth catching at
    YAML parse time, not at the structural validator."""
    workflow_dir = tmp_path / "wf-seq-with-when"
    _make_agent(workflow_dir / "agents" / "first", name="first", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "second", name="second", output_key="reviewed")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[
            {
                "from": "first",
                "to": "second",
                "kind": "sequential",
                "when": "$.score < 0.7",
            },
        ],
    )
    from movate.core.workflow.spec import WorkflowSpecLoadError  # noqa: PLC0415 — narrow scope

    with pytest.raises(WorkflowSpecLoadError, match="kind: sequential"):
        load_workflow_spec(yaml_path)


# ---------------------------------------------------------------------------
# End-to-end conditional routing
# ---------------------------------------------------------------------------


class _RouteAwareProvider(BaseLLMProvider):
    """Returns a different output depending on which agent's prompt is
    being rendered, so each branch's agent has a satisfiable output."""

    name = "route_aware"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "reviewed" in body:
            return CompletionResponse(text='{"reviewed": true}')
        return CompletionResponse(text='{"approved": true}')

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


def _build_runner(
    *,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> WorkflowRunner:
    executor = Executor(
        provider=_RouteAwareProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    return WorkflowRunner(executor=executor, storage=storage)


@pytest.mark.unit
@pytest.mark.parametrize(
    "score,expected_path",
    [
        (0.3, ["classify", "review"]),  # low score → review branch
        (0.95, ["classify", "approve"]),  # high score → approve branch
        (0.8, ["classify", "fallback"]),  # in between → default
    ],
)
async def test_three_branch_conditional_routes_correctly(
    score: float,
    expected_path: list[str],
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    yaml_path = _scaffold_three_branch_workflow(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    runner = _build_runner(pricing=pricing, storage=storage, tracer=tracer)

    result = await runner.run(graph, initial_state={"score": score})
    assert result.status is WorkflowStatus.SUCCESS
    visited = [r.node_id for r in result.runs]
    assert visited == expected_path, f"score={score} expected {expected_path}, got {visited}"


@pytest.mark.unit
def test_compile_workflow_rejects_malformed_when_expression(tmp_path: Path) -> None:
    """A bad `when:` syntax fails workflow compile, not first routing call.
    Operators see the failure as soon as they edit workflow.yaml."""
    workflow_dir = tmp_path / "wf-bad-when"
    _make_agent(workflow_dir / "agents" / "classify", name="classify", output_key="reviewed")
    _make_agent(workflow_dir / "agents" / "fallback", name="fallback", output_key="approved")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "classify", "type": "agent", "ref": "./agents/classify"},
            {"id": "fallback", "type": "agent", "ref": "./agents/fallback"},
        ],
        edges=[
            {
                "from": "classify",
                "to": "fallback",
                "kind": "conditional",
                "when": "$.score @@ 7",  # not valid syntax
            },
            {"from": "classify", "to": "fallback", "kind": "conditional", "when": None},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="condition failed to parse"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_irkind_when_is_default_helper() -> None:
    """`when_is_default()` only returns True for conditional edges with
    no condition — sequential edges with no condition are not 'defaults'."""
    from movate.core.workflow.ir import WorkflowEdge  # noqa: PLC0415 — narrow scope

    seq = WorkflowEdge(from_id="a", to_id="b", kind=EdgeKind.SEQUENTIAL)
    cond_with = WorkflowEdge(
        from_id="a", to_id="b", kind=EdgeKind.CONDITIONAL, condition="$.x == 1"
    )
    cond_default = WorkflowEdge(from_id="a", to_id="b", kind=EdgeKind.CONDITIONAL)
    assert seq.when_is_default() is False
    assert cond_with.when_is_default() is False
    assert cond_default.when_is_default() is True
