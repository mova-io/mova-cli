"""B9 self-healing certification batch — structural + skill tests.

Covers the two scenarios of the B9 batch (program #16 / #26):

* ``agent-self-healing`` — TOOL health-check → DECISION(quality_score) →
  {healthy-report | diagnose → TOOL apply-fix → DECISION(fix_status) →
  {verify-report | HUMAN(ack) → incident-report}};
* ``self-healing-ops`` — TOOL detect → triage AGENT → TOOL remediate-1 →
  DECISION(r1_status) → {closure | TOOL remediate-2 → DECISION(r2_status) →
  {closure | HUMAN(ack) → closure}} — the two-attempt retry UNROLLED (no
  cycles in mdk workflows; the attempt cap is structural).

Each ships in THREE copies that must stay coherent (the itsm-request
convention, ``test_itsm_scenario.py``):

* ``workflows/<name>/`` — the deployable (``runtime: temporal``,
  workflow-local ``skills/`` so the worker image bakes each ``impl.py`` —
  ADR 097 D2);
* ``certification/scenarios/<name>/`` — the suite mirror (relative refs +
  ``cases.yaml`` driven by ``certification/run_suite.py``);
* ``src/movate/templates/pattern_<name>/`` — the ``mdk init --pattern``
  template (registry/guard coverage in ``test_pattern_templates`` /
  ``test_init_pattern_cmd``; the graph-shape checks here run on it as a
  third compile source).

What this module asserts (all deterministic — no LLM, no network):

1. graph shape per scenario x copy — node types, the ADR 098 exclusive
   convergences (ONE closure for all three ops exits), the ADR 099
   single-vocabulary ``ack`` gates with their fail-safe fallbacks;
2. Temporal compilation — the exact activity set per scenario and the inline
   (activity-free) ``evaluate_decision`` / ``evaluate_human_route`` routing;
3. routing parity — ``evaluate_decision`` over the workflows' OWN case
   tables x the skills' OWN outputs reproduces every certification path
   (healthy short-circuit / fix applied / drift escalates; attempt-1 /
   attempt-2 / both-fail), and the ack gates fall back on prose;
4. ``cases.yaml`` — parses through the driver's own loader
   (:func:`certification.harness.driver.load_scenario_spec`) with the right
   per-path ledger + final-state expectations (incl. the ``times: 2``
   remediate count that proves the retry ran);
5. the sim skills — THE load-bearing pieces of the batch: the canned
   catalogs (closed, loud KeyError outside them), the pure outcome
   predicates (drift never self-fixes; transient faults need the retry;
   hardware never applies), ledger record + read-back on a tmp SQLite
   (``MOVATE_DB``), ``ctx.mock`` short-circuit, run_id precedence, and
   retry-stable determinism;
6. anti-drift — skill AND agent files byte-identical across the three copies
   of each scenario (plus the shared state.json), and the pattern registry
   carries both scenarios as workflow patterns.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from certification.harness.driver import load_scenario_spec

from movate.core.workflow.compiler import compile_workflow, validate_graph
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.decision import evaluate_decision, evaluate_human_route
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.spec import load_workflow_spec
from movate.templates import PATTERN_TEMPLATES, get_pattern_path, pattern_is_workflow

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYABLES = REPO_ROOT / "workflows"
SCENARIOS = REPO_ROOT / "certification" / "scenarios"
TEMPLATES = REPO_ROOT / "src" / "movate" / "templates"

#: scenario → (scenario-mirror workflow subdir, template dir name).
B9 = {
    "agent-self-healing": ("healing", "pattern_agent_self_healing"),
    "self-healing-ops": ("ops", "pattern_self_healing_ops"),
}


def _workflow_dirs(scenario: str) -> dict[str, Path]:
    """The three compile sources for one scenario (id → workflow dir)."""
    subdir, template = B9[scenario]
    return {
        "deployable": DEPLOYABLES / scenario,
        "scenario": SCENARIOS / scenario / "workflows" / subdir,
        "template": TEMPLATES / template,
    }


#: (scenario, copy) pairs — the parametrization grid for the shape tests.
GRID = [(s, which) for s in sorted(B9) for which in ("deployable", "scenario", "template")]


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


def _edges_into(graph: WorkflowGraph, node_id: str) -> set[tuple[str, str | None]]:
    return {(e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == node_id}


# ---------------------------------------------------------------------------
# 1. Graph shape — node types, convergence (ADR 098), ack gates (ADR 099)
# ---------------------------------------------------------------------------

_EXPECTED_NODE_TYPES: dict[str, dict[str, NodeType]] = {
    "agent-self-healing": {
        "health-check": NodeType.TOOL,
        "quality-gate": NodeType.DECISION,
        "healthy-report": NodeType.AGENT,
        "diagnose": NodeType.AGENT,
        "apply-fix": NodeType.TOOL,
        "verify": NodeType.DECISION,
        "verify-report": NodeType.AGENT,
        "escalate": NodeType.HUMAN,
        "incident-report": NodeType.AGENT,
    },
    "self-healing-ops": {
        "detect": NodeType.TOOL,
        "triage": NodeType.AGENT,
        "remediate-1": NodeType.TOOL,
        "verify-1": NodeType.DECISION,
        "remediate-2": NodeType.TOOL,
        "verify-2": NodeType.DECISION,
        "escalate": NodeType.HUMAN,
        "closure": NodeType.AGENT,
    },
}

_EXPECTED_ENTRYPOINTS = {
    "agent-self-healing": "health-check",
    "self-healing-ops": "detect",
}


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "which"), GRID)
def test_graph_shape_and_node_types(scenario: str, which: str) -> None:
    graph = _graph(_workflow_dirs(scenario)[which])
    validate_graph(graph)  # the phase gate must admit the routed/converged shape

    assert graph.entrypoint == _EXPECTED_ENTRYPOINTS[scenario]
    assert {nid: node.type for nid, node in graph.nodes.items()} == _EXPECTED_NODE_TYPES[scenario]
    # Every TOOL node resolved its workflow/scenario-local skill at compile
    # time (ADR 097 D2) and the self-contained impl.py is where the worker
    # image will look for it.
    for nid, node in graph.nodes.items():
        if node.type is NodeType.TOOL:
            assert Path(node.ref).is_dir(), f"{scenario}/{which}/{nid}"
            assert (Path(node.ref) / "impl.py").is_file(), f"{scenario}/{which}/{nid}"


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_self_healing_no_cycles_failed_fix_can_only_escalate(which: str) -> None:
    """The self-healing shape is acyclic by construction: incident-report is
    reachable ONLY via the ack gate, the gate ONLY via the failed-fix leg —
    a failed fix can never loop back into another fix."""
    graph = _graph(_workflow_dirs("agent-self-healing")[which])
    assert not graph.has_cycle()
    assert _edges_into(graph, "healthy-report") == {("quality-gate", "decision")}
    assert _edges_into(graph, "diagnose") == {("quality-gate", "decision")}
    assert _edges_into(graph, "apply-fix") == {("diagnose", None)}
    assert _edges_into(graph, "verify-report") == {("verify", "decision")}
    assert _edges_into(graph, "escalate") == {("verify", "decision")}
    assert _edges_into(graph, "incident-report") == {("escalate", "human-route")}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_ops_attempt_cap_is_structural_and_exits_converge_on_closure(which: str) -> None:
    """ADR 098: attempt-1 success, attempt-2 success, and the acknowledged
    escalation all land on the ONE shared closure agent; the retry is
    reachable only via attempt 1's failed leg and the gate only via attempt
    2's — exactly two attempts, by graph, not by convention."""
    graph = _graph(_workflow_dirs("self-healing-ops")[which])
    assert not graph.has_cycle()
    assert _edges_into(graph, "remediate-1") == {("triage", None)}
    assert _edges_into(graph, "remediate-2") == {("verify-1", "decision")}
    assert _edges_into(graph, "escalate") == {("verify-2", "decision")}
    assert _edges_into(graph, "closure") == {
        ("verify-1", "decision"),
        ("verify-2", "decision"),
        ("escalate", "human-route"),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "terminal"),
    [("agent-self-healing", "incident-report"), ("self-healing-ops", "closure")],
)
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_ack_gate_routes_and_fail_safe_fallback(scenario: str, terminal: str, which: str) -> None:
    """ADR 099: ONE route vocabulary ("ack") and a fallback aimed at the SAME
    terminal — however the operator words it, the run lands on the report."""
    graph = _graph(_workflow_dirs(scenario)[which])
    gate = graph.nodes["escalate"]
    assert gate.metadata["routes"] == {"ack": terminal}
    assert gate.metadata["fallback"] == terminal
    assert gate.metadata["route_on"] == "decision"
    assert gate.metadata["output_contract"] == ["decision"]


# ---------------------------------------------------------------------------
# 2. Temporal compilation — exact activity sets, inline routing
# ---------------------------------------------------------------------------

#: agents + TOOL dispatch + the durable HUMAN pause + terminal persist; no
#: gate-classifier activity (ADR 094) — both scenarios carry every primitive.
_EXPECTED_ACTIVITIES = {
    "call_agent_activity",
    "call_human_activity",
    "call_skill_activity",
    "persist_workflow_result_activity",
}


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "which"), GRID)
def test_temporal_compiles_with_expected_activity_set(scenario: str, which: str) -> None:
    result = TemporalCompiler().compile(_graph(_workflow_dirs(scenario)[which]))
    src = result.module_source
    ast.parse(src)  # parses as Python
    # Decision nodes route inline via the shared helpers — no activity, no
    # LLM; the ack gates route their own decision (ADR 099).
    assert "evaluate_decision(" in src
    assert "evaluate_human_route(" in src
    assert set(result.activity_names) == _EXPECTED_ACTIVITIES


# ---------------------------------------------------------------------------
# 3. Routing parity — the workflows' own case tables x the skills' outputs
# ---------------------------------------------------------------------------


def _decision_meta(scenario: str, node_id: str) -> tuple[list[dict[str, Any]], str]:
    graph = _graph(_workflow_dirs(scenario)["deployable"])
    meta = graph.nodes[node_id].metadata
    return meta["cases"], meta["default"]


def _load_impl(scenario: str, skill: str) -> ModuleType:
    """Import a deployable copy's impl.py under a unique module name (the
    copies are byte-identical; a plain namespace import would be ambiguous
    in-repo)."""
    path = DEPLOYABLES / scenario / "skills" / skill / "impl.py"
    module_name = f"b9_{scenario}_{skill}_impl".replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MOCK_CTX = SimpleNamespace(run_id="wfr-parity", mock=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_name", "expected_route"),
    [
        ("order-tracker", "healthy-report"),  # 0.93 ≥ 0.8 — healthy short-circuit
        ("invoice-parser", "diagnose"),  # 0.55 — degraded, fixable
        ("support-summarizer", "diagnose"),  # 0.41 — degraded, drift
    ],
)
def test_self_healing_quality_gate_routes_each_canned_agent(
    agent_name: str, expected_route: str
) -> None:
    """evaluate_decision over the workflow's OWN case table x the canned
    monitor data reproduces the certification paths exactly (ADR 094 D3 —
    the same helper the Temporal-compiled workflow calls inline)."""
    cases, default = _decision_meta("agent-self-healing", "quality-gate")
    state = _load_impl("agent-self-healing", "sim-health-check").run(
        {"agent_name": agent_name}, _MOCK_CTX
    )
    assert evaluate_decision(cases, default, state) == expected_route


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symptom", "expected_route"),
    [
        ("elevated output-schema validation failures on recent runs", "verify-report"),
        ("model drift: hallucinated order numbers rising vs the eval baseline", "escalate"),
    ],
)
def test_self_healing_verify_gate_routes_the_fix_outcome(symptom: str, expected_route: str) -> None:
    cases, default = _decision_meta("agent-self-healing", "verify")
    state = _load_impl("agent-self-healing", "sim-apply-fix").run(
        {"agent_name": "a", "symptom": symptom, "fix_action": "Apply the fix."}, _MOCK_CTX
    )
    assert evaluate_decision(cases, default, state) == expected_route


@pytest.mark.unit
@pytest.mark.parametrize(
    ("signal", "r1_route", "r2_route"),
    [
        # Pool exhaustion: applied on attempt 1 — closure, no retry leg taken.
        ("checkout-latency-spike", "closure", None),
        # Transient stuck consumer: attempt 1 fails, the retry lands it.
        ("queue-backlog-alarm", "remediate-2", "closure"),
        # Hardware: both attempts fail — escalate.
        ("disk-failure-alert", "remediate-2", "escalate"),
    ],
)
def test_ops_verify_gates_route_each_canned_fault(
    signal: str, r1_route: str, r2_route: str | None
) -> None:
    """The full two-attempt parity: detect's canned fault through BOTH
    attempt predicates and BOTH verification case tables."""
    detected = _load_impl("self-healing-ops", "sim-detect").run({"signal": signal}, _MOCK_CTX)
    payload = {**detected, "remediation_action": "Apply the runbook action."}

    cases1, default1 = _decision_meta("self-healing-ops", "verify-1")
    state1 = _load_impl("self-healing-ops", "sim-remediate").run(payload, _MOCK_CTX)
    assert evaluate_decision(cases1, default1, state1) == r1_route

    if r2_route is not None:
        cases2, default2 = _decision_meta("self-healing-ops", "verify-2")
        state2 = _load_impl("self-healing-ops", "sim-remediate-retry").run(payload, _MOCK_CTX)
        assert evaluate_decision(cases2, default2, state2) == r2_route


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "terminal"),
    [("agent-self-healing", "incident-report"), ("self-healing-ops", "closure")],
)
@pytest.mark.parametrize("answer", ["ack", " ACK ", "acknowledged", "ok do it", None])
def test_ack_gate_every_wording_lands_on_the_terminal(
    scenario: str, terminal: str, answer: str | None
) -> None:
    """ADR 099 fail-safe: exact-match ack routes; everything else (prose,
    casing handled by trim+casefold, even a missing value) falls back to the
    SAME terminal — silence or creativity can never re-route an escalation."""
    graph = _graph(_workflow_dirs(scenario)["deployable"])
    gate = graph.nodes["escalate"].metadata
    assert evaluate_human_route(gate["routes"], gate["fallback"], answer) == terminal


# ---------------------------------------------------------------------------
# 4. cases.yaml — parses through the DRIVER's loader, per-path expectations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_self_healing_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "agent-self-healing" / "cases.yaml")
    assert spec.scenario == "agent-self-healing"
    assert spec.target == "agent-self-healing"
    assert [c.name for c in spec.cases] == [
        "healthy-short-circuit",
        "degraded-fix-applied",
        "drift-escalates",
    ]
    healthy, fixed, escalated = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    # A failed FIX is a route, not a workflow error — all three end success.
    assert all(c.expect.status == "success" for c in spec.cases)
    # EVERY run records the monitor's health_check row (it is the entrypoint).
    for case in spec.cases:
        assert (case.expect.side_effects[0].system, case.expect.side_effects[0].action) == (
            "monitor",
            "health_check",
        )
        assert case.expect.side_effects[0].times == 1

    # Healthy short-circuit: no pause, no fix row, no degraded-path keys.
    assert healthy.hitl == ()
    assert [(e.system, e.action) for e in healthy.expect.no_side_effects] == [
        ("agent_registry", "apply_fix")
    ]
    assert "summary" in healthy.expect.final_state_has
    for key in ("cause", "fix_action", "fix_status", "decision"):
        assert key in healthy.expect.final_state_lacks

    # Fix applied: no pause, the apply_fix row exists, fix_status pinned.
    assert fixed.hitl == ()
    assert [(e.system, e.action, e.times) for e in fixed.expect.side_effects] == [
        ("monitor", "health_check", 1),
        ("agent_registry", "apply_fix", 1),
    ]
    assert dict(fixed.expect.final_state_contains)["fix_status"] == ("applied",)
    assert "decision" in fixed.expect.final_state_lacks

    # Drift escalates: pause at the gate + ack signal; the failed attempt is
    # still auditable (the apply_fix row exists) with fix_status pinned.
    assert [(s.node, s.decision) for s in escalated.hitl] == [("escalate", {"decision": "ack"})]
    assert [(e.system, e.action, e.times) for e in escalated.expect.side_effects] == [
        ("monitor", "health_check", 1),
        ("agent_registry", "apply_fix", 1),
    ]
    assert dict(escalated.expect.final_state_contains)["fix_status"] == ("failed",)
    assert "decision" in escalated.expect.final_state_has


@pytest.mark.unit
def test_ops_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "self-healing-ops" / "cases.yaml")
    assert spec.scenario == "self-healing-ops"
    assert spec.target == "self-healing-ops"
    assert [c.name for c in spec.cases] == [
        "attempt-1-success",
        "attempt-2-success",
        "hardware-escalates",
    ]
    first, retry, escalated = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)
    # EVERY run records the monitor's detect row (it is the entrypoint).
    for case in spec.cases:
        assert (case.expect.side_effects[0].system, case.expect.side_effects[0].action) == (
            "monitor",
            "detect",
        )
        assert case.expect.side_effects[0].times == 1

    # The remediate row COUNT is the attempt evidence: 1 vs 2.
    def remediate_times(case: Any) -> int | None:
        (effect,) = [e for e in case.expect.side_effects if e.system == "ops"]
        assert effect.action == "remediate"
        return effect.times

    assert remediate_times(first) == 1
    assert remediate_times(retry) == 2
    assert remediate_times(escalated) == 2

    # Attempt-1 success: no pause; r2_status must NEVER be set (the unrolled
    # retry never ran) — that absence is the no-third-attempt proof's twin.
    assert first.hitl == ()
    assert dict(first.expect.final_state_contains)["r1_status"] == ("applied",)
    assert "r2_status" in first.expect.final_state_lacks
    assert "decision" in first.expect.final_state_lacks

    # Attempt-2 success: no pause; both statuses pinned (failed → applied).
    assert retry.hitl == ()
    contains = dict(retry.expect.final_state_contains)
    assert contains["r1_status"] == ("failed",)
    assert contains["r2_status"] == ("applied",)
    assert "decision" in retry.expect.final_state_lacks

    # Both fail: pause at the gate + ack signal; both statuses pinned failed.
    assert [(s.node, s.decision) for s in escalated.hitl] == [("escalate", {"decision": "ack"})]
    contains = dict(escalated.expect.final_state_contains)
    assert contains["r1_status"] == ("failed",)
    assert contains["r2_status"] == ("failed",)
    assert "decision" in escalated.expect.final_state_has


# ---------------------------------------------------------------------------
# 5. Sim skills — canned catalogs, pure predicates, tmp-SQLite ledger
# ---------------------------------------------------------------------------


def _rows(db: Path) -> list[tuple[str, str, str, dict[str, Any]]]:
    conn = sqlite3.connect(db)
    try:
        raw = conn.execute(
            "SELECT run_id, system, action, payload FROM sim_side_effects ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in raw]


@pytest.fixture
def sim_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the impls' ledger at a tmp SQLite file (never the shared PG)."""
    db = tmp_path / "sim.db"
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.delenv("MOVATE_PG_URL", raising=False)
    monkeypatch.setenv("MOVATE_DB", str(db))
    return db


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_name", "quality_score", "symptom_marker"),
    [
        ("order-tracker", 0.93, "none"),
        ("invoice-parser", 0.55, "validation failures"),
        ("support-summarizer", 0.41, "drift"),
    ],
)
def test_sim_health_check_canned_metrics_and_ledger_row(
    sim_db: Path, agent_name: str, quality_score: float, symptom_marker: str
) -> None:
    impl = _load_impl("agent-self-healing", "sim-health-check")
    out = impl.run({"agent_name": agent_name}, SimpleNamespace(run_id="wfr-hc-1", mock=False))
    assert set(out) == {"quality_score", "symptom"}
    assert out["quality_score"] == quality_score
    assert symptom_marker in out["symptom"]
    assert _rows(sim_db) == [
        (
            "wfr-hc-1",
            "monitor",
            "health_check",
            {"agent_name": agent_name, "quality_score": quality_score, "symptom": out["symptom"]},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symptom", "fix_status"),
    [
        ("elevated output-schema validation failures on recent runs", "applied"),
        ("intermittent provider timeouts", "applied"),
        ("model drift: hallucinated order numbers rising vs the eval baseline", "failed"),
        ("MODEL DRIFT detected", "failed"),  # case-insensitive predicate
    ],
)
def test_sim_apply_fix_outcome_is_a_pure_symptom_predicate(
    sim_db: Path, symptom: str, fix_status: str
) -> None:
    """Drift never self-fixes; everything else applies — and the diagnosed
    fix_action is recorded for the audit trail but never decides."""
    impl = _load_impl("agent-self-healing", "sim-apply-fix")
    out = impl.run(
        {"agent_name": "a1", "symptom": symptom, "fix_action": "Repin the model."},
        SimpleNamespace(run_id="wfr-fix-1", mock=False),
    )
    assert out == {"fix_status": fix_status}
    assert _rows(sim_db) == [
        (
            "wfr-fix-1",
            "agent_registry",
            "apply_fix",
            {"agent_name": "a1", "fix_action": "Repin the model.", "fix_status": fix_status},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("signal", "fault_marker", "component"),
    [
        ("checkout-latency-spike", "connection pool exhaustion", "checkout-api"),
        ("queue-backlog-alarm", "stuck", "billing-worker"),
        ("disk-failure-alert", "hardware", "etcd-cluster"),
    ],
)
def test_sim_detect_canned_catalog_and_ledger_row(
    sim_db: Path, signal: str, fault_marker: str, component: str
) -> None:
    impl = _load_impl("self-healing-ops", "sim-detect")
    out = impl.run({"signal": signal}, SimpleNamespace(run_id="wfr-det-1", mock=False))
    assert set(out) == {"fault", "component"}
    assert fault_marker in out["fault"]
    assert out["component"] == component
    assert _rows(sim_db) == [
        (
            "wfr-det-1",
            "monitor",
            "detect",
            {"signal": signal, "fault": out["fault"], "component": component},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fault", "r1", "r2"),
    [
        # Plain fault: applied first try (the retry would also apply).
        ("connection pool exhaustion", "applied", "applied"),
        # Transient: attempt 1 races it and fails; the retry lands it.
        ("stuck consumer after deploy", "failed", "applied"),
        # Hardware: software remediation never applies — both fail.
        ("hardware fault: failing disk on node-7", "failed", "failed"),
        ("HARDWARE fault", "failed", "failed"),  # case-insensitive predicate
    ],
)
def test_sim_remediate_attempt_predicates_differ_exactly_on_transients(
    fault: str, r1: str, r2: str
) -> None:
    """The two attempts are distinct pure predicates: transient ("stuck")
    faults are the retry's reason to exist; hardware defeats both."""
    a1 = _load_impl("self-healing-ops", "sim-remediate")
    a2 = _load_impl("self-healing-ops", "sim-remediate-retry")
    assert a1.attempt_outcome(fault) == r1
    assert a2.attempt_outcome(fault) == r2


@pytest.mark.unit
def test_sim_remediate_attempts_share_action_but_carry_their_attempt(sim_db: Path) -> None:
    """Both attempts write the SAME {ops, remediate} row shape (the driver
    counts times: 2) distinguished by `attempt` in the payload."""
    payload = {
        "fault": "stuck consumer after deploy",
        "component": "billing-worker",
        "remediation_action": "Restart the consumer group.",
    }
    ctx = SimpleNamespace(run_id="wfr-rem-1", mock=False)
    out1 = _load_impl("self-healing-ops", "sim-remediate").run(payload, ctx)
    out2 = _load_impl("self-healing-ops", "sim-remediate-retry").run(payload, ctx)
    assert out1 == {"r1_status": "failed"}
    assert out2 == {"r2_status": "applied"}
    rows = _rows(sim_db)
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("wfr-rem-1", "ops", "remediate"),
        ("wfr-rem-1", "ops", "remediate"),
    ]
    assert [r[3]["attempt"] for r in rows] == [1, 2]
    assert [r[3]["status"] for r in rows] == ["failed", "applied"]
    assert all(r[3]["component"] == "billing-worker" for r in rows)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "skill", "payload"),
    [
        ("agent-self-healing", "sim-health-check", {"agent_name": "nonexistent-agent"}),
        ("self-healing-ops", "sim-detect", {"signal": "unknown-alarm"}),
    ],
)
def test_canned_catalogs_are_closed_and_fail_loud(
    sim_db: Path, scenario: str, skill: str, payload: dict[str, Any]
) -> None:
    """An input outside the canned catalog is a KeyError (the sim-audit-store
    posture) — never a silent default; nothing is recorded."""
    impl = _load_impl(scenario, skill)
    with pytest.raises(KeyError):
        impl.run(payload, SimpleNamespace(run_id="wfr-x", mock=False))
    assert not sim_db.exists()  # nothing was recorded


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "skill", "payload"),
    [
        ("agent-self-healing", "sim-health-check", {"agent_name": "order-tracker"}),
        (
            "agent-self-healing",
            "sim-apply-fix",
            {"agent_name": "a", "symptom": "s", "fix_action": "f"},
        ),
        ("self-healing-ops", "sim-detect", {"signal": "disk-failure-alert"}),
        (
            "self-healing-ops",
            "sim-remediate",
            {"fault": "f", "component": "c", "remediation_action": "r"},
        ),
        (
            "self-healing-ops",
            "sim-remediate-retry",
            {"fault": "f", "component": "c", "remediation_action": "r"},
        ),
    ],
)
def test_sim_skills_mock_short_circuits_without_db_write(
    sim_db: Path, scenario: str, skill: str, payload: dict[str, Any]
) -> None:
    impl = _load_impl(scenario, skill)
    out = impl.run(payload, SimpleNamespace(run_id="wfr-mock", mock=True))
    assert out  # the stub result still satisfies the output contract
    assert not sim_db.exists()  # the ledger was never touched


@pytest.mark.unit
def test_sim_skill_run_id_input_overrides_ctx(sim_db: Path) -> None:
    """An explicit run_id in the input wins (the harness facades' convention);
    ctx.run_id is the fallback the tool node actually exercises."""
    impl = _load_impl("self-healing-ops", "sim-detect")
    impl.run(
        {"signal": "disk-failure-alert", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


@pytest.mark.unit
def test_sim_skill_outputs_are_retry_stable(sim_db: Path) -> None:
    """A Temporal activity retry must observe the SAME deterministic result."""
    impl = _load_impl("agent-self-healing", "sim-health-check")
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    first = impl.run({"agent_name": "support-summarizer"}, ctx)
    second = impl.run({"agent_name": "support-summarizer"}, ctx)
    assert first == second


# ---------------------------------------------------------------------------
# 6. Anti-drift — skills, agents and state.json byte-identical in all three
#    copies; the pattern registry carries both scenarios.
# ---------------------------------------------------------------------------

_SKILL_FILES = ("skill.yaml", "impl.py", "schema/input.json", "schema/output.json")
_AGENT_FILES = ("agent.yaml", "prompt.md", "schema/input.json", "schema/output.json")

_SCENARIO_SKILLS = {
    "agent-self-healing": ("sim-health-check", "sim-apply-fix"),
    "self-healing-ops": ("sim-detect", "sim-remediate", "sim-remediate-retry"),
}
_SCENARIO_AGENTS = {
    "agent-self-healing": ("healthy-report", "diagnose", "verify-report", "incident-report"),
    "self-healing-ops": ("triage", "closure"),
}


def _assert_identical_across_copies(scenario: str, rel: str) -> None:
    canonical = (DEPLOYABLES / scenario / rel).read_bytes()
    scenario_root = SCENARIOS / scenario
    template_root = TEMPLATES / B9[scenario][1]
    assert (scenario_root / rel).read_bytes() == canonical, f"{scenario}: scenario copy of {rel}"
    assert (template_root / rel).read_bytes() == canonical, f"{scenario}: template copy of {rel}"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "rel"),
    [
        (s, f"skills/{skill}/{f}")
        for s, skills in sorted(_SCENARIO_SKILLS.items())
        for skill in skills
        for f in _SKILL_FILES
    ],
)
def test_skill_files_identical_across_copies(scenario: str, rel: str) -> None:
    _assert_identical_across_copies(scenario, rel)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "rel"),
    [
        (s, f"agents/{agent}/{f}")
        for s, agents in sorted(_SCENARIO_AGENTS.items())
        for agent in agents
        for f in _AGENT_FILES
    ],
)
def test_agent_files_identical_across_copies(scenario: str, rel: str) -> None:
    _assert_identical_across_copies(scenario, rel)


@pytest.mark.unit
@pytest.mark.parametrize("scenario", sorted(B9))
def test_state_schema_identical_across_copies(scenario: str) -> None:
    """All three copies share one state contract (the deployable's
    ./state.json, the scenario root's state.json, the template's)."""
    canonical = (DEPLOYABLES / scenario / "state.json").read_bytes()
    assert (SCENARIOS / scenario / "state.json").read_bytes() == canonical
    assert (TEMPLATES / B9[scenario][1] / "state.json").read_bytes() == canonical


@pytest.mark.unit
@pytest.mark.parametrize("scenario", sorted(B9))
def test_pattern_template_registered(scenario: str) -> None:
    """Both B9 scenarios scaffold via ``mdk init --pattern <name>`` — the
    registry entry points at the packaged template dir and marks it a
    workflow bundle (full coverage lives in test_pattern_templates /
    test_init_pattern_cmd; this pins the B9 registration specifically)."""
    assert scenario in PATTERN_TEMPLATES
    assert PATTERN_TEMPLATES[scenario][0] == B9[scenario][1]
    assert pattern_is_workflow(scenario)
    assert get_pattern_path(scenario).is_dir()
    assert (get_pattern_path(scenario) / "GOVERNANCE.md").is_file()
    assert (get_pattern_path(scenario) / "evals" / "judge.yaml.example").is_file()
