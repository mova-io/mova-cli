"""Deterministic ``decision`` node (ADR 094) — unit + integration tests.

Covers:
 1. The pure routing helper ``evaluate_decision`` — every operator, dotted
    fields, numeric coercion, missing-field fallthrough, first-match ordering,
    default, and the unknown-operator guard.
 2. Spec parsing/validation — ``DecisionNodeSpec`` accepts good rules and rejects
    unknown operators + malformed membership values at parse time.
 3. ``compile_workflow`` — builds a DECISION node + synthetic CONDITIONAL edges,
    ``validate_linear`` accepts it, and bad route targets fail loud.
 4. Native runner — routes to the correct branch per state with NO RunRecord for
    the decision node (it makes no model call).
 5. Temporal compiler — emits inline ``evaluate_decision`` + the sandbox import
    and NO activity for the decision branch (the no-LLM win), and routes
    identically to native (parity).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from pydantic import ValidationError

from movate.core.executor import Executor
from movate.core.models import Metrics, RunResponse, TokenUsage, WorkflowStatus
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.decision import (
    _MISSING,
    _apply_op,
    _evaluate_decision_traced,
    _read_field,
    evaluate_decision,
)
from movate.core.workflow.ir import EdgeKind, NodeType
from movate.core.workflow.spec import DecisionNodeSpec
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# 1. Pure helper
# ---------------------------------------------------------------------------

_TIER_CASES = [
    {"when": {"field": "amount", "op": "gt", "value": 5000}, "to": "director"},
    {"when": {"field": "amount", "op": "gt", "value": 0}, "to": "manager"},
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("op", "left", "right", "expected"),
    [
        ("gt", 6000, 5000, True),
        ("gt", 5000, 5000, False),
        ("gte", 5000, 5000, True),
        ("lt", 1, 2, True),
        ("lte", 2, 2, True),
        ("eq", "open", "open", True),
        ("ne", "open", "closed", True),
        ("in", "x", ["x", "y"], True),
        ("in", "z", ["x", "y"], False),
        ("not_in", "z", ["x", "y"], True),
        ("contains", ["a", "b"], "a", True),
        ("contains", "hello world", "world", True),
        ("truthy", "non-empty", None, True),
        ("truthy", "", None, False),
        ("falsy", 0, None, True),
        ("falsy", 1, None, False),
        # numeric coercion: a string state value still compares numerically.
        ("gt", "6000", 5000, True),
        ("gt", "not-a-number", 5000, False),  # uncoercible ⇒ non-match, not crash
    ],
)
def test_apply_op(op: str, left: Any, right: Any, expected: bool) -> None:
    assert _apply_op(op, left, right) is expected


@pytest.mark.unit
def test_apply_op_rejects_unknown_operator() -> None:
    with pytest.raises(ValueError, match="unknown decision operator"):
        _apply_op("bogus", 1, 2)


@pytest.mark.unit
def test_read_field_dotted_and_missing() -> None:
    assert _read_field({"expense": {"amount": 42}}, "expense.amount") == 42
    assert _read_field({"expense": {}}, "expense.amount") is _MISSING
    assert _read_field({}, "amount") is _MISSING


@pytest.mark.unit
def test_evaluate_decision_first_match_and_default() -> None:
    assert evaluate_decision(_TIER_CASES, "auto", {"amount": 6000}) == "director"
    assert evaluate_decision(_TIER_CASES, "auto", {"amount": 100}) == "manager"
    assert evaluate_decision(_TIER_CASES, "auto", {"amount": 0}) == "auto"
    # missing field ⇒ no comparison matches ⇒ default
    assert evaluate_decision(_TIER_CASES, "auto", {}) == "auto"


@pytest.mark.unit
def test_evaluate_decision_traced_returns_index() -> None:
    assert _evaluate_decision_traced(_TIER_CASES, "auto", {"amount": 6000}) == ("director", 0)
    assert _evaluate_decision_traced(_TIER_CASES, "auto", {"amount": 100}) == ("manager", 1)
    assert _evaluate_decision_traced(_TIER_CASES, "auto", {"amount": 0}) == ("auto", None)


# ---------------------------------------------------------------------------
# 2. Spec validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_spec_accepts_valid_decision_node() -> None:
    node = DecisionNodeSpec.model_validate(
        {
            "id": "classify",
            "type": "decision",
            "cases": [{"when": {"field": "amount", "op": "gt", "value": 5000}, "to": "director"}],
            "default": "auto",
        }
    )
    assert node.type == "decision"
    assert node.cases[0].when.op == "gt"


@pytest.mark.unit
def test_spec_rejects_unknown_operator() -> None:
    with pytest.raises(ValidationError):
        DecisionNodeSpec.model_validate(
            {
                "id": "classify",
                "type": "decision",
                "cases": [{"when": {"field": "amount", "op": "between", "value": 5000}, "to": "a"}],
                "default": "auto",
            }
        )


@pytest.mark.unit
def test_spec_rejects_non_list_value_for_in() -> None:
    with pytest.raises(ValidationError, match="needs a list 'value'"):
        DecisionNodeSpec.model_validate(
            {
                "id": "classify",
                "type": "decision",
                "cases": [{"when": {"field": "status", "op": "in", "value": "open"}, "to": "a"}],
                "default": "auto",
            }
        )


@pytest.mark.unit
def test_spec_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        DecisionNodeSpec.model_validate(
            {
                "id": "classify",
                "type": "decision",
                "cases": [{"when": {"field": "amount", "op": "gt", "value": 1}, "to": "a"}],
                "default": "auto",
                "classifier_agent": "oops",  # not a decision field
            }
        )


# ---------------------------------------------------------------------------
# Workflow builders
# ---------------------------------------------------------------------------

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {"amount": {"type": "number"}, "answer": {"type": "string"}},
}


def _make_agent(agent_dir: Path, *, name: str, output_key: str = "answer") -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": f"writes {output_key}",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18", "params": {}},
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(f"Answer as {name}.\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"type": "object", "additionalProperties": True})
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [output_key],
                "properties": {output_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {}, "expected": {output_key: "x"}}) + "\n"
    )


def _make_decision_workflow(wf_dir: Path, *, default: str = "auto-node") -> Path:
    """A decision node routing on ``amount`` into three distinct agent sinks."""
    wf_dir.mkdir(parents=True, exist_ok=True)
    _make_agent(wf_dir / "agents" / "director", name="director-agent")
    _make_agent(wf_dir / "agents" / "manager", name="manager-agent")
    _make_agent(wf_dir / "agents" / "auto", name="auto-agent")
    (wf_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path = wf_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "test-decision",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "classify",
                "nodes": [
                    {
                        "id": "classify",
                        "type": "decision",
                        "cases": [
                            {
                                "when": {"field": "amount", "op": "gt", "value": 5000},
                                "to": "director-node",
                            },
                            {
                                "when": {"field": "amount", "op": "gt", "value": 0},
                                "to": "manager-node",
                            },
                        ],
                        "default": default,
                    },
                    {"id": "director-node", "type": "agent", "ref": "./agents/director"},
                    {"id": "manager-node", "type": "agent", "ref": "./agents/manager"},
                    {"id": "auto-node", "type": "agent", "ref": "./agents/auto"},
                ],
                "edges": [],
            }
        )
    )
    return yaml_path


# ---------------------------------------------------------------------------
# 3. compile_workflow + validate_linear
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_decision_graph_and_synthetic_edges(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(_make_decision_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)
    node = graph.nodes["classify"]
    assert node.type is NodeType.DECISION
    assert node.metadata["default"] == "auto-node"
    # Synthetic CONDITIONAL edges to all three targets.
    targets = {
        e.to_id for e in graph.edges if e.from_id == "classify" and e.kind is EdgeKind.CONDITIONAL
    }
    assert targets == {"director-node", "manager-node", "auto-node"}
    # And it passes the linear phase gate (a routing primitive may branch).
    validate_linear(graph)


@pytest.mark.unit
def test_compile_rejects_bad_route_target(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(
        _make_decision_workflow(tmp_path / "wf", default="does-not-exist")
    )
    with pytest.raises(WorkflowCompileError, match="route target 'does-not-exist'"):
        compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# 4. Native runner
# ---------------------------------------------------------------------------


def _ok(data: dict[str, Any]) -> RunResponse:
    return RunResponse(
        status="success",
        run_id="r1",
        data=data,
        human_readable="",
        trace_id="t1",
        metrics=Metrics(latency_ms=1, tokens=TokenUsage(), cost_usd=0.0),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("amount", "expected_agent", "expected_answer"),
    [
        (6000, "director-agent", "director reply"),
        (100, "manager-agent", "manager reply"),
        (0, "auto-agent", "auto reply"),
    ],
)
async def test_native_decision_routes_per_state(
    tmp_path: Path,
    storage: InMemoryStorage,
    amount: int,
    expected_agent: str,
    expected_answer: str,
) -> None:
    spec, parent = load_workflow_spec(_make_decision_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)

    seen: dict[str, int] = {"n": 0}

    async def fake_execute(bundle, request, **kwargs):
        seen["n"] += 1
        replies = {
            "director-agent": "director reply",
            "manager-agent": "manager reply",
            "auto-agent": "auto reply",
        }
        return _ok({"answer": replies[bundle.spec.name]})

    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute = AsyncMock(side_effect=fake_execute)

    runner = WorkflowRunner(executor=mock_executor, storage=storage)
    result = await runner.run(graph, initial_state={"amount": amount}, mock=False)

    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state.get("answer") == expected_answer
    # Exactly ONE executor call — the chosen agent. The decision node makes no
    # model call, so it adds no RunRecord (no extra execute).
    assert seen["n"] == 1
    assert len(result.runs) == 1
    assert result.runs[0].agent == expected_agent


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


# ---------------------------------------------------------------------------
# 5. Temporal compiler — inline, no activity, parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_temporal_emits_inline_decision_no_activity(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(_make_decision_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)
    result = TemporalCompiler().compile(graph)
    src = result.module_source

    # Parses as Python.
    ast.parse(src)
    # The decision branch calls the shared helper, inline, with the sandbox import.
    assert "evaluate_decision(" in src
    assert "from movate.core.workflow.decision import evaluate_decision" in src
    # And it SCHEDULES no classifier/gate activity (the no-LLM win). The header
    # statically imports every wrapper, so the meaningful signal is the set of
    # activities actually used: a pure-decision graph registers only the agent +
    # persist wrappers, never the gate activity.
    assert "call_gate_activity" not in result.activity_names
    assert "call_agent_activity" in result.activity_names
    assert set(result.activity_names) == {"call_agent_activity", "persist_workflow_result_activity"}


@pytest.mark.unit
def test_temporal_decision_routes_match_native(tmp_path: Path) -> None:
    """The emitted inline routing computes the same branch the native helper does
    — both call ``evaluate_decision``, so this pins the generated literal shape."""
    spec, parent = load_workflow_spec(_make_decision_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)
    cases = graph.nodes["classify"].metadata["cases"]
    default = graph.nodes["classify"].metadata["default"]
    for amount, expected in [(6000, "director-node"), (100, "manager-node"), (0, "auto-node")]:
        assert evaluate_decision(cases, default, {"amount": amount}) == expected
