"""B5 cross-system certification batch — structural + skill tests.

Covers the three scenarios of the B5 batch (program #6 / #7 / #18):

* ``employee-onboarding`` — plan AGENT → TOOL provision-ad → TOOL
  provision-email → TOOL provision-equipment → welcome AGENT (sequential by
  phase-gate design — see the fan-out section below);
* ``incident-response`` — diagnose AGENT → DECISION(confidence) → {TOOL
  remediate → verify AGENT → DECISION(resolved) → notify | HUMAN escalate
  (routes ack) → notify};
* ``cross-system-action`` — plan AGENT → TOOL crm-update → TOOL erp-update →
  TOOL create-ticket → TOOL send-email → audit-summary AGENT.

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

1. graph shape per scenario x copy — node types, the STRICT CHAIN ORDER of
   the onboarding/cross-system TOOL chains (the structural order proof the
   cases.yaml honesty notes lean on), the ADR 098 exclusive convergences and
   ADR 099 routes/fallback on the incident escalation gate;
2. THE FAN-OUT FINDING — employee-onboarding is conceptually a fan-out
   diamond over three TOOL nodes; it ships sequential because (a) ADR 092's
   parallel phase gate (``validate_dag``, Phase 1) admits AGENT-ONLY
   branches and rejects TOOL nodes anywhere in a parallel graph, and (b) the
   Temporal lowering (``_emit_fan_out_node``, Phase 2) emits EVERY branch
   via ``call_agent_activity`` — a TOOL branch would be dispatched as an
   agent even if validation admitted it. Both blockers are pinned here so a
   later ADR 092 phase flips the chain back to the diamond deliberately;
3. Temporal compilation — the exact activity set per scenario and the inline
   (activity-free) ``evaluate_decision`` routing where decisions exist;
4. ``cases.yaml`` — parses through the driver's own loader
   (:func:`certification.harness.driver.load_scenario_spec`) with the right
   per-path ledger + marker expectations;
5. the sim ledger skills — record + read-back on a tmp SQLite
   (``MOVATE_DB``): the role-keyed equipment bundle (payload reflects the
   role — the part the driver's (system, action, times) matcher cannot
   see), the deterministic remediate applied/failed rule (the attempt row
   recorded EITHER way), the cross-system chain's ledger rows reading back
   in insertion order, ``ctx.mock`` short-circuit, run_id precedence, and
   retry-stable references;
6. the incident notify prompt renders on EVERY path — its input keys are
   conditionally present (three exclusive paths), and a missing key without
   a ``default`` filter raises Jinja's ``UndefinedError`` under the
   loader's ``StrictUndefined`` at run time;
7. the ADR 100 trigger binding documented in
   ``workflows/incident-response/README.md`` — registration through the
   real CLI and the event→state mapping through the real core helpers, so
   the documented commands cannot silently drift;
8. anti-drift — skill AND agent files byte-identical across the three copies
   of each scenario.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from certification.harness.driver import load_scenario_spec
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent
from movate.core.models import JobKind
from movate.core.triggers import (
    build_triggered_job,
    mint_trigger,
    resolve_body_delivery_id,
)
from movate.core.workflow.compiler import (
    WorkflowCompileError,
    compile_workflow,
    validate_graph,
)
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.spec import load_workflow_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYABLES = REPO_ROOT / "workflows"
SCENARIOS = REPO_ROOT / "certification" / "scenarios"
TEMPLATES = REPO_ROOT / "src" / "movate" / "templates"

#: scenario → (scenario-mirror workflow subdir, template dir name).
B5 = {
    "employee-onboarding": ("onboarding", "pattern_employee_onboarding"),
    "incident-response": ("incident", "pattern_incident_response"),
    "cross-system-action": ("chain", "pattern_cross_system_action"),
}


def _workflow_dirs(scenario: str) -> dict[str, Path]:
    """The three compile sources for one scenario (id → workflow dir)."""
    subdir, template = B5[scenario]
    return {
        "deployable": DEPLOYABLES / scenario,
        "scenario": SCENARIOS / scenario / "workflows" / subdir,
        "template": TEMPLATES / template,
    }


#: (scenario, copy) pairs — the parametrization grid for the shape tests.
GRID = [(s, which) for s in sorted(B5) for which in ("deployable", "scenario", "template")]


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


def _edges_into(graph: WorkflowGraph, node_id: str) -> set[tuple[str, str | None]]:
    return {(e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == node_id}


def _sequential_chain(graph: WorkflowGraph, start: str) -> list[str]:
    """Walk single sequential successors from ``start`` — the chain order."""
    order = [start]
    current = start
    while True:
        nxt = [e.to_id for e in graph.successors(current) if not e.metadata.get("synthetic")]
        if not nxt:
            return order
        assert len(nxt) == 1, f"{current!r} is not a single-successor chain node: {nxt}"
        current = nxt[0]
        order.append(current)


# ---------------------------------------------------------------------------
# 1. Graph shape — node types, chain order, convergence (098), routes (099)
# ---------------------------------------------------------------------------

_EXPECTED_NODE_TYPES: dict[str, dict[str, NodeType]] = {
    "employee-onboarding": {
        "plan": NodeType.AGENT,
        "provision-ad": NodeType.TOOL,
        "provision-email": NodeType.TOOL,
        "provision-equipment": NodeType.TOOL,
        "welcome": NodeType.AGENT,
    },
    "incident-response": {
        "diagnose": NodeType.AGENT,
        "confidence-gate": NodeType.DECISION,
        "remediate": NodeType.TOOL,
        "verify": NodeType.AGENT,
        "resolved-gate": NodeType.DECISION,
        "escalate": NodeType.HUMAN,
        "notify": NodeType.AGENT,
    },
    "cross-system-action": {
        "plan": NodeType.AGENT,
        "crm-update": NodeType.TOOL,
        "erp-update": NodeType.TOOL,
        "create-ticket": NodeType.TOOL,
        "send-email": NodeType.TOOL,
        "audit-summary": NodeType.AGENT,
    },
}

_EXPECTED_ENTRYPOINTS = {
    "employee-onboarding": "plan",
    "incident-response": "diagnose",
    "cross-system-action": "plan",
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
def test_onboarding_chain_order_is_structural(which: str) -> None:
    """The provisioning order is the GRAPH, not a convention: a strict
    single-successor chain from plan to welcome."""
    graph = _graph(_workflow_dirs("employee-onboarding")[which])
    assert _sequential_chain(graph, "plan") == [
        "plan",
        "provision-ad",
        "provision-email",
        "provision-equipment",
        "welcome",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_cross_system_chain_order_is_structural(which: str) -> None:
    """THE order assertion of scenario #18: CRM → ERP → ticket → email is a
    strict single-successor chain — the ERP can never be touched before the
    CRM (the cases.yaml honesty notes lean on exactly this)."""
    graph = _graph(_workflow_dirs("cross-system-action")[which])
    assert _sequential_chain(graph, "plan") == [
        "plan",
        "crm-update",
        "erp-update",
        "create-ticket",
        "send-email",
        "audit-summary",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_incident_escalation_convergence_and_reachability(which: str) -> None:
    """ADR 098: BOTH escalation reasons (low confidence, failed remediation)
    converge on the ONE escalate gate; notify is reached only via the
    resolved-decision leg or the gate's own routing; remediation is reachable
    only via the confidence leg."""
    graph = _graph(_workflow_dirs("incident-response")[which])
    assert _edges_into(graph, "escalate") == {
        ("confidence-gate", "decision"),
        ("resolved-gate", "decision"),
    }
    assert _edges_into(graph, "notify") == {
        ("resolved-gate", "decision"),
        ("escalate", "human-route"),
    }
    assert _edges_into(graph, "remediate") == {("confidence-gate", "decision")}
    assert _edges_into(graph, "verify") == {("remediate", None)}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_incident_gate_routes_and_fail_open_fallback(which: str) -> None:
    """ADR 099: the escalation gate routes its own ack; the fallback is ALSO
    notify — an acknowledgement can delay closure, never wedge the run."""
    graph = _graph(_workflow_dirs("incident-response")[which])
    gate = graph.nodes["escalate"]
    assert gate.metadata["routes"] == {"ack": "notify"}
    assert gate.metadata["fallback"] == "notify"
    assert gate.metadata["route_on"] == "decision"


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_incident_decision_rules(which: str) -> None:
    """The two ADR 094 decision nodes carry exactly the documented rules:
    confidence gte 0.7 → remediate (default escalate); resolved eq true →
    notify (default escalate)."""
    graph = _graph(_workflow_dirs("incident-response")[which])
    conf = graph.nodes["confidence-gate"]
    assert conf.metadata["cases"] == [
        {"when": {"field": "confidence", "op": "gte", "value": 0.7}, "to": "remediate"}
    ]
    assert conf.metadata["default"] == "escalate"
    res = graph.nodes["resolved-gate"]
    assert res.metadata["cases"] == [
        {"when": {"field": "resolved", "op": "eq", "value": True}, "to": "notify"}
    ]
    assert res.metadata["default"] == "escalate"


# ---------------------------------------------------------------------------
# 2. The fan-out finding (ADR 092) — why onboarding ships sequential
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_branch_diamond(tmp_path: Path) -> Path:
    """The employee-onboarding deployable rewritten as its conceptual fan-out
    diamond: plan fans out to the three provisioning TOOL nodes, which
    reconverge on welcome."""
    wf = tmp_path / "onboarding-diamond"
    shutil.copytree(DEPLOYABLES / "employee-onboarding", wf)
    (wf / "workflow.yaml").write_text(
        """\
api_version: movate/v1
kind: Workflow
name: onboarding-diamond
version: 0.1.0
description: the conceptual fan-out diamond over the three provisioning TOOLs
runtime: temporal
state_schema: ./state.json
entrypoint: plan
nodes:
  - {id: plan, type: agent, ref: ./agents/plan}
  - {id: provision-ad, type: tool, skill: sim-provision-ad}
  - {id: provision-email, type: tool, skill: sim-provision-email}
  - {id: provision-equipment, type: tool, skill: sim-provision-equipment}
  - {id: welcome, type: agent, ref: ./agents/welcome}
edges:
  - {from: plan, to: provision-ad, kind: fan_out}
  - {from: plan, to: provision-email, kind: fan_out}
  - {from: plan, to: provision-equipment, kind: fan_out}
  - {from: provision-ad, to: welcome, kind: fan_in}
  - {from: provision-email, to: welcome, kind: fan_in}
  - {from: provision-equipment, to: welcome, kind: fan_in}
"""
    )
    return wf


@pytest.mark.unit
def test_fan_out_with_tool_branches_is_rejected_by_the_phase_gate(
    tool_branch_diamond: Path,
) -> None:
    """Blocker (a): ADR 092 Phase 1 parallel graphs are AGENT-ONLY —
    ``validate_dag`` rejects TOOL nodes anywhere in a fan-out/fan-in graph.
    When a later phase admits TOOL branches this test fails, which is the
    deliberate reminder to flip employee-onboarding back to the diamond
    (its branch output keys are already disjoint, so the default last_wins
    join is safe)."""
    spec, parent = load_workflow_spec(tool_branch_diamond)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="only type=agent"):
        validate_graph(graph)


@pytest.mark.unit
def test_fan_out_lowering_dispatches_every_branch_as_an_agent(
    tool_branch_diamond: Path,
) -> None:
    """Blocker (b): even past validation, the Temporal lowering
    (``_emit_fan_out_node``) emits EVERY branch via ``call_agent_activity``
    — a TOOL branch would be loaded as an agent (its ref is a skill dir) at
    run time, and ``call_skill_activity`` is never scheduled. Pinned so the
    sequential fallback reads as a decision, not an accident."""
    spec, parent = load_workflow_spec(tool_branch_diamond)
    graph = compile_workflow(spec, parent)
    result = TemporalCompiler().compile(graph)  # deliberately NOT validated
    assert "call_skill_activity" not in result.activity_names
    src = result.module_source
    # The three TOOL branch refs (skill dirs) are passed to the AGENT activity.
    gather = src[src.index("asyncio.gather") :]
    for skill in ("sim-provision-ad", "sim-provision-email", "sim-provision-equipment"):
        assert f"skills/{skill}" in gather
    assert "call_agent_activity" in gather
    assert "call_skill_activity" not in gather


# ---------------------------------------------------------------------------
# 3. Temporal compilation — exact activity sets, inline decision routing
# ---------------------------------------------------------------------------

_EXPECTED_ACTIVITIES = {
    # agents + TOOL dispatch + terminal persist; no decision/human anywhere.
    "employee-onboarding": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    # + the durable HUMAN pause for escalate; decisions stay inline (ADR 094).
    "incident-response": {
        "call_agent_activity",
        "call_human_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    "cross-system-action": {
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
    if scenario == "incident-response":
        # Decision nodes route inline via the shared helper — no activity,
        # no LLM (ADR 094).
        assert "evaluate_decision(" in src


# ---------------------------------------------------------------------------
# 4. cases.yaml — parses through the DRIVER's loader, per-path expectations
# ---------------------------------------------------------------------------

_ONBOARDING_ROWS = [
    ("identity", "provision_ad", 1),
    ("email", "provision_mailbox", 1),
    ("itsm", "order_equipment", 1),
]
_CHAIN_ROWS = [
    ("salesforce", "update_record", 1),
    ("sap", "update_vendor", 1),
    ("servicenow", "create_ticket", 1),
    ("email", "send", 1),
]


@pytest.mark.unit
def test_onboarding_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "employee-onboarding" / "cases.yaml")
    assert spec.scenario == "employee-onboarding"
    assert spec.target == "employee-onboarding"
    assert [c.name for c in spec.cases] == ["standard-onboarding", "engineer-role"]
    standard, engineer = spec.cases

    # No human gate anywhere — no case declares hitl; governance + success
    # asserted on every case.
    assert all(c.hitl == () for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)

    # ALL THREE provisioning rows, exactly once, on both cases.
    for case in spec.cases:
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == _ONBOARDING_ROWS
        for key in ("ad_result", "email_result", "equipment_result", "summary"):
            assert key in case.expect.final_state_has

    # The role-keyed proof: the engineer case pins the role AND the resolved
    # bundle text in equipment_result; the standard case pins the default
    # bundle.
    assert dict(standard.expect.final_state_contains)["equipment_result"] == (
        "standard issue bundle",
    )
    engineer_contains = dict(engineer.expect.final_state_contains)["equipment_result"]
    assert set(engineer_contains) == {"engineer", "engineering workstation bundle"}


@pytest.mark.unit
def test_incident_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "incident-response" / "cases.yaml")
    assert spec.scenario == "incident-response"
    assert spec.target == "incident-response"
    assert [c.name for c in spec.cases] == [
        "high-confidence-auto-remediated",
        "low-confidence-escalated",
        "remediation-failed-escalated",
    ]
    auto, low, failed = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    # An escalation is a route, not an error — all three terminate success.
    assert all(c.expect.status == "success" for c in spec.cases)

    # Auto path: NO hitl declared (a wrong pause starves the fact poll and
    # fails durable-execution), the ops ATTEMPT row exactly once, applied
    # status pinned, and the human gate's decision key must NOT exist.
    assert auto.hitl == ()
    assert [(e.system, e.action, e.times) for e in auto.expect.side_effects] == [
        ("ops", "remediate", 1)
    ]
    assert "decision" in auto.expect.final_state_lacks
    assert dict(auto.expect.final_state_contains)["remediation_status"] == ("applied",)

    # Low-confidence path: pause at escalate + ack signal; automation never
    # touched the ops system, and no remediation/verify markers in state.
    assert [(s.node, s.decision) for s in low.hitl] == [("escalate", {"decision": "ack"})]
    assert low.expect.side_effects == ()
    assert [(e.system, e.action) for e in low.expect.no_side_effects] == [("ops", "remediate")]
    for key in ("remediation_result", "status_note"):
        assert key in low.expect.final_state_lacks
    assert "decision" in low.expect.final_state_has

    # Failed-remediation path: the attempt row IS on the ledger (automation
    # tried — the audit trail says so), failed status pinned, pause + ack.
    assert [(s.node, s.decision) for s in failed.hitl] == [("escalate", {"decision": "ack"})]
    assert [(e.system, e.action, e.times) for e in failed.expect.side_effects] == [
        ("ops", "remediate", 1)
    ]
    assert dict(failed.expect.final_state_contains)["remediation_status"] == ("failed",)
    for key in ("remediation_result", "status_note", "decision"):
        assert key in failed.expect.final_state_has


@pytest.mark.unit
def test_cross_system_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "cross-system-action" / "cases.yaml")
    assert spec.scenario == "cross-system-action"
    assert spec.target == "cross-system-action"
    assert [c.name for c in spec.cases] == ["full-chain", "vendor-address-update"]

    # ALL FOUR rows exactly once on every case; governance asserted; no
    # human gate anywhere. The ORDER guarantee is structural (the chain
    # tests above) — the driver's matcher is count-only, documented in the
    # cases.yaml honesty notes.
    for case in spec.cases:
        assert case.hitl == ()
        assert case.expect.status == "success"
        assert case.expect.governance == "allow"
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == _CHAIN_ROWS
        keys = ("steps", "crm_result", "erp_result", "ticket_result", "email_result", "summary")
        for key in keys:
            assert key in case.expect.final_state_has


# ---------------------------------------------------------------------------
# 5. Sim ledger skills — tmp-SQLite record + read-back
# ---------------------------------------------------------------------------


def _load_impl(scenario: str, skill: str) -> ModuleType:
    """Import a deployable copy's impl.py under a unique module name (the
    copies are byte-identical; a plain namespace import would be ambiguous
    in-repo)."""
    path = DEPLOYABLES / scenario / "skills" / skill / "impl.py"
    module_name = f"b5_{scenario}_{skill}_impl".replace("-", "_")
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
def test_sim_provision_ad_records_identity_row(sim_db: Path) -> None:
    impl = _load_impl("employee-onboarding", "sim-provision-ad")
    out = impl.run(
        {"employee": "Jordan Lee", "role": "account-manager"},
        SimpleNamespace(run_id="wfr-onb-1", mock=False),
    )
    assert set(out) == {"ad_result"}
    assert "Jordan Lee" in out["ad_result"]
    assert "AD-ACCT-" in out["ad_result"]
    expected_payload = {"employee": "Jordan Lee", "role": "account-manager"}
    assert _rows(sim_db) == [("wfr-onb-1", "identity", "provision_ad", expected_payload)]


@pytest.mark.unit
def test_sim_provision_email_records_mailbox_row(sim_db: Path) -> None:
    impl = _load_impl("employee-onboarding", "sim-provision-email")
    out = impl.run({"employee": "Jordan Lee"}, SimpleNamespace(run_id="wfr-onb-2", mock=False))
    assert set(out) == {"email_result"}
    assert "MBX-" in out["email_result"]
    assert _rows(sim_db) == [
        ("wfr-onb-2", "email", "provision_mailbox", {"employee": "Jordan Lee"})
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "bundle_marker"),
    [
        ("engineer", "engineering workstation bundle"),
        ("Engineer", "engineering workstation bundle"),  # trim+casefold key
        ("designer", "design workstation bundle"),
        ("account-manager", "standard issue bundle"),  # unmapped → default
    ],
)
def test_sim_provision_equipment_bundle_keyed_by_role(
    sim_db: Path, role: str, bundle_marker: str
) -> None:
    """The part the driver's (system, action, times) matcher cannot see: the
    ledger PAYLOAD reflects the role + the deterministically resolved bundle,
    and the result string names both (what cases.yaml pins via
    final_state_contains)."""
    impl = _load_impl("employee-onboarding", "sim-provision-equipment")
    out = impl.run(
        {"employee": "Priya Nair", "role": role},
        SimpleNamespace(run_id="wfr-onb-3", mock=False),
    )
    assert set(out) == {"equipment_result"}
    assert bundle_marker in out["equipment_result"]
    assert role in out["equipment_result"]  # the role itself is visible
    assert "EQP-" in out["equipment_result"]
    rows = _rows(sim_db)
    assert [(r[0], r[1], r[2]) for r in rows] == [("wfr-onb-3", "itsm", "order_equipment")]
    assert rows[0][3] == {"employee": "Priya Nair", "role": role, "bundle": rows[0][3]["bundle"]}
    assert bundle_marker in rows[0][3]["bundle"]


@pytest.mark.unit
def test_sim_remediate_applies_clean_faults(sim_db: Path) -> None:
    impl = _load_impl("incident-response", "sim-remediate")
    out = impl.run(
        {
            "alert": {
                "service": "payments-api",
                "severity": "high",
                "message": "Connection pool exhausted since the last deploy.",
            },
            "remediation": "Restart the workers and roll back the deploy.",
        },
        SimpleNamespace(run_id="wfr-inc-1", mock=False),
    )
    assert out["remediation_status"] == "applied"
    assert "status applied" in out["remediation_result"]
    assert "OPS-REM-" in out["remediation_result"]
    assert _rows(sim_db) == [
        (
            "wfr-inc-1",
            "ops",
            "remediate",
            {"service": "payments-api", "severity": "high", "status": "applied"},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "Primary disk array degraded on node db-7 — hardware fault reported.",
        "HARDWARE fault on the controller; failover stuck.",  # case-insensitive
        "Physical cabling damage suspected in rack B4.",
        "Requires manual intervention by the storage vendor.",
    ],
)
def test_sim_remediate_fails_unremediable_faults_but_records_the_attempt(
    sim_db: Path, message: str
) -> None:
    """The deterministic failure rule the remediation-failed case rides on —
    keyed off the alert MESSAGE (case input), never off LLM output. The
    attempt row lands on the ledger EITHER way: automation tried, the audit
    trail says so."""
    impl = _load_impl("incident-response", "sim-remediate")
    out = impl.run(
        {
            "alert": {"service": "db-cluster", "severity": "critical", "message": message},
            "remediation": "Fail over to the replica.",
        },
        SimpleNamespace(run_id="wfr-inc-2", mock=False),
    )
    assert out["remediation_status"] == "failed"
    assert "status failed" in out["remediation_result"]
    rows = _rows(sim_db)
    assert [(r[1], r[2]) for r in rows] == [("ops", "remediate")]
    assert rows[0][3]["status"] == "failed"


@pytest.mark.unit
def test_cross_system_chain_ledger_reads_back_in_insertion_order(sim_db: Path) -> None:
    """The four chain impls, run in chain order against one run_id, read back
    in exactly that order (``ORDER BY id``) — the ledger preserves what the
    chain wrote. The workflow-level order proof is structural (the chain
    tests above); the driver's case matcher is count-only, as documented in
    the cases.yaml honesty notes."""
    chain = [
        ("sim-crm-update", "crm_result"),
        ("sim-erp-update", "erp_result"),
        ("sim-ticket", "ticket_result"),
        ("sim-notify-email", "email_result"),
    ]
    ctx = SimpleNamespace(run_id="wfr-chain-1", mock=False)
    request = "Offboard contractor Alex Kim"
    for skill, out_key in chain:
        impl = _load_impl("cross-system-action", skill)
        out = impl.run({"action_request": request}, ctx)
        assert set(out) == {out_key}
    assert [(r[1], r[2]) for r in _rows(sim_db)] == [
        ("salesforce", "update_record"),
        ("sap", "update_vendor"),
        ("servicenow", "create_ticket"),
        ("email", "send"),
    ]
    assert all(r[0] == "wfr-chain-1" for r in _rows(sim_db))


_ALL_SKILLS: list[tuple[str, str, dict[str, Any]]] = [
    ("employee-onboarding", "sim-provision-ad", {"employee": "e", "role": "r"}),
    ("employee-onboarding", "sim-provision-email", {"employee": "e"}),
    ("employee-onboarding", "sim-provision-equipment", {"employee": "e", "role": "engineer"}),
    (
        "incident-response",
        "sim-remediate",
        {"alert": {"service": "s", "severity": "high", "message": "m"}, "remediation": "r"},
    ),
    ("cross-system-action", "sim-crm-update", {"action_request": "a"}),
    ("cross-system-action", "sim-erp-update", {"action_request": "a"}),
    ("cross-system-action", "sim-ticket", {"action_request": "a"}),
    ("cross-system-action", "sim-notify-email", {"action_request": "a"}),
]


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "skill", "payload"), _ALL_SKILLS)
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
    impl = _load_impl("cross-system-action", "sim-crm-update")
    impl.run(
        {"action_request": "a", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


@pytest.mark.unit
def test_sim_skill_reference_is_stable_per_run(sim_db: Path) -> None:
    """A Temporal activity retry must confirm the SAME synthetic reference."""
    impl = _load_impl("employee-onboarding", "sim-provision-equipment")
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    payload = {"employee": "Priya Nair", "role": "engineer"}
    assert impl.run(payload, ctx) == impl.run(payload, ctx)


# ---------------------------------------------------------------------------
# 6. The incident notify prompt renders on EVERY path
# ---------------------------------------------------------------------------

_NOTIFY_PATH_INPUTS = {
    # auto-remediated: verify ran, the human gate did not.
    "auto-remediated": {
        "alert": {"service": "payments-api", "severity": "high", "message": "pool exhausted"},
        "root_cause": "exhausted pool",
        "confidence": 0.9,
        "remediation_status": "applied",
        "status_note": "verified",
    },
    # low-confidence escalation: neither remediate nor verify ran.
    "low-confidence-escalated": {
        "alert": {"service": "checkout", "severity": "medium", "message": "cause unclear"},
        "root_cause": "unknown",
        "confidence": 0.3,
        "decision": "ack",
    },
    # failed remediation: everything ran.
    "remediation-failed-escalated": {
        "alert": {"service": "db-cluster", "severity": "critical", "message": "hardware fault"},
        "root_cause": "disk array fault",
        "confidence": 0.9,
        "remediation_status": "failed",
        "status_note": "still failing",
        "decision": "ack",
    },
}


@pytest.mark.unit
@pytest.mark.parametrize("path_name", sorted(_NOTIFY_PATH_INPUTS))
def test_incident_notify_prompt_renders_on_every_path(path_name: str) -> None:
    """The notify agent is reached from three exclusive paths with different
    key sets; the loader renders prompts under Jinja ``StrictUndefined``, so
    a conditionally-present key referenced WITHOUT a ``default`` filter
    raises ``UndefinedError`` at run time. This pins that every path's
    projection renders."""
    bundle = load_agent(DEPLOYABLES / "incident-response" / "agents" / "notify")
    state = _NOTIFY_PATH_INPUTS[path_name]
    props = bundle.input_schema["properties"]
    projected = {k: state[k] for k in props if k in state}  # the activity's projection
    rendered = bundle.render_prompt(projected)
    assert state["alert"]["service"] in rendered
    assert "n/a" in rendered or path_name == "remediation-failed-escalated"


# ---------------------------------------------------------------------------
# 7. ADR 100 trigger binding — the README's commands, smoke-tested for real
# ---------------------------------------------------------------------------

_ALERT_BODY = {
    "id": "ALRT-2026-0610-001",
    "service": "payments-api",
    "severity": "high",
    "message": "Connection pool exhausted on payments-api.",
}


@pytest.mark.unit
def test_trigger_registration_cli_matches_the_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact registration documented in
    workflows/incident-response/README.md, through the real CLI against a
    tmp local DB — flags, persisted record, and webhook path."""
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "local.db"))
    monkeypatch.setenv("MOVATE_TRACER", "silent")
    runner = CliRunner(mix_stderr=False)
    r = runner.invoke(
        app,
        [
            "trigger",
            "create",
            "incident-response",
            "-k",
            "workflow",
            "--name",
            "alertmanager-alerts",
            "--auth-mode",
            "token",
            "--dedup-key",
            "id",
            "--event-key",
            "alert",
            "--format",
            "json",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["kind"] == "workflow"
    assert payload["target"] == "incident-response"
    assert payload["name"] == "alertmanager-alerts"
    assert payload["auth_mode"] == "token"
    assert payload["dedup_key"] == "id"
    assert payload["event_key"] == "alert"
    assert payload["enabled"] is True
    assert payload["webhook_path"].startswith("/api/v1/triggers/")
    assert payload["webhook_path"].endswith("/events")
    assert payload["secret"]  # shown once for scripting capture
    # The documented token-mode caveat is actually surfaced to the operator.
    assert "weaker than hmac" in r.stderr


@pytest.mark.unit
def test_trigger_event_body_maps_to_the_workflow_input_shape() -> None:
    """ADR 100 D2 through the real core helpers: an alert source's raw body
    nests under the `alert` state key (exactly the diagnose agent's input
    shape) and the body `id` dedups redeliveries."""
    minted = mint_trigger(
        tenant_id="local",
        name="alertmanager-alerts",
        kind=JobKind.WORKFLOW,
        target="incident-response",
        input_defaults={},
        event_key="alert",
        dedup_key="id",
        auth_mode="token",
    )
    job = build_triggered_job(minted.record, dict(_ALERT_BODY))
    assert job.kind is JobKind.WORKFLOW
    assert job.target == "incident-response"
    # The whole raw body nests under `alert` — extra fields (id) ride along
    # harmlessly (the state schema is additionalProperties: true).
    assert job.input == {"alert": _ALERT_BODY}
    assert job.origin == f"trigger:{minted.record.trigger_id}"
    # Redeliveries dedup on the body id when the header is absent.
    assert resolve_body_delivery_id(minted.record, dict(_ALERT_BODY)) == "ALRT-2026-0610-001"
    # No id in the body → no dedup (fail-soft), never an exception.
    assert resolve_body_delivery_id(minted.record, {"service": "x"}) is None


# ---------------------------------------------------------------------------
# 8. Anti-drift — skills AND agents ship byte-identical in all three copies
# ---------------------------------------------------------------------------

_SKILL_FILES = ("skill.yaml", "impl.py", "schema/input.json", "schema/output.json")
_AGENT_FILES = ("agent.yaml", "prompt.md", "schema/input.json", "schema/output.json")

_SCENARIO_SKILLS = {
    "employee-onboarding": ("sim-provision-ad", "sim-provision-email", "sim-provision-equipment"),
    "incident-response": ("sim-remediate",),
    "cross-system-action": ("sim-crm-update", "sim-erp-update", "sim-ticket", "sim-notify-email"),
}
_SCENARIO_AGENTS = {
    "employee-onboarding": ("plan", "welcome"),
    "incident-response": ("diagnose", "verify", "notify"),
    "cross-system-action": ("plan", "audit-summary"),
}


def _assert_identical_across_copies(scenario: str, rel: str) -> None:
    canonical = (DEPLOYABLES / scenario / rel).read_bytes()
    scenario_root = SCENARIOS / scenario
    template_root = TEMPLATES / B5[scenario][1]
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
@pytest.mark.parametrize("scenario", sorted(B5))
def test_state_schema_identical_across_copies(scenario: str) -> None:
    """All three copies share one state contract (the deployable's
    ./state.json, the scenario root's state.json, the template's)."""
    canonical = (DEPLOYABLES / scenario / "state.json").read_bytes()
    assert (SCENARIOS / scenario / "state.json").read_bytes() == canonical
    assert (TEMPLATES / B5[scenario][1] / "state.json").read_bytes() == canonical
