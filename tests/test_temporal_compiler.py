"""Tests for the Phase 1 Temporal workflow compiler (ADR 054 Track B).

These tests use real ``workflow.yaml`` fixtures from ``src/movate/templates/``
to exercise the compiler against the 5 governed patterns (chatbot ≈
workflow-starter / task-oriented / goal-oriented / monitor / simulation).

The Temporal SDK is mocked via ``sys.modules`` patching so the suite passes
without the ``[temporal]`` extra installed (the compiler module itself is
lazy-imported per ADR 054 D7's import-isolation contract).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

from movate.core.workflow.compiler import WorkflowCompileError, compile_workflow
from movate.core.workflow.compilers.temporal import (
    LINT_NONDETERMINISTIC_SKILL,
    LINT_NONDETERMINISTIC_TIME,
    LINT_UNBOUNDED_LOOP,
    CompiledWorkflow,
    CompilerProtocol,
    LintIssue,
    TemporalCompiler,
    compile_temporal,
    lint_temporal,
    supports_spec,
)
from movate.core.workflow.ir import (
    EdgeKind,
    NodeType,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)
from movate.core.workflow.spec import load_workflow_spec

# ---------------------------------------------------------------------------
# Test fixtures — point at the real shipped pattern templates so we cover
# the 5 governed shapes end-to-end (chatbot is single-agent; the linear
# workflow_starter is its multi-step proxy because pattern_chatbot itself
# ships an agent.yaml only).
# ---------------------------------------------------------------------------

_TEMPLATES = Path(__file__).resolve().parent.parent / "src/movate/templates"
PATTERN_FIXTURES: dict[str, Path] = {
    # workflow_starter is the linear-pipeline analogue of pattern_chatbot
    # (the chatbot pattern is single-agent, no workflow.yaml).
    "chatbot": _TEMPLATES / "workflow_starter" / "workflow.yaml",
    "goal_oriented": _TEMPLATES / "pattern_goal_oriented" / "workflow.yaml",
    "task_oriented": _TEMPLATES / "pattern_task_oriented" / "workflow.yaml",
    "monitor": _TEMPLATES / "pattern_monitor" / "workflow.yaml",
    "simulation": _TEMPLATES / "pattern_simulation" / "workflow.yaml",
}


def _load_graph(name: str) -> WorkflowGraph:
    """Parse + compile a pattern fixture's workflow.yaml into the IR."""
    spec, workflow_dir = load_workflow_spec(PATTERN_FIXTURES[name])
    return compile_workflow(spec, workflow_dir)


def _make_node(nid: str, ntype: NodeType = NodeType.AGENT, **meta: Any) -> WorkflowNode:
    return WorkflowNode(id=nid, type=ntype, ref=f"/agents/{nid}", metadata=dict(meta))


def _make_graph(
    nodes: list[WorkflowNode], edges: list[WorkflowEdge], entrypoint: str | None = None
) -> WorkflowGraph:
    return WorkflowGraph(
        name="test-flow",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint=entrypoint or nodes[0].id,
        nodes={n.id: n for n in nodes},
        edges=edges,
        workflow_dir=Path("/tmp/fake"),
    )


# ---------------------------------------------------------------------------
# 1-5: end-to-end compile of each governed pattern.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_chatbot_pattern() -> None:
    """Linear chatbot-like workflow compiles to a valid ``@workflow.defn`` class."""
    graph = _load_graph("chatbot")
    result = TemporalCompiler().compile(graph)
    assert isinstance(result, CompiledWorkflow)
    # The output must parse as Python.
    tree = ast.parse(result.module_source)
    # And it must define exactly one class decorated with @workflow.defn.
    class_defs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert len(class_defs) == 1
    assert any(
        isinstance(dec, ast.Attribute) and dec.attr == "defn"
        for dec in class_defs[0].decorator_list
    )
    assert "@workflow.defn" in result.module_source
    assert "call_agent_activity" in result.activity_names
    # Reads cleanly under D10 — control flow only, conversation in session.
    assert "run_id = workflow.info().workflow_id" in result.module_source
    # ADR 082 follow-on — duration instrumentation: capture the deterministic
    # start time and pass the computed duration to the terminal persist activity
    # on BOTH terminal paths (success + handled error).
    src = result.module_source
    assert "_wf_start = workflow.info().start_time" in src
    assert src.count("(workflow.now() - _wf_start).total_seconds() * 1000.0") == 2


@pytest.mark.unit
def test_compile_goal_oriented_pattern() -> None:
    """Goal-oriented compiles + the emitted bounded-loop helper produces the
    canonical ``for _i in range(N)`` shape per ADR 054 D4 row 6."""
    graph = _load_graph("goal_oriented")
    compiler = TemporalCompiler()
    result = compiler.compile(graph)
    ast.parse(result.module_source)
    # Goal-oriented uses an intent-router as the JUDGE/GATE; emitter
    # produces call_gate_activity for each router node.
    assert "call_gate_activity" in result.activity_names
    # Each AGENT worker → call_agent_activity.
    assert "call_agent_activity" in result.activity_names

    # The bounded-loop helper independently produces the canonical shape.
    loop = compiler.emit_bounded_loop(2, ["pass"])
    joined = "\n".join(loop)
    assert "for _i in range(2):" in joined


@pytest.mark.unit
def test_compile_task_oriented_pattern() -> None:
    """Task-oriented compiles + the fan-out helper produces ``asyncio.gather``
    per ADR 054 D4 row 8."""
    graph = _load_graph("task_oriented")
    compiler = TemporalCompiler()
    result = compiler.compile(graph)
    ast.parse(result.module_source)

    # Each task agent is an AGENT node → call_agent_activity.
    assert "call_agent_activity" in result.activity_names

    # Fan-out helper produces gather. (The task-oriented template models the
    # bound as a linear chain per its governance note; the helper is the
    # canonical lowering for concurrent siblings when authors opt in.)
    fanout = compiler.emit_fan_out(
        "call_agent_activity",
        ["task-a", "task-b"],
    )
    joined = "\n".join(fanout)
    assert "asyncio.gather" in joined
    assert "'task-a'" in joined
    assert "'task-b'" in joined


@pytest.mark.unit
def test_compile_simulation_pattern() -> None:
    """Simulation compiles; the JUDGE/GATE emitter encodes the turn-cap
    terminate logic (per ADR 054 D4 row 4: judge → branch on
    ``verdict.terminate``)."""
    graph = _load_graph("simulation")
    compiler = TemporalCompiler()
    result = compiler.compile(graph)
    ast.parse(result.module_source)
    # JUDGE/GATE nodes (turn-judge) lower to call_gate_activity.
    assert "call_gate_activity" in result.activity_names

    # The JUDGE shape (terminate branch) is encoded by _emit_judge_node.
    # Construct a minimal JUDGE node fixture and assert the shape.
    judge = WorkflowNode(id="j", type=NodeType.AGENT, ref="/agents/j")
    lines, used = compiler._emit_judge_node("j", judge, graph)
    src = "\n".join(lines)
    assert "call_judge_activity" in used
    assert "verdict.get('terminate')" in src
    assert "return state" in src


@pytest.mark.unit
def test_compile_monitor_pattern() -> None:
    """Monitor compiles + the GATE node emits branch-comments documenting
    the routing decisions per ADR 054 D4 row 3."""
    graph = _load_graph("monitor")
    result = TemporalCompiler().compile(graph)
    ast.parse(result.module_source)
    # threshold-gate (intent-router) → call_gate_activity.
    assert "call_gate_activity" in result.activity_names
    # Both routes are documented in the emitted source.
    assert "route 'breach'" in result.module_source
    assert "route 'ok'" in result.module_source


# ---------------------------------------------------------------------------
# 6: HUMAN node — durable HITL (ADR 062): wait_condition + signal.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_human_node_durable_hitl() -> None:
    """HUMAN node compiles to a durable pause: pause-record activity + a
    ``wait_condition`` on a ``human_response`` signal, merging the
    ``output_contract`` and advancing to the sequential successor (ADR 062)."""
    human = WorkflowNode(
        id="approval",
        type=NodeType.HUMAN,
        ref="",
        metadata={"prompt": "Approve?", "output_contract": ["decision"]},
    )
    graph = _make_graph(
        [_make_node("start"), human, _make_node("done")],
        [
            WorkflowEdge(from_id="start", to_id="approval"),
            WorkflowEdge(from_id="approval", to_id="done"),
        ],
    )
    compiled = TemporalCompiler().compile(graph)
    src = compiled.module_source
    # The durable-HITL primitives are emitted...
    assert "call_human_activity" in src
    assert "@workflow.signal" in src
    assert "def human_response(self" in src
    assert "self._human" in src
    assert "wait_condition(lambda: 'approval' in self._human)" in src
    # ...the response merges output_contract and advances to the successor.
    assert "for k in ['decision']" in src
    assert "current = 'done'" in src
    # ...and the pause-record activity is registered for the worker.
    assert "call_human_activity" in compiled.activity_names
    # The compiled module is valid Python (parses + exec-imports cleanly).
    compile(src, "<emitted-human>", "exec")


@pytest.mark.unit
def test_compile_human_node_timeout_route() -> None:
    """A HUMAN node with ``timeout`` + ``on_timeout`` emits the durable
    deadline + the timeout route (ADR 062 D4)."""
    human = WorkflowNode(
        id="approval",
        type=NodeType.HUMAN,
        ref="",
        metadata={
            "prompt": "Approve?",
            "output_contract": ["decision"],
            "timeout": 3600,
            "on_timeout": "done",
        },
    )
    graph = _make_graph(
        [_make_node("start"), human, _make_node("done")],
        [
            WorkflowEdge(from_id="start", to_id="approval"),
            WorkflowEdge(from_id="approval", to_id="done"),
        ],
    )
    src = TemporalCompiler().compile(graph).module_source
    assert "timeout=timedelta(seconds=3600.0)" in src
    assert "except asyncio.TimeoutError:" in src
    compile(src, "<emitted-human-timeout>", "exec")


# ---------------------------------------------------------------------------
# 7-9: Linter — Phase 1 emits warnings, never errors.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lint_warns_on_time_time_call() -> None:
    """A ``time.time()`` reference in node metadata → warning, not error."""
    node = _make_node("nondet", prompt="now = time.time()")
    graph = _make_graph([node], [])
    issues = TemporalCompiler().lint(graph)
    codes = {i.code for i in issues}
    assert LINT_NONDETERMINISTIC_TIME in codes
    # Warnings only — Phase 1 contract.
    assert all(i.severity == "warning" for i in issues)


@pytest.mark.unit
def test_lint_clean_on_human_node() -> None:
    """HUMAN node is first-class (ADR 062) — the linter no longer flags it."""
    human = WorkflowNode(
        id="hold",
        type=NodeType.HUMAN,
        ref="",
        metadata={"prompt": "Wait", "output_contract": []},
    )
    graph = _make_graph([_make_node("a"), human], [WorkflowEdge(from_id="a", to_id="hold")])
    issues = TemporalCompiler().lint(graph)
    human_codes = [i.code for i in issues if "HUMAN" in i.code]
    assert not human_codes, f"expected no HUMAN lint, got {issues}"


@pytest.mark.unit
def test_lint_warns_on_unbounded_loop() -> None:
    """A back-edge with no ``max_iterations`` bound → unbounded-loop warning."""
    a = _make_node("a")
    b = _make_node("b")
    graph = _make_graph(
        [a, b],
        [
            WorkflowEdge(from_id="a", to_id="b"),
            WorkflowEdge(from_id="b", to_id="a"),  # back-edge: cycle
        ],
    )
    issues = TemporalCompiler().lint(graph)
    codes = {i.code for i in issues}
    assert LINT_UNBOUNDED_LOOP in codes
    assert all(i.severity == "warning" for i in issues)


@pytest.mark.unit
def test_lint_warns_on_nondeterministic_skill() -> None:
    """``capabilities.deterministic=false`` on a node → warning."""
    node = _make_node("skill-n", capabilities={"deterministic": False})
    graph = _make_graph([node], [])
    issues = TemporalCompiler().lint(graph)
    codes = {i.code for i in issues}
    assert LINT_NONDETERMINISTIC_SKILL in codes


# ---------------------------------------------------------------------------
# 10: Lazy import — the compiler module imports without temporalio installed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lazy_temporalio_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compiler module imports cleanly even when ``temporalio`` is absent.

    Patches ``sys.modules`` to hide ``temporalio`` and re-imports the
    compiler; the module itself must load (so ``mdk`` keeps booting
    without the [temporal] extra). Only ``.compile()`` invokes
    ``_require_temporalio()`` and raises a clear install hint.
    """
    # Force a fresh import while hiding any installed temporalio.
    blocked = [m for m in sys.modules if m == "temporalio" or m.startswith("temporalio.")]
    saved = {m: sys.modules[m] for m in blocked}
    for m in blocked:
        del sys.modules[m]

    # Also block re-imports during this test.
    class _BlockTemporalioFinder:
        def find_module(self, name: str, path: Any = None) -> Any:
            return self if name == "temporalio" or name.startswith("temporalio.") else None

        def load_module(self, name: str) -> Any:
            raise ImportError(f"hidden by test: {name}")

    finder = _BlockTemporalioFinder()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    # Remove the compiler module so the import is fresh.
    if "movate.core.workflow.compilers.temporal" in sys.modules:
        del sys.modules["movate.core.workflow.compilers.temporal"]

    try:
        # Importing must succeed (module is import-safe per ADR 054 D7).
        import movate.core.workflow.compilers.temporal as fresh_mod  # noqa: PLC0415

        # And the lazy gate raises a clear install hint when invoked.
        with pytest.raises(RuntimeError) as ei:
            fresh_mod._require_temporalio()
        assert "[temporal] extra is not installed" in str(ei.value)
        assert "uv tool install" in str(ei.value)
    finally:
        # Restore the original temporalio modules so other tests see them.
        for m, mod in saved.items():
            sys.modules[m] = mod
        # Drop our patched compiler module so the next test gets the normal one.
        if "movate.core.workflow.compilers.temporal" in sys.modules:
            del sys.modules["movate.core.workflow.compilers.temporal"]


# ---------------------------------------------------------------------------
# 11: TemporalCompiler conforms to the runner Protocol shape.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compiler_implements_runner_protocol() -> None:
    """TemporalCompiler structurally satisfies the runner-Protocol seam.

    The runner Protocol (ADR 054 D1 / ADR 030 D1) keeps the three backends
    swappable: a concrete compiler must expose ``compile(spec)`` and
    ``lint(spec)`` with the agreed return shapes. This test pins that
    contract by treating an instance as a :class:`CompilerProtocol` value.
    """
    compiler: CompilerProtocol = TemporalCompiler()
    # Smoke-call both methods on a real graph.
    graph = _load_graph("chatbot")
    compiled = compiler.compile(graph)
    issues = compiler.lint(graph)
    assert isinstance(compiled, CompiledWorkflow)
    assert isinstance(issues, list)
    # Every LintIssue is the dataclass shape we promised.
    for i in issues:
        assert isinstance(i, LintIssue)
        assert i.severity in {"warning", "error"}


# ---------------------------------------------------------------------------
# Extra: module-level helpers compile_temporal / lint_temporal / supports_spec
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_module_level_helpers() -> None:
    """Convenience wrappers preserve parity with the class API."""
    graph = _load_graph("chatbot")
    compiled = compile_temporal(graph)
    issues = lint_temporal(graph)
    assert isinstance(compiled, CompiledWorkflow)
    assert isinstance(issues, list)

    spec, _ = load_workflow_spec(PATTERN_FIXTURES["chatbot"])
    assert supports_spec(spec) is True


# ---------------------------------------------------------------------------
# ADR 056 D5 — JUDGE node lowers LIVE onto Temporal (resolves §11 caveat).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_judge_node_emits_live_judge_activity() -> None:
    """A JUDGE IR node lowers to a call_judge_activity with the ref + config.

    The emitter is no longer the canonical-but-unused shape: a real JUDGE node
    (ADR 056 D1) passes its ``judge_agent`` ref + judge_config to the activity
    and gates on ``terminate``. The generated source must parse.
    """
    judge = WorkflowNode(
        id="judge",
        type=NodeType.JUDGE,
        ref="/agents/judge",
        metadata={
            "criteria": "",
            "input_field": "answer",
            "pass_threshold": 0.7,
            "on_accept": None,
            "on_revise": None,
            "max_iterations": 1,
        },
    )
    produce = _make_node("produce", NodeType.AGENT)
    graph = _make_graph(
        [produce, judge],
        [WorkflowEdge(from_id="produce", to_id="judge")],
        entrypoint="produce",
    )
    result = TemporalCompiler().compile(graph)
    ast.parse(result.module_source)  # generated source is valid Python
    assert "call_judge_activity" in result.activity_names
    src = result.module_source
    # Activity is called with the ref + a judge_config dict carrying the
    # threshold + input_field (so the activity runs the real judge — §11 fix).
    assert "'/agents/judge'" in src
    assert "'pass_threshold': 0.7" in src
    assert "'input_field': 'answer'" in src
    # And the workflow gates on terminate.
    assert "get('terminate')" in src
    assert "return state" in src


# ---------------------------------------------------------------------------
# Fan-out diamond (ADR 092 Phase 2 / D3) — the Temporal compiler lowers a
# canonical single-node-branch diamond to native `asyncio.gather` parallelism,
# joining by the declared strategy and advancing to the fan-in node.
# ---------------------------------------------------------------------------


def _diamond_graph(
    *, strategy: str | None = None, join_key: str | None = None, multi_node: bool = False
) -> WorkflowGraph:
    """start ⇉ {a, b} ⇉ merge (canonical diamond). ``multi_node`` makes branch
    ``a`` two nodes (a → a2 → merge) to exercise the Phase-2 single-node guard."""
    fan_in_meta: dict[str, Any] = {}
    if strategy is not None:
        fan_in_meta["join"] = strategy
    if join_key is not None:
        fan_in_meta["join_key"] = join_key
    if multi_node:
        nodes = [
            _make_node("start"),
            _make_node("a"),
            _make_node("a2"),
            _make_node("b"),
            _make_node("merge"),
        ]
        edges = [
            WorkflowEdge("start", "a", EdgeKind.PARALLEL_FAN_OUT),
            WorkflowEdge("start", "b", EdgeKind.PARALLEL_FAN_OUT),
            WorkflowEdge("a", "a2", EdgeKind.SEQUENTIAL),
            WorkflowEdge("a2", "merge", EdgeKind.PARALLEL_FAN_IN, metadata=dict(fan_in_meta)),
            WorkflowEdge("b", "merge", EdgeKind.PARALLEL_FAN_IN, metadata=dict(fan_in_meta)),
        ]
    else:
        nodes = [_make_node("start"), _make_node("a"), _make_node("b"), _make_node("merge")]
        edges = [
            WorkflowEdge("start", "a", EdgeKind.PARALLEL_FAN_OUT),
            WorkflowEdge("start", "b", EdgeKind.PARALLEL_FAN_OUT),
            WorkflowEdge("a", "merge", EdgeKind.PARALLEL_FAN_IN, metadata=dict(fan_in_meta)),
            WorkflowEdge("b", "merge", EdgeKind.PARALLEL_FAN_IN, metadata=dict(fan_in_meta)),
        ]
    return _make_graph(nodes, edges, entrypoint="start")


@pytest.mark.unit
def test_compile_fan_out_diamond_last_wins() -> None:
    src = TemporalCompiler().compile(_diamond_graph()).module_source
    compile(src, "<emitted-fanout>", "exec")  # valid Python
    # The fan-out node runs its own agent, then gathers the branches concurrently.
    assert "start_branches = await asyncio.gather(" in src
    assert src.count("call_agent_activity,") >= 4  # start + a + b + merge
    # last-wins join + advance to the fan-in node.
    assert "for _b in start_branches:" in src
    assert "state.update(_b)" in src
    assert "current = 'merge'" in src
    # Branch nodes are emitted INSIDE the gather, not as standalone dispatch arms.
    assert "current == 'a'" not in src
    assert "current == 'b'" not in src
    # The join node is a normal dispatch arm.
    assert "current == 'merge'" in src


@pytest.mark.unit
def test_compile_fan_out_join_by_key() -> None:
    src = TemporalCompiler().compile(_diamond_graph(strategy="by_key")).module_source
    compile(src, "<emitted-fanout-bykey>", "exec")
    assert "state['a'] = dict(start_branches[0])" in src
    assert "state['b'] = dict(start_branches[1])" in src


@pytest.mark.unit
def test_compile_fan_out_join_collect() -> None:
    src = (
        TemporalCompiler()
        .compile(_diamond_graph(strategy="collect", join_key="results"))
        .module_source
    )
    compile(src, "<emitted-fanout-collect>", "exec")
    assert "state['results'] = [_b.get('results') for _b in start_branches]" in src


@pytest.mark.unit
def test_compile_fan_out_multi_node_branch_rejected() -> None:
    """A multi-node fan-out branch fails loud on Temporal (Phase 2 = single-node
    canonical diamond); the author is pointed at runtime: native."""
    with pytest.raises(WorkflowCompileError, match="single-node"):
        TemporalCompiler().compile(_diamond_graph(multi_node=True))
