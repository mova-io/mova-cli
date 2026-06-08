"""ADR 092 Phase 1 — native fan-out (the canonical diamond).

Proves a fan-out/fan-in workflow:

* authors + compiles (the spec accepts ``kind: fan_out``/``fan_in`` + ``join``),
* validates via the DAG gate (``validate_dag``/``validate_graph``) while the
  linear path still routes to ``validate_linear`` unchanged,
* RUNS concurrently on the native runner and joins branch state by each
  declared strategy (last_wins / by_key / collect),
* propagates a branch failure as a partial ERROR,
* and is bounded by the ``max_fanout`` cap + the canonical-diamond shape rules.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowRunner,
    compile_workflow,
    declares_parallel,
    load_workflow_spec,
    validate_dag,
    validate_graph,
)
from movate.core.workflow.compiler import DEFAULT_MAX_FANOUT
from movate.core.workflow.spec import WorkflowSpecLoadError
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Fixtures — agents + a parameterizable diamond workflow on disk
# ---------------------------------------------------------------------------

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {"text": {"type": "string"}},
}


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> Path:
    """A minimal agent that reads ``input_key`` and writes ``output_key``."""
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
                "description": f"reads {input_key}, writes {output_key}",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(
        "echo {{ input." + input_key + " }} as " + output_key + "\n"
    )
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": [input_key],
                "properties": {input_key: {"type": "string", "minLength": 1}},
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
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )
    return agent_dir


def _write_workflow(workflow_dir: Path, *, nodes: list[dict], edges: list[dict]) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "diamond-workflow",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "start",
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _diamond(
    tmp_path: Path,
    *,
    branch_a_out: str = "a_out",
    branch_b_out: str = "b_out",
    join: str | None = None,
    join_key: str | None = None,
) -> Path:
    """A canonical diamond: start → {a, b} → merge.

    ``start`` reads ``text`` → ``seed``; branch ``a``/``b`` read ``seed`` →
    their out keys; ``merge`` reads ``seed`` → ``final``.
    """
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "start", name="start-agent", input_key="text", output_key="seed")
    _make_agent(wf / "agents" / "a", name="a-agent", input_key="seed", output_key=branch_a_out)
    _make_agent(wf / "agents" / "b", name="b-agent", input_key="seed", output_key=branch_b_out)
    _make_agent(wf / "agents" / "merge", name="merge-agent", input_key="seed", output_key="final")

    fan_in_a: dict = {"from": "a", "to": "merge", "kind": "fan_in"}
    fan_in_b: dict = {"from": "b", "to": "merge", "kind": "fan_in"}
    if join is not None:
        fan_in_a["join"] = fan_in_b["join"] = join
    if join_key is not None:
        fan_in_a["join_key"] = fan_in_b["join_key"] = join_key

    return _write_workflow(
        wf,
        nodes=[
            {"id": "start", "type": "agent", "ref": "./agents/start"},
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
            {"id": "merge", "type": "agent", "ref": "./agents/merge"},
        ],
        edges=[
            {"from": "start", "to": "a", "kind": "fan_out"},
            {"from": "start", "to": "b", "kind": "fan_out"},
            fan_in_a,
            fan_in_b,
        ],
    )


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


class _DiamondProvider(BaseLLMProvider):
    """Returns ``{<out_key>: <out_key>.upper()}`` based on the prompt's ``as X``.

    Branch agents (``a_out``/``b_out``/``result``) rendezvous on a barrier so
    the test deadlocks (and times out) unless the two branches run concurrently
    — a deterministic proof of ``asyncio.gather`` parallelism.
    """

    name = "diamond"
    version = "0.0.1"

    def __init__(self, *, barrier_keys: tuple[str, ...] = ("a_out", "b_out")) -> None:
        self._barrier = asyncio.Barrier(2)
        self._barrier_keys = barrier_keys

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        for key in ("seed", "a_out", "b_out", "result", "final"):
            if f"as {key}" in body:
                if key in self._barrier_keys:
                    await self._barrier.wait()
                return CompletionResponse(text=json.dumps({key: key.upper()}))
        return CompletionResponse(text="{}")  # pragma: no cover

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


def _runner(
    provider: BaseLLMProvider, storage: InMemoryStorage, pricing: PricingTable
) -> WorkflowRunner:
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=NullTracer())
    return WorkflowRunner(executor=executor, storage=storage)


# ---------------------------------------------------------------------------
# Authoring + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_diamond_authors_and_compiles(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(_diamond(tmp_path))
    graph = compile_workflow(spec, parent)
    assert declares_parallel(graph) is True
    # validate_graph routes a parallel graph to the DAG gate and it passes.
    validate_graph(graph)  # must not raise
    validate_dag(graph)  # must not raise


@pytest.mark.unit
def test_validate_graph_routes_linear_to_linear_gate(tmp_path: Path) -> None:
    """A graph with no fan-out edge is NOT parallel and takes validate_linear."""
    wf = tmp_path / "lin"
    _make_agent(wf / "agents" / "one", name="one", input_key="text", output_key="seed")
    _make_agent(wf / "agents" / "two", name="two", input_key="seed", output_key="final")
    yaml_path = wf / "workflow.yaml"
    (wf / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "lin",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "one",
                "nodes": [
                    {"id": "one", "type": "agent", "ref": "./agents/one"},
                    {"id": "two", "type": "agent", "ref": "./agents/two"},
                ],
                "edges": [{"from": "one", "to": "two"}],
            }
        )
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    assert declares_parallel(graph) is False
    validate_graph(graph)  # routes to validate_linear, passes


@pytest.mark.unit
def test_fan_in_carries_join_strategy_metadata(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(_diamond(tmp_path, join="collect", join_key="result"))
    graph = compile_workflow(spec, parent)
    fan_in = [e for e in graph.edges if e.kind.value == "fan_in"]
    assert fan_in and all(e.metadata.get("join") == "collect" for e in fan_in)
    assert all(e.metadata.get("join_key") == "result" for e in fan_in)


# ---------------------------------------------------------------------------
# Spec-level guards (join/join_key are fan-in-only)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_join_on_non_fan_in_edge_rejected(tmp_path: Path) -> None:
    wf = tmp_path / "wf"
    (wf).mkdir(parents=True)
    (wf / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path = wf / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "bad",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "a",
                "nodes": [{"id": "a", "type": "agent", "ref": "./agents/a"}],
                "edges": [{"from": "a", "to": "b", "join": "last_wins"}],
            }
        )
    )
    with pytest.raises(WorkflowSpecLoadError, match=r"join.*only valid on a 'fan_in'"):
        load_workflow_spec(yaml_path)


@pytest.mark.unit
def test_collect_requires_join_key(tmp_path: Path) -> None:
    wf = tmp_path / "wf"
    (wf).mkdir(parents=True)
    (wf / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path = wf / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "bad",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "a",
                "nodes": [{"id": "a", "type": "agent", "ref": "./agents/a"}],
                "edges": [{"from": "a", "to": "b", "kind": "fan_in", "join": "collect"}],
            }
        )
    )
    with pytest.raises(WorkflowSpecLoadError, match=r"collect.*requires.*join_key"):
        load_workflow_spec(yaml_path)


# ---------------------------------------------------------------------------
# DAG-shape rejections
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_dag_rejects_over_max_fanout(tmp_path: Path) -> None:
    wf = tmp_path / "wf"
    n = DEFAULT_MAX_FANOUT + 1
    _make_agent(wf / "agents" / "start", name="start", input_key="text", output_key="seed")
    for i in range(n):
        _make_agent(wf / "agents" / f"b{i}", name=f"b{i}", input_key="seed", output_key=f"o{i}")
    _make_agent(wf / "agents" / "merge", name="merge", input_key="seed", output_key="final")
    nodes = [{"id": "start", "type": "agent", "ref": "./agents/start"}]
    edges = []
    for i in range(n):
        nodes.append({"id": f"b{i}", "type": "agent", "ref": f"./agents/b{i}"})
        edges.append({"from": "start", "to": f"b{i}", "kind": "fan_out"})
        edges.append({"from": f"b{i}", "to": "merge", "kind": "fan_in"})
    nodes.append({"id": "merge", "type": "agent", "ref": "./agents/merge"})
    spec, parent = load_workflow_spec(_write_workflow(wf, nodes=nodes, edges=edges))
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="max_fanout"):
        validate_dag(graph)


@pytest.mark.unit
def test_validate_dag_rejects_non_agent_node(tmp_path: Path) -> None:
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "start", name="start", input_key="text", output_key="seed")
    _make_agent(wf / "agents" / "a", name="a", input_key="seed", output_key="a_out")
    _make_agent(wf / "agents" / "merge", name="merge", input_key="seed", output_key="final")
    nodes = [
        {"id": "start", "type": "agent", "ref": "./agents/start"},
        {"id": "a", "type": "agent", "ref": "./agents/a"},
        {
            "id": "gate",
            "type": "human",
            "prompt": "ok?",
            "output_contract": ["decision"],
        },
        {"id": "merge", "type": "agent", "ref": "./agents/merge"},
    ]
    edges = [
        {"from": "start", "to": "a", "kind": "fan_out"},
        {"from": "start", "to": "gate", "kind": "fan_out"},
        {"from": "a", "to": "merge", "kind": "fan_in"},
        {"from": "gate", "to": "merge", "kind": "fan_in"},
    ]
    spec, parent = load_workflow_spec(_write_workflow(wf, nodes=nodes, edges=edges))
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="only type=agent"):
        validate_dag(graph)


@pytest.mark.unit
def test_validate_dag_rejects_non_reconverging_branches(tmp_path: Path) -> None:
    """Two branches that each end at their own sink (no shared join) are not a
    canonical diamond."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "start", name="start", input_key="text", output_key="seed")
    _make_agent(wf / "agents" / "a", name="a", input_key="seed", output_key="a_out")
    _make_agent(wf / "agents" / "b", name="b", input_key="seed", output_key="b_out")
    nodes = [
        {"id": "start", "type": "agent", "ref": "./agents/start"},
        {"id": "a", "type": "agent", "ref": "./agents/a"},
        {"id": "b", "type": "agent", "ref": "./agents/b"},
    ]
    edges = [
        {"from": "start", "to": "a", "kind": "fan_out"},
        {"from": "start", "to": "b", "kind": "fan_out"},
    ]
    spec, parent = load_workflow_spec(_write_workflow(wf, nodes=nodes, edges=edges))
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="reconverge"):
        validate_dag(graph)


# ---------------------------------------------------------------------------
# Execution on the native runner
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_diamond_runs_concurrently_last_wins(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    spec, parent = load_workflow_spec(_diamond(tmp_path))
    graph = compile_workflow(spec, parent)
    runner = _runner(_DiamondProvider(), storage, pricing)

    # wait_for: if the two branches ran sequentially the barrier never releases.
    result = await asyncio.wait_for(runner.run(graph, initial_state={"text": "seed"}), timeout=5.0)

    assert result.status is WorkflowStatus.SUCCESS
    # Both branch outputs joined into state (last-wins, disjoint keys) + merge.
    assert result.final_state["a_out"] == "A_OUT"
    assert result.final_state["b_out"] == "B_OUT"
    assert result.final_state["final"] == "FINAL"
    # 4 node runs: start + a + b + merge.
    assert {r.node_id for r in result.runs} == {"start", "a", "b", "merge"}
    # Persisted workflow run is SUCCESS with all four child runs joinable.
    rows = await storage.list_runs(workflow_run_id=result.workflow_run_id)
    assert len(rows) == 4


@pytest.mark.unit
async def test_diamond_join_by_key(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    spec, parent = load_workflow_spec(_diamond(tmp_path, join="by_key"))
    graph = compile_workflow(spec, parent)
    runner = _runner(_DiamondProvider(), storage, pricing)
    result = await asyncio.wait_for(runner.run(graph, initial_state={"text": "seed"}), timeout=5.0)
    assert result.status is WorkflowStatus.SUCCESS
    # by_key namespaces each branch's delta under its start-node id.
    assert result.final_state["a"] == {"a_out": "A_OUT"}
    assert result.final_state["b"] == {"b_out": "B_OUT"}


@pytest.mark.unit
async def test_diamond_join_collect(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    # Both branches write the SAME key so collect gathers them into a list.
    spec, parent = load_workflow_spec(
        _diamond(
            tmp_path,
            branch_a_out="result",
            branch_b_out="result",
            join="collect",
            join_key="result",
        )
    )
    graph = compile_workflow(spec, parent)
    runner = _runner(_DiamondProvider(barrier_keys=("result",)), storage, pricing)
    result = await asyncio.wait_for(runner.run(graph, initial_state={"text": "seed"}), timeout=5.0)
    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state["result"] == ["RESULT", "RESULT"]


@pytest.mark.unit
async def test_diamond_branch_failure_is_partial_error(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    spec, parent = load_workflow_spec(_diamond(tmp_path))
    graph = compile_workflow(spec, parent)

    class FailBranchB(BaseLLMProvider):
        name = "failb"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            if "as b_out" in body:
                # Output that violates b's schema (missing required b_out) →
                # the executor records a node failure.
                return CompletionResponse(text='{"wrong": "x"}')
            for key in ("seed", "a_out", "final"):
                if f"as {key}" in body:
                    return CompletionResponse(text=json.dumps({key: key.upper()}))
            return CompletionResponse(text="{}")

        async def stream(self, request):  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text, *, model):  # pragma: no cover
            raise NotImplementedError

    runner = _runner(FailBranchB(), storage, pricing)
    result = await runner.run(graph, initial_state={"text": "seed"})

    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "b"
    # The persisted workflow run reflects the partial failure.
    wf = storage.workflow_runs[-1]
    assert wf.status is WorkflowStatus.ERROR
    assert wf.error_node_id == "b"
