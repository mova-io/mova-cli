"""Hermetic Temporal execution + dispatch-fork tests (ADR 055 steps 1-2 / D7 seed).

Two layers:

1. **No-extra-needed** — the runtime field (D1), the dispatch-fork resolution +
   fail-loud availability rule (D2/D6), and import isolation. These run on every
   CI machine.
2. **Hermetic Temporal smoke (D7 seed)** — compiles a real workflow to a
   Temporal ``@workflow.defn``, runs it end-to-end on
   ``temporalio.testing.WorkflowEnvironment.start_time_skipping()`` (NOT a
   manually-spawned server), and asserts the final state matches the NATIVE
   runner on the same spec — the first cross-backend conformance assertion.
   Skipped cleanly when the ``[temporal]`` extra is absent (mirrors
   ``test_temporal_compiler.py``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.core.workflow.runner import WorkflowRunner
from movate.core.workflow.spec import WorkflowSpecLoadError
from movate.providers.base import BaseLLMProvider, CompletionRequest, CompletionResponse
from movate.providers.pricing import PricingTable, load_pricing
from movate.runtime.workflow_backend import (
    VALID_RUNTIMES,
    WorkflowBackendError,
    require_backend_available,
    resolve_effective_runtime,
)
from movate.testing import InMemoryStorage, NullTracer

# ``temporalio`` may not be installed (it is the opt-in [temporal] extra).
# The hermetic-smoke tests skip cleanly when it's absent — importorskip at
# module scope skips the WHOLE module (incl. these top-level SDK imports).
temporalio = pytest.importorskip(
    "temporalio",
    reason="the [temporal] extra is not installed; hermetic Temporal smoke skipped",
)

from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: E402

from movate.core.workflow.compilers.temporal import TemporalCompiler  # noqa: E402
from movate.core.workflow.temporal_activities import (  # noqa: E402
    call_agent_activity,
    call_gate_activity,
    call_human_activity,
    call_judge_activity,
    call_skill_activity,
    configure_activities,
    persist_workflow_result_activity,
)
from movate.runtime.workflow_backend import (  # noqa: E402
    DEFAULT_TASK_QUEUE,
    _build_temporal_metrics_runtime,
    _tracing_interceptors,
    load_compiled_workflow_class,
)

# ---------------------------------------------------------------------------
# Deterministic provider — same output on native + temporal, so the two
# backends are comparable (the conformance precondition, ADR 055 D7).
# ---------------------------------------------------------------------------


class _StateAwareProvider(BaseLLMProvider):
    """Returns ``{step1: alpha}`` or ``{step2: beta}`` by which key the prompt names.

    Deterministic + offline so the native runner and the Temporal backend
    produce identical state for the same workflow (no real LLM, no keys).
    """

    name = "state_aware"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "step1" in body and "step2" not in body:
            return CompletionResponse(text='{"step1": "alpha"}')
        return CompletionResponse(text='{"step2": "beta"}')

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


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


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> None:
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


def _scaffold_two_step(tmp_path: Path, *, runtime: str | None = None) -> Path:
    """``text → step1 → step2`` (linear, two agent nodes).

    ``runtime`` (when set) writes a ``runtime:`` key into workflow.yaml so the
    same scaffolding exercises both the default (native) and an explicit
    declaration.
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
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    body: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "test-workflow",
        "version": "0.1.0",
        "state_schema": "./state.json",
        "entrypoint": "first",
        "nodes": [
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        "edges": [{"from": "first", "to": "second"}],
    }
    if runtime is not None:
        body["runtime"] = runtime
    (workflow_dir / "workflow.yaml").write_text(yaml.safe_dump(body))
    return workflow_dir / "workflow.yaml"


def _load_graph(yaml_path: Path) -> Any:
    spec, parent = load_workflow_spec(yaml_path)
    return compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# D1 — the runtime field is additive + default native.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runtime_field_defaults_auto(tmp_path: Path) -> None:
    """A workflow.yaml with no ``runtime:`` key defaults to 'auto' (ADR 091),
    resolved to temporal-when-available-else-native at dispatch (not here)."""
    graph = _load_graph(_scaffold_two_step(tmp_path))
    assert graph.runtime == "auto"
    # And the spec surfaces it too.
    spec, _ = load_workflow_spec(_scaffold_two_step(tmp_path / "b"))
    assert spec.runtime == "auto"


@pytest.mark.unit
def test_runtime_field_explicit_temporal(tmp_path: Path) -> None:
    """``runtime: temporal`` parses + surfaces read-only on the IR."""
    graph = _load_graph(_scaffold_two_step(tmp_path, runtime="temporal"))
    assert graph.runtime == "temporal"


@pytest.mark.unit
def test_runtime_field_rejects_unknown(tmp_path: Path) -> None:
    """An invalid ``runtime:`` value fails spec validation (extra='forbid' enum)."""
    with pytest.raises(WorkflowSpecLoadError):
        load_workflow_spec(_scaffold_two_step(tmp_path, runtime="bogus"))


# ---------------------------------------------------------------------------
# D2/D3 — effective-runtime resolution + precedence.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_effective_runtime_precedence(tmp_path: Path) -> None:
    """override > graph.runtime > native; override never mutates the graph."""
    graph = _load_graph(_scaffold_two_step(tmp_path, runtime="temporal"))
    # No override → the declared runtime.
    assert resolve_effective_runtime(graph, None) == "temporal"
    # Override wins.
    assert resolve_effective_runtime(graph, "native") == "native"
    # Override is read-only — the graph's declared runtime is unchanged.
    assert graph.runtime == "temporal"


@pytest.mark.unit
def test_effective_runtime_rejects_bad_override(tmp_path: Path) -> None:
    graph = _load_graph(_scaffold_two_step(tmp_path))
    with pytest.raises(WorkflowBackendError):
        resolve_effective_runtime(graph, "bogus")
    assert set(VALID_RUNTIMES) == {"auto", "native", "langgraph", "temporal"}


# ---------------------------------------------------------------------------
# D6 — fail loud, never silent downgrade.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_langgraph_available_when_extra_present() -> None:
    """langgraph is now a wired backend (ADR 030 D1): availability is gated on
    the [langgraph] extra being importable, NOT rejected outright. The dev env
    ships langgraph, so this is a no-op (it must never raise here)."""
    require_backend_available("langgraph")  # no raise — backend is wired.


@pytest.mark.unit
def test_langgraph_fails_loud_when_extra_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the [langgraph] extra absent, selecting langgraph fails loud with the
    install hint (D6) — never a silent downgrade to native."""

    class _BlockLanggraphFinder:
        def find_module(self, name: str, path: Any = None) -> Any:
            return self if name == "langgraph" or name.startswith("langgraph.") else None

        def load_module(self, name: str) -> Any:
            raise ImportError(f"hidden by test: {name}")

    blocked = [m for m in sys.modules if m == "langgraph" or m.startswith("langgraph.")]
    for m in blocked:
        monkeypatch.delitem(sys.modules, m, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockLanggraphFinder(), *sys.meta_path])

    with pytest.raises(WorkflowBackendError) as ei:
        require_backend_available("langgraph")
    assert "[langgraph] extra" in str(ei.value)


@pytest.mark.unit
def test_native_always_available() -> None:
    require_backend_available("native")  # no raise — the floor never fails.


@pytest.mark.unit
def test_temporal_without_connection_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """temporal selected but no TEMPORAL_HOST → fail loud with the connection hint."""
    monkeypatch.delenv("TEMPORAL_HOST", raising=False)
    with pytest.raises(WorkflowBackendError) as ei:
        require_backend_available("temporal")
    assert "TEMPORAL_HOST" in str(ei.value)


# ---------------------------------------------------------------------------
# D7 seed — hermetic Temporal smoke + native-vs-temporal equivalence.
# ---------------------------------------------------------------------------


def _build_executor(
    storage: InMemoryStorage, tracer: NullTracer, pricing: PricingTable
) -> Executor:
    return Executor(provider=_StateAwareProvider(), pricing=pricing, storage=storage, tracer=tracer)


@pytest.mark.smoke
async def test_temporal_smoke_matches_native(tmp_path: Path) -> None:
    """Compile a workflow to Temporal, run it on the test env, and assert the
    final state equals the NATIVE runner's on the same spec (conformance, D7).

    Uses ``WorkflowEnvironment.start_time_skipping()`` — the SDK's in-memory
    test server — so there is no externally-spawned ``temporal server``.
    """
    pricing = load_pricing()
    initial_state = {"text": "hello"}

    # --- NATIVE baseline -------------------------------------------------
    native_storage = InMemoryStorage()
    await native_storage.init()
    native_tracer = NullTracer()
    graph = _load_graph(_scaffold_two_step(tmp_path))
    native_runner = WorkflowRunner(
        executor=_build_executor(native_storage, native_tracer, pricing),
        storage=native_storage,
    )
    native_result = await native_runner.run(graph, initial_state=dict(initial_state))
    assert native_result.status is WorkflowStatus.SUCCESS
    assert native_result.final_state == {"text": "hello", "step1": "alpha", "step2": "beta"}

    # --- TEMPORAL via the in-memory test env -----------------------------
    temporal_storage = InMemoryStorage()
    await temporal_storage.init()
    temporal_tracer = NullTracer()
    configure_activities(
        storage=temporal_storage,
        pricing=pricing,
        tracer=temporal_tracer,
        provider=_StateAwareProvider(),
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
            activities=[
                call_agent_activity,
                call_skill_activity,
                call_gate_activity,
                call_judge_activity,
                persist_workflow_result_activity,
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        temporal_final = await env.client.execute_workflow(
            workflow_cls.run,
            {**initial_state, "tenant_id": "local"},
            id="conformance-smoke-1",
            task_queue=DEFAULT_TASK_QUEUE,
        )

    # The conformance assertion: temporal's final state equals native's
    # (modulo the tenant_id we stamp for the activity context).
    temporal_final.pop("tenant_id", None)
    assert temporal_final == native_result.final_state


# ---------------------------------------------------------------------------
# Fan-out parity (ADR 092 Phase 2 / D3) — a canonical diamond runs concurrently
# on Temporal (asyncio.gather) and reaches the SAME joined state the native
# fan-out runner does. This is the cross-backend conformance anchor for D3.
# ---------------------------------------------------------------------------


class _DiamondProvider(BaseLLMProvider):
    """Deterministic per-output-key provider for the diamond conformance test.

    Returns ``{<out_key>: <out_key>.upper()}`` based on the ``as <key>`` suffix
    the agents' prompts carry — identical on native + Temporal so the two
    backends are comparable.
    """

    name = "diamond"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        for key in ("seed", "a_out", "b_out", "final"):
            if f"as {key}" in body:
                return CompletionResponse(text=json.dumps({key: key.upper()}))
        return CompletionResponse(text="{}")  # pragma: no cover

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def _scaffold_diamond(tmp_path: Path) -> Path:
    """``start ⇉ {a, b} ⇉ merge`` — a canonical single-node-branch diamond
    (``runtime: temporal``)."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "start", name="start-agent", input_key="text", output_key="seed")
    _make_agent(wf / "agents" / "a", name="a-agent", input_key="seed", output_key="a_out")
    _make_agent(wf / "agents" / "b", name="b-agent", input_key="seed", output_key="b_out")
    _make_agent(wf / "agents" / "merge", name="merge-agent", input_key="seed", output_key="final")
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "state.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    (wf / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "diamond-workflow",
                "version": "0.1.0",
                "runtime": "temporal",
                "state_schema": "./state.json",
                "entrypoint": "start",
                "nodes": [
                    {"id": "start", "type": "agent", "ref": "./agents/start"},
                    {"id": "a", "type": "agent", "ref": "./agents/a"},
                    {"id": "b", "type": "agent", "ref": "./agents/b"},
                    {"id": "merge", "type": "agent", "ref": "./agents/merge"},
                ],
                "edges": [
                    {"from": "start", "to": "a", "kind": "fan_out"},
                    {"from": "start", "to": "b", "kind": "fan_out"},
                    {"from": "a", "to": "merge", "kind": "fan_in"},
                    {"from": "b", "to": "merge", "kind": "fan_in"},
                ],
            }
        )
    )
    return wf / "workflow.yaml"


@pytest.mark.smoke
async def test_temporal_fan_out_matches_native(tmp_path: Path) -> None:
    """A canonical diamond fan-out reaches the SAME joined state on Temporal
    (durable ``asyncio.gather`` parallelism) as on the native fan-out runner."""
    pricing = load_pricing()
    initial_state = {"text": "hello"}
    graph = _load_graph(_scaffold_diamond(tmp_path))

    # --- NATIVE baseline (the native fan-out block executor, ADR 092 Phase 1) -
    native_storage = InMemoryStorage()
    await native_storage.init()
    native_runner = WorkflowRunner(
        executor=Executor(
            provider=_DiamondProvider(),
            pricing=pricing,
            storage=native_storage,
            tracer=NullTracer(),
        ),
        storage=native_storage,
    )
    native_result = await native_runner.run(graph, initial_state=dict(initial_state))
    assert native_result.status is WorkflowStatus.SUCCESS
    assert native_result.final_state == {
        "text": "hello",
        "seed": "SEED",
        "a_out": "A_OUT",
        "b_out": "B_OUT",
        "final": "FINAL",
    }

    # --- TEMPORAL via the in-memory test env (durable parallelism) ------------
    temporal_storage = InMemoryStorage()
    await temporal_storage.init()
    configure_activities(
        storage=temporal_storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_DiamondProvider(),
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
            activities=[
                call_agent_activity,
                call_skill_activity,
                call_gate_activity,
                call_judge_activity,
                persist_workflow_result_activity,
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        temporal_final = await env.client.execute_workflow(
            workflow_cls.run,
            {**initial_state, "tenant_id": "local"},
            id="conformance-fanout-1",
            task_queue=DEFAULT_TASK_QUEUE,
        )

    temporal_final.pop("tenant_id", None)
    assert temporal_final == native_result.final_state


# ---------------------------------------------------------------------------
# SUPERVISOR parity (ADR 092 D4 / Phase 3b) — the bounded managerial delegation
# loop reaches the SAME final state on Temporal (a durable bounded loop) as on
# the native runner.
# ---------------------------------------------------------------------------


class _SupervisorProvider(BaseLLMProvider):
    """Manager delegates to ``researcher`` once, then says ``done``; the
    researcher writes ``findings``; finalize writes ``answer``. Stateful so the
    manager's decision flips after the first round — deterministic per backend
    (each backend gets a fresh instance)."""

    name = "supervisor"
    version = "0.0.1"

    def __init__(self) -> None:
        self._manager_calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "as next" in body:
            self._manager_calls += 1
            choice = "researcher" if self._manager_calls == 1 else "done"
            return CompletionResponse(text=json.dumps({"next": choice}))
        if "as findings" in body:
            return CompletionResponse(text=json.dumps({"findings": "data"}))
        if "as answer" in body:
            return CompletionResponse(text=json.dumps({"answer": "final"}))
        return CompletionResponse(text="{}")  # pragma: no cover

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def _scaffold_supervisor(tmp_path: Path) -> Path:
    """``orchestrate (supervisor: manager → researcher) → finalize`` (temporal)."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "manager", name="mgr", input_key="task", output_key="next")
    _make_agent(wf / "agents" / "researcher", name="rsr", input_key="task", output_key="findings")
    _make_agent(wf / "agents" / "finalize", name="fin", input_key="findings", output_key="answer")
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "state.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "properties": {"task": {"type": "string"}},
            }
        )
    )
    (wf / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "supervisor-demo",
                "version": "0.1.0",
                "runtime": "temporal",
                "state_schema": "./state.json",
                "entrypoint": "orchestrate",
                "nodes": [
                    {
                        "id": "orchestrate",
                        "type": "supervisor",
                        "manager": "./agents/manager",
                        "specialists": {"researcher": "./agents/researcher"},
                        "max_delegations": 4,
                    },
                    {"id": "finalize", "type": "agent", "ref": "./agents/finalize"},
                ],
                "edges": [{"from": "orchestrate", "to": "finalize"}],
            }
        )
    )
    return wf / "workflow.yaml"


@pytest.mark.smoke
async def test_temporal_supervisor_matches_native(tmp_path: Path) -> None:
    """A bounded SUPERVISOR reaches the SAME joined state on Temporal (a durable
    bounded delegation loop) as on the native runner."""
    pricing = load_pricing()
    initial_state = {"task": "investigate"}
    graph = _load_graph(_scaffold_supervisor(tmp_path))

    native_storage = InMemoryStorage()
    await native_storage.init()
    native_runner = WorkflowRunner(
        executor=Executor(
            provider=_SupervisorProvider(),
            pricing=pricing,
            storage=native_storage,
            tracer=NullTracer(),
        ),
        storage=native_storage,
    )
    native_result = await native_runner.run(graph, initial_state=dict(initial_state))
    assert native_result.status is WorkflowStatus.SUCCESS
    assert native_result.final_state == {
        "task": "investigate",
        "next": "done",
        "findings": "data",
        "answer": "final",
    }

    temporal_storage = InMemoryStorage()
    await temporal_storage.init()
    configure_activities(
        storage=temporal_storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_SupervisorProvider(),
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
            activities=[
                call_agent_activity,
                call_skill_activity,
                call_gate_activity,
                call_judge_activity,
                persist_workflow_result_activity,
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        temporal_final = await env.client.execute_workflow(
            workflow_cls.run,
            {**initial_state, "tenant_id": "local"},
            id="conformance-supervisor-1",
            task_queue=DEFAULT_TASK_QUEUE,
        )

    temporal_final.pop("tenant_id", None)
    assert temporal_final == native_result.final_state


# ---------------------------------------------------------------------------
# Durable HITL (ADR 062) — HUMAN node pauses durably + resumes on a signal,
# matching the native runner's pause/resume final state (D7 parity), plus the
# Temporal-only durable timeout route (D4).
# ---------------------------------------------------------------------------


def _scaffold_with_human(
    tmp_path: Path, *, timeout: int | None = None, on_timeout: str | None = None
) -> Path:
    """``text → step1 → [HUMAN approval] → step2`` — a durable-HITL workflow.

    ``runtime: temporal`` drives the Temporal backend; the native runner is
    invoked directly in the conformance test (it ignores the field). When
    ``timeout`` is set the HUMAN node carries the durable deadline + the
    ``on_timeout`` route (ADR 062 D4).
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
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    approval: dict[str, Any] = {
        "id": "approval",
        "type": "human",
        "prompt": "Approve the step?",
        "output_contract": ["approved_by"],
    }
    if timeout is not None:
        approval["timeout"] = timeout
    if on_timeout is not None:
        approval["on_timeout"] = on_timeout
    body: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "test-human-workflow",
        "version": "0.1.0",
        "state_schema": "./state.json",
        "entrypoint": "first",
        "runtime": "temporal",
        "nodes": [
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            approval,
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        "edges": [
            {"from": "first", "to": "approval"},
            {"from": "approval", "to": "second"},
        ],
    }
    (workflow_dir / "workflow.yaml").write_text(yaml.safe_dump(body))
    return workflow_dir / "workflow.yaml"


def _human_worker(env: Any, workflow_cls: Any) -> Any:
    """A Worker registering every activity the compiled workflow may call."""
    return Worker(
        env.client,
        task_queue=DEFAULT_TASK_QUEUE,
        workflows=[workflow_cls],
        activities=[
            call_agent_activity,
            call_skill_activity,
            call_gate_activity,
            call_judge_activity,
            call_human_activity,
            persist_workflow_result_activity,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


@pytest.mark.smoke
async def test_temporal_human_node_pause_resume_matches_native(tmp_path: Path) -> None:
    """A HUMAN node pauses durably on Temporal and resumes on a ``human_response``
    signal to the SAME final state the native runner reaches via pause + resume
    (ADR 062 / ADR 055 D7 — durable-HITL parity)."""
    pricing = load_pricing()
    initial_state = {"text": "hello"}
    decision = {"approved_by": "alice"}
    graph = _load_graph(_scaffold_with_human(tmp_path))

    # --- NATIVE baseline: run → PAUSED, merge decision, resume → SUCCESS ---
    native_storage = InMemoryStorage()
    await native_storage.init()
    native_runner = WorkflowRunner(
        executor=_build_executor(native_storage, NullTracer(), pricing),
        storage=native_storage,
    )
    paused = await native_runner.run(graph, initial_state=dict(initial_state))
    assert paused.status is WorkflowStatus.PAUSED
    record = await native_storage.get_workflow_run(paused.workflow_run_id, tenant_id="local")
    assert record is not None
    assert record.human_task is not None and record.human_task["output_contract"] == ["approved_by"]
    # Emulate the signal endpoint: merge the decision into the checkpoint.
    merged = {**(record.paused_state or {}), **decision}
    resumed = record.model_copy(update={"paused_state": merged})
    native_result = await native_runner.resume(graph, resumed)
    assert native_result.status is WorkflowStatus.SUCCESS
    assert native_result.final_state == {
        "text": "hello",
        "step1": "alpha",
        "approved_by": "alice",
        "step2": "beta",
    }

    # --- TEMPORAL: start → durable pause → signal → SUCCESS ---------------
    temporal_storage = InMemoryStorage()
    await temporal_storage.init()
    configure_activities(
        storage=temporal_storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_StateAwareProvider(),
        tenant_id="local",
    )
    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )

    env = await WorkflowEnvironment.start_time_skipping()
    async with env, _human_worker(env, workflow_cls):
        handle = await env.client.start_workflow(
            workflow_cls.run,
            {**initial_state, "tenant_id": "local"},
            id="conformance-human-1",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        # Deliver the human's decision; the durable wait_condition resolves.
        await handle.signal("human_response", args=["approval", decision])
        temporal_final = await handle.result()

    # ADR 080 D2 — terminal-state sync: after the durable run resumes + completes,
    # the store holds the TERMINAL record (SUCCESS, runtime temporal, final state),
    # having overwritten the PAUSED checkpoint — so mdk runs show + the
    # ?status=paused approvals list reflect reality.
    final_record = await temporal_storage.get_workflow_run("conformance-human-1", tenant_id="local")
    assert final_record is not None
    assert final_record.runtime == "temporal"
    assert final_record.status is WorkflowStatus.SUCCESS
    assert final_record.final_state is not None
    assert final_record.final_state.get("approved_by") == "alice"

    temporal_final.pop("tenant_id", None)
    assert temporal_final == native_result.final_state


@pytest.mark.smoke
async def test_temporal_human_node_durable_timeout_route(tmp_path: Path) -> None:
    """With no human response before the durable deadline, the HUMAN node takes
    the ``on_timeout`` route (ADR 062 D4). Native has no durable timer (it waits
    forever), so this capability is Temporal-only and asserted on Temporal."""
    pricing = load_pricing()
    graph = _load_graph(_scaffold_with_human(tmp_path, timeout=3600, on_timeout="second"))

    temporal_storage = InMemoryStorage()
    await temporal_storage.init()
    configure_activities(
        storage=temporal_storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_StateAwareProvider(),
        tenant_id="local",
    )
    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )

    env = await WorkflowEnvironment.start_time_skipping()
    async with env, _human_worker(env, workflow_cls):
        # No signal — the time-skipping server fast-forwards past the deadline,
        # firing the durable timeout, which routes to 'second'.
        temporal_final = await env.client.execute_workflow(
            workflow_cls.run,
            {"text": "hello", "tenant_id": "local"},
            id="timeout-human-1",
            task_queue=DEFAULT_TASK_QUEUE,
        )

    temporal_final.pop("tenant_id", None)
    # The timeout route ran 'second' (step2 present) but no human contributed.
    assert "approved_by" not in temporal_final
    assert temporal_final.get("step1") == "alpha"
    assert temporal_final.get("step2") == "beta"


# ---------------------------------------------------------------------------
# Temporal SDK metrics → OTel runtime builder (ADR 082 follow-on).
# _build_temporal_metrics_runtime() must be fail-soft + opt-in: None unless an
# OTLP endpoint AND an OTLP-bearing sink are configured; a real Runtime when both
# are. No live server needed — Runtime construction only sets up core telemetry.
# ---------------------------------------------------------------------------
def test_metrics_runtime_none_without_otlp_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "MOVATE_TRACE_SINK",
    ):
        monkeypatch.delenv(var, raising=False)
    assert _build_temporal_metrics_runtime() is None


def test_metrics_runtime_none_when_sink_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Endpoint present but the operator turned the sink off → no Temporal metrics
    # (mirrors mdk's own metrics gate).
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("MOVATE_TRACE_SINK", "none")
    assert _build_temporal_metrics_runtime() is None


def test_metrics_runtime_built_when_otlp_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    runtime = _build_temporal_metrics_runtime()
    assert runtime is not None
    # It's a real temporalio Runtime (used as Client.connect(runtime=...)).
    from temporalio.runtime import Runtime  # noqa: PLC0415

    assert isinstance(runtime, Runtime)


# ---------------------------------------------------------------------------
# Trace propagation across the workflow→activity boundary — the interceptor is
# gated on the same OTLP-sink condition as the metrics runtime, and (the
# load-bearing assertion) a temporal run is ONE connected trace, not N orphans.
# ---------------------------------------------------------------------------
def test_tracing_interceptors_empty_without_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "MOVATE_TRACE_SINK",
    ):
        monkeypatch.delenv(var, raising=False)
    assert _tracing_interceptors() == []


def test_tracing_interceptors_empty_when_sink_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("MOVATE_TRACE_SINK", "none")
    assert _tracing_interceptors() == []


def test_tracing_interceptors_built_when_otlp_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    interceptors = _tracing_interceptors()
    assert len(interceptors) == 1
    from temporalio.contrib.opentelemetry import TracingInterceptor  # noqa: PLC0415

    assert isinstance(interceptors[0], TracingInterceptor)


@pytest.mark.smoke
async def test_temporal_trace_propagates_across_activities(tmp_path: Path) -> None:
    """The fix: with the tracing interceptor wired onto the client, a compiled
    multi-agent workflow's activity spans share ONE trace with the workflow —
    not the orphan-per-activity traces the unwired path produced.

    Hermetic: an injected in-memory tracer (the interceptor's ``tracer=`` seam)
    captures the StartWorkflow → RunWorkflow → RunActivity spans without touching
    the global OTel provider, and the SDK's time-skipping env stands in for a
    real server.
    """
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )
    from temporalio.client import Client  # noqa: PLC0415
    from temporalio.contrib.opentelemetry import TracingInterceptor  # noqa: PLC0415

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    interceptor = TracingInterceptor(provider.get_tracer("test"))

    pricing = load_pricing()
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_StateAwareProvider(),
        tenant_id="local",
    )
    graph = _load_graph(_scaffold_two_step(tmp_path))
    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )

    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        # Reconstruct the env's client WITH the interceptor (the same thing
        # _tracing_interceptors() does at the real Client.connect edge).
        client = Client(**{**env.client.config(), "interceptors": [interceptor]})
        async with Worker(
            client,
            task_queue=DEFAULT_TASK_QUEUE,
            workflows=[workflow_cls],
            activities=[
                call_agent_activity,
                call_skill_activity,
                call_gate_activity,
                call_judge_activity,
                persist_workflow_result_activity,
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await client.execute_workflow(
                workflow_cls.run,
                {"text": "hello", "tenant_id": "local"},
                id="trace-link-1",
                task_queue=DEFAULT_TASK_QUEUE,
            )

    spans = exporter.get_finished_spans()
    assert spans, "the tracing interceptor emitted no spans"
    # THE assertion: every span (workflow start/run + each activity) is in ONE
    # trace. Before the fix the activities were orphans ⇒ multiple trace ids.
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 1, (
        f"expected one connected trace, got {len(trace_ids)} ({[s.name for s in spans]})"
    )
    names = [s.name for s in spans]
    assert any("Workflow" in n for n in names), names
    assert any("Activity" in n for n in names), names
