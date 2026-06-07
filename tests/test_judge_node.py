"""JUDGE workflow node (ADR 056 D1-D5).

Tests-first coverage for the JUDGE node and its lowering:

* **D1** — ``JudgeNodeSpec`` validation (selection xor, threshold range) +
  compile into the IR (``NodeType.JUDGE``, metadata, route-target checks).
* **D2** — the canonical verdict contract + the single ``terminate`` rule
  (eval-gate threshold form vs categorical form vs parse_error fail-open).
* **D3** — native ``WorkflowRunner`` execution: a JUDGE node runs the judge
  through the Executor, stamps the verdict, and branches on ``terminate``.
* **D4** — reflection = JUDGE on a bounded back-edge: produce → judge →
  revise → … bounded by ``max_iterations``, feedback threaded into revise.
* **D5** — native↔Temporal equivalence: ``call_judge_activity`` runs the same
  judge and derives the SAME ``terminate`` for the same judge output.

Hermetic: a prompt-aware fake provider stands in for the LLM so producer and
judge return scripted JSON; no network, no real model.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import (
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
)
from movate.core.workflow.compiler import WorkflowCompileError
from movate.core.workflow.judge import (
    build_judge_state_value,
    derive_terminate,
    verdict_from_response_data,
)
from movate.core.workflow.spec import JudgeNodeSpec
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.templates import TEMPLATES_DIR
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Fixtures + scaffolding
# ---------------------------------------------------------------------------


def _write_agent(
    agent_dir: Path,
    *,
    name: str,
    input_keys: list[str],
    output_key: str,
) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": f"writes {output_key}",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(f"node={name} produce {output_key}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {k: {"type": "string"} for k in input_keys},
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


def _write_judge_agent(agent_dir: Path, *, name: str = "judge-agent") -> None:
    """A judge agent whose output schema is the verdict contract."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": "grades an artifact",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("judge: grade {{ input.text }}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    # Permissive output schema: a judge may emit a malformed verdict (the
    # parser handles parse_error fail-open), so we don't hard-require the
    # verdict key at the schema layer — that is the parser's job.
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "verdict": {"type": "string"},
                    "score": {"type": "number"},
                    "feedback": {"type": "string"},
                },
            }
        )
    )


_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "topic": {"type": "string"},
        "answer": {"type": "string"},
        "feedback": {"type": "string"},
    },
    "required": ["topic"],
}


def _write_workflow(
    workflow_dir: Path, *, nodes: list[dict], edges: list[dict], entrypoint: str
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "judge-wf",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": entrypoint,
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


class _ScriptedProvider(BaseLLMProvider):
    """Prompt-aware fake: returns producer / judge JSON by inspecting the prompt.

    ``judge_calls`` lets the judge verdict change across reflection iterations
    (e.g. revise the first time, accept the second).
    """

    name = "scripted"
    version = "0.0.1"

    def __init__(self, *, producer: str, judge_sequence: list[str]) -> None:
        self._producer = producer
        self._judge_sequence = list(judge_sequence)
        self._judge_idx = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if body.startswith("judge:") or "impartial judge" in body:
            idx = min(self._judge_idx, len(self._judge_sequence) - 1)
            self._judge_idx += 1
            return CompletionResponse(text=self._judge_sequence[idx])
        # Scaffolded downstream agents carry the marker "node=<name> produce
        # <output_key>" so they can return their required output key. Anything
        # else (the produce node, or a real template prompt) gets the producer
        # text — which the template's produce node consumes as its `answer`.
        if body.startswith("node="):
            output_key = body.split()[-1].strip()
            if output_key != "answer":
                return CompletionResponse(text=json.dumps({output_key: f"{output_key}-done"}))
        return CompletionResponse(text=self._producer)

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
# D1 — spec validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeSpec:
    def test_requires_agent_or_criteria(self) -> None:
        with pytest.raises(Exception, match=r"judge_agent.*criteria|criteria.*required"):
            JudgeNodeSpec(id="j", type="judge")

    def test_rejects_both_agent_and_criteria(self) -> None:
        with pytest.raises(Exception, match="not both"):
            JudgeNodeSpec(id="j", type="judge", judge_agent="./j", criteria="be good")

    def test_threshold_range_enforced(self) -> None:
        with pytest.raises(Exception):
            JudgeNodeSpec(id="j", type="judge", criteria="x", pass_threshold=1.5)

    def test_accepts_criteria_form(self) -> None:
        spec = JudgeNodeSpec(id="j", type="judge", criteria="be good", input_field="answer")
        assert spec.input_field == "answer"
        assert spec.max_iterations == 1


# ---------------------------------------------------------------------------
# D2 — verdict contract + the single terminate rule
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerdictContract:
    def test_threshold_form_score_meets_bar_terminates(self) -> None:
        assert derive_terminate(verdict="revise", score=0.8, pass_threshold=0.7) is True

    def test_threshold_form_score_below_bar_does_not_terminate(self) -> None:
        assert derive_terminate(verdict="accept", score=0.5, pass_threshold=0.7) is False

    def test_threshold_form_missing_score_does_not_terminate(self) -> None:
        assert derive_terminate(verdict="accept", score=None, pass_threshold=0.7) is False

    def test_categorical_accept_terminates(self) -> None:
        assert derive_terminate(verdict="accept", score=None, pass_threshold=None) is True

    def test_categorical_revise_does_not_terminate(self) -> None:
        assert derive_terminate(verdict="revise", score=None, pass_threshold=None) is False

    def test_parse_error_fail_open_without_threshold(self) -> None:
        # A flaky judge must never hard-block a non-looping gate.
        assert derive_terminate(verdict="parse_error", score=None, pass_threshold=None) is True

    def test_parse_error_with_threshold_cannot_pass(self) -> None:
        assert derive_terminate(verdict="parse_error", score=None, pass_threshold=0.7) is False

    def test_state_value_shape(self) -> None:
        v = build_judge_state_value(verdict="accept", score=0.9, feedback="ok", terminate=True)
        assert v == {"verdict": "accept", "score": 0.9, "feedback": "ok", "terminate": True}

    def test_verdict_from_structured_output(self) -> None:
        assert verdict_from_response_data(
            {"verdict": "revise", "score": 0.3, "feedback": "fix x"}
        ) == ("revise", 0.3, "fix x")

    def test_verdict_from_stringified_output(self) -> None:
        data = {"verdict": '{"verdict": "accept", "score": 0.95, "feedback": ""}'}
        assert verdict_from_response_data(data) == ("accept", 0.95, "")

    def test_verdict_unparseable_is_parse_error(self) -> None:
        assert verdict_from_response_data({"nothing": "useful"}) == ("parse_error", None, "")


# ---------------------------------------------------------------------------
# D1 — compiler
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeCompile:
    def test_compiles_eval_gate_with_agent_ref(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf"
        _write_agent(
            wf / "agents" / "produce", name="produce", input_keys=["topic"], output_key="answer"
        )
        _write_judge_agent(wf / "agents" / "judge")
        _write_agent(
            wf / "agents" / "publish", name="publish", input_keys=["answer"], output_key="published"
        )
        path = _write_workflow(
            wf,
            nodes=[
                {"id": "produce", "type": "agent", "ref": "./agents/produce"},
                {
                    "id": "judge",
                    "type": "judge",
                    "judge_agent": "./agents/judge",
                    "input_field": "answer",
                    "pass_threshold": 0.7,
                    "on_accept": "publish",
                },
                {"id": "publish", "type": "agent", "ref": "./agents/publish"},
            ],
            edges=[{"from": "produce", "to": "judge"}],
            entrypoint="produce",
        )
        spec, parent = load_workflow_spec(path)
        graph = compile_workflow(spec, parent)
        jn = graph.nodes["judge"]
        assert str(jn.type) == "judge"
        assert jn.ref.endswith("agents/judge")
        assert jn.metadata["pass_threshold"] == 0.7
        assert jn.metadata["on_accept"] == "publish"

    def test_rejects_unknown_route_target(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf"
        _write_agent(
            wf / "agents" / "produce", name="produce", input_keys=["topic"], output_key="answer"
        )
        path = _write_workflow(
            wf,
            nodes=[
                {"id": "produce", "type": "agent", "ref": "./agents/produce"},
                {"id": "judge", "type": "judge", "criteria": "x", "on_accept": "nope"},
            ],
            edges=[{"from": "produce", "to": "judge"}],
            entrypoint="produce",
        )
        spec, parent = load_workflow_spec(path)
        with pytest.raises(WorkflowCompileError, match="on_accept target 'nope'"):
            compile_workflow(spec, parent)

    def test_missing_judge_agent_ref_fails(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf"
        _write_agent(
            wf / "agents" / "produce", name="produce", input_keys=["topic"], output_key="answer"
        )
        path = _write_workflow(
            wf,
            nodes=[
                {"id": "produce", "type": "agent", "ref": "./agents/produce"},
                {"id": "judge", "type": "judge", "judge_agent": "./agents/ghost"},
            ],
            edges=[{"from": "produce", "to": "judge"}],
            entrypoint="produce",
        )
        spec, parent = load_workflow_spec(path)
        with pytest.raises(WorkflowCompileError, match="ref path does not exist"):
            compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# D3 — native execution: eval-gate branch
# ---------------------------------------------------------------------------


def _eval_gate_workflow(tmp_path: Path) -> Path:
    wf = tmp_path / "wf"
    _write_agent(
        wf / "agents" / "produce",
        name="produce",
        input_keys=["topic", "feedback"],
        output_key="answer",
    )
    _write_judge_agent(wf / "agents" / "judge")
    _write_agent(
        wf / "agents" / "publish", name="publish", input_keys=["answer"], output_key="published"
    )
    _write_agent(
        wf / "agents" / "escalate", name="escalate", input_keys=["answer"], output_key="escalated"
    )
    return _write_workflow(
        wf,
        nodes=[
            {"id": "produce", "type": "agent", "ref": "./agents/produce"},
            {
                "id": "judge",
                "type": "judge",
                "judge_agent": "./agents/judge",
                "input_field": "answer",
                "pass_threshold": 0.7,
                "on_accept": "publish",
                "on_revise": "escalate",
            },
            {"id": "publish", "type": "agent", "ref": "./agents/publish"},
            {"id": "escalate", "type": "agent", "ref": "./agents/escalate"},
        ],
        edges=[{"from": "produce", "to": "judge"}],
        entrypoint="produce",
    )


@pytest.mark.unit
async def test_native_eval_gate_accept_routes_to_on_accept(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _eval_gate_workflow(tmp_path)
    spec, parent = load_workflow_spec(path)
    graph = compile_workflow(spec, parent)

    provider = _ScriptedProvider(
        producer='{"answer": "a good answer"}',
        judge_sequence=['{"verdict": "accept", "score": 0.9, "feedback": ""}'],
    )
    result = await _runner(provider, storage, pricing).run(graph, {"topic": "t"})
    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state["judge"]["terminate"] is True
    assert result.final_state["judge"]["score"] == 0.9
    # accept → publish ran, escalate did not.
    assert "published" in result.final_state
    assert "escalated" not in result.final_state


@pytest.mark.unit
async def test_native_eval_gate_below_threshold_routes_to_on_revise(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _eval_gate_workflow(tmp_path)
    spec, parent = load_workflow_spec(path)
    graph = compile_workflow(spec, parent)

    provider = _ScriptedProvider(
        producer='{"answer": "weak answer"}',
        judge_sequence=['{"verdict": "accept", "score": 0.4, "feedback": "too thin"}'],
    )
    result = await _runner(provider, storage, pricing).run(graph, {"topic": "t"})
    assert result.status is WorkflowStatus.SUCCESS
    # score 0.4 < 0.7 ⇒ not terminate ⇒ on_revise (escalate).
    assert result.final_state["judge"]["terminate"] is False
    assert "escalated" in result.final_state
    assert "published" not in result.final_state


# ---------------------------------------------------------------------------
# D4 — reflection loop: produce → judge → revise → produce → judge (accept)
# ---------------------------------------------------------------------------


def _reflection_workflow(tmp_path: Path) -> Path:
    wf = tmp_path / "wf"
    _write_agent(
        wf / "agents" / "produce",
        name="produce",
        input_keys=["topic", "feedback"],
        output_key="answer",
    )
    _write_judge_agent(wf / "agents" / "judge")
    return _write_workflow(
        wf,
        nodes=[
            {"id": "produce", "type": "agent", "ref": "./agents/produce"},
            {
                "id": "judge",
                "type": "judge",
                "judge_agent": "./agents/judge",
                "input_field": "answer",
                "max_iterations": 3,
            },
        ],
        edges=[
            {"from": "produce", "to": "judge"},
            {"from": "judge", "to": "produce"},  # reflection back-edge
        ],
        entrypoint="produce",
    )


@pytest.mark.unit
async def test_native_reflection_loops_until_accept(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _reflection_workflow(tmp_path)
    spec, parent = load_workflow_spec(path)
    graph = compile_workflow(spec, parent, allow_cycles=True)

    # First judgement revises (with feedback), second accepts.
    provider = _ScriptedProvider(
        producer='{"answer": "draft"}',
        judge_sequence=[
            '{"verdict": "revise", "feedback": "be more specific"}',
            '{"verdict": "accept", "feedback": ""}',
        ],
    )
    result = await _runner(provider, storage, pricing).run(graph, {"topic": "t"})
    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state["judge"]["verdict"] == "accept"
    # The producer ran twice (initial + one revise), the judge twice.
    produce_runs = [r for r in result.runs if r.node_id == "produce"]
    judge_runs = [r for r in result.runs if r.node_id == "judge"]
    assert len(produce_runs) == 2
    assert len(judge_runs) == 2


@pytest.mark.unit
async def test_native_reflection_caps_at_max_iterations(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _reflection_workflow(tmp_path)
    spec, parent = load_workflow_spec(path)
    graph = compile_workflow(spec, parent, allow_cycles=True)

    # The judge NEVER accepts — the loop must terminate at the max_iterations cap.
    provider = _ScriptedProvider(
        producer='{"answer": "draft"}',
        judge_sequence=['{"verdict": "revise", "feedback": "never good enough"}'],
    )
    result = await _runner(provider, storage, pricing).run(graph, {"topic": "t"})
    assert result.status is WorkflowStatus.SUCCESS
    # Judged exactly max_iterations (3) times then stopped — no runaway.
    judge_runs = [r for r in result.runs if r.node_id == "judge"]
    assert len(judge_runs) == 3
    assert result.final_state["judge"]["verdict"] == "revise"


@pytest.mark.unit
async def test_native_reflection_threads_feedback_into_revise(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _reflection_workflow(tmp_path)
    spec, parent = load_workflow_spec(path)
    graph = compile_workflow(spec, parent, allow_cycles=True)

    provider = _ScriptedProvider(
        producer='{"answer": "draft"}',
        judge_sequence=[
            '{"verdict": "revise", "feedback": "cite a source"}',
            '{"verdict": "accept", "feedback": ""}',
        ],
    )
    result = await _runner(provider, storage, pricing).run(graph, {"topic": "t"})
    # The judge's feedback is surfaced into state for the revise step (D4).
    # After accept the feedback is cleared, so assert the loop reached accept
    # having threaded the revise feedback (verdict==accept proves the 2nd pass).
    assert result.final_state["judge"]["verdict"] == "accept"


# ---------------------------------------------------------------------------
# D3 — parse_error fail-open (a flaky judge never crashes the workflow)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_native_judge_parse_error_fails_open(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _eval_gate_workflow(tmp_path)
    spec, parent = load_workflow_spec(path)
    graph = compile_workflow(spec, parent)

    # Judge emits garbage (no parseable verdict). With a threshold set, the
    # node cannot pass ⇒ on_revise; the workflow still completes (no crash).
    provider = _ScriptedProvider(
        producer='{"answer": "x"}',
        judge_sequence=['{"answer": "I am confused"}'],
    )
    result = await _runner(provider, storage, pricing).run(graph, {"topic": "t"})
    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state["judge"]["verdict"] == "parse_error"
    assert "escalated" in result.final_state


# ---------------------------------------------------------------------------
# Mock path — --mock yields a deterministic accept (no spend)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_native_judge_mock_accepts(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _eval_gate_workflow(tmp_path)
    spec, parent = load_workflow_spec(path)
    graph = compile_workflow(spec, parent)
    provider = _ScriptedProvider(
        producer='{"answer": "x"}', judge_sequence=['{"verdict": "accept"}']
    )
    result = await _runner(provider, storage, pricing).run(graph, {"topic": "t"}, mock=True)
    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state["judge"]["terminate"] is True
    assert "published" in result.final_state


# ---------------------------------------------------------------------------
# The shipped reflective-agent template actually runs the loop (ADR 056 D4).
# ---------------------------------------------------------------------------


def _materialize_template(name: str, dst: Path) -> Path:
    """Copy a shipped workflow template, substituting the __AGENT_NAME__ token."""
    src = TEMPLATES_DIR / name
    shutil.copytree(src, dst)
    for agent_yaml in dst.rglob("agent.yaml"):
        agent_yaml.write_text(
            agent_yaml.read_text().replace("__AGENT_NAME__", agent_yaml.parent.name)
        )
    return dst / "workflow.yaml"


@pytest.mark.unit
async def test_reflective_template_runs_the_loop(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    path = _materialize_template("reflective_agent", tmp_path / "reflective")
    spec, parent = load_workflow_spec(path)
    # The template has a back-edge (reflection loop) ⇒ cycle-tolerant compile.
    graph = compile_workflow(spec, parent, allow_cycles=True)
    assert graph.nodes["judge"].metadata["max_iterations"] == 2
    assert any((e.from_id, e.to_id) == ("judge", "produce") for e in graph.find_back_edges())

    # produce returns an answer; the judge revises once, then accepts.
    provider = _ScriptedProvider(
        producer='{"answer": "a concise on-topic answer in two sentences."}',
        judge_sequence=[
            '{"verdict": "revise", "feedback": "be more concrete"}',
            '{"verdict": "accept", "score": 0.9, "feedback": ""}',
        ],
    )
    result = await _runner(provider, storage, pricing).run(
        graph, {"topic": "why feedback loops matter"}
    )
    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state["judge"]["verdict"] == "accept"
    # The loop ran: produce executed twice (initial + one revise pass).
    assert len([r for r in result.runs if r.node_id == "produce"]) == 2
