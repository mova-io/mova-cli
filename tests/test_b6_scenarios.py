"""B6 eval/lifecycle certification batch — structural + skill tests.

Covers the five scenarios of the B6 batch (program #4 / #10 / #21 / #22 /
#29):

* ``agent-deploy-approval`` — TOOL eval-run → DECISION(eval_score ≥ 0.85) →
  HUMAN promote-approval → TOOL promote → notify | rejected-with-report;
* ``agent-benchmark`` — candidate-a → candidate-b → compare → TOOL
  record-benchmark → notify (sequential two-config benchmark);
* ``continuous-eval`` — scorer AGENT → DECISION(score < 0.6) → {TOOL alert →
  escalate | TOOL record → ack} (one increment of an ADR 100-scheduled
  sampling pipeline);
* ``promotion-pipeline`` — DECISION(stage) → {TOOL run-tests | TOOL
  stage-eval → HUMAN staging-signoff | HUMAN prod-approval → TOOL deploy} →
  notify | rejected;
* ``ab-testing`` — TOOL assign-variant → DECISION(variant) → {variant-a |
  variant-b} → TOOL record-outcome → notify.

Each ships in THREE copies that must stay coherent (the itsm-request
convention, ``test_itsm_scenario.py`` / ``test_b2_scenarios.py``):

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
   convergences, the ADR 099 routes/fallback on every human gate (the
   promote/deploy TOOLs reachable ONLY via an approve route);
2. Temporal compilation — the exact activity set per scenario and the inline
   (activity-free) ``evaluate_decision`` routing;
3. ``cases.yaml`` — parses through the driver's own loader
   (:func:`certification.harness.driver.load_scenario_spec`) with the right
   per-path ledger + pause expectations;
4. the deterministic lifecycle skills — THE load-bearing pieces of the
   batch: the eval-runner fixture table (calibrated simulation, fail-loud on
   unknown fixtures) and the assign-variant SHA-256 parity split (including
   the precomputed user_ids the cases pin);
5. the sim ledger skills — record + read-back on a tmp SQLite
   (``MOVATE_DB``), ``ctx.mock`` short-circuit, run_id precedence, stable
   references;
6. anti-drift — skill AND agent files byte-identical across the three copies
   of each scenario, state.json identical, and the ab-testing arms'
   prompt.md byte-identical ACROSS the two agent dirs (the params-only
   experiment contract).
"""

from __future__ import annotations

import ast
import hashlib
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
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.spec import load_workflow_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYABLES = REPO_ROOT / "workflows"
SCENARIOS = REPO_ROOT / "certification" / "scenarios"
TEMPLATES = REPO_ROOT / "src" / "movate" / "templates"

#: scenario → (scenario-mirror workflow subdir, template dir name).
B6 = {
    "agent-deploy-approval": ("deploy", "pattern_agent_deploy_approval"),
    "agent-benchmark": ("benchmark", "pattern_agent_benchmark"),
    "continuous-eval": ("sampling", "pattern_continuous_eval"),
    "promotion-pipeline": ("pipeline", "pattern_promotion_pipeline"),
    "ab-testing": ("ab", "pattern_ab_testing"),
}


def _workflow_dirs(scenario: str) -> dict[str, Path]:
    """The three compile sources for one scenario (id → workflow dir)."""
    subdir, template = B6[scenario]
    return {
        "deployable": DEPLOYABLES / scenario,
        "scenario": SCENARIOS / scenario / "workflows" / subdir,
        "template": TEMPLATES / template,
    }


#: (scenario, copy) pairs — the parametrization grid for the shape tests.
GRID = [(s, which) for s in sorted(B6) for which in ("deployable", "scenario", "template")]


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


def _edges_into(graph: WorkflowGraph, node_id: str) -> set[tuple[str, str | None]]:
    return {(e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == node_id}


# ---------------------------------------------------------------------------
# 1. Graph shape — node types, convergence (ADR 098), gate routes (ADR 099)
# ---------------------------------------------------------------------------

_EXPECTED_NODE_TYPES: dict[str, dict[str, NodeType]] = {
    "agent-deploy-approval": {
        "eval-run": NodeType.TOOL,
        "score-gate": NodeType.DECISION,
        "promote-approval": NodeType.HUMAN,
        "promote": NodeType.TOOL,
        "notify": NodeType.AGENT,
        "rejected": NodeType.AGENT,
    },
    "agent-benchmark": {
        "candidate-a": NodeType.AGENT,
        "candidate-b": NodeType.AGENT,
        "compare": NodeType.AGENT,
        "record-benchmark": NodeType.TOOL,
        "notify": NodeType.AGENT,
    },
    "continuous-eval": {
        "scorer": NodeType.AGENT,
        "quality-gate": NodeType.DECISION,
        "alert": NodeType.TOOL,
        "escalate": NodeType.AGENT,
        "record": NodeType.TOOL,
        "ack": NodeType.AGENT,
    },
    "promotion-pipeline": {
        "stage-gate": NodeType.DECISION,
        "run-tests": NodeType.TOOL,
        "stage-eval": NodeType.TOOL,
        "staging-signoff": NodeType.HUMAN,
        "prod-approval": NodeType.HUMAN,
        "deploy": NodeType.TOOL,
        "notify": NodeType.AGENT,
        "rejected": NodeType.AGENT,
    },
    "ab-testing": {
        "assign-variant": NodeType.TOOL,
        "variant-gate": NodeType.DECISION,
        "variant-a": NodeType.AGENT,
        "variant-b": NodeType.AGENT,
        "record-outcome": NodeType.TOOL,
        "notify": NodeType.AGENT,
    },
}

_EXPECTED_ENTRYPOINTS = {
    "agent-deploy-approval": "eval-run",
    "agent-benchmark": "candidate-a",
    "continuous-eval": "scorer",
    "promotion-pipeline": "stage-gate",
    "ab-testing": "assign-variant",
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
def test_deploy_approval_promote_only_via_human_approve(which: str) -> None:
    """The registry write is graph-gated twice: `promote` is reachable ONLY
    via the human gate's approve route, and the gate ONLY via the score
    gate's passing case — a failing eval can never be promoted."""
    graph = _graph(_workflow_dirs("agent-deploy-approval")[which])
    assert _edges_into(graph, "promote") == {("promote-approval", "human-route")}
    assert _edges_into(graph, "promote-approval") == {("score-gate", "decision")}
    assert _edges_into(graph, "notify") == {("promote", None)}
    # Every rejected exit (failed eval OR human veto) converges on the ONE
    # rejected-with-report agent (ADR 098).
    assert _edges_into(graph, "rejected") == {
        ("score-gate", "decision"),
        ("promote-approval", "human-route"),
    }


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_deploy_approval_gate_routes_and_fail_safe_fallback(which: str) -> None:
    """ADR 099: the human gate routes its own decision; prose fails safe."""
    graph = _graph(_workflow_dirs("agent-deploy-approval")[which])
    gate = graph.nodes["promote-approval"]
    assert gate.metadata["routes"] == {"approve": "promote", "reject": "rejected"}
    assert gate.metadata["fallback"] == "rejected"
    assert gate.metadata["route_on"] == "decision"


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_benchmark_is_strictly_sequential(which: str) -> None:
    """The benchmark chain is linear by design (the ADR 092 parallel diamond
    is deliberately not used): each node has exactly one inbound edge."""
    graph = _graph(_workflow_dirs("agent-benchmark")[which])
    assert _edges_into(graph, "candidate-b") == {("candidate-a", None)}
    assert _edges_into(graph, "compare") == {("candidate-b", None)}
    assert _edges_into(graph, "record-benchmark") == {("compare", None)}
    assert _edges_into(graph, "notify") == {("record-benchmark", None)}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_continuous_eval_paths_are_exclusive(which: str) -> None:
    """ADR 098: each ledger TOOL is reachable only via its decision leg, and
    each terminal agent only via its own path's TOOL — alert/record can
    never both fire on one increment."""
    graph = _graph(_workflow_dirs("continuous-eval")[which])
    assert _edges_into(graph, "alert") == {("quality-gate", "decision")}
    assert _edges_into(graph, "record") == {("quality-gate", "decision")}
    assert _edges_into(graph, "escalate") == {("alert", None)}
    assert _edges_into(graph, "ack") == {("record", None)}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_promotion_pipeline_stage_paths_and_convergence(which: str) -> None:
    """The three stage paths are entered only from the stage gate; deploy is
    reachable ONLY via prod-approval's approve route (the graph, not a
    convention, keeps unapproved changes out of production); all successes
    converge on ONE notify and all rejects on ONE rejected (ADR 098)."""
    graph = _graph(_workflow_dirs("promotion-pipeline")[which])
    assert _edges_into(graph, "run-tests") == {("stage-gate", "decision")}
    assert _edges_into(graph, "stage-eval") == {("stage-gate", "decision")}
    assert _edges_into(graph, "prod-approval") == {("stage-gate", "decision")}
    assert _edges_into(graph, "staging-signoff") == {("stage-eval", None)}
    assert _edges_into(graph, "deploy") == {("prod-approval", "human-route")}
    assert _edges_into(graph, "notify") == {
        ("run-tests", None),
        ("staging-signoff", "human-route"),
        ("deploy", None),
    }
    assert _edges_into(graph, "rejected") == {
        ("stage-gate", "decision"),
        ("staging-signoff", "human-route"),
        ("prod-approval", "human-route"),
    }


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
@pytest.mark.parametrize("gate_id", ["staging-signoff", "prod-approval"])
def test_promotion_pipeline_gate_routes_and_fail_safe_fallback(which: str, gate_id: str) -> None:
    """ADR 099: both human gates route their own decision; prose fails safe."""
    graph = _graph(_workflow_dirs("promotion-pipeline")[which])
    gate = graph.nodes[gate_id]
    expected_approve = "notify" if gate_id == "staging-signoff" else "deploy"
    assert gate.metadata["routes"] == {"approve": expected_approve, "reject": "rejected"}
    assert gate.metadata["fallback"] == "rejected"
    assert gate.metadata["route_on"] == "decision"


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_ab_testing_arms_converge_on_one_recorder(which: str) -> None:
    """ADR 098: each arm is reachable only via its decision leg, and BOTH
    converge on the ONE record-outcome TOOL — no outcome can go unrecorded
    and no run can record two."""
    graph = _graph(_workflow_dirs("ab-testing")[which])
    assert _edges_into(graph, "variant-a") == {("variant-gate", "decision")}
    assert _edges_into(graph, "variant-b") == {("variant-gate", "decision")}
    assert _edges_into(graph, "record-outcome") == {("variant-a", None), ("variant-b", None)}
    assert _edges_into(graph, "notify") == {("record-outcome", None)}


# ---------------------------------------------------------------------------
# 2. Temporal compilation — exact activity sets, inline decision routing
# ---------------------------------------------------------------------------

_EXPECTED_ACTIVITIES = {
    # agents + TOOL dispatch + the durable HUMAN pause + terminal persist; no
    # gate-classifier activity anywhere (ADR 094 decisions route inline).
    "agent-deploy-approval": {
        "call_agent_activity",
        "call_human_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    "agent-benchmark": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    "continuous-eval": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    "promotion-pipeline": {
        "call_agent_activity",
        "call_human_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    "ab-testing": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
}


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "which"), GRID)
def test_temporal_compiles_with_expected_activity_set(scenario: str, which: str) -> None:
    result = TemporalCompiler().compile(_graph(_workflow_dirs(scenario)[which]))
    src = result.module_source
    ast.parse(src)  # parses as Python
    assert set(result.activity_names) == _EXPECTED_ACTIVITIES[scenario]
    # Decision nodes route inline via the shared helper — no activity, no
    # LLM (agent-benchmark is the linear exception: it has no decision node).
    if scenario != "agent-benchmark":
        assert "evaluate_decision(" in src


# ---------------------------------------------------------------------------
# 3. cases.yaml — parses through the DRIVER's loader, per-path expectations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_approval_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "agent-deploy-approval" / "cases.yaml")
    assert spec.scenario == "agent-deploy-approval"
    assert spec.target == "agent-deploy-approval"
    assert [c.name for c in spec.cases] == [
        "passing-eval-approved",
        "passing-eval-human-rejected",
        "failing-eval-blocked",
    ]
    approved, vetoed, blocked = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    # Rejection is a route, not an error — all three terminate success.
    assert all(c.expect.status == "success" for c in spec.cases)
    # The eval ALWAYS runs: every case asserts exactly one eval/run row.
    for case in spec.cases:
        assert ("eval", "run", 1) in [
            (e.system, e.action, e.times) for e in case.expect.side_effects
        ]

    # Approved: the ONLY case with a promote row; pause + approve signal.
    assert [(s.node, s.decision) for s in approved.hitl] == [
        ("promote-approval", {"decision": "approve"})
    ]
    assert ("registry", "promote", 1) in [
        (e.system, e.action, e.times) for e in approved.expect.side_effects
    ]
    assert "promote_result" in approved.expect.final_state_has

    # The human veto: eval PASSED, the approver rejects — pause + reject
    # signal observed, and the promote row must NOT exist.
    assert [(s.node, s.decision) for s in vetoed.hitl] == [
        ("promote-approval", {"decision": "reject"})
    ]
    assert [(e.system, e.action) for e in vetoed.expect.no_side_effects] == [
        ("registry", "promote")
    ]
    assert "promote_result" in vetoed.expect.final_state_lacks

    # The eval block: NO hitl declared (no pause — a wrong pause starves the
    # fact poll), no promote row, and final_state lacks the human gate's
    # decision key (structural no-pause proof) — but HAS the eval report
    # (rejected-with-report).
    assert blocked.hitl == ()
    assert [(e.system, e.action) for e in blocked.expect.no_side_effects] == [
        ("registry", "promote")
    ]
    assert "promote_result" in blocked.expect.final_state_lacks
    assert "decision" in blocked.expect.final_state_lacks
    assert "eval_report" in blocked.expect.final_state_has


@pytest.mark.unit
def test_benchmark_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "agent-benchmark" / "cases.yaml")
    assert spec.scenario == "agent-benchmark"
    assert spec.target == "agent-benchmark"
    assert [c.name for c in spec.cases] == ["clear-winner", "judgment-call"]
    for case in spec.cases:
        # No gate in this workflow — hitl is an honest skip on every case.
        assert case.hitl == ()
        assert case.expect.governance == "allow"
        assert case.expect.status == "success"
        # The CONTRACT, not a specific winner (the winner is an LLM
        # judgment): both responses ran, the verdict + BOTH scores exist,
        # and the ledger row was recorded exactly once.
        for marker in (
            "response_a",
            "response_b",
            "winner",
            "rationale",
            "score_a",
            "score_b",
            "benchmark_result",
        ):
            assert marker in case.expect.final_state_has, marker
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("eval", "benchmark", 1)
        ]


@pytest.mark.unit
def test_continuous_eval_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "continuous-eval" / "cases.yaml")
    assert spec.scenario == "continuous-eval"
    assert spec.target == "continuous-eval"
    assert [c.name for c in spec.cases] == ["healthy-sample", "regression-sample"]
    healthy, regression = spec.cases
    assert all(c.hitl == () for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)
    # Every increment is a nested {sample: {prompt, response}} input.
    for case in spec.cases:
        assert set(case.input["sample"]) == {"prompt", "response"}

    # Healthy: score row, NO alert row, ack-path markers only.
    assert [(e.system, e.action, e.times) for e in healthy.expect.side_effects] == [
        ("eval", "record_score", 1)
    ]
    assert [(e.system, e.action) for e in healthy.expect.no_side_effects] == [
        ("eval", "quality_alert")
    ]
    assert "score_result" in healthy.expect.final_state_has
    assert "alert_result" in healthy.expect.final_state_lacks

    # Regression: alert row, NO score row, escalation-path markers only.
    assert [(e.system, e.action, e.times) for e in regression.expect.side_effects] == [
        ("eval", "quality_alert", 1)
    ]
    assert [(e.system, e.action) for e in regression.expect.no_side_effects] == [
        ("eval", "record_score")
    ]
    assert "alert_result" in regression.expect.final_state_has
    assert "score_result" in regression.expect.final_state_lacks


@pytest.mark.unit
def test_promotion_pipeline_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "promotion-pipeline" / "cases.yaml")
    assert spec.scenario == "promotion-pipeline"
    assert spec.target == "promotion-pipeline"
    assert [c.name for c in spec.cases] == ["test-stage", "staging-signoff", "prod-promote"]
    test_stage, staging, prod = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)

    # Each case asserts EXACTLY its stage's ledger row and the absence of
    # BOTH other stages' rows (the deterministic router makes this exact).
    stage_rows = {
        "test-stage": ("pipeline", "stage_test"),
        "staging-signoff": ("eval", "stage_eval"),
        "prod-promote": ("pipeline", "promote_prod"),
    }
    all_rows = set(stage_rows.values())
    for case in spec.cases:
        own = stage_rows[case.name]
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [(*own, 1)]
        assert {(e.system, e.action) for e in case.expect.no_side_effects} == all_rows - {own}

    # test: fully automatic — no pause (structurally: no hitl + no decision
    # key in final state).
    assert test_stage.hitl == ()
    assert "decision" in test_stage.expect.final_state_lacks
    assert "test_result" in test_stage.expect.final_state_has

    # staging: exactly one pause, at the signoff gate, AFTER the eval row.
    assert [(s.node, s.decision) for s in staging.hitl] == [
        ("staging-signoff", {"decision": "approve"})
    ]
    assert "stage_eval_result" in staging.expect.final_state_has

    # production: exactly one pause, at the approval gate, BEFORE the row.
    assert [(s.node, s.decision) for s in prod.hitl] == [("prod-approval", {"decision": "approve"})]
    assert "deploy_result" in prod.expect.final_state_has


@pytest.mark.unit
def test_ab_testing_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "ab-testing" / "cases.yaml")
    assert spec.scenario == "ab-testing"
    assert spec.target == "ab-testing"
    assert [c.name for c in spec.cases] == ["variant-a-user", "variant-b-user"]
    case_a, case_b = spec.cases
    assert all(c.hitl == () for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)

    # Both rows on every run: the experiment is auditable end to end.
    for case in spec.cases:
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("ab", "assign", 1),
            ("ab", "record_outcome", 1),
        ]
        assert "response" in case.expect.final_state_has

    # The pinned user_ids actually hash to the pinned variants — the same
    # parity rule the assign-variant impl ships (precomputed expectations).
    def parity_variant(user_id: str) -> str:
        return "a" if int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % 2 == 0 else "b"

    for case, variant in ((case_a, "a"), (case_b, "b")):
        assert parity_variant(str(case.input["user_id"])) == variant
        assert dict(case.expect.final_state_contains)["variant"] == (variant,)


# ---------------------------------------------------------------------------
# 4. The deterministic lifecycle skills — eval-runner + assign-variant
# ---------------------------------------------------------------------------


def _load_impl(scenario: str, skill: str) -> ModuleType:
    """Import a deployable copy's impl.py under a unique module name (the
    copies are byte-identical; a plain namespace import would be ambiguous
    in-repo)."""
    path = DEPLOYABLES / scenario / "skills" / skill / "impl.py"
    module_name = f"b6_{scenario}_{skill}_impl".replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
def test_eval_runner_passing_fixture_clears_the_gate_threshold(sim_db: Path) -> None:
    """The calibrated table IS the contract: passing ≥ 0.85 ≥ failing, with
    the gate threshold strictly between them — the scenario's branches stay
    deterministic."""
    impl = _load_impl("agent-deploy-approval", "eval-runner")
    out = impl.run(
        {"candidate": "support-triage", "version": "2026.6.10.3", "fixture": "passing"},
        SimpleNamespace(run_id="wfr-dep-1", mock=False),
    )
    assert set(out) == {"eval_score", "eval_report"}
    assert out["eval_score"] == 0.93
    assert out["eval_score"] >= 0.85  # clears the score-gate
    assert "EVAL-RUN-" in out["eval_report"]
    assert "accuracy=0.94" in out["eval_report"]
    assert _rows(sim_db) == [
        (
            "wfr-dep-1",
            "eval",
            "run",
            {
                "candidate": "support-triage",
                "version": "2026.6.10.3",
                "fixture": "passing",
                "scores": {"accuracy": 0.94, "safety": 0.97, "overall": 0.93},
            },
        )
    ]


@pytest.mark.unit
def test_eval_runner_failing_fixture_lands_below_the_gate(sim_db: Path) -> None:
    impl = _load_impl("agent-deploy-approval", "eval-runner")
    out = impl.run(
        {"candidate": "support-triage", "version": "2026.6.10.4", "fixture": "failing"},
        SimpleNamespace(run_id="wfr-dep-2", mock=False),
    )
    assert out["eval_score"] == 0.41
    assert out["eval_score"] < 0.85  # blocked at the score-gate
    rows = _rows(sim_db)
    assert len(rows) == 1 and rows[0][2] == "run"
    assert rows[0][3]["scores"]["overall"] == 0.41


@pytest.mark.unit
def test_eval_runner_rejects_unknown_fixture(sim_db: Path) -> None:
    """The enum lives in the input schema (dispatch_skill enforces it); the
    impl's KeyError is the fail-loud backstop — no silent default score."""
    impl = _load_impl("agent-deploy-approval", "eval-runner")
    with pytest.raises(KeyError):
        impl.run(
            {"candidate": "c", "version": "v", "fixture": "flaky"},
            SimpleNamespace(run_id="wfr-x", mock=False),
        )
    assert not sim_db.exists()  # nothing was recorded


@pytest.mark.unit
@pytest.mark.parametrize(("user_id", "variant"), [("user-1001", "a"), ("user-1003", "b")])
def test_assign_variant_parity_matches_the_pinned_cases(
    sim_db: Path, user_id: str, variant: str
) -> None:
    """The cases.yaml user_ids are PRECOMPUTED against this exact split —
    sha256 parity, never the salted builtin hash()."""
    impl = _load_impl("ab-testing", "assign-variant")
    out = impl.run({"user_id": user_id}, SimpleNamespace(run_id="wfr-ab-1", mock=False))
    assert out["variant"] == variant
    assert f"variant {variant}" in out["assign_result"]
    assert "AB-ASSIGN-" in out["assign_result"]
    assert _rows(sim_db) == [("wfr-ab-1", "ab", "assign", {"user_id": user_id, "variant": variant})]


@pytest.mark.unit
def test_assign_variant_is_deterministic_and_process_independent(sim_db: Path) -> None:
    """Same user ⇒ same variant on every call (the sticky-assignment and
    Temporal-replay property), and the split is pure sha256 parity."""
    impl = _load_impl("ab-testing", "assign-variant")
    ctx = SimpleNamespace(run_id="wfr-ab-2", mock=True)
    for user_id in ("user-1001", "user-1003", "alice@example.com", "x"):
        expected = "a" if int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % 2 == 0 else "b"
        first = impl.run({"user_id": user_id}, ctx)
        second = impl.run({"user_id": user_id}, ctx)
        assert first == second
        assert first["variant"] == expected


# ---------------------------------------------------------------------------
# 5. Sim ledger skills — tmp-SQLite record + read-back
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sim_promote_records_registry_row(sim_db: Path) -> None:
    impl = _load_impl("agent-deploy-approval", "sim-promote")
    out = impl.run(
        {"candidate": "support-triage", "version": "2026.6.10.3"},
        SimpleNamespace(run_id="wfr-dep-3", mock=False),
    )
    assert set(out) == {"promote_result"}
    assert "REG-PROMO-" in out["promote_result"]
    assert _rows(sim_db) == [
        (
            "wfr-dep-3",
            "registry",
            "promote",
            {"candidate": "support-triage", "version": "2026.6.10.3"},
        )
    ]


@pytest.mark.unit
def test_sim_record_benchmark_records_verdict_row(sim_db: Path) -> None:
    impl = _load_impl("agent-benchmark", "sim-record-benchmark")
    out = impl.run(
        {"task": "t", "winner": "a", "score_a": 0.9, "score_b": 0.55},
        SimpleNamespace(run_id="wfr-bench-1", mock=False),
    )
    assert set(out) == {"benchmark_result"}
    assert "winner candidate-a" in out["benchmark_result"]
    assert "EVAL-BENCH-" in out["benchmark_result"]
    assert _rows(sim_db) == [
        (
            "wfr-bench-1",
            "eval",
            "benchmark",
            {"task": "t", "winner": "a", "score_a": 0.9, "score_b": 0.55},
        )
    ]


@pytest.mark.unit
def test_sim_alert_records_quality_alert_row(sim_db: Path) -> None:
    impl = _load_impl("continuous-eval", "sim-alert")
    out = impl.run(
        {"score": 0.1, "issues": "wrong answer; solicits credentials"},
        SimpleNamespace(run_id="wfr-ce-1", mock=False),
    )
    assert set(out) == {"alert_result"}
    assert "below the 0.6 floor" in out["alert_result"]
    assert "EVAL-ALERT-" in out["alert_result"]
    assert _rows(sim_db) == [
        (
            "wfr-ce-1",
            "eval",
            "quality_alert",
            {"score": 0.1, "issues": "wrong answer; solicits credentials"},
        )
    ]


@pytest.mark.unit
def test_sim_record_score_records_score_row(sim_db: Path) -> None:
    impl = _load_impl("continuous-eval", "sim-record-score")
    out = impl.run({"score": 0.95}, SimpleNamespace(run_id="wfr-ce-2", mock=False))
    assert set(out) == {"score_result"}
    assert "EVAL-SCORE-" in out["score_result"]
    assert _rows(sim_db) == [("wfr-ce-2", "eval", "record_score", {"score": 0.95})]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("skill", "action", "result_key", "ref"),
    [
        ("sim-run-tests", "stage_test", "test_result", "CI-TEST-"),
        ("sim-stage-eval", "stage_eval", "stage_eval_result", "EVAL-STG-"),
        ("sim-deploy", "promote_prod", "deploy_result", "PIPE-PROD-"),
    ],
)
def test_promotion_pipeline_skills_record_their_stage_rows(
    sim_db: Path, skill: str, action: str, result_key: str, ref: str
) -> None:
    impl = _load_impl("promotion-pipeline", skill)
    out = impl.run(
        {"change": "checkout-service 2026.6.10"},
        SimpleNamespace(run_id="wfr-pp-1", mock=False),
    )
    assert set(out) == {result_key}
    assert ref in out[result_key]
    system = "eval" if skill == "sim-stage-eval" else "pipeline"
    assert _rows(sim_db) == [("wfr-pp-1", system, action, {"change": "checkout-service 2026.6.10"})]


@pytest.mark.unit
def test_sim_record_outcome_records_variant_row(sim_db: Path) -> None:
    impl = _load_impl("ab-testing", "sim-record-outcome")
    out = impl.run(
        {"variant": "b", "user_id": "user-1003"},
        SimpleNamespace(run_id="wfr-ab-3", mock=False),
    )
    assert set(out) == {"outcome_result"}
    assert "variant b" in out["outcome_result"]
    assert "AB-OUT-" in out["outcome_result"]
    assert _rows(sim_db) == [
        ("wfr-ab-3", "ab", "record_outcome", {"user_id": "user-1003", "variant": "b"})
    ]


#: every (scenario, skill) of the batch with a minimal valid payload — the
#: grid for the shared-contract tests below.
_ALL_SKILLS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "agent-deploy-approval",
        "eval-runner",
        {"candidate": "c", "version": "v", "fixture": "passing"},
    ),
    ("agent-deploy-approval", "sim-promote", {"candidate": "c", "version": "v"}),
    ("agent-benchmark", "sim-record-benchmark", {"winner": "a", "score_a": 0.9, "score_b": 0.5}),
    ("continuous-eval", "sim-alert", {"score": 0.1, "issues": "x"}),
    ("continuous-eval", "sim-record-score", {"score": 0.9}),
    ("promotion-pipeline", "sim-run-tests", {"change": "c"}),
    ("promotion-pipeline", "sim-stage-eval", {"change": "c"}),
    ("promotion-pipeline", "sim-deploy", {"change": "c"}),
    ("ab-testing", "assign-variant", {"user_id": "user-1001"}),
    ("ab-testing", "sim-record-outcome", {"variant": "a"}),
]


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "skill", "payload"), _ALL_SKILLS)
def test_skills_mock_short_circuits_without_db_write(
    sim_db: Path, scenario: str, skill: str, payload: dict[str, Any]
) -> None:
    impl = _load_impl(scenario, skill)
    out = impl.run(payload, SimpleNamespace(run_id="wfr-mock", mock=True))
    assert out  # the stub result still satisfies the output contract
    assert not sim_db.exists()  # the ledger was never touched


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "skill", "payload"), _ALL_SKILLS)
def test_skills_reference_is_stable_per_run(
    sim_db: Path, scenario: str, skill: str, payload: dict[str, Any]
) -> None:
    """A Temporal activity retry must confirm the SAME synthetic reference."""
    impl = _load_impl(scenario, skill)
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    assert impl.run(payload, ctx) == impl.run(payload, ctx)


@pytest.mark.unit
def test_skill_run_id_input_overrides_ctx(sim_db: Path) -> None:
    """An explicit run_id in the input wins (the harness facades' convention);
    ctx.run_id is the fallback the tool node actually exercises."""
    impl = _load_impl("ab-testing", "sim-record-outcome")
    impl.run(
        {"variant": "a", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


# ---------------------------------------------------------------------------
# 6. Anti-drift — skills AND agents ship byte-identical in all three copies
# ---------------------------------------------------------------------------

_SKILL_FILES = ("skill.yaml", "impl.py", "schema/input.json", "schema/output.json")
_AGENT_FILES = ("agent.yaml", "prompt.md", "schema/input.json", "schema/output.json")

_SCENARIO_SKILLS = {
    "agent-deploy-approval": ("eval-runner", "sim-promote"),
    "agent-benchmark": ("sim-record-benchmark",),
    "continuous-eval": ("sim-alert", "sim-record-score"),
    "promotion-pipeline": ("sim-run-tests", "sim-stage-eval", "sim-deploy"),
    "ab-testing": ("assign-variant", "sim-record-outcome"),
}
_SCENARIO_AGENTS = {
    "agent-deploy-approval": ("notify", "rejected"),
    "agent-benchmark": ("candidate-a", "candidate-b", "compare", "notify"),
    "continuous-eval": ("scorer", "escalate", "ack"),
    "promotion-pipeline": ("notify", "rejected"),
    "ab-testing": ("variant-a", "variant-b", "notify"),
}


def _assert_identical_across_copies(scenario: str, rel: str) -> None:
    canonical = (DEPLOYABLES / scenario / rel).read_bytes()
    scenario_root = SCENARIOS / scenario
    template_root = TEMPLATES / B6[scenario][1]
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
@pytest.mark.parametrize("scenario", sorted(B6))
def test_state_schema_identical_across_copies(scenario: str) -> None:
    """All three copies share one state contract (the deployable's
    ./state.json, the scenario root's state.json, the template's)."""
    canonical = (DEPLOYABLES / scenario / "state.json").read_bytes()
    assert (SCENARIOS / scenario / "state.json").read_bytes() == canonical
    assert (TEMPLATES / B6[scenario][1] / "state.json").read_bytes() == canonical


@pytest.mark.unit
def test_ab_arms_share_one_prompt_byte_for_byte() -> None:
    """The A/B experiment contract: the arms' prompt.md files are
    BYTE-IDENTICAL — agent.yaml model params are the ONLY delta (the
    variable under test). Drift here silently turns a one-variable
    experiment into a two-variable one."""
    a = (DEPLOYABLES / "ab-testing" / "agents" / "variant-a" / "prompt.md").read_bytes()
    b = (DEPLOYABLES / "ab-testing" / "agents" / "variant-b" / "prompt.md").read_bytes()
    assert a == b


@pytest.mark.unit
def test_benchmark_candidates_differ_only_in_params_and_output_key() -> None:
    """The benchmark contract: candidate prompts are identical except for
    the output key (response_a vs response_b), and the agent.yaml model
    params actually differ (otherwise the benchmark compares nothing)."""
    base = DEPLOYABLES / "agent-benchmark" / "agents"
    prompt_a = (base / "candidate-a" / "prompt.md").read_text()
    prompt_b = (base / "candidate-b" / "prompt.md").read_text()
    assert prompt_a.replace("response_a", "response_x") == prompt_b.replace(
        "response_b", "response_x"
    )
    yaml_a = (base / "candidate-a" / "agent.yaml").read_text()
    yaml_b = (base / "candidate-b" / "agent.yaml").read_text()
    assert "temperature: 0.0" in yaml_a
    assert "temperature: 0.9" in yaml_b


@pytest.mark.unit
def test_ab_arms_params_actually_differ() -> None:
    """Belt-and-braces for the experiment: identical prompts are only
    meaningful if the params DO differ."""
    base = DEPLOYABLES / "ab-testing" / "agents"
    assert "temperature: 0.0" in (base / "variant-a" / "agent.yaml").read_text()
    assert "temperature: 0.9" in (base / "variant-b" / "agent.yaml").read_text()
