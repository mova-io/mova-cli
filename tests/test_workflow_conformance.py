"""Native-vs-Temporal conformance suite (ADR 055 D7).

ADR 055 D7 makes cross-backend behavioral equivalence the *precondition* for
offering a non-native runtime in the dispatch fork: one parametrized suite that
runs the same fixture workflows on the ``native`` :class:`WorkflowRunner` and on
the ``temporal`` backend (:func:`run_temporal_workflow`-equivalent wiring on
``temporalio.testing.WorkflowEnvironment.start_time_skipping()``) and asserts an
**identical terminal ``status`` + ``final_state``** for the same
``initial_state`` and the SAME deterministic, offline provider.

This generalizes the single hermetic smoke in ``test_temporal_execution.py``
(``test_temporal_smoke_matches_native``) into a parametrized matrix — one case
per fixture — covering the supported workflow shapes:

================  ==============================  ============================
fixture           shape                           source
================  ==============================  ============================
``linear_chain``  linear two-agent chain          hand-built (smoke analogue)
``task_oriented`` linear DAG, four agent nodes     ``templates/pattern_task_oriented``
``gate_router``   intent-router branch            hand-built minimal router
``goal_oriented`` bounded loop + JUDGE/GATE        ``templates/pattern_goal_oriented``
``monitor``       observe → GATE → action         ``templates/pattern_monitor``
``simulation``    bounded turns + JUDGE/GATEs      ``templates/pattern_simulation``
================  ==============================  ============================

HUMAN nodes are deliberately excluded (ADR 054 Phase 2 / ADR 055 D7 shared
subset) — the Temporal compiler rejects them in Phase 1.

Backend-metadata normalization (documented, per the task brief): the two
backends legitimately differ on *result bookkeeping*, not on terminal
behaviour. We compare ``status`` + ``final_state`` only and DO NOT compare the
``runs`` list:

* the native runner returns per-node ``RunRecord`` s in
  :attr:`WorkflowResult.runs`;
* the Temporal path persists per-node records through storage (via the
  Executor) and leaves ``WorkflowResult.runs`` empty by construction (see
  :func:`movate.runtime.workflow_backend.run_temporal_workflow` — "the in-memory
  ``runs`` list is left empty here because the Temporal path records them
  through storage").

We also strip the ``tenant_id`` the Temporal path stamps into the initial state
for activity context (the same normalization the smoke does).

Branching conformance (the gap this suite originally caught — now closed). The
branching fixtures (``gate_router`` / ``goal_oriented`` / ``monitor`` /
``simulation``) exercise GATE/INTENT_ROUTER routing and assert native==temporal
just like the linear ones. They were previously ``xfail(strict=True)`` because
of two real Phase-1 divergences:

1. **The Temporal compiler did not branch.** ``TemporalCompiler._emit_gate_node``
   emitted an intent-router as a *linear* block (route table as comments only),
   so every node executed unconditionally. It now emits REAL control flow — a
   dispatch loop that follows ``routes[label]`` (or ``fallback``) on the gate
   activity's decision, matching the native runner's chosen-branch traversal
   (under ``mock`` the first sorted route key; each fixture's ``label`` is that
   key, so the deterministic provider drives the same branch).
2. **The gate activity could not resolve a relative classifier ref.** The IR
   rewrites AGENT ``node.ref`` to an absolute path but leaves a gate's
   ``classifier_agent`` ref relative (e.g. ``./agents/goal-judge``).
   :func:`call_gate_activity` now resolves it against the workflow dir the
   compiler bakes into the activity args (the same resolution the native runner
   does), so ``load_agent`` succeeds on the worker.

The ``xfail(strict=True)`` mechanism (see :func:`_param`) is retained for any
*future* divergence: a fixture given an ``xfail_reason`` stays green while the
gap is open and reports a failing XPASS the moment it closes, keeping D7 a
living gate rather than a comment.

Hermetic: a deterministic offline provider (no network, no keys) +
:class:`InMemoryStorage` + the SDK's in-memory time-skipping test server. The
whole module ``importorskip`` s ``temporalio`` so it skips cleanly without the
``[temporal]`` extra.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.core.workflow.runner import WorkflowResult, WorkflowRunner
from movate.providers.base import BaseLLMProvider, CompletionRequest, CompletionResponse
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

# ``temporalio`` is the opt-in [temporal] extra. importorskip at module scope
# skips the WHOLE module (incl. the top-level SDK imports below) when it is
# absent — so the conformance suite skips cleanly on a native-only install,
# mirroring ``test_temporal_execution.py``.
pytest.importorskip(
    "temporalio",
    reason="the [temporal] extra is not installed; native-vs-Temporal conformance skipped",
)

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.temporal_activities import (
    call_agent_activity,
    call_gate_activity,
    call_human_activity,
    call_judge_activity,
    call_skill_activity,
    configure_activities,
    persist_workflow_result_activity,
)
from movate.runtime.workflow_backend import (
    DEFAULT_TASK_QUEUE,
    load_compiled_workflow_class,
)

_TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "movate" / "templates"


# ---------------------------------------------------------------------------
# Deterministic, offline provider — the conformance precondition (ADR 055 D7).
#
# A single shared provider must satisfy EVERY agent's output schema in a given
# workflow so that native and Temporal see byte-identical agent outputs (the
# only way the two backends are comparable). Every packaged pattern node uses
# ``additionalProperties: true`` output schemas, so a UNION of all the string
# keys any fixture node requires validates against each individual node's
# schema. The one constrained field is a JUDGE/GATE ``label`` enum, which
# differs per pattern (``breach|ok`` / ``continue|satisfied`` /
# ``continue|resolved``) — so the provider is constructed with a per-fixture
# ``label`` that is valid for that pattern's classifier.
#
# Deterministic + offline (no real LLM, no keys, no network) so the native
# runner and the Temporal backend produce identical state for the same
# workflow.
# ---------------------------------------------------------------------------


class _ConformanceProvider(BaseLLMProvider):
    """Returns a fixed union of every output key the fixtures' agents require.

    Each value is a constant string (or the per-fixture ``label``), so two
    runs on two backends with the same workflow produce identical state.
    """

    name = "conformance"
    version = "0.0.1"

    def __init__(self, *, label: str = "ok") -> None:
        self._label = label

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload: dict[str, Any] = {
            # linear-chain hand-built fixture
            "step1": "alpha",
            "step2": "beta",
            # gate-router hand-built fixture (both branch outputs, so the union
            # validates whichever branch the router chooses)
            "high_result": "hr",
            "low_result": "lr",
            # workflow_starter-shaped keys
            "draft": "d",
            "final": "f",
            # task-oriented
            "plan": "p",
            "task_a_result": "ta",
            "task_b_result": "tb",
            "answer": "ans",
            # goal-oriented
            "attempt": "att",
            "result": "r",
            # monitor
            "metric": "m",
            "action_taken": "a",
            "status": "ok",
            # simulation
            "transcript": "t",
            "outcome": "o",
            # JUDGE/GATE classifiers (enum-constrained per pattern)
            "label": self._label,
        }
        return CompletionResponse(text=json.dumps(payload))

    async def stream(self, request: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Hand-built linear fixture (the simple agent-chain case).
#
# A faithful copy of ``test_temporal_execution.py::_scaffold_two_step`` so the
# conformance suite owns a self-contained linear baseline that does not depend
# on any packaged template staying runnable. ``text -> step1 -> step2``.
# ---------------------------------------------------------------------------

_LINEAR_STATE_SCHEMA = {
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
                # ``additionalProperties: true`` (matching every packaged pattern
                # node) so the single shared union provider's response validates
                # against each node — the conformance precondition.
                "additionalProperties": True,
                "required": [output_key],
                "properties": {output_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )


def _scaffold_linear_chain(tmp_path: Path) -> Path:
    """``text -> step1 -> step2`` (linear, two agent nodes)."""
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
    (workflow_dir / "state.json").write_text(json.dumps(_LINEAR_STATE_SCHEMA))
    (workflow_dir / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "conformance-linear",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "first",
                "nodes": [
                    {"id": "first", "type": "agent", "ref": "./agents/first"},
                    {"id": "second", "type": "agent", "ref": "./agents/second"},
                ],
                "edges": [{"from": "first", "to": "second"}],
            }
        )
    )
    return workflow_dir / "workflow.yaml"


# ---------------------------------------------------------------------------
# Hand-built gate/intent-router fixture (the minimal branching case).
#
# ``entry (agent) -> gate (intent-router) -> {high|low}``. Under the native
# runner's ``mock`` path the router picks the first sorted route key (``high``),
# so only ``entry`` and ``high`` run. The classifier agent exists on disk so the
# native NON-mock path could resolve it too, but the conformance run uses mock
# (no real classifier call) — matching how the packaged patterns are exercised.
# ---------------------------------------------------------------------------

_GATE_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "required": ["text"],
    "properties": {
        "text": {"type": "string"},
        "step1": {"type": "string"},
        "high_result": {"type": "string"},
        "low_result": {"type": "string"},
        "label": {"type": "string"},
    },
}


def _make_classifier_agent(agent_dir: Path) -> None:
    """A classifier agent whose output is the enum ``{high, low}`` ``label``."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "gate-judge",
                "version": "0.1.0",
                "description": "classifies into high|low",
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
    (agent_dir / "prompt.md").write_text("classify {{ input.text }} into {{ input.labels }}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "required": ["label"],
                "properties": {"label": {"type": "string", "enum": ["high", "low"]}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"text": "x"}, "expected": {"label": "high"}}) + "\n"
    )


def _scaffold_gate_router(tmp_path: Path) -> Path:
    """``entry -> gate(intent-router) -> {high|low}`` (minimal branch)."""
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "entry", name="entry-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        workflow_dir / "agents" / "high",
        name="high-agent",
        input_key="step1",
        output_key="high_result",
    )
    _make_agent(
        workflow_dir / "agents" / "low",
        name="low-agent",
        input_key="step1",
        output_key="low_result",
    )
    _make_classifier_agent(workflow_dir / "agents" / "gate-judge")
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_GATE_STATE_SCHEMA))
    (workflow_dir / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "conformance-gate",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "entry",
                "nodes": [
                    {"id": "entry", "type": "agent", "ref": "./agents/entry"},
                    {
                        "id": "gate",
                        "type": "intent-router",
                        "classifier_agent": "./agents/gate-judge",
                        "input_field": "step1",
                        "routes": {"high": "high", "low": "low"},
                        "fallback": "low",
                    },
                    {"id": "high", "type": "agent", "ref": "./agents/high"},
                    {"id": "low", "type": "agent", "ref": "./agents/low"},
                ],
                "edges": [{"from": "entry", "to": "gate"}],
            }
        )
    )
    return workflow_dir / "workflow.yaml"


# ---------------------------------------------------------------------------
# The fixture matrix.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fixture:
    """One conformance case.

    ``builder`` returns the workflow.yaml path (hand-built fixtures take a
    ``tmp_path``; packaged ones ignore it and point at the real template).
    ``label`` is the per-pattern JUDGE/GATE enum value the deterministic
    provider returns. ``xfail_reason`` (when set) marks a fixture as a *known*
    Phase-1 native!=temporal divergence — strict, so it flips to a failing
    XPASS if the gap ever closes.
    """

    name: str
    builder: Any
    initial_state: dict[str, Any]
    label: str
    xfail_reason: str | None = None


def _packaged(template: str) -> Any:
    """Return a builder that points at a shipped pattern template's workflow.yaml."""

    def _build(_tmp_path: Path) -> Path:
        return _TEMPLATES / template / "workflow.yaml"

    return _build


_FIXTURES: tuple[_Fixture, ...] = (
    # --- backends agree: linear shapes -----------------------------------
    _Fixture(
        name="linear_chain",
        builder=_scaffold_linear_chain,
        initial_state={"text": "hello"},
        label="ok",
    ),
    _Fixture(
        name="task_oriented",
        builder=_packaged("pattern_task_oriented"),
        initial_state={"request": "decompose me"},
        label="ok",
    ),
    # --- backends agree: branching shapes --------------------------------
    # The Temporal compiler now emits REAL branching for GATE/INTENT_ROUTER
    # nodes (TemporalCompiler._emit_gate_node → a dispatch loop that follows
    # routes[label]/fallback) and call_gate_activity resolves the relative
    # classifier ref against the baked-in workflow dir, so these match the
    # native runner's chosen-branch traversal node-for-node (ADR 055 D7).
    _Fixture(
        name="gate_router",
        builder=_scaffold_gate_router,
        initial_state={"text": "hello"},
        label="high",
    ),
    _Fixture(
        name="goal_oriented",
        builder=_packaged("pattern_goal_oriented"),
        initial_state={"goal": "ship it"},
        label="continue",
    ),
    _Fixture(
        name="monitor",
        builder=_packaged("pattern_monitor"),
        initial_state={"signal": "5xx spiking"},
        label="breach",
    ),
    _Fixture(
        name="simulation",
        builder=_packaged("pattern_simulation"),
        initial_state={"scenario": "negotiate"},
        label="continue",
    ),
)


def _param(fixture: _Fixture) -> Any:
    """Wrap a fixture as a ``pytest.param`` with id + (optional) strict xfail.

    A known-divergent fixture is marked ``xfail(strict=True)`` rather than
    skipped, so the conformance comparison *still runs*: the case is expected to
    FAIL today (native != temporal) and the suite stays green, but the moment
    the Phase-1 gap closes the case XPASSes — which a strict xfail reports as a
    failure, forcing this suite to be updated. That makes D7 a living gate, not
    a comment.
    """
    marks = (
        (pytest.mark.xfail(reason=fixture.xfail_reason, strict=True),)
        if fixture.xfail_reason is not None
        else ()
    )
    return pytest.param(fixture, id=fixture.name, marks=marks)


def _load_graph(yaml_path: Path) -> Any:
    spec, parent = load_workflow_spec(yaml_path)
    return compile_workflow(spec, parent)


async def _run_native(
    yaml_path: Path, initial_state: dict[str, Any], *, label: str
) -> WorkflowResult:
    storage = InMemoryStorage()
    await storage.init()
    graph = _load_graph(yaml_path)
    executor = Executor(
        provider=_ConformanceProvider(label=label),
        pricing=_PRICING,
        storage=storage,
        tracer=NullTracer(),
    )
    runner = WorkflowRunner(executor=executor, storage=storage)
    # ``mock=True`` mirrors how the packaged patterns are exercised: the
    # intent-router picks the first sorted route key without calling the
    # classifier (the agent nodes still run through the Executor).
    return await runner.run(graph, initial_state=dict(initial_state), mock=True)


async def _run_temporal(
    yaml_path: Path, initial_state: dict[str, Any], *, label: str, wf_id: str
) -> dict[str, Any]:
    """Compile + execute on the in-memory time-skipping env; return final_state.

    Mirrors :func:`movate.runtime.workflow_backend.run_temporal_workflow`'s
    wiring (configure_activities + compile + load class + ephemeral worker on
    the shared task queue) but drives it on ``WorkflowEnvironment.
    start_time_skipping()`` so there is no externally-spawned Temporal server.
    """
    storage = InMemoryStorage()
    await storage.init()
    graph = _load_graph(yaml_path)
    configure_activities(
        storage=storage,
        pricing=_PRICING,
        tracer=NullTracer(),
        provider=_ConformanceProvider(label=label),
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
                call_human_activity,
                persist_workflow_result_activity,
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        final = await env.client.execute_workflow(
            workflow_cls.run,
            {**initial_state, "tenant_id": "local"},
            id=wf_id,
            task_queue=DEFAULT_TASK_QUEUE,
        )
    # Normalize the tenant_id the Temporal path stamps for activity context
    # (the same normalization the smoke does).
    final.pop("tenant_id", None)
    return final


_PRICING: PricingTable = load_pricing()


@pytest.mark.parametrize("fixture", [_param(f) for f in _FIXTURES])
async def test_native_temporal_conformance(fixture: _Fixture, tmp_path: Path) -> None:
    """Same fixture on native + Temporal → identical terminal status + final_state.

    The conformance assertion of ADR 055 D7: a workflow may only be offered on
    a non-native runtime once it behaves identically on the shared feature
    subset. We compare ``status`` + ``final_state`` (the terminal contract) and
    normalize the documented backend metadata (the empty Temporal ``runs`` list
    + the stamped ``tenant_id``).

    All fixtures — linear AND branching — are expected to pass: the Temporal
    compiler now emits real branching for GATE/INTENT_ROUTER nodes and the gate
    activity resolves the relative classifier ref, so native==temporal on the
    whole shared subset. A fixture given an ``xfail_reason`` (none today) would
    be marked ``xfail(strict=True)`` (see :func:`_param`) so a *future*
    divergence stays green while open and reports a failing XPASS once closed.
    """
    yaml_path = fixture.builder(tmp_path)

    native = await _run_native(yaml_path, fixture.initial_state, label=fixture.label)
    assert native.status is WorkflowStatus.SUCCESS, (
        f"{fixture.name}: native run did not succeed: {native.status} ({native.error})"
    )

    temporal_final = await _run_temporal(
        yaml_path,
        fixture.initial_state,
        label=fixture.label,
        wf_id=f"conformance-{fixture.name}",
    )

    # Terminal-state conformance: temporal's final state equals native's.
    assert temporal_final == native.final_state, (
        f"{fixture.name}: native != temporal final_state\n"
        f"  native:   {native.final_state}\n"
        f"  temporal: {temporal_final}"
    )
