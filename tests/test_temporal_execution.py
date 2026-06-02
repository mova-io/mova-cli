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
    call_judge_activity,
    call_skill_activity,
    configure_activities,
)
from movate.runtime.workflow_backend import (  # noqa: E402
    DEFAULT_TASK_QUEUE,
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
def test_runtime_field_defaults_native(tmp_path: Path) -> None:
    """A workflow.yaml with no ``runtime:`` key compiles to graph.runtime='native'."""
    graph = _load_graph(_scaffold_two_step(tmp_path))
    assert graph.runtime == "native"
    # And the spec surfaces it too.
    spec, _ = load_workflow_spec(_scaffold_two_step(tmp_path / "b"))
    assert spec.runtime == "native"


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
    assert set(VALID_RUNTIMES) == {"native", "langgraph", "temporal"}


# ---------------------------------------------------------------------------
# D6 — fail loud, never silent downgrade.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_langgraph_execution_fails_loud() -> None:
    """Selecting langgraph for execution is rejected (ADR 055 step 3) — not downgraded."""
    with pytest.raises(WorkflowBackendError) as ei:
        require_backend_available("langgraph")
    assert "not yet wired" in str(ei.value)


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
