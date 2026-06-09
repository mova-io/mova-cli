"""ADR 092 D4 — the bounded SUPERVISOR (managerial delegation) primitive.

Proves a supervisor workflow:

* authors + compiles + validates (a single linear node; the delegation loop is
  internal so the graph stays acyclic),
* RUNS on the native runner — the manager delegates to a FIXED specialist
  allowlist, merging each specialist's output into state, until it decides
  "done",
* is bounded by ``max_delegations`` (a manager that never says done still
  terminates) and by the allowlist (an out-of-roster choice ends the loop),
* propagates a manager/specialist failure as a partial ERROR,
* and rejects malformed specs (bad ref, reserved 'done' id) at compile/parse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from movate.cli.validate import _print_workflow_governance
from movate.core.executor import Executor
from movate.core.models import TokenUsage, WorkflowStatus
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
    validate_graph,
)
from movate.core.workflow.spec import WorkflowSpecLoadError
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {"task": {"type": "string"}},
}


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> Path:
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
    # Inputs are NOT required — a supervisor specialist may run before the key
    # it reads has been produced (the manager decides ordering), so a missing
    # input must not be an input-schema failure.
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "properties": {input_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "required": [output_key],
                "properties": {output_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )
    return agent_dir


def _write_supervisor_workflow(
    tmp_path: Path,
    *,
    specialists: dict[str, str] | None = None,
    max_delegations: int = 4,
    manager_ref: str = "./agents/manager",
    with_finalize: bool = True,
    budget: float | None = None,
) -> Path:
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "manager", name="mgr", input_key="task", output_key="next")
    _make_agent(wf / "agents" / "researcher", name="rsr", input_key="task", output_key="findings")
    _make_agent(wf / "agents" / "finalize", name="fin", input_key="findings", output_key="answer")
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    supervisor: dict = {
        "id": "orchestrate",
        "type": "supervisor",
        "manager": manager_ref,
        "specialists": specialists or {"researcher": "./agents/researcher"},
        "max_delegations": max_delegations,
    }
    if budget is not None:
        supervisor["budget"] = budget
    nodes: list[dict] = [supervisor]
    edges: list[dict] = []
    if with_finalize:
        nodes.append({"id": "finalize", "type": "agent", "ref": "./agents/finalize"})
        edges.append({"from": "orchestrate", "to": "finalize"})
    (wf / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "supervisor-demo",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "orchestrate",
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return wf / "workflow.yaml"


class _SupervisorProvider(BaseLLMProvider):
    """Manager delegates to ``researcher`` once, then says ``done``; the
    researcher writes ``findings``; finalize writes ``answer``. Stateful so the
    manager's decision flips after the first round (deterministic)."""

    name = "supervisor"
    version = "0.0.1"

    def __init__(self, *, manager_always: str | None = None) -> None:
        self._manager_calls = 0
        self._manager_always = manager_always

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "as next" in body:
            self._manager_calls += 1
            if self._manager_always is not None:
                choice = self._manager_always
            else:
                choice = "researcher" if self._manager_calls == 1 else "done"
            return CompletionResponse(text=json.dumps({"next": choice}))
        if "as findings" in body:
            return CompletionResponse(text=json.dumps({"findings": "data"}))
        if "as answer" in body:
            return CompletionResponse(text=json.dumps({"answer": "final"}))
        return CompletionResponse(text="{}")  # pragma: no cover

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


def _runner(
    provider: BaseLLMProvider, storage: InMemoryStorage, pricing: PricingTable
) -> WorkflowRunner:
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=NullTracer())
    return WorkflowRunner(executor=executor, storage=storage)


# ---------------------------------------------------------------------------
# Authoring + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_supervisor_authors_and_validates(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(_write_supervisor_workflow(tmp_path))
    graph = compile_workflow(spec, parent)
    validate_graph(graph)  # a supervisor workflow is linear in graph shape
    meta = graph.nodes["orchestrate"].metadata
    assert set(meta["specialists"]) == {"researcher"}
    assert meta["max_delegations"] == 4
    assert meta["decision_field"] == "next"


@pytest.mark.unit
def test_supervisor_rejects_missing_specialist_ref(tmp_path: Path) -> None:
    spec, parent = load_workflow_spec(
        _write_supervisor_workflow(tmp_path, specialists={"ghost": "./agents/does-not-exist"})
    )
    with pytest.raises(WorkflowCompileError, match="specialist 'ghost' ref path does not exist"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_supervisor_rejects_done_as_specialist_id(tmp_path: Path) -> None:
    with pytest.raises(WorkflowSpecLoadError, match="reserved terminate sentinel"):
        load_workflow_spec(
            _write_supervisor_workflow(tmp_path, specialists={"done": "./agents/researcher"})
        )


# ---------------------------------------------------------------------------
# Execution on the native runner
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_supervisor_delegates_then_finishes(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    spec, parent = load_workflow_spec(_write_supervisor_workflow(tmp_path))
    graph = compile_workflow(spec, parent)
    result = await _runner(_SupervisorProvider(), storage, pricing).run(
        graph, initial_state={"task": "investigate"}
    )
    assert result.status is WorkflowStatus.SUCCESS
    # Manager delegated to the researcher (findings merged), then the post-loop
    # finalize agent ran (answer merged).
    assert result.final_state["findings"] == "data"
    assert result.final_state["answer"] == "final"
    # Runs: manager → researcher → manager(done) → finalize.
    node_ids = [r.node_id for r in result.runs]
    assert node_ids == ["orchestrate", "orchestrate/researcher", "orchestrate", "finalize"]


@pytest.mark.unit
async def test_supervisor_is_bounded_by_max_delegations(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """A manager that ALWAYS delegates (never says done) still terminates after
    max_delegations rounds — the anti-runaway bound."""
    spec, parent = load_workflow_spec(_write_supervisor_workflow(tmp_path, max_delegations=2))
    graph = compile_workflow(spec, parent)
    result = await _runner(_SupervisorProvider(manager_always="researcher"), storage, pricing).run(
        graph, initial_state={"task": "loop forever"}
    )
    assert result.status is WorkflowStatus.SUCCESS
    # 2 rounds x (manager + researcher) = 4 supervisor runs, then finalize.
    sup_runs = [r for r in result.runs if r.node_id.startswith("orchestrate")]
    assert len(sup_runs) == 4
    assert [r.node_id for r in result.runs][-1] == "finalize"


@pytest.mark.unit
async def test_supervisor_out_of_allowlist_choice_terminates(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """A manager that names a specialist NOT in the allowlist ends the loop (it
    may not reach beyond its fixed roster)."""
    spec, parent = load_workflow_spec(_write_supervisor_workflow(tmp_path, with_finalize=False))
    graph = compile_workflow(spec, parent)
    result = await _runner(_SupervisorProvider(manager_always="hacker"), storage, pricing).run(
        graph, initial_state={"task": "x"}
    )
    assert result.status is WorkflowStatus.SUCCESS
    # Manager ran once, named an out-of-roster id → loop ended; no specialist ran.
    assert [r.node_id for r in result.runs] == ["orchestrate"]


@pytest.mark.unit
async def test_supervisor_specialist_failure_is_partial_error(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    spec, parent = load_workflow_spec(_write_supervisor_workflow(tmp_path))
    graph = compile_workflow(spec, parent)

    class FailResearcher(_SupervisorProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            if "as findings" in body:
                # Violates the researcher's output schema (missing 'findings').
                return CompletionResponse(text='{"wrong": "x"}')
            return await super().complete(request)

    result = await _runner(FailResearcher(), storage, pricing).run(
        graph, initial_state={"task": "x"}
    )
    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "orchestrate/researcher"


# ---------------------------------------------------------------------------
# Governance contract (ADR 092 D5 / Phase 4) — aggregate budget enforcement +
# the `mdk validate` governance surface.
# ---------------------------------------------------------------------------


class _CostingSupervisorProvider(_SupervisorProvider):
    """Like the deterministic provider, but reports output tokens so each run
    carries a real (priced) cost — lets the aggregate-budget cap actually fire."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        resp = await super().complete(request)
        return CompletionResponse(text=resp.text, tokens=TokenUsage(output=1_000_000))


@pytest.mark.unit
def test_supervisor_budget_stamped_only_when_set(tmp_path: Path) -> None:
    # set
    spec, parent = load_workflow_spec(_write_supervisor_workflow(tmp_path, budget=2.5))
    assert compile_workflow(spec, parent).nodes["orchestrate"].metadata["budget"] == 2.5
    # unset → absent (metadata unchanged)
    spec2, parent2 = load_workflow_spec(_write_supervisor_workflow(tmp_path / "b"))
    assert "budget" not in compile_workflow(spec2, parent2).nodes["orchestrate"].metadata


@pytest.mark.unit
def test_supervisor_budget_rejects_negative(tmp_path: Path) -> None:
    with pytest.raises(WorkflowSpecLoadError):
        load_workflow_spec(_write_supervisor_workflow(tmp_path, budget=-1.0))


@pytest.mark.unit
async def test_supervisor_budget_stops_loop_early(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """A never-done manager + costing runs + a tiny aggregate budget: the loop
    stops on the budget, well before max_delegations."""
    spec, parent = load_workflow_spec(
        _write_supervisor_workflow(tmp_path, max_delegations=5, with_finalize=False, budget=0.01)
    )
    graph = compile_workflow(spec, parent)
    result = await _runner(
        _CostingSupervisorProvider(manager_always="researcher"), storage, pricing
    ).run(graph, initial_state={"task": "x"})
    assert result.status is WorkflowStatus.SUCCESS
    # Round 1 (manager + specialist) spends past $0.01, so round 2's budget gate
    # breaks the loop → exactly 2 runs, not the 10 that 5 delegations would give.
    assert len(result.runs) == 2


@pytest.mark.unit
def test_validate_surfaces_supervisor_governance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec, parent = load_workflow_spec(
        _write_supervisor_workflow(tmp_path, max_delegations=6, budget=3.0)
    )
    graph = compile_workflow(spec, parent)
    _print_workflow_governance(graph)
    out = capsys.readouterr().out
    assert "governance" in out
    assert "supervisor" in out and "orchestrate" in out
    assert "max_delegations=6" in out
    assert "budget=$3.00" in out
    assert "researcher" in out  # the specialist roster is shown
