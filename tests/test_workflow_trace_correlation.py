"""ADR 024 PR 3 (#99 workflow half) — workflow trace correlation (D4).

The workflow orchestrator opens ONE ``workflow.execute`` root span per workflow
run and threads its :class:`SpanCtx` into each node's
``Executor.execute(..., parent_span=...)``, so every node's ``agent.execute``
nests under the workflow root — a multi-node workflow renders as one trace tree
(Langfuse / OTel) instead of N disconnected roots.

Hermetic: a provider double + InMemoryStorage + a capturing tracer that records
the span tree (name + ``parent_id``), no API keys, no network.

Cases:
1. multi-node workflow (≥2 agent nodes) → each node's ``agent.execute`` has
   ``parent_id`` == the single ``workflow.execute`` root span; the workflow root
   is opened once and closed once.
2. intent-router workflow → the classifier node's ``agent.execute`` AND the
   routed agent node's ``agent.execute`` both nest under the same workflow root.
3. standalone (non-workflow) run → ``agent.execute`` has NO workflow parent
   (back-compat: byte-for-byte unchanged, no ``workflow.execute`` span).
4. ``Executor.execute(..., parent_span=<span>)`` explicit → nests under it;
   omitted → root as today (the additive optional param, in isolation).
5. offline correlation is unchanged — per-node RunRecords still carry
   ``workflow_run_id`` + ``node_id`` (no new linkage field), so the node tree
   reconstructs without a backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest, WorkflowStatus
from movate.core.workflow import (
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage
from movate.tracing.base import SpanCtx

# ---------------------------------------------------------------------------
# Capturing tracer — records every span (name + parent_id) for assertions.
# Mirrors the convention in tests/test_step_observability.py.
# ---------------------------------------------------------------------------


class _CapturingTracer:
    """Tracer double retaining every span it creates (in creation order) so
    tests can assert the ``workflow.execute → agent.execute`` parent wiring."""

    name = "capturing"

    def __init__(self) -> None:
        self.spans: list[SpanCtx] = []
        self.ended: list[tuple[str, str]] = []  # (span_id, status)
        self.events: list[dict[str, Any]] = []

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        span = SpanCtx(
            trace_id="trace-cap",
            name=name,
            attributes=dict(attrs or {}),
            parent_id=parent.span_id if parent else None,
        )
        self.spans.append(span)
        return span

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        self.ended.append((span.span_id, status))

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value

    def log_generation(self, span: SpanCtx, **kwargs: Any) -> None:
        return None

    # --- assertion helpers ---
    def by_name(self, name: str) -> list[SpanCtx]:
        return [s for s in self.spans if s.name == name]


# ---------------------------------------------------------------------------
# Scaffolding (a trimmed copy of tests/test_workflow_runner.py's helpers so
# this file is self-contained).
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
    entrypoint: str,
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
                "name": "trace-corr-workflow",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": entrypoint,
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


class _StateAwareProvider(BaseLLMProvider):
    """Returns whichever node-output key the prompt body names — so each node
    in a chain validates against its own output schema."""

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def mem_storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


# ---------------------------------------------------------------------------
# Case 1 — multi-node workflow: every node nests under one workflow root
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_workflow_nodes_nest_under_single_workflow_root(
    tmp_path: Path, pricing: PricingTable, mem_storage: InMemoryStorage
) -> None:
    """A two-node workflow opens ONE ``workflow.execute`` root span; each
    node's ``agent.execute`` is a direct child of it (parent_id wiring)."""
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
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
        entrypoint="first",
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    tracer = _CapturingTracer()
    executor = Executor(
        provider=_StateAwareProvider(), pricing=pricing, storage=mem_storage, tracer=tracer
    )
    runner = WorkflowRunner(executor=executor, storage=mem_storage)

    result = await runner.run(graph, initial_state={"text": "seed"})
    assert result.status is WorkflowStatus.SUCCESS

    # Exactly one workflow-root span, opened once.
    wf_spans = tracer.by_name("workflow.execute")
    assert len(wf_spans) == 1
    wf_root = wf_spans[0]
    assert wf_root.parent_id is None  # the workflow root is itself a root span
    assert wf_root.attributes.get("workflow") == graph.name
    assert wf_root.attributes.get("workflow_run_id") == result.workflow_run_id

    # Two agent.execute spans (one per node), EACH a child of the workflow root.
    agent_spans = tracer.by_name("agent.execute")
    assert len(agent_spans) == 2
    assert all(s.parent_id == wf_root.span_id for s in agent_spans)

    # The workflow root was closed exactly once.
    ended_ids = [sid for sid, _ in tracer.ended]
    assert ended_ids.count(wf_root.span_id) == 1


# ---------------------------------------------------------------------------
# Case 2 — intent-router: classifier + routed node both nest under the root
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_intent_router_classifier_and_routed_node_nest_under_root(
    tmp_path: Path, pricing: PricingTable, mem_storage: InMemoryStorage
) -> None:
    """Both the classifier node's ``agent.execute`` (run from
    ``_run_intent_router``) and the routed downstream agent node's
    ``agent.execute`` nest under the single workflow root span."""
    workflow_dir = tmp_path / "wf"
    # Classifier agent: text+labels → {label}. Routed agent: text → step1.
    clf_dir = workflow_dir / "agents" / "clf"
    clf_dir.mkdir(parents=True, exist_ok=True)
    (clf_dir / "schema").mkdir(exist_ok=True)
    (clf_dir / "evals").mkdir(exist_ok=True)
    (clf_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "clf-agent",
                "version": "0.1.0",
                "description": "classifier",
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
    (clf_dir / "prompt.md").write_text("classify {{ input.text }} into {{ input.labels }}\n")
    (clf_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "required": ["text"],
                "properties": {"text": {"type": "string"}, "labels": {"type": "array"}},
            }
        )
    )
    (clf_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["label"],
                "properties": {"label": {"type": "string"}},
            }
        )
    )
    (clf_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"text": "x"}, "expected": {"label": "a"}}) + "\n"
    )

    _make_agent(
        workflow_dir / "agents" / "billing",
        name="billing-agent",
        input_key="text",
        output_key="step1",
    )

    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {
                "id": "triage",
                "type": "intent-router",
                "routes": {"a": "billing"},
                "fallback": "billing",
                "classifier_agent": "./agents/clf",
                "input_field": "text",
            },
            {"id": "billing", "type": "agent", "ref": "./agents/billing"},
        ],
        edges=[{"from": "triage", "to": "billing"}],
        entrypoint="triage",
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    class _RouterProvider(BaseLLMProvider):
        name = "router_aware"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            if "classify" in body:
                return CompletionResponse(text='{"label": "a"}')
            return CompletionResponse(text='{"step1": "done"}')

        async def stream(self, request):  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text, *, model):  # pragma: no cover
            raise NotImplementedError

    tracer = _CapturingTracer()
    executor = Executor(
        provider=_RouterProvider(), pricing=pricing, storage=mem_storage, tracer=tracer
    )
    runner = WorkflowRunner(executor=executor, storage=mem_storage)

    result = await runner.run(graph, initial_state={"text": "seed"})
    assert result.status is WorkflowStatus.SUCCESS

    wf_spans = tracer.by_name("workflow.execute")
    assert len(wf_spans) == 1
    wf_root = wf_spans[0]

    # Classifier (from _run_intent_router) + routed billing node = 2 agent.execute,
    # both children of the single workflow root.
    agent_spans = tracer.by_name("agent.execute")
    assert len(agent_spans) == 2
    assert all(s.parent_id == wf_root.span_id for s in agent_spans)


# ---------------------------------------------------------------------------
# Case 3 — standalone run: no workflow parent (back-compat unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_standalone_run_has_no_workflow_parent(
    tmp_path: Path, pricing: PricingTable, mem_storage: InMemoryStorage
) -> None:
    """A plain ``Executor.execute`` (no workflow, no parent_span) opens a ROOT
    ``agent.execute`` span — no ``workflow.execute`` span exists and the run
    span has no parent. Byte-for-byte the pre-change behavior."""
    agent_dir = _make_agent(
        tmp_path / "solo", name="solo-agent", input_key="text", output_key="step1"
    )
    bundle = load_agent(agent_dir)

    tracer = _CapturingTracer()
    executor = Executor(
        provider=_StateAwareProvider(), pricing=pricing, storage=mem_storage, tracer=tracer
    )
    resp = await executor.execute(bundle, RunRequest(agent="solo-agent", input={"text": "hi"}))
    assert resp.status == "success"

    assert tracer.by_name("workflow.execute") == []
    agent_spans = tracer.by_name("agent.execute")
    assert len(agent_spans) == 1
    assert agent_spans[0].parent_id is None  # root, as today


# ---------------------------------------------------------------------------
# Case 4 — explicit parent_span nests; omitted → root (the param in isolation)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_execute_parent_span_param_nests_when_passed(
    tmp_path: Path, pricing: PricingTable, mem_storage: InMemoryStorage
) -> None:
    """``Executor.execute(..., parent_span=<span>)`` makes ``agent.execute`` a
    child of that span; omitting it leaves ``agent.execute`` a root span."""
    agent_dir = _make_agent(tmp_path / "p", name="p-agent", input_key="text", output_key="step1")
    bundle = load_agent(agent_dir)

    tracer = _CapturingTracer()
    executor = Executor(
        provider=_StateAwareProvider(), pricing=pricing, storage=mem_storage, tracer=tracer
    )

    # Open a caller-provided parent span and thread it in.
    parent = tracer.start_span("caller.root")
    resp = await executor.execute(
        bundle, RunRequest(agent="p-agent", input={"text": "hi"}), parent_span=parent
    )
    assert resp.status == "success"
    nested = tracer.by_name("agent.execute")
    assert len(nested) == 1
    assert nested[0].parent_id == parent.span_id

    # Omitted → root (default None → unchanged).
    resp2 = await executor.execute(bundle, RunRequest(agent="p-agent", input={"text": "yo"}))
    assert resp2.status == "success"
    roots = [s for s in tracer.by_name("agent.execute") if s.parent_id is None]
    assert len(roots) == 1


# ---------------------------------------------------------------------------
# Case 5 — offline correlation unchanged (workflow_run_id + node_id retained)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_offline_node_links_unchanged(
    tmp_path: Path, pricing: PricingTable, mem_storage: InMemoryStorage
) -> None:
    """The span nesting does NOT change offline correlation: each node's
    persisted RunRecord still carries ``workflow_run_id`` + ``node_id`` (no new
    field), so the node tree reconstructs without a tracing backend."""
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
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
        entrypoint="first",
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    tracer = _CapturingTracer()
    executor = Executor(
        provider=_StateAwareProvider(), pricing=pricing, storage=mem_storage, tracer=tracer
    )
    runner = WorkflowRunner(executor=executor, storage=mem_storage)

    result = await runner.run(graph, initial_state={"text": "seed"})
    assert result.status is WorkflowStatus.SUCCESS

    persisted = await mem_storage.list_runs(
        tenant_id="local", workflow_run_id=result.workflow_run_id
    )
    assert len(persisted) == 2
    assert all(r.workflow_run_id == result.workflow_run_id for r in persisted)
    assert {r.node_id for r in persisted} == {"first", "second"}
