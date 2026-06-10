"""HUMAN-node decision routing (ADR 099): routes/fallback, no LLM classifier.

Covers the full seam, layer by layer:

* spec — ``HumanNodeSpec`` accepts ``routes``/``fallback``/``route_on`` and
  rejects the malformed combinations (fallback required with routes, route_on
  must be in the output_contract, route keys casefold-unique, no orphan
  fallback/route_on);
* helper — ``evaluate_human_route`` exact-match matrix (case, whitespace,
  no-match → fallback, missing/None, non-string values), pure + total;
* compile — route targets + fallback validated like decision-node targets,
  synthetic ``{"synthetic": True, "source": "human-route"}`` CONDITIONAL edges
  injected (deduped; they compose with ADR 098's OR-merge so routes may
  converge on shared sinks), and a PLAIN gate's metadata stays byte-identical
  (no new keys);
* native — ``WorkflowRunner.resume`` routes approve AND reject AND
  prose→fallback through the one shared helper (mirrors the ADR 017 D5 resume
  fixtures in test_workflow_runner.py);
* temporal — the delivered-decision arm emits the routing table +
  ``evaluate_human_route`` call (gated import) when routes are set, and a
  routeless HUMAN node's emission carries none of the new tokens; with a
  durable timeout, the timeout arm is untouched (timeout wins, ADR 099 D4);
* endpoint — ``POST /workflow-runs/{id}/signal`` gains NO routing-value
  validation: an unmatched decision value is accepted (202) and merged
  (routing to fallback happens where the backend resumes), never a 422.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.executor import Executor
from movate.core.models import JobKind, WorkflowRunRecord, WorkflowStatus
from movate.core.workflow import (
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
)
from movate.core.workflow.compiler import WorkflowCompileError, validate_graph
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.decision import evaluate_human_route
from movate.core.workflow.ir import EdgeKind, NodeType, WorkflowEdge, WorkflowGraph, WorkflowNode
from movate.core.workflow.spec import HumanNodeSpec
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.runtime import build_app
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Spec — accept / reject
# ---------------------------------------------------------------------------


def _human(**overrides: Any) -> HumanNodeSpec:
    base: dict[str, Any] = {
        "id": "gate",
        "type": "human",
        "prompt": "Approve?",
        "output_contract": ["decision"],
    }
    base.update(overrides)
    return HumanNodeSpec(**base)


@pytest.mark.unit
def test_spec_accepts_routed_gate_with_defaults() -> None:
    spec = _human(routes={"approve": "ok", "reject": "no"}, fallback="no")
    assert spec.route_on == "decision"  # the ratified default
    assert spec.routes == {"approve": "ok", "reject": "no"}
    assert spec.fallback == "no"


@pytest.mark.unit
def test_spec_accepts_explicit_route_on_in_contract() -> None:
    spec = _human(
        output_contract=["verdict", "approver"],
        routes={"approve": "ok"},
        fallback="no",
        route_on="verdict",
    )
    assert spec.route_on == "verdict"


@pytest.mark.unit
def test_spec_plain_gate_unchanged() -> None:
    spec = _human()
    assert spec.routes is None
    assert spec.fallback is None
    assert spec.route_on == "decision"


@pytest.mark.unit
def test_spec_rejects_routes_without_fallback() -> None:
    with pytest.raises(ValidationError, match=r"'fallback' .*required"):
        _human(routes={"approve": "ok"})


@pytest.mark.unit
def test_spec_rejects_fallback_without_routes() -> None:
    with pytest.raises(ValidationError, match="only valid when 'routes'"):
        _human(fallback="no")


@pytest.mark.unit
def test_spec_rejects_explicit_route_on_without_routes() -> None:
    with pytest.raises(ValidationError, match="only valid when 'routes'"):
        _human(route_on="decision")


@pytest.mark.unit
def test_spec_rejects_route_on_outside_output_contract() -> None:
    # Load-bearing for D3: the signal endpoint's existing output_contract 422
    # is what guarantees a delivered decision carries the routing key.
    with pytest.raises(ValidationError, match=r"must be .*listed in 'output_contract'"):
        _human(routes={"approve": "ok"}, fallback="no", route_on="verdict")


@pytest.mark.unit
def test_spec_rejects_casefold_colliding_route_keys() -> None:
    with pytest.raises(ValidationError, match="collide after trim\\+casefold"):
        _human(routes={"Approve": "ok", " approve ": "no"}, fallback="no")


@pytest.mark.unit
def test_spec_rejects_empty_routes_map() -> None:
    with pytest.raises(ValidationError, match="at least one decision value"):
        _human(routes={}, fallback="no")


# ---------------------------------------------------------------------------
# Helper — evaluate_human_route matrix (pure + total, ADR 099 D2)
# ---------------------------------------------------------------------------

_ROUTES = {"approve": "post-erp", "Reject": "rejected"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("approve", "post-erp"),  # exact
        ("APPROVE", "post-erp"),  # casefold
        ("  Approve \n", "post-erp"),  # trim + casefold
        ("reject", "rejected"),  # key itself is normalized too
        (" REJECT ", "rejected"),
        ("ship it", "fb"),  # prose → fallback
        ("approved, looks fine — Dana", "fb"),  # near-miss prose → fallback
        ("", "fb"),  # empty → fallback
        (None, "fb"),  # missing key → fallback (never str(None))
        (5, "fb"),  # non-string, no match
        (True, "fb"),
        ({"nested": 1}, "fb"),
    ],
)
def test_evaluate_human_route_matrix(value: Any, expected: str) -> None:
    assert evaluate_human_route(_ROUTES, "fb", value) == expected


@pytest.mark.unit
def test_evaluate_human_route_non_string_values_match_stringified() -> None:
    # str(value) before matching: a numeric answer can be a vocabulary entry.
    assert evaluate_human_route({"5": "five"}, "fb", 5) == "five"
    assert evaluate_human_route({"true": "yes"}, "fb", True) == "yes"


@pytest.mark.unit
def test_evaluate_human_route_empty_routes_total() -> None:
    # Pure + total — never raises, even with no vocabulary at all.
    assert evaluate_human_route({}, "fb", "anything") == "fb"


# ---------------------------------------------------------------------------
# Compile — target validation, synthetic edges, metadata stability
# ---------------------------------------------------------------------------

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "text": {"type": "string"},
        "step1": {"type": "string"},
        "decision": {"type": "string"},
        "approve_out": {"type": "string"},
        "reject_out": {"type": "string"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> Path:
    """Minimal agent reading ``input_key``, writing ``output_key`` (mirrors the
    test_workflow_runner.py fixture so the resume tests stay comparable)."""
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
                "type": "object",
                "additionalProperties": True,
                "required": [input_key],
                "properties": {input_key: {"type": "string", "minLength": 1}},
            }
        )
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
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )
    return agent_dir


def _make_workflow(
    workflow_dir: Path,
    *,
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
                "name": "routed-gate",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": entrypoint,
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _scaffold_routed_gate(tmp_path: Path, *, fallback: str = "reject-step") -> Path:
    """first(agent) → gate(human, routes approve|reject) → {approve-step | reject-step}.

    The gate has NO sequential successor — its routes are its only exits,
    exactly the shape the expense-approval workflow now ships.
    """
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        workflow_dir / "agents" / "approve-step",
        name="approve-agent",
        input_key="step1",
        output_key="approve_out",
    )
    _make_agent(
        workflow_dir / "agents" / "reject-step",
        name="reject-agent",
        input_key="step1",
        output_key="reject_out",
    )
    return _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {
                "id": "gate",
                "type": "human",
                "prompt": "Approve? Respond with decision: approve or reject.",
                "output_contract": ["decision"],
                "routes": {"approve": "approve-step", "reject": "reject-step"},
                "fallback": fallback,
            },
            {"id": "approve-step", "type": "agent", "ref": "./agents/approve-step"},
            {"id": "reject-step", "type": "agent", "ref": "./agents/reject-step"},
        ],
        edges=[{"from": "first", "to": "gate"}],
    )


@pytest.mark.unit
def test_compile_stamps_routes_and_injects_synthetic_edges(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(_scaffold_routed_gate(tmp_path))
    graph = compile_workflow(spec, parent)
    validate_graph(graph)  # routed gates pass the phase gate (multiple sinks OK)

    gate = graph.nodes["gate"]
    assert gate.metadata["routes"] == {"approve": "approve-step", "reject": "reject-step"}
    assert gate.metadata["fallback"] == "reject-step"
    assert gate.metadata["route_on"] == "decision"

    route_edges = [
        e for e in graph.edges if e.from_id == "gate" and e.metadata.get("source") == "human-route"
    ]
    # approve-step + reject-step; the fallback (== reject-step) is DEDUPED.
    assert {e.to_id for e in route_edges} == {"approve-step", "reject-step"}
    assert len(route_edges) == 2
    assert all(e.kind is EdgeKind.CONDITIONAL and e.metadata.get("synthetic") for e in route_edges)


@pytest.mark.unit
def test_compile_plain_gate_metadata_byte_identical(tmp_path: Path) -> None:
    """A routeless gate's metadata carries NONE of the new keys (ADR 099 D5)."""
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {
                "id": "gate",
                "type": "human",
                "prompt": "Approve?",
                "output_contract": ["decision"],
            },
        ],
        edges=[{"from": "first", "to": "gate"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    # Byte-identical to the pre-ADR-099 shape: exactly prompt + output_contract.
    assert graph.nodes["gate"].metadata == {
        "prompt": "Approve?",
        "output_contract": ["decision"],
    }
    assert not [e for e in graph.edges if e.metadata.get("source") == "human-route"]


@pytest.mark.unit
def test_compile_rejects_bad_route_target(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {
                "id": "gate",
                "type": "human",
                "prompt": "Approve?",
                "output_contract": ["decision"],
                "routes": {"approve": "nope"},
                "fallback": "first",
            },
        ],
        edges=[{"from": "first", "to": "gate"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="route target 'nope'"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_rejects_bad_fallback_target(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {
                "id": "gate",
                "type": "human",
                "prompt": "Approve?",
                "output_contract": ["decision"],
                "routes": {"approve": "first"},
                "fallback": "missing-node",
            },
        ],
        edges=[{"from": "first", "to": "gate"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="route target 'missing-node'"):
        compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# Native resume — approve / reject / prose→fallback (ADR 099 D2)
# ---------------------------------------------------------------------------


class _PerNodeProvider(BaseLLMProvider):
    """Dispatch on the rendered prompt's ``as <output_key>`` marker (the
    test_workflow_runner.py pattern) so each agent gets its own shape."""

    name = "per_node"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        for key in ("approve_out", "reject_out", "step1"):
            if f"as {key}" in body:
                return CompletionResponse(text=json.dumps({key: f"{key}-val"}))
        return CompletionResponse(text='{"step1": "step1-val"}')

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


async def _pause_then_resume(
    tmp_path: Path,
    storage: InMemoryStorage,
    tracer: NullTracer,
    pricing: PricingTable,
    *,
    decision: Any,
):
    spec, parent = load_workflow_spec(_scaffold_routed_gate(tmp_path))
    graph = compile_workflow(spec, parent)
    executor = Executor(
        provider=_PerNodeProvider(), pricing=pricing, storage=storage, tracer=tracer
    )
    runner = WorkflowRunner(executor=executor, storage=storage)

    paused = await runner.run(graph, initial_state={"text": "seed"})
    assert paused.status is WorkflowStatus.PAUSED

    record = await storage.get_workflow_run(paused.workflow_run_id, tenant_id=runner._tenant_id)
    assert record is not None
    # The signal endpoint merges the decision into paused_state (tested
    # separately); simulate that merge here, exactly like the ADR 017 fixtures.
    record = record.model_copy(
        update={"paused_state": {**(record.paused_state or {}), "decision": decision}}
    )
    return await runner.resume(graph, record)


@pytest.mark.unit
async def test_resume_routes_approve(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    resumed = await _pause_then_resume(tmp_path, storage, tracer, pricing, decision="approve")
    assert resumed.status is WorkflowStatus.SUCCESS
    assert resumed.final_state["approve_out"] == "approve_out-val"
    assert "reject_out" not in resumed.final_state
    assert [r.node_id for r in resumed.runs] == ["approve-step"]


@pytest.mark.unit
async def test_resume_routes_reject_case_and_whitespace_insensitive(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    # " REJECT \n" must route exactly like "reject" — the value is typed by a
    # person (ADR 099 D1's trim+casefold rationale).
    resumed = await _pause_then_resume(tmp_path, storage, tracer, pricing, decision=" REJECT \n")
    assert resumed.status is WorkflowStatus.SUCCESS
    assert resumed.final_state["reject_out"] == "reject_out-val"
    assert "approve_out" not in resumed.final_state
    assert [r.node_id for r in resumed.runs] == ["reject-step"]


@pytest.mark.unit
async def test_resume_prose_routes_to_fallback(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    # Free text matches no route key ⇒ the author's declared fallback — never
    # an error that wedges the run (ADR 099 D1/D3).
    resumed = await _pause_then_resume(
        tmp_path, storage, tracer, pricing, decision="approved, looks fine — Dana"
    )
    assert resumed.status is WorkflowStatus.SUCCESS
    assert resumed.final_state["reject_out"] == "reject_out-val"
    assert "approve_out" not in resumed.final_state
    assert [r.node_id for r in resumed.runs] == ["reject-step"]


# ---------------------------------------------------------------------------
# Temporal emission — golden bits (ADR 099 D2, parity with native)
# ---------------------------------------------------------------------------


def _ir_node(nid: str, ntype: NodeType = NodeType.AGENT, **meta: Any) -> WorkflowNode:
    return WorkflowNode(id=nid, type=ntype, ref=f"/agents/{nid}", metadata=dict(meta))


def _ir_graph(nodes: list[WorkflowNode], edges: list[WorkflowEdge]) -> WorkflowGraph:
    return WorkflowGraph(
        name="test-flow",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint=nodes[0].id,
        nodes={n.id: n for n in nodes},
        edges=edges,
        workflow_dir=Path("/tmp/fake"),
    )


def _route_edge(from_id: str, to_id: str) -> WorkflowEdge:
    return WorkflowEdge(
        from_id=from_id,
        to_id=to_id,
        kind=EdgeKind.CONDITIONAL,
        metadata={"synthetic": True, "source": "human-route"},
    )


def _routed_human(nid: str = "gate", **extra: Any) -> WorkflowNode:
    meta: dict[str, Any] = {
        "prompt": "Approve?",
        "output_contract": ["decision"],
        "routes": {"approve": "ok", "reject": "no"},
        "fallback": "no",
        "route_on": "decision",
    }
    meta.update(extra)
    return WorkflowNode(id=nid, type=NodeType.HUMAN, ref="", metadata=meta)


@pytest.mark.unit
def test_temporal_emits_routing_table_and_helper_call() -> None:
    graph = _ir_graph(
        [_ir_node("start"), _routed_human(), _ir_node("ok"), _ir_node("no")],
        [
            WorkflowEdge(from_id="start", to_id="gate"),
            _route_edge("gate", "ok"),
            _route_edge("gate", "no"),
        ],
    )
    compiled = TemporalCompiler().compile(graph)
    src = compiled.module_source
    # The routing table + the ONE shared helper, fed by the route_on key.
    assert "gate_routes = {'approve': 'ok', 'reject': 'no'}" in src
    assert "current = evaluate_human_route(gate_routes, 'no', state.get('decision'))" in src
    # Gated import rides the passed-through block (sandbox-safe pure helper).
    assert "from movate.core.workflow.decision import evaluate_human_route" in src
    # Still a durable HITL gate: pause record + signal + wait_condition.
    assert "call_human_activity" in compiled.activity_names
    assert "wait_condition(lambda: 'gate' in self._human)" in src
    compile(src, "<emitted-routed-human>", "exec")


@pytest.mark.unit
def test_temporal_routeless_human_emission_has_no_new_tokens() -> None:
    """A routeless gate emits byte-for-byte the prior shape: sequential-successor
    advance, no routing table, no helper import (ADR 099 D5)."""
    human = WorkflowNode(
        id="gate",
        type=NodeType.HUMAN,
        ref="",
        metadata={"prompt": "Approve?", "output_contract": ["decision"]},
    )
    graph = _ir_graph(
        [_ir_node("start"), human, _ir_node("done")],
        [WorkflowEdge(from_id="start", to_id="gate"), WorkflowEdge(from_id="gate", to_id="done")],
    )
    src = TemporalCompiler().compile(graph).module_source
    assert "evaluate_human_route" not in src
    assert "_routes" not in src
    assert "current = 'done'" in src
    compile(src, "<emitted-plain-human>", "exec")


@pytest.mark.unit
def test_temporal_timeout_arm_unchanged_with_routes() -> None:
    """Timeout wins (ADR 099 D4): routes only rewrite the delivered-decision
    (else) arm; the ``except asyncio.TimeoutError`` arm still takes on_timeout."""
    gate = _routed_human(timeout=3600, on_timeout="escalate")
    graph = _ir_graph(
        [_ir_node("start"), gate, _ir_node("ok"), _ir_node("no"), _ir_node("escalate")],
        [
            WorkflowEdge(from_id="start", to_id="gate"),
            _route_edge("gate", "ok"),
            _route_edge("gate", "no"),
            WorkflowEdge(
                from_id="gate",
                to_id="escalate",
                kind=EdgeKind.CONDITIONAL,
                metadata={"synthetic": True, "source": "human-timeout"},
            ),
        ],
    )
    src = TemporalCompiler().compile(graph).module_source
    assert "timeout=timedelta(seconds=3600.0)" in src
    assert "except asyncio.TimeoutError:" in src
    assert "current = 'escalate'" in src  # the timeout route, untouched
    # The delivered-decision arm routes via the helper.
    assert "current = evaluate_human_route(gate_routes, 'no', state.get('decision'))" in src
    compile(src, "<emitted-routed-timeout>", "exec")


@pytest.mark.unit
def test_temporal_decision_and_routed_human_share_one_import_line() -> None:
    """A workflow with BOTH a decision node and a routed gate imports both pure
    helpers through the one passed-through line."""
    decision = WorkflowNode(
        id="classify",
        type=NodeType.DECISION,
        ref="",
        metadata={
            "cases": [{"when": {"field": "amount", "op": "gt", "value": 5}, "to": "gate"}],
            "default": "ok",
        },
    )
    graph = _ir_graph(
        [decision, _routed_human(), _ir_node("ok"), _ir_node("no")],
        [
            WorkflowEdge(
                from_id="classify",
                to_id="gate",
                kind=EdgeKind.CONDITIONAL,
                metadata={"synthetic": True, "source": "decision"},
            ),
            WorkflowEdge(
                from_id="classify",
                to_id="ok",
                kind=EdgeKind.CONDITIONAL,
                metadata={"synthetic": True, "source": "decision"},
            ),
            _route_edge("gate", "ok"),
            _route_edge("gate", "no"),
        ],
    )
    src = TemporalCompiler().compile(graph).module_source
    assert (
        "from movate.core.workflow.decision import evaluate_decision, evaluate_human_route" in src
    )
    compile(src, "<emitted-decision-plus-routed>", "exec")


# ---------------------------------------------------------------------------
# Signal endpoint — unmatched values are ACCEPTED, never 422 (ADR 099 D3)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="hroute-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


@pytest.mark.unit
async def test_signal_unmatched_decision_value_is_202_not_422(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """The endpoint stays a transport (ADR 062 D2): it validates the
    output_contract KEY is present and nothing about the VALUE — prose that
    matches no route key is accepted (202) and the backend routes it to the
    gate's fallback at resume time."""
    auth_header, tenant_id = auth_setup
    await storage.save_workflow_run(
        WorkflowRunRecord(
            workflow_run_id="wf-routed",
            tenant_id=tenant_id,
            workflow="expense-approval",
            workflow_version="0.1.0",
            status=WorkflowStatus.PAUSED,
            initial_state={"text": "seed"},
            final_state={"text": "seed"},
            paused_node_id="manager-approval",
            paused_state={"text": "seed"},
            human_task={"prompt": "Approve?", "output_contract": ["decision"]},
        )
    )

    r = client.post(
        "/api/v1/workflow-runs/wf-routed/signal",
        json={"decision": {"decision": "approved, looks fine — Dana"}},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text  # accepted — NEVER a 422 on the value

    # The continuation job is enqueued and the prose value rode the merge —
    # fallback resolution happens in the backend, not the control plane.
    record = await storage.get_workflow_run("wf-routed", tenant_id=tenant_id)
    assert record is not None
    assert record.paused_state["decision"] == "approved, looks fine — Dana"
    jobs = await storage.list_jobs(tenant_id=tenant_id)
    assert [j.kind for j in jobs if j.resume_workflow_run_id == "wf-routed"] == [JobKind.WORKFLOW]

    # The missing-KEY 422 is unchanged (D1 pins route_on into output_contract,
    # so this existing check is what guards the routing key).
    await storage.save_workflow_run(
        WorkflowRunRecord(
            workflow_run_id="wf-routed-2",
            tenant_id=tenant_id,
            workflow="expense-approval",
            workflow_version="0.1.0",
            status=WorkflowStatus.PAUSED,
            initial_state={},
            final_state={},
            paused_node_id="manager-approval",
            paused_state={},
            human_task={"prompt": "Approve?", "output_contract": ["decision"]},
        )
    )
    r2 = client.post(
        "/api/v1/workflow-runs/wf-routed-2/signal",
        json={"decision": {"note": "no decision key"}},
        headers=auth_header,
    )
    assert r2.status_code == 422, r2.text
