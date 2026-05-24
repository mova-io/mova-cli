"""WorkflowRunner: state threading, persistence, link integrity, partial failure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import JobStatus, WorkflowRunRecord, WorkflowStatus
from movate.core.workflow import (
    WorkflowRunError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "text": {"type": "string"},
        "step1": {"type": "string"},
        "step2": {"type": "string"},
    },
}


def _make_agent(
    agent_dir: Path,
    *,
    name: str,
    input_key: str,
    output_key: str,
) -> Path:
    """Build a minimal agent that reads ``input_key`` and writes ``output_key``."""
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
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
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


def _make_workflow(
    workflow_dir: Path,
    *,
    nodes: list[dict],
    edges: list[dict],
    entrypoint: str = "first",
    state_schema: dict | None = None,
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(state_schema or _STATE_SCHEMA))
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "test-workflow",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": entrypoint,
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _scaffold_two_step(tmp_path: Path) -> Path:
    """text → step1 → step2 (linear, two-node)."""
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        workflow_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    return _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )


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
    response: str,
    storage: InMemoryStorage,
    tracer: NullTracer,
    pricing: PricingTable,
) -> tuple[WorkflowRunner, MockProvider]:
    provider = MockProvider(response=response)
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    runner = WorkflowRunner(executor=executor, storage=storage, tracer=tracer)  # type: ignore[call-arg]
    return runner, provider


def _build_runner_simple(
    storage: InMemoryStorage, tracer: NullTracer, pricing: PricingTable, response: str
) -> WorkflowRunner:
    provider = MockProvider(response=response)
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    return WorkflowRunner(executor=executor, storage=storage)


# ---------------------------------------------------------------------------
# Happy-path two-node pipeline
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_two_node_pipeline_threads_state(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    yaml_path = _scaffold_two_step(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    # MockProvider returns a constant. To make both nodes succeed we'd need
    # different responses, but the v0.3 runner just needs *some* JSON that
    # validates against each node's output schema. Both schemas are different
    # ({"step1": ...} vs {"step2": ...}), so a single-response mock can't
    # satisfy both. Use a provider double that watches the prompt.

    class StateAwareProvider(BaseLLMProvider):
        name = "state_aware"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            # Return whichever output key matches the prompt body.
            body = request.messages[0].content
            if "step1" in body and "step2" not in body:
                return CompletionResponse(text='{"step1": "alpha"}')
            return CompletionResponse(text='{"step2": "beta"}')

        async def stream(self, request):  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text, *, model):  # pragma: no cover
            raise NotImplementedError

    provider = StateAwareProvider()
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    runner = WorkflowRunner(executor=executor, storage=storage)

    result = await runner.run(graph, initial_state={"text": "seed"})

    assert result.status is WorkflowStatus.SUCCESS
    # Both node outputs merged into final state, plus the original "text".
    assert result.final_state == {"text": "seed", "step1": "alpha", "step2": "beta"}
    assert len(result.runs) == 2
    # Per-node runs are stamped with the workflow link + node id.
    assert all(r.workflow_run_id == result.workflow_run_id for r in result.runs)
    assert [r.node_id for r in result.runs] == ["first", "second"]


@pytest.mark.unit
async def test_runner_persists_workflow_run(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    yaml_path = _scaffold_two_step(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    class TwoOutputs(BaseLLMProvider):
        name = "two"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            if "step1" in body and "step2" not in body:
                return CompletionResponse(text='{"step1": "x"}')
            return CompletionResponse(text='{"step2": "y"}')

        async def stream(self, request):  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text, *, model):  # pragma: no cover
            raise NotImplementedError

    executor = Executor(provider=TwoOutputs(), pricing=pricing, storage=storage, tracer=tracer)
    runner = WorkflowRunner(executor=executor, storage=storage)
    result = await runner.run(graph, initial_state={"text": "seed"})

    # One workflow run + two child runs persisted in the in-memory store.
    assert len(storage.workflow_runs) == 1
    wf = storage.workflow_runs[0]
    assert wf.workflow_run_id == result.workflow_run_id
    assert wf.workflow == "test-workflow"
    assert wf.status is WorkflowStatus.SUCCESS
    assert wf.final_state == result.final_state

    # `list_runs(workflow_run_id=...)` returns the per-node runs.
    rows = await storage.list_runs(workflow_run_id=result.workflow_run_id)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Single-node workflow (degenerate but valid)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_single_node_workflow(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "only", name="only-agent", input_key="text", output_key="step1"
    )
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[{"id": "first", "type": "agent", "ref": "./agents/only"}],
        edges=[],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    runner = _build_runner_simple(storage, tracer, pricing, response='{"step1": "ok"}')
    result = await runner.run(graph, initial_state={"text": "hi"})

    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state == {"text": "hi", "step1": "ok"}
    assert len(result.runs) == 1


# ---------------------------------------------------------------------------
# Initial-state schema rejection
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_rejects_invalid_initial_state(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    yaml_path = _scaffold_two_step(tmp_path)
    # Override state schema to require a "text" string field.
    state_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    }
    (yaml_path.parent / "state.json").write_text(json.dumps(state_schema))
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    runner = _build_runner_simple(storage, tracer, pricing, response='{"step1": "x"}')

    with pytest.raises(WorkflowRunError, match="initial_state failed"):
        await runner.run(graph, initial_state={"wrong_field": 123})


# ---------------------------------------------------------------------------
# Mid-pipeline failure preserves partial state + records the failed node
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_partial_failure_at_node_2(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    yaml_path = _scaffold_two_step(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    # First node returns valid output; second node returns garbage that fails
    # its output schema.

    class FailSecond(BaseLLMProvider):
        name = "fail_second"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            if "step1" in body and "step2" not in body:
                return CompletionResponse(text='{"step1": "good"}')
            return CompletionResponse(text='{"oops": "wrong shape"}')

        async def stream(self, request):  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text, *, model):  # pragma: no cover
            raise NotImplementedError

    executor = Executor(provider=FailSecond(), pricing=pricing, storage=storage, tracer=tracer)
    runner = WorkflowRunner(executor=executor, storage=storage)
    result = await runner.run(graph, initial_state={"text": "seed"})

    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "second"
    # Partial state is what the failing node *saw* — i.e. before its
    # output got merged. So step1 is present (from node 1), step2 absent.
    assert "step1" in result.final_state
    assert "step2" not in result.final_state
    # Both per-node RunRecords appear (the second one with status=ERROR).
    assert len(result.runs) == 2
    assert result.runs[0].status is JobStatus.SUCCESS
    assert result.runs[1].status is JobStatus.ERROR
    # Workflow row persisted with ERROR status.
    assert len(storage.workflow_runs) == 1
    assert storage.workflow_runs[0].status is WorkflowStatus.ERROR
    assert storage.workflow_runs[0].error_node_id == "second"


# ---------------------------------------------------------------------------
# HITL pause at a HUMAN gate (ADR 017 D5, PR 1)
# ---------------------------------------------------------------------------


def _scaffold_agent_human_agent(tmp_path: Path) -> Path:
    """text → first(agent: step1) → gate(human) → second(agent: step2).

    The runner should execute ``first``, pause at ``gate``, and NEVER run
    ``second`` (PR 1 stops at the gate; PR 2 resumes from its successor).
    """
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        workflow_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    return _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {
                "id": "gate",
                "type": "human",
                "prompt": "Approve before step 2?",
                "output_contract": ["decision"],
            },
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "gate"}, {"from": "gate", "to": "second"}],
    )


@pytest.mark.unit
async def test_runner_pauses_at_human_gate(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    yaml_path = _scaffold_agent_human_agent(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    runner = _build_runner_simple(storage, tracer, pricing, response='{"step1": "alpha"}')
    result = await runner.run(graph, initial_state={"text": "seed"})

    # The workflow paused — not SUCCESS, not ERROR.
    assert result.status is WorkflowStatus.PAUSED
    # State reflects the post-first-agent merge (step1 present), and step2 is
    # absent because the second agent NEVER ran.
    assert result.final_state == {"text": "seed", "step1": "alpha"}
    assert "step2" not in result.final_state
    # Exactly one node executed (the first agent) — the gate executed nothing.
    assert len(result.runs) == 1
    assert result.runs[0].node_id == "first"

    # The durable checkpoint persisted with PAUSED status + the full handle PR 2
    # resumes from.
    assert len(storage.workflow_runs) == 1
    wf = storage.workflow_runs[0]
    assert wf.workflow_run_id == result.workflow_run_id
    assert wf.status is WorkflowStatus.PAUSED
    assert wf.paused_node_id == "gate"
    assert wf.paused_state == {"text": "seed", "step1": "alpha"}
    assert wf.human_task == {
        "prompt": "Approve before step 2?",
        "output_contract": ["decision"],
    }
    # Tenant correctly stamped on the paused record.
    assert wf.tenant_id == runner._tenant_id

    # The second agent did NOT run: no per-node RunRecord exists for it past the
    # gate. (Only the first agent's executor-persisted row is present.)
    rows = await storage.list_runs(workflow_run_id=result.workflow_run_id)
    assert {r.node_id for r in rows} == {"first"}
    assert all(r.node_id != "second" for r in rows)


@pytest.mark.unit
async def test_human_free_workflow_unchanged_regression(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """No-regression guard: a workflow with NO human gate still runs to SUCCESS
    exactly as before — the pause branch is inert when no gate is present."""
    yaml_path = _scaffold_two_step(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    class TwoOutputs(BaseLLMProvider):
        name = "two"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            if "step1" in body and "step2" not in body:
                return CompletionResponse(text='{"step1": "x"}')
            return CompletionResponse(text='{"step2": "y"}')

        async def stream(self, request):  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text, *, model):  # pragma: no cover
            raise NotImplementedError

    executor = Executor(provider=TwoOutputs(), pricing=pricing, storage=storage, tracer=tracer)
    runner = WorkflowRunner(executor=executor, storage=storage)
    result = await runner.run(graph, initial_state={"text": "seed"})

    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state == {"text": "seed", "step1": "x", "step2": "y"}
    assert len(result.runs) == 2
    # The persisted record is a plain SUCCESS run with no checkpoint fields set.
    assert len(storage.workflow_runs) == 1
    wf = storage.workflow_runs[0]
    assert wf.status is WorkflowStatus.SUCCESS
    assert wf.paused_node_id is None
    assert wf.paused_state is None
    assert wf.human_task is None


# ---------------------------------------------------------------------------
# State projection: only schema-named keys reach the agent
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_runner_filters_state_to_agent_input_schema(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """First node's input schema only declares 'text'; runner must drop
    'extra_garbage' from the projection so the agent's schema validator
    doesn't reject the call.
    """
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "only",
        name="only-agent",
        input_key="text",
        output_key="step1",
    )
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[{"id": "first", "type": "agent", "ref": "./agents/only"}],
        edges=[],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    runner = _build_runner_simple(storage, tracer, pricing, response='{"step1": "ok"}')

    result = await runner.run(
        graph,
        initial_state={"text": "hi", "extra_garbage": "ignored"},
    )

    assert result.status is WorkflowStatus.SUCCESS
    # Final state still carries the extra_garbage (passes through, never merged
    # over) plus the new step1.
    assert result.final_state["extra_garbage"] == "ignored"
    assert result.final_state["step1"] == "ok"


# ---------------------------------------------------------------------------
# HITL resume from a paused gate (ADR 017 D5, PR 2)
# ---------------------------------------------------------------------------


class _PerNodeProvider(BaseLLMProvider):
    """Returns whichever output key matches the prompt body.

    Each agent's prompt.md is ``echo {{ input.<in> }} as <out>``, so the
    rendered prompt names the output key. We dispatch on the LAST matching
    output key present in the prompt so a multi-node workflow (step1/step2/
    step3) gets the right shape at every node.
    """

    name = "per_node"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        # The prompt says "echo {{ input.X }} as stepN" — pick stepN.
        for key in ("step3", "step2", "step1"):
            if f"as {key}" in body:
                return CompletionResponse(text=json.dumps({key: f"{key}-out"}))
        return CompletionResponse(text='{"step1": "step1-out"}')

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


def _build_per_node_runner(storage: InMemoryStorage, tracer: NullTracer, pricing: PricingTable):
    executor = Executor(
        provider=_PerNodeProvider(), pricing=pricing, storage=storage, tracer=tracer
    )
    return WorkflowRunner(executor=executor, storage=storage)


@pytest.mark.unit
async def test_resume_completes_workflow(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """agent -> human -> agent: pause at the gate (PR 1), then resume with the
    human decision merged -> the SECOND agent runs and the workflow reaches
    SUCCESS. Final state reflects both the human decision and the 2nd output."""
    yaml_path = _scaffold_agent_human_agent(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    runner = _build_per_node_runner(storage, tracer, pricing)
    paused = await runner.run(graph, initial_state={"text": "seed"})
    assert paused.status is WorkflowStatus.PAUSED

    # The endpoint would merge the human decision into paused_state. Simulate
    # that here (the endpoint is tested separately).
    record = await storage.get_workflow_run(paused.workflow_run_id, tenant_id=runner._tenant_id)
    assert record is not None
    record = record.model_copy(
        update={"paused_state": {**(record.paused_state or {}), "decision": "approve"}}
    )

    resumed = await runner.resume(graph, record)

    assert resumed.status is WorkflowStatus.SUCCESS
    # Same workflow_run_id — a resume UPDATES the existing run, not a new one.
    assert resumed.workflow_run_id == paused.workflow_run_id
    # The 2nd agent ran (step2 present) and the human decision is in final state.
    assert resumed.final_state["step1"] == "step1-out"
    assert resumed.final_state["step2"] == "step2-out"
    assert resumed.final_state["decision"] == "approve"
    # Only the second agent ran during resume (the runs view starts fresh).
    assert [r.node_id for r in resumed.runs] == ["second"]

    # One workflow_run row (upserted, not duplicated), now SUCCESS with the
    # checkpoint fields cleared.
    rows = await storage.list_workflow_runs(tenant_id=runner._tenant_id)
    assert len(rows) == 1
    final = rows[0]
    assert final.status is WorkflowStatus.SUCCESS
    assert final.paused_node_id is None


@pytest.mark.unit
async def test_resume_guards_non_paused_and_no_gate(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """resume() raises a clear error if the record isn't PAUSED or has no gate."""
    yaml_path = _scaffold_agent_human_agent(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    runner = _build_per_node_runner(storage, tracer, pricing)

    # status SUCCESS (terminal) -> not resumable.
    terminal = WorkflowRunRecord(
        workflow_run_id="wf-1",
        tenant_id=runner._tenant_id,
        workflow=graph.name,
        workflow_version=graph.version,
        status=WorkflowStatus.SUCCESS,
        initial_state={"text": "x"},
        final_state={"text": "x"},
    )
    with pytest.raises(WorkflowRunError, match=r"expected 'paused'"):
        await runner.resume(graph, terminal)

    # PAUSED but no paused_node_id -> not resumable.
    no_gate = WorkflowRunRecord(
        workflow_run_id="wf-2",
        tenant_id=runner._tenant_id,
        workflow=graph.name,
        workflow_version=graph.version,
        status=WorkflowStatus.PAUSED,
        initial_state={"text": "x"},
        paused_state={"text": "x"},
    )
    with pytest.raises(WorkflowRunError, match=r"no .*paused_node_id"):
        await runner.resume(graph, no_gate)


def _scaffold_multi_gate(tmp_path: Path) -> Path:
    """text -> first(step1) -> gate1(human) -> second(step2) -> gate2(human)
    -> third(step3). Resuming gate1 should re-pause at gate2."""
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        workflow_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    _make_agent(
        workflow_dir / "agents" / "third",
        name="third-agent",
        input_key="step2",
        output_key="step3",
    )
    return _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "gate1", "type": "human", "prompt": "gate 1?", "output_contract": ["d1"]},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
            {"id": "gate2", "type": "human", "prompt": "gate 2?", "output_contract": ["d2"]},
            {"id": "third", "type": "agent", "ref": "./agents/third"},
        ],
        edges=[
            {"from": "first", "to": "gate1"},
            {"from": "gate1", "to": "second"},
            {"from": "second", "to": "gate2"},
            {"from": "gate2", "to": "third"},
        ],
    )


@pytest.mark.unit
async def test_resume_multi_gate_repauses_at_next_gate(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """5-node multi-gate: run pauses at gate1; resuming gate1 runs `second`
    and RE-PAUSES at gate2 (a fresh PAUSED checkpoint with the right
    paused_node_id). Resuming gate2 then runs `third` to SUCCESS."""
    yaml_path = _scaffold_multi_gate(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    runner = _build_per_node_runner(storage, tracer, pricing)

    # 1) Run -> pause at gate1.
    p1 = await runner.run(graph, initial_state={"text": "seed"})
    assert p1.status is WorkflowStatus.PAUSED
    rec1 = await storage.get_workflow_run(p1.workflow_run_id, tenant_id=runner._tenant_id)
    assert rec1 is not None and rec1.paused_node_id == "gate1"

    # 2) Resume gate1 with d1 -> runs `second` -> re-pauses at gate2.
    rec1 = rec1.model_copy(update={"paused_state": {**(rec1.paused_state or {}), "d1": "ok1"}})
    p2 = await runner.resume(graph, rec1)
    assert p2.status is WorkflowStatus.PAUSED
    assert p2.workflow_run_id == p1.workflow_run_id  # same run
    rec2 = await storage.get_workflow_run(p1.workflow_run_id, tenant_id=runner._tenant_id)
    assert rec2 is not None
    assert rec2.paused_node_id == "gate2"
    assert rec2.human_task == {"prompt": "gate 2?", "output_contract": ["d2"]}
    # The merged state carried forward: step1 + step2 + d1 captured at gate2.
    assert rec2.paused_state == {
        "text": "seed",
        "step1": "step1-out",
        "d1": "ok1",
        "step2": "step2-out",
    }
    # Still exactly one workflow_run row (upserted across both pauses).
    assert len(await storage.list_workflow_runs(tenant_id=runner._tenant_id)) == 1

    # 3) Resume gate2 with d2 -> runs `third` -> SUCCESS.
    rec2 = rec2.model_copy(update={"paused_state": {**(rec2.paused_state or {}), "d2": "ok2"}})
    done = await runner.resume(graph, rec2)
    assert done.status is WorkflowStatus.SUCCESS
    assert done.final_state["step3"] == "step3-out"
    assert done.final_state["d1"] == "ok1"
    assert done.final_state["d2"] == "ok2"
