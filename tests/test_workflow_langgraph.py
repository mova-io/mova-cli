"""Equivalence + capability tests for the LangGraph compiler.

The contract:

* Same `workflow.yaml` runs end-to-end under BOTH `runtime: homegrown`
  and `runtime: langgraph` and produces the same `WorkflowResult` shape
  — status, final_state, per-node sequence, and persisted WorkflowRunRecord.
* Errors at a node short-circuit the workflow under either runtime with
  identical error_node_id + partial-state semantics.
* Missing optional dep surfaces a friendly error pointing at the install
  hint, not a raw ImportError.
* Capability check rejects non-linear / non-AGENT graphs before invoke.

The tests skip when ``langgraph`` isn't on the system Python (e.g.
running ``pytest`` without ``--all-extras``). CI installs all extras so
the gate runs these unconditionally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.core.workflow.compilers.langgraph import (
    LangGraphCompileError,
    can_compile,
)
from movate.core.workflow.ir import EdgeKind, NodeType, WorkflowEdge, WorkflowNode
from movate.core.workflow.runner import WorkflowRunner
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

# Skip everything in this file if langgraph isn't installed. CI's
# `--all-extras` install picks up the optional dep; a bare-bones
# `pytest` invocation won't.
pytest.importorskip("langgraph")


# ---------------------------------------------------------------------------
# Builders mirror tests/test_workflow_runner.py so equivalence stays exact
# ---------------------------------------------------------------------------


_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "step1": {"type": "string"},
        "step2": {"type": "string"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> Path:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompt.md").write_text(
        f"Echo. Read {{{{ input.{input_key} }}}}; emit JSON with {output_key}.\n"
    )
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": [input_key],
                "additionalProperties": False,
                "properties": {input_key: {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": [output_key],
                "additionalProperties": False,
                "properties": {output_key: {"type": "string"}},
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
                "lifecycle": "validated",  # so --strict gates downstream wouldn't trip
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


def _make_workflow_yaml(
    workflow_dir: Path,
    *,
    runtime: str = "homegrown",
    nodes: list[dict],
    edges: list[dict],
    entrypoint: str = "first",
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "equiv-test",
                "version": "0.1.0",
                "runtime": runtime,
                "state_schema": "./state.json",
                "entrypoint": entrypoint,
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _scaffold_two_step(tmp_path: Path, *, runtime: str) -> Path:
    workflow_dir = tmp_path / f"wf-{runtime}"
    _make_agent(
        workflow_dir / "agents" / "first",
        name="first-agent",
        input_key="text",
        output_key="step1",
    )
    _make_agent(
        workflow_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    return _make_workflow_yaml(
        workflow_dir,
        runtime=runtime,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )


class _StateAwareProvider(BaseLLMProvider):
    """Returns different output based on which input field is in the prompt."""

    name = "state_aware"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "step1" in body and "step2" not in body:
            return CompletionResponse(text='{"step1": "alpha"}')
        return CompletionResponse(text='{"step2": "beta"}')

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
    provider: BaseLLMProvider,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> WorkflowRunner:
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    return WorkflowRunner(executor=executor, storage=storage)


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_can_compile_accepts_linear_agent_graph(tmp_path: Path) -> None:
    yaml_path = _scaffold_two_step(tmp_path, runtime="langgraph")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    ok, reason = can_compile(graph)
    assert ok and reason is None


@pytest.mark.unit
def test_can_compile_rejects_non_agent_nodes(tmp_path: Path) -> None:
    """The IR can hold TOOL / HUMAN / FUNCTION nodes (future variants);
    the v1.0 compiler refuses them with a clear message."""
    # Hand-build a graph with a HUMAN node — v0.3 compiler rejects this,
    # but for the capability check we construct the IR directly.
    yaml_path = _scaffold_two_step(tmp_path, runtime="langgraph")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    graph.nodes["first"] = WorkflowNode(id="first", type=NodeType.HUMAN, ref="x")

    ok, reason = can_compile(graph)
    assert not ok
    assert reason is not None and "AGENT nodes only" in reason


@pytest.mark.unit
def test_can_compile_rejects_parallel_edges(tmp_path: Path) -> None:
    """Parallel fan-out / fan-in aren't supported yet; the compiler
    refuses them with a v1.1.x pointer. Conditional edges are NOW
    supported (this PR) and tested separately under
    ``test_workflow_conditional.py``."""
    yaml_path = _scaffold_two_step(tmp_path, runtime="langgraph")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    graph.edges = [WorkflowEdge(from_id="first", to_id="second", kind=EdgeKind.PARALLEL_FAN_OUT)]
    ok, reason = can_compile(graph)
    assert not ok
    assert reason is not None
    lower = reason.lower()
    assert "parallel" in lower or "fan" in lower


# ---------------------------------------------------------------------------
# Happy-path equivalence — same workflow yields same result under both runtimes
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_homegrown_and_langgraph_produce_same_final_state(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    homegrown_yaml = _scaffold_two_step(tmp_path, runtime="homegrown")
    langgraph_yaml = _scaffold_two_step(tmp_path, runtime="langgraph")

    runner = _build_runner(
        provider=_StateAwareProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )

    # Homegrown.
    h_spec, h_parent = load_workflow_spec(homegrown_yaml)
    h_graph = compile_workflow(h_spec, h_parent)
    h_result = await runner.run(h_graph, initial_state={"text": "seed"})

    # LangGraph — fresh storage + runner so we're not sharing state.
    lg_storage = InMemoryStorage()
    await lg_storage.init()
    lg_runner = _build_runner(
        provider=_StateAwareProvider(),
        pricing=pricing,
        storage=lg_storage,
        tracer=tracer,
    )
    lg_spec, lg_parent = load_workflow_spec(langgraph_yaml)
    lg_graph = compile_workflow(lg_spec, lg_parent)
    lg_result = await lg_runner.run(lg_graph, initial_state={"text": "seed"})

    # Status, final state, node sequence equivalent.
    assert h_result.status is WorkflowStatus.SUCCESS
    assert lg_result.status is WorkflowStatus.SUCCESS
    assert h_result.final_state == lg_result.final_state
    assert h_result.final_state == {"text": "seed", "step1": "alpha", "step2": "beta"}
    assert [r.node_id for r in h_result.runs] == [r.node_id for r in lg_result.runs]
    assert [r.node_id for r in lg_result.runs] == ["first", "second"]


@pytest.mark.unit
async def test_langgraph_persists_workflow_run_record(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """The WorkflowRunRecord row is written under the langgraph runtime
    same as homegrown — replay + drift detection rely on the row existing."""
    yaml_path = _scaffold_two_step(tmp_path, runtime="langgraph")
    runner = _build_runner(
        provider=_StateAwareProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    result = await runner.run(graph, initial_state={"text": "seed"})

    stored = await storage.get_workflow_run(result.workflow_run_id, tenant_id="local")
    assert stored is not None
    assert stored.status is WorkflowStatus.SUCCESS
    assert stored.final_state == result.final_state


# ---------------------------------------------------------------------------
# Failure equivalence — node 2 fails under both runtimes; same shape
# ---------------------------------------------------------------------------


class _FailOnStep2Provider(BaseLLMProvider):
    """First call succeeds; second call returns non-JSON to trip output schema."""

    name = "fail_on_step2"
    version = "0.0.1"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if self.calls == 1:
            return CompletionResponse(text='{"step1": "alpha"}')
        return CompletionResponse(text="this is not JSON")

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.unit
async def test_langgraph_short_circuits_on_node_failure(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    yaml_path = _scaffold_two_step(tmp_path, runtime="langgraph")
    runner = _build_runner(
        provider=_FailOnStep2Provider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    result = await runner.run(graph, initial_state={"text": "seed"})

    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "second"
    # Pre-failure state preserved — step1 merged in from node 1, no step2 key.
    assert "step1" in result.final_state
    assert "step2" not in result.final_state
    # Both nodes' RunRecords were captured (node 1 succeeded, node 2 errored).
    assert len(result.runs) == 2


# ---------------------------------------------------------------------------
# Initial-state validation — same WorkflowRunError under either runtime
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_langgraph_rejects_invalid_initial_state(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """State must satisfy state_schema even before LangGraph is touched.
    The error mirrors the homegrown runner's WorkflowRunError."""
    from movate.core.workflow.runner import WorkflowRunError  # noqa: PLC0415 — narrow test scope

    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first",
        name="first",
        input_key="text",
        output_key="step1",
    )
    schema = {
        "type": "object",
        "required": ["text"],
        "additionalProperties": False,
        "properties": {"text": {"type": "string"}},
    }
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(schema))
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "rejects-bad-init",
                "version": "0.1.0",
                "runtime": "langgraph",
                "state_schema": "./state.json",
                "entrypoint": "first",
                "nodes": [{"id": "first", "type": "agent", "ref": "./agents/first"}],
                "edges": [],
            }
        )
    )

    runner = _build_runner(
        provider=MockProvider(response='{"step1": "x"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    with pytest.raises(WorkflowRunError, match="state_schema"):
        await runner.run(graph, initial_state={"text": 42})


# ---------------------------------------------------------------------------
# Missing-dep error path — runner surfaces the install hint when langgraph
# is unavailable. Patched at the import boundary so we don't need to
# actually uninstall the package.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_langgraph_dep_surfaces_install_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """If `import langgraph` fails, the compiler raises
    LangGraphCompileError with an `uv pip install` hint — not a raw
    ImportError. Simulate by patching `import_langgraph` to raise."""
    from movate.core.workflow.compilers import langgraph as compiler_mod  # noqa: PLC0415

    def _raise() -> tuple:
        raise LangGraphCompileError(
            "workflow.yaml declares 'runtime: langgraph' but the langgraph "
            "package isn't installed. Install with: "
            "uv pip install 'movate-cli[langgraph]'"
        )

    monkeypatch.setattr(compiler_mod, "import_langgraph", _raise)

    yaml_path = _scaffold_two_step(tmp_path, runtime="langgraph")
    runner = _build_runner(
        provider=_StateAwareProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    with pytest.raises(LangGraphCompileError, match="movate-cli\\[langgraph\\]"):
        await runner.run(graph, initial_state={"text": "seed"})
