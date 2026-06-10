"""Exclusive convergence / OR-merge (ADR 098) — validator + conformance tests.

Covers:
 1. The accept matrix — joins whose every inbound edge is a mutually exclusive
    branch leg now pass ``validate_linear``: two routing legs onto one sink,
    a routing leg converging with an exclusive sequential tail, and two
    exclusive tails onto one shared sink.
 2. The reject matrix — a plain agent fan-out converging still fails (the
    branch guard), a join fed by a non-exclusive sequential edge fails the
    per-join rule with the offending edge named, and fan-in mixed with other
    edge kinds at a join still fails behind ``validate_dag`` (the barrier path
    stays separate — ADR 092).
 3. HUMAN ``on_timeout`` (the bundled ADR 062 D4 fix) — a bad target now fails
    at compile time; a good target injects a synthetic CONDITIONAL edge
    (``source: human-timeout``) that makes timeout-only continuations
    reachable and convergence-eligible.
 4. Native ≡ Temporal conformance over a CONVERGED workflow (a decision
    routing two ways, both legs ending in ONE shared sink): the native runner
    executes the chosen leg + the shared sink; the Temporal compiler emits ONE
    dispatch arm for the shared sink reached from both legs; the full
    time-skipping execution parity run is the smoke-marked proof-in-CI.
 5. LangGraph export smoke — the converged graph exports with the shared node
    added once and one ``add_edge`` per exclusive tail.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import Metrics, RunResponse, TokenUsage, WorkflowStatus
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.compiler import validate_graph
from movate.core.workflow.compilers.langgraph import compile_langgraph
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.ir import (
    EdgeKind,
    NodeType,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Scaffolding (mirrors tests/test_workflow_decision.py)
# ---------------------------------------------------------------------------

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "amount": {"type": "number"},
        "notice": {"type": "string"},
        "posted": {"type": "string"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, output_key: str = "notice") -> None:
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
    (agent_dir / "prompt.md").write_text(f"As {name} write {output_key}.\n")
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


def _write_workflow(wf_dir: Path, body: dict[str, Any]) -> Path:
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    base: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "test-joins",
        "version": "0.1.0",
        "state_schema": "./state.json",
        **body,
    }
    (wf_dir / "workflow.yaml").write_text(yaml.safe_dump(base))
    return wf_dir / "workflow.yaml"


def _decision(nid: str, cases: list[dict[str, Any]], default: str) -> dict[str, Any]:
    return {"id": nid, "type": "decision", "cases": cases, "default": default}


def _case(field: str, op: str, value: Any, to: str) -> dict[str, Any]:
    return {"when": {"field": field, "op": op, "value": value}, "to": to}


def _make_converged_workflow(wf_dir: Path) -> Path:
    """``classify ⇒ {notify-director | notify-manager} → shared-post`` — a
    decision routes two ways and BOTH legs end in one shared sink (two
    exclusive sequential tails, ADR 098 clause (b))."""
    _make_agent(wf_dir / "agents" / "director", name="notify-dir-agent")
    _make_agent(wf_dir / "agents" / "manager", name="notify-mgr-agent")
    _make_agent(wf_dir / "agents" / "post", name="post-agent", output_key="posted")
    return _write_workflow(
        wf_dir,
        {
            "entrypoint": "classify",
            "nodes": [
                _decision(
                    "classify", [_case("amount", "gt", 5000, "notify-director")], "notify-manager"
                ),
                {"id": "notify-director", "type": "agent", "ref": "./agents/director"},
                {"id": "notify-manager", "type": "agent", "ref": "./agents/manager"},
                {"id": "shared-post", "type": "agent", "ref": "./agents/post"},
            ],
            "edges": [
                {"from": "notify-director", "to": "shared-post"},
                {"from": "notify-manager", "to": "shared-post"},
            ],
        },
    )


# ---------------------------------------------------------------------------
# 1. Accept matrix
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_two_routing_legs_converge_on_shared_sink(tmp_path: Path) -> None:
    """Two DECISION legs (different sources) onto one sink — clause (a) only."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "work-a", name="work-a-agent")
    _make_agent(wf / "agents" / "work-b", name="work-b-agent")
    _make_agent(wf / "agents" / "sink", name="sink-agent")
    yaml_path = _write_workflow(
        wf,
        {
            "entrypoint": "d0",
            "nodes": [
                _decision("d0", [_case("amount", "gt", 100, "d1")], "d2"),
                _decision("d1", [_case("amount", "gt", 1000, "work-a")], "shared-sink"),
                _decision("d2", [_case("amount", "lt", 0, "work-b")], "shared-sink"),
                {"id": "work-a", "type": "agent", "ref": "./agents/work-a"},
                {"id": "work-b", "type": "agent", "ref": "./agents/work-b"},
                {"id": "shared-sink", "type": "agent", "ref": "./agents/sink"},
            ],
            "edges": [],
        },
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    inbound = graph.predecessors("shared-sink")
    assert len(inbound) == 2
    assert all(e.kind is EdgeKind.CONDITIONAL and e.metadata.get("synthetic") for e in inbound)
    validate_linear(graph)  # must not raise
    validate_graph(graph)  # the dispatcher routes here too (no parallel edges)


@pytest.mark.unit
def test_routing_leg_and_exclusive_tail_converge(tmp_path: Path) -> None:
    """A DECISION default leg converging with per-branch tail work — mixed
    clause (a) + clause (b). Also: a case and the default naming the SAME
    target dedupe to one synthetic edge (no join at all)."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "notify", name="notify-agent")
    _make_agent(wf / "agents" / "sink", name="sink-agent")
    yaml_path = _write_workflow(
        wf,
        {
            "entrypoint": "classify",
            "nodes": [
                _decision("classify", [_case("amount", "gt", 100, "notify")], "shared-sink"),
                {"id": "notify", "type": "agent", "ref": "./agents/notify"},
                {"id": "shared-sink", "type": "agent", "ref": "./agents/sink"},
            ],
            "edges": [{"from": "notify", "to": "shared-sink"}],
        },
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    kinds = {(e.kind, bool(e.metadata.get("synthetic"))) for e in graph.predecessors("shared-sink")}
    assert kinds == {(EdgeKind.CONDITIONAL, True), (EdgeKind.SEQUENTIAL, False)}
    validate_linear(graph)  # must not raise

    # Dedupe: case + default both → one target collapse to ONE synthetic edge.
    dedupe_wf = tmp_path / "wf-dedupe"
    _make_agent(dedupe_wf / "agents" / "sink", name="sink-agent")
    spec2, parent2 = load_workflow_spec(
        _write_workflow(
            dedupe_wf,
            {
                "entrypoint": "classify",
                "nodes": [
                    _decision("classify", [_case("amount", "gt", 100, "only")], "only"),
                    {"id": "only", "type": "agent", "ref": "./agents/sink"},
                ],
                "edges": [],
            },
        )
    )
    graph2 = compile_workflow(spec2, parent2)
    assert len(graph2.predecessors("only")) == 1
    validate_linear(graph2)


@pytest.mark.unit
def test_two_exclusive_tails_converge_on_shared_sink(tmp_path: Path) -> None:
    """Both decision legs do per-branch work, then converge — clause (b) only."""
    spec, parent = load_workflow_spec(_make_converged_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)
    inbound = graph.predecessors("shared-post")
    assert len(inbound) == 2
    assert all(e.kind is EdgeKind.SEQUENTIAL and not e.metadata.get("synthetic") for e in inbound)
    validate_linear(graph)  # must not raise
    validate_graph(graph)


# ---------------------------------------------------------------------------
# 2. Reject matrix
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plain_agent_branch_converging_rejected(tmp_path: Path) -> None:
    """A plain agent fanning out (then converging) is NOT exclusive — both
    legs would execute. Still rejected (by the branch guard, which fires
    before the join is even considered)."""
    wf = tmp_path / "wf"
    for name in ("entry", "left", "right", "sink"):
        _make_agent(wf / "agents" / name, name=f"{name}-agent")
    yaml_path = _write_workflow(
        wf,
        {
            "entrypoint": "entry",
            "nodes": [
                {"id": "entry", "type": "agent", "ref": "./agents/entry"},
                {"id": "left", "type": "agent", "ref": "./agents/left"},
                {"id": "right", "type": "agent", "ref": "./agents/right"},
                {"id": "sink", "type": "agent", "ref": "./agents/sink"},
            ],
            "edges": [
                {"from": "entry", "to": "left"},
                {"from": "entry", "to": "right"},
                {"from": "left", "to": "sink"},
                {"from": "right", "to": "sink"},
            ],
        },
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="forbids branches"):
        validate_linear(graph)


@pytest.mark.unit
def test_join_with_non_exclusive_inbound_names_offender() -> None:
    """A join fed by a sequential edge whose source has >1 non-synthetic
    successor fails the per-join rule, naming the join node AND the
    disqualifying inbound edge. (DECISION source — exempt from the branch
    guard — isolates the join rule itself.)"""
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="d",
        nodes={
            "d": WorkflowNode(id="d", type=NodeType.DECISION, ref=""),
            "x": WorkflowNode(id="x", type=NodeType.AGENT, ref="/x"),
            "j": WorkflowNode(id="j", type=NodeType.AGENT, ref="/j"),
        },
        edges=[
            WorkflowEdge(from_id="d", to_id="j"),
            WorkflowEdge(from_id="d", to_id="x"),
            WorkflowEdge(from_id="x", to_id="j"),
        ],
        workflow_dir=Path("/"),
    )
    with pytest.raises(WorkflowCompileError) as ei:
        validate_linear(g)
    msg = str(ei.value)
    assert "join at node 'j'" in msg
    assert "'d'→'j'" in msg
    assert "fan_in" in msg  # points at the barrier primitive (ADR 092)


@pytest.mark.unit
def test_fan_in_mixed_with_sequential_at_join_rejected(tmp_path: Path) -> None:
    """A join mixing a barrier ``fan_in`` edge with another kind stays
    rejected — that graph routes to ``validate_dag`` (ADR 092), whose
    kind-homogeneity rule is the OR-merge/barrier discriminator (ADR 098 D3)."""
    wf = tmp_path / "wf"
    for name in ("entry", "a", "b", "sink"):
        _make_agent(wf / "agents" / name, name=f"{name}-agent")
    yaml_path = _write_workflow(
        wf,
        {
            "entrypoint": "entry",
            "nodes": [
                {"id": "entry", "type": "agent", "ref": "./agents/entry"},
                {"id": "a", "type": "agent", "ref": "./agents/a"},
                {"id": "b", "type": "agent", "ref": "./agents/b"},
                {"id": "sink", "type": "agent", "ref": "./agents/sink"},
            ],
            "edges": [
                {"from": "entry", "to": "a", "kind": "fan_out"},
                {"from": "entry", "to": "b", "kind": "fan_out"},
                {"from": "a", "to": "sink", "kind": "fan_in"},
                {"from": "b", "to": "sink", "kind": "sequential"},
            ],
        },
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="mixes fan-in"):
        validate_graph(graph)


# ---------------------------------------------------------------------------
# 3. HUMAN on_timeout — compile-time validation + synthetic edge (ADR 098)
# ---------------------------------------------------------------------------


def _make_human_timeout_workflow(wf_dir: Path, *, on_timeout: str) -> Path:
    """``first → [HUMAN approval] → second`` with ``escalate`` reachable only
    via the approval gate's timeout route."""
    _make_agent(wf_dir / "agents" / "first", name="first-agent")
    _make_agent(wf_dir / "agents" / "second", name="second-agent")
    _make_agent(wf_dir / "agents" / "escalate", name="escalate-agent")
    return _write_workflow(
        wf_dir,
        {
            "entrypoint": "first",
            "nodes": [
                {"id": "first", "type": "agent", "ref": "./agents/first"},
                {
                    "id": "approval",
                    "type": "human",
                    "prompt": "Approve?",
                    "output_contract": ["decision"],
                    "timeout": 3600,
                    "on_timeout": on_timeout,
                },
                {"id": "second", "type": "agent", "ref": "./agents/second"},
                {"id": "escalate", "type": "agent", "ref": "./agents/escalate"},
            ],
            "edges": [
                {"from": "first", "to": "approval"},
                {"from": "approval", "to": "second"},
                {"from": "second", "to": "escalate"},
            ],
        },
    )


@pytest.mark.unit
def test_on_timeout_bad_target_fails_at_compile(tmp_path: Path) -> None:
    """A typo'd on_timeout target fails compile_workflow — not the Nth run as
    the Temporal dispatch loop's 'unknown workflow node'."""
    spec, parent = load_workflow_spec(
        _make_human_timeout_workflow(tmp_path / "wf", on_timeout="does-not-exist")
    )
    with pytest.raises(WorkflowCompileError, match="on_timeout target 'does-not-exist'"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_on_timeout_injects_synthetic_edge_and_reachability(tmp_path: Path) -> None:
    """A valid on_timeout target gets a synthetic CONDITIONAL edge (source:
    human-timeout) — a timeout-ONLY continuation is now reachable, the gate's
    extra leg doesn't trip the branch guard, and metadata is unchanged."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "first", name="first-agent")
    _make_agent(wf / "agents" / "second", name="second-agent")
    _make_agent(wf / "agents" / "escalate", name="escalate-agent")
    yaml_path = _write_workflow(
        wf,
        {
            "entrypoint": "first",
            "nodes": [
                {"id": "first", "type": "agent", "ref": "./agents/first"},
                {
                    "id": "approval",
                    "type": "human",
                    "prompt": "Approve?",
                    "output_contract": ["decision"],
                    "timeout": 3600,
                    "on_timeout": "escalate",
                },
                {"id": "second", "type": "agent", "ref": "./agents/second"},
                # Reachable ONLY via the timeout route — pre-ADR 098 this node
                # failed the reachability check (no edge was injected).
                {"id": "escalate", "type": "agent", "ref": "./agents/escalate"},
            ],
            "edges": [
                {"from": "first", "to": "approval"},
                {"from": "approval", "to": "second"},
            ],
        },
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    timeout_edges = [
        e for e in graph.successors("approval") if e.metadata.get("source") == "human-timeout"
    ]
    assert len(timeout_edges) == 1
    edge = timeout_edges[0]
    assert edge.to_id == "escalate"
    assert edge.kind is EdgeKind.CONDITIONAL
    assert edge.metadata.get("synthetic") is True
    # Durable-HITL metadata is unchanged (the Temporal compiler's contract).
    assert graph.nodes["approval"].metadata["timeout"] == 3600
    assert graph.nodes["approval"].metadata["on_timeout"] == "escalate"
    validate_linear(graph)  # gate + timeout leg is not a "branch"


@pytest.mark.unit
def test_on_timeout_leg_converges_with_sequential_successor(tmp_path: Path) -> None:
    """on_timeout pointing at the gate's own sequential successor — the join
    (one sequential + one synthetic leg, same source) is convergence-legal."""
    spec, parent = load_workflow_spec(
        _make_human_timeout_workflow(tmp_path / "wf", on_timeout="second")
    )
    graph = compile_workflow(spec, parent)
    assert len(graph.predecessors("second")) == 2
    validate_linear(graph)  # must not raise


# ---------------------------------------------------------------------------
# 4. Conformance — native run + Temporal compile over a converged workflow
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


_REPLIES: dict[str, dict[str, str]] = {
    "notify-dir-agent": {"notice": "director-notice"},
    "notify-mgr-agent": {"notice": "manager-notice"},
    "post-agent": {"posted": "posted-ok"},
}


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.mark.unit
@pytest.mark.parametrize(
    ("amount", "expected_leg", "expected_notice"),
    [
        (6000, "notify-dir-agent", "director-notice"),
        (100, "notify-mgr-agent", "manager-notice"),
    ],
)
async def test_native_converged_runs_chosen_leg_then_shared_sink(
    tmp_path: Path,
    storage: InMemoryStorage,
    amount: int,
    expected_leg: str,
    expected_notice: str,
) -> None:
    spec, parent = load_workflow_spec(_make_converged_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)
    validate_linear(graph)

    async def fake_execute(bundle: Any, request: Any, **kwargs: Any) -> RunResponse:
        return _ok(dict(_REPLIES[bundle.spec.name]))

    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute = AsyncMock(side_effect=fake_execute)
    runner = WorkflowRunner(executor=mock_executor, storage=storage)
    result = await runner.run(graph, initial_state={"amount": amount}, mock=False)

    assert result.status is WorkflowStatus.SUCCESS
    # The chosen leg ran, the OTHER leg did not, and the SHARED sink ran once.
    assert [r.agent for r in result.runs] == [expected_leg, "post-agent"]
    assert result.final_state.get("notice") == expected_notice
    assert result.final_state.get("posted") == "posted-ok"


@pytest.mark.unit
def test_temporal_compiles_converged_graph_single_shared_arm(tmp_path: Path) -> None:
    """The Temporal compiler emits ONE dispatch arm for the shared sink and
    one ``current = 'shared-post'`` assignment per exclusive tail — two arms
    assigning the same target is exactly how exclusive legs share a node
    (ADR 098 D2: zero runtime change)."""
    spec, parent = load_workflow_spec(_make_converged_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)
    result = TemporalCompiler().compile(graph)
    src = result.module_source
    ast.parse(src)
    # One dispatch arm per NODE — never per inbound edge.
    assert src.count("current == 'shared-post'") == 1
    # Both exclusive tails advance to the shared sink.
    assert src.count("current = 'shared-post'") == 2
    # The decision routes inline (no gate activity), agents via activities.
    assert "evaluate_decision(" in src
    assert set(result.activity_names) == {"call_agent_activity", "persist_workflow_result_activity"}


@pytest.mark.smoke
@pytest.mark.parametrize("amount", [6000, 100])
async def test_temporal_converged_matches_native(tmp_path: Path, amount: int) -> None:
    """Full conformance (ADR 055 D7 pattern): the converged workflow runs on
    the Temporal time-skipping test env and reaches the SAME final state the
    native runner does — for BOTH exclusive legs into the shared sink."""
    temporalio = pytest.importorskip(
        "temporalio", reason="the [temporal] extra is not installed; conformance smoke skipped"
    )
    assert temporalio is not None
    from temporalio.testing import WorkflowEnvironment  # noqa: PLC0415
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: PLC0415

    from movate.core.workflow.temporal_activities import (  # noqa: PLC0415
        call_agent_activity,
        configure_activities,
        persist_workflow_result_activity,
    )
    from movate.providers.base import (  # noqa: PLC0415
        BaseLLMProvider,
        CompletionRequest,
        CompletionResponse,
    )
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.runtime.workflow_backend import (  # noqa: PLC0415
        DEFAULT_TASK_QUEUE,
        load_compiled_workflow_class,
    )
    from movate.testing import NullTracer  # noqa: PLC0415

    class _LegAwareProvider(BaseLLMProvider):
        """Deterministic per-agent provider — identical on native + Temporal."""

        name = "leg_aware"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            for agent_name, reply in _REPLIES.items():
                if agent_name in body:
                    return CompletionResponse(text=json.dumps(reply))
            return CompletionResponse(text="{}")  # pragma: no cover

        async def stream(self, request: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
            raise NotImplementedError

    pricing = load_pricing()
    initial_state = {"amount": amount}
    spec, parent = load_workflow_spec(_make_converged_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)

    # --- NATIVE baseline --------------------------------------------------
    native_storage = InMemoryStorage()
    await native_storage.init()
    native_runner = WorkflowRunner(
        executor=Executor(
            provider=_LegAwareProvider(),
            pricing=pricing,
            storage=native_storage,
            tracer=NullTracer(),
        ),
        storage=native_storage,
    )
    native_result = await native_runner.run(graph, initial_state=dict(initial_state))
    assert native_result.status is WorkflowStatus.SUCCESS
    assert native_result.final_state.get("posted") == "posted-ok"

    # --- TEMPORAL via the in-memory time-skipping env ----------------------
    temporal_storage = InMemoryStorage()
    await temporal_storage.init()
    configure_activities(
        storage=temporal_storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_LegAwareProvider(),
        tenant_id="local",
    )
    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )
    env = await WorkflowEnvironment.start_time_skipping()
    async with (
        env,
        Worker(
            env.client,
            task_queue=DEFAULT_TASK_QUEUE,
            workflows=[workflow_cls],
            activities=[call_agent_activity, persist_workflow_result_activity],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        temporal_final = await env.client.execute_workflow(
            workflow_cls.run,
            {**initial_state, "tenant_id": "local"},
            id=f"converged-conformance-{amount}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
    temporal_final.pop("tenant_id", None)
    assert temporal_final == native_result.final_state


# ---------------------------------------------------------------------------
# 5. LangGraph export smoke
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_langgraph_export_converged_smoke(tmp_path: Path) -> None:
    """The converged graph exports to LangGraph: the shared sink is added
    ONCE, with one ``add_edge`` per exclusive tail (idiomatic LangGraph —
    only the taken leg triggers the node)."""
    spec, parent = load_workflow_spec(_make_converged_workflow(tmp_path / "wf"))
    graph = compile_workflow(spec, parent)
    source = compile_langgraph(graph)
    ast.parse(source)
    assert source.count("add_node('shared-post'") == 1
    assert "add_edge('notify-director', 'shared-post')" in source
    assert "add_edge('notify-manager', 'shared-post')" in source
