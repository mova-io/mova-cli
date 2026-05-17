"""intent-router workflow node — unit tests.

Tests cover:
 1. WorkflowSpec parsing of intent-router nodes (YAML → Pydantic)
 2. compile_workflow building the graph + injecting synthetic edges
 3. validate_linear accepting intent-router workflows
 4. validate_linear rejecting bad route targets (caught at compile time)
 5. Runner under mock mode picks the first (sorted) route key
 6. Runner routes to the correct downstream agent node
 7. Runner falls back to fallback when classifier returns unknown label
 8. Runner surfaces classifier failure as WorkflowStatus.ERROR
 9. Route target node IDs validated at compile time (bad ID → error)
10. Input_field default ("text") and custom field are both respected
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.loader import AgentLoadError
from movate.core.models import (
    ErrorInfo,
    JobStatus,
    Metrics,
    RunResponse,
    TokenUsage,
    WorkflowStatus,
)
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.ir import EdgeKind, NodeType
from movate.core.workflow.spec import (
    AgentNodeSpec,
    IntentRouterNodeSpec,
    WorkflowSpec,
)
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "question": {"type": "string"},
        "answer": {"type": "string"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, output_key: str = "answer") -> Path:
    """Build a minimal agent that writes ``output_key``."""
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
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(f"Answer as {name}.\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
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
    return agent_dir


def _make_clf_agent(clf_dir: Path) -> Path:
    """Build a minimal classifier agent that returns ``{label: ...}``."""
    clf_dir.mkdir(parents=True, exist_ok=True)
    (clf_dir / "schema").mkdir(exist_ok=True)
    (clf_dir / "evals").mkdir(exist_ok=True)

    (clf_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "intent-clf",
                "version": "0.1.0",
                "description": "classifies intent",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (clf_dir / "prompt.md").write_text("Classify: {{ input.text }}\n")
    (clf_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["text", "labels"],
                "properties": {
                    "text": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
            }
        )
    )
    (clf_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["label"],
                "properties": {"label": {"type": "string"}},
            }
        )
    )
    (clf_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"text": "x", "labels": ["a"]}, "expected": {"label": "a"}}) + "\n"
    )
    return clf_dir


def _make_router_workflow(
    workflow_dir: Path,
    *,
    routes: dict[str, str],
    fallback: str,
    classifier_agent: str,
    input_field: str = "question",
    extra_nodes: list[dict] | None = None,
    extra_edges: list[dict] | None = None,
    state_schema: dict | None = None,
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(state_schema or _STATE_SCHEMA))
    router_node: dict[str, Any] = {
        "id": "triage",
        "type": "intent-router",
        "routes": routes,
        "fallback": fallback,
        "classifier_agent": classifier_agent,
        "input_field": input_field,
    }
    nodes = [router_node] + (extra_nodes or [])
    edges = extra_edges or []
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "test-router",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "triage",
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _success_response(data: dict[str, Any]) -> RunResponse:
    return RunResponse(
        status="success",
        run_id="run-1",
        data=data,
        human_readable="",
        trace_id="t1",
        metrics=Metrics(latency_ms=1, tokens=TokenUsage(), cost_usd=0.0),
    )


def _error_response() -> RunResponse:
    return RunResponse(
        status="error",
        run_id="run-err",
        data={},
        human_readable="error",
        trace_id="t1",
        metrics=Metrics(latency_ms=1, tokens=TokenUsage(), cost_usd=0.0),
        error=ErrorInfo(type="schema_error", message="bad output", retryable=False),
    )


def _build_runner(
    mock_executor: Executor,
    storage: InMemoryStorage,
) -> WorkflowRunner:
    return WorkflowRunner(executor=mock_executor, storage=storage)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing():
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


# ---------------------------------------------------------------------------
# Test 1: WorkflowSpec parsing of intent-router YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_spec_parses_intent_router_node() -> None:
    raw = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "test-router",
        "version": "0.1.0",
        "state_schema": "./state.json",
        "entrypoint": "triage",
        "nodes": [
            {
                "id": "triage",
                "type": "intent-router",
                "routes": {"billing": "billing-node", "general": "general-node"},
                "fallback": "general-node",
                "classifier_agent": "intent-clf",
                "input_field": "question",
            },
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
            {"id": "general-node", "type": "agent", "ref": "./agents/general"},
        ],
        "edges": [],
    }
    spec = WorkflowSpec.model_validate(raw)
    assert len(spec.nodes) == 3
    triage = spec.nodes[0]
    assert isinstance(triage, IntentRouterNodeSpec)
    assert triage.type == "intent-router"
    assert triage.routes == {"billing": "billing-node", "general": "general-node"}
    assert triage.fallback == "general-node"
    assert triage.classifier_agent == "intent-clf"
    assert triage.input_field == "question"

    billing = spec.nodes[1]
    assert isinstance(billing, AgentNodeSpec)
    assert billing.type == "agent"


# ---------------------------------------------------------------------------
# Test 2: input_field defaults to "text"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_spec_input_field_defaults_to_text() -> None:
    raw = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "test-router",
        "version": "0.1.0",
        "state_schema": "./state.json",
        "entrypoint": "triage",
        "nodes": [
            {
                "id": "triage",
                "type": "intent-router",
                "routes": {"a": "node-a"},
                "fallback": "node-a",
                "classifier_agent": "clf",
                # input_field omitted — should default to "text"
            },
            {"id": "node-a", "type": "agent", "ref": "./agents/a"},
        ],
        "edges": [],
    }
    spec = WorkflowSpec.model_validate(raw)
    triage = spec.nodes[0]
    assert isinstance(triage, IntentRouterNodeSpec)
    assert triage.input_field == "text"


# ---------------------------------------------------------------------------
# Test 3: compile_workflow builds graph with INTENT_ROUTER node + synthetic edges
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_intent_router_graph(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_agent(wf_dir / "agents" / "general", name="general-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node", "general": "general-node"},
        fallback="general-node",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
            {"id": "general-node", "type": "agent", "ref": "./agents/general"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    # Router node should have INTENT_ROUTER type.
    assert "triage" in graph.nodes
    triage_node = graph.nodes["triage"]
    assert triage_node.type is NodeType.INTENT_ROUTER
    assert triage_node.metadata["routes"] == {"billing": "billing-node", "general": "general-node"}
    assert triage_node.metadata["fallback"] == "general-node"
    assert triage_node.metadata["classifier_agent"] == "./agents/intent-clf"

    # Synthetic conditional edges injected from router to each unique target.
    router_edges = [e for e in graph.edges if e.from_id == "triage"]
    assert len(router_edges) == 2  # billing-node and general-node (deduped from fallback)
    assert all(e.kind is EdgeKind.CONDITIONAL for e in router_edges)
    assert all(e.metadata.get("synthetic") for e in router_edges)
    targets = {e.to_id for e in router_edges}
    assert targets == {"billing-node", "general-node"}


# ---------------------------------------------------------------------------
# Test 4: validate_linear accepts intent-router workflow
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_linear_accepts_intent_router(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_agent(wf_dir / "agents" / "general", name="general-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node", "general": "general-node"},
        fallback="general-node",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
            {"id": "general-node", "type": "agent", "ref": "./agents/general"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    # Should not raise.
    validate_linear(graph)


# ---------------------------------------------------------------------------
# Test 5: compile_workflow rejects unknown route target IDs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_rejects_bad_route_target(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node", "general": "DOES-NOT-EXIST"},
        fallback="billing-node",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="DOES-NOT-EXIST"):
        compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# Test 6: compile_workflow rejects unknown fallback node ID
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_rejects_bad_fallback_target(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node"},
        fallback="MISSING-FALLBACK",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="MISSING-FALLBACK"):
        compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# Test 7: Runner under mock=True picks first sorted route key
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_mock_picks_first_sorted_route(
    tmp_path: Path, pricing: Any, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_agent(wf_dir / "agents" / "general", name="general-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node", "general": "general-node"},
        fallback="general-node",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
            {"id": "general-node", "type": "agent", "ref": "./agents/general"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    # Mock the executor: when called for billing-agent return valid output.
    mock_executor = MagicMock(spec=Executor)
    # billing is sorted first; billing-agent returns {"answer": "billing reply"}
    mock_executor.execute = AsyncMock(
        return_value=_success_response({"answer": "billing reply"})
    )

    runner = WorkflowRunner(executor=mock_executor, storage=storage)
    result = await runner.run(graph, initial_state={"question": "need help"}, mock=True)

    assert result.status is WorkflowStatus.SUCCESS
    # "billing" < "general" alphabetically, so billing-node was chosen.
    assert result.final_state.get("answer") == "billing reply"
    # Executor was called once (for the billing-node agent, NOT the classifier).
    mock_executor.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 8: Runner routes correctly based on classifier label
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_routes_to_correct_node(
    tmp_path: Path, pricing: Any, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_agent(wf_dir / "agents" / "general", name="general-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node", "general": "general-node"},
        fallback="general-node",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
            {"id": "general-node", "type": "agent", "ref": "./agents/general"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    call_count = {"n": 0}

    async def fake_execute(bundle, request, **kwargs):
        call_count["n"] += 1
        agent_name = bundle.spec.name
        if agent_name == "intent-clf":
            # Classifier returns "general"
            return _success_response({"label": "general"})
        if agent_name == "general-agent":
            return _success_response({"answer": "general answer"})
        # Should not reach billing-agent
        return _success_response({"answer": "should not happen"})

    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute = AsyncMock(side_effect=fake_execute)

    runner = WorkflowRunner(executor=mock_executor, storage=storage)
    result = await runner.run(graph, initial_state={"question": "how do I pay?"}, mock=False)

    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state.get("answer") == "general answer"
    # Two calls: classifier + general-agent
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Test 9: Runner falls back when classifier returns unknown label
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_fallback_on_unknown_label(
    tmp_path: Path, pricing: Any, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_agent(wf_dir / "agents" / "general", name="general-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node"},
        fallback="general-node",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
            {"id": "general-node", "type": "agent", "ref": "./agents/general"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    async def fake_execute(bundle, request, **kwargs):
        agent_name = bundle.spec.name
        if agent_name == "intent-clf":
            # Returns a label that's not in routes → should fall back to general-node
            return _success_response({"label": "UNKNOWN_LABEL"})
        if agent_name == "general-agent":
            return _success_response({"answer": "fallback answer"})
        return _success_response({"answer": "should not happen"})

    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute = AsyncMock(side_effect=fake_execute)

    runner = WorkflowRunner(executor=mock_executor, storage=storage)
    result = await runner.run(graph, initial_state={"question": "random?"}, mock=False)

    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state.get("answer") == "fallback answer"


# ---------------------------------------------------------------------------
# Test 10: Runner surfaces classifier failure as WorkflowStatus.ERROR
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_classifier_failure_is_error(
    tmp_path: Path, pricing: Any, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    wf_dir = tmp_path / "wf"
    _make_agent(wf_dir / "agents" / "billing", name="billing-agent")
    _make_agent(wf_dir / "agents" / "general", name="general-agent")
    _make_clf_agent(wf_dir / "agents" / "intent-clf")

    yaml_path = _make_router_workflow(
        wf_dir,
        routes={"billing": "billing-node", "general": "general-node"},
        fallback="general-node",
        classifier_agent="./agents/intent-clf",
        extra_nodes=[
            {"id": "billing-node", "type": "agent", "ref": "./agents/billing"},
            {"id": "general-node", "type": "agent", "ref": "./agents/general"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    async def fake_execute(bundle, request, **kwargs):
        # Classifier always fails
        return _error_response()

    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute = AsyncMock(side_effect=fake_execute)

    runner = WorkflowRunner(executor=mock_executor, storage=storage)
    result = await runner.run(graph, initial_state={"question": "help!"}, mock=False)

    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "triage"
    assert result.error is not None
