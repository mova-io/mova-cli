"""ITSM service-request certification scenario — structural + skill tests.

The first production scenario of the 30-use-case program ships in THREE
copies that must stay coherent:

* ``workflows/itsm-request/`` — the deployable (``runtime: temporal``,
  workflow-local ``skills/sim-provision/`` so the worker image bakes the
  skill's ``impl.py`` for free — ADR 097 D2);
* ``certification/scenarios/itsm-request/`` — the suite mirror (relative
  refs + ``cases.yaml`` driven by ``certification/run_suite.py``);
* ``src/movate/templates/pattern_itsm_request/`` — the ``mdk init
  --pattern itsm-request`` template (covered by ``test_pattern_templates`` /
  ``test_init_pattern_cmd`` too; the graph-shape checks here run on it as a
  third compile source).

A native mock-run of the auto path is deliberately NOT tested: the tail
``notify`` agent needs the provision TOOL output threaded through a real
structured-output turn, which the mock provider cannot emulate meaningfully.
Instead this module asserts the things that ARE deterministic:

1. graph shape — node types (decision / human / tool / agent x2), the ADR 098
   exclusive convergence of the auto + approve paths on ``provision``, and the
   ADR 099 routes/fallback stamped on the gate;
2. Temporal compilation — both backends compile; the durable module schedules
   ``call_skill_activity`` for the TOOL node and NO activity for the decision
   routing (inline ``evaluate_decision``) nor any gate-classifier activity;
3. ``cases.yaml`` — parses through the driver's own loader
   (:func:`certification.harness.driver.load_scenario_spec`) with the six
   catalog cases and the right side-effect expectations per path;
4. the self-contained ``sim-provision`` skill impl — records the
   ``{system: itsm, action: provision}`` ledger row to a tmp SQLite
   (``MOVATE_DB``) and reads it back; honors ``ctx.mock`` + run_id precedence;
5. anti-drift — the skill files are byte-identical across the three copies.
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
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.spec import load_workflow_spec

REPO_ROOT = Path(__file__).resolve().parent.parent

DEPLOYABLE = REPO_ROOT / "workflows" / "itsm-request"
SCENARIO = REPO_ROOT / "certification" / "scenarios" / "itsm-request"
TEMPLATE = REPO_ROOT / "src" / "movate" / "templates" / "pattern_itsm_request"

# The three compile sources (id → workflow dir). The scenario copy nests its
# workflow under workflows/itsm (relative refs to the scenario root, the
# expense-approval layout).
WORKFLOW_DIRS = {
    "deployable": DEPLOYABLE,
    "scenario": SCENARIO / "workflows" / "itsm",
    "template": TEMPLATE,
}


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# 1. Graph shape — node types, convergence (ADR 098), gate routes (ADR 099)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", sorted(WORKFLOW_DIRS))
def test_graph_shape_and_node_types(which: str) -> None:
    graph = _graph(WORKFLOW_DIRS[which])
    validate_graph(graph)  # the phase gate must admit the routed/converged shape

    assert graph.entrypoint == "classify"
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "classify": NodeType.DECISION,
        "approval": NodeType.HUMAN,
        "provision": NodeType.TOOL,
        "notify": NodeType.AGENT,
        "rejected": NodeType.AGENT,
    }
    # The TOOL node resolved the sim-provision skill at compile time (ADR 097
    # D2) and stamped its metadata for the lints + the activity args.
    provision = graph.nodes["provision"]
    assert provision.metadata["skill"] == "sim-provision"
    assert Path(provision.ref).is_dir()
    assert (Path(provision.ref) / "impl.py").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("which", sorted(WORKFLOW_DIRS))
def test_auto_and_approve_paths_converge_on_provision(which: str) -> None:
    """ADR 098 exclusive convergence: the decision's auto case AND the gate's
    approve route both land on the ONE shared provision→notify tail."""
    graph = _graph(WORKFLOW_DIRS[which])

    into_provision = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "provision"
    }
    assert into_provision == {("classify", "decision"), ("approval", "human-route")}
    # One shared tail: provision → notify, and both reject exits land on the
    # single rejected node.
    assert {e.to_id for e in graph.edges if e.from_id == "provision"} == {"notify"}
    into_rejected = {e.from_id for e in graph.edges if e.to_id == "rejected"}
    assert into_rejected == {"approval"}


@pytest.mark.unit
@pytest.mark.parametrize("which", sorted(WORKFLOW_DIRS))
def test_gate_routes_and_fail_safe_fallback(which: str) -> None:
    """ADR 099: the gate routes its own decision; prose fails safe to rejected."""
    graph = _graph(WORKFLOW_DIRS[which])
    gate = graph.nodes["approval"]
    assert gate.metadata["routes"] == {"approve": "provision", "reject": "rejected"}
    assert gate.metadata["fallback"] == "rejected"
    assert gate.metadata["route_on"] == "decision"


# ---------------------------------------------------------------------------
# 2. Temporal compilation — call_skill_activity for the TOOL node, inline
#    (activity-free) decision routing, no gate-classifier activity
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", sorted(WORKFLOW_DIRS))
def test_temporal_compiles_with_skill_activity_and_inline_decision(which: str) -> None:
    result = TemporalCompiler().compile(_graph(WORKFLOW_DIRS[which]))
    src = result.module_source
    ast.parse(src)  # parses as Python

    # The decision routes inline via the shared helper — no activity, no LLM.
    assert "evaluate_decision(" in src
    # The exact activity set: agents (notify/rejected) + the durable HUMAN
    # pause + the TOOL dispatch + terminal persist. No gate-classifier
    # activity (ADR 094/099) and nothing new invented for the routing.
    assert set(result.activity_names) == {
        "call_agent_activity",
        "call_human_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    }


# ---------------------------------------------------------------------------
# 3. cases.yaml — parses through the DRIVER's loader with the 6 catalog cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cases_yaml_schema_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIO / "cases.yaml")
    assert spec.scenario == "itsm-request"
    assert spec.target == "itsm-request"
    assert [c.name for c in spec.cases] == [
        "password-reset-auto",
        "vpn-access-auto",
        "email-group-auto",
        "software-license-approve",
        "hardware-request-approve",
        "new-user-onboarding-reject",
    ]

    auto = [c for c in spec.cases if c.name.endswith("-auto")]
    approves = [c for c in spec.cases if c.name.endswith("-approve")]
    reject = next(c for c in spec.cases if c.name.endswith("-reject"))

    # 3 auto cases: no HITL, one positive itsm/provision ledger expectation.
    assert len(auto) == 3
    for case in auto:
        assert case.input["auto_approved"] is True
        assert case.hitl == ()
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("itsm", "provision", 1)
        ]
        assert "provision_result" in case.expect.final_state_has

    # 2 approval cases: pause at `approval`, signal approve, same ledger row.
    assert len(approves) == 2
    for case in approves:
        assert case.input["auto_approved"] is False
        assert [(s.node, s.decision) for s in case.hitl] == [("approval", {"decision": "approve"})]
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("itsm", "provision", 1)
        ]
        assert "provision_result" in case.expect.final_state_has

    # 1 reject case: pause + reject signal, NO provision row, no
    # provision_result marker — and still a terminal SUCCESS (rejection is a
    # route, not an error).
    assert [(s.node, s.decision) for s in reject.hitl] == [("approval", {"decision": "reject"})]
    assert reject.expect.status == "success"
    assert reject.expect.side_effects == ()
    assert [(e.system, e.action) for e in reject.expect.no_side_effects] == [("itsm", "provision")]
    assert "provision_result" in reject.expect.final_state_lacks

    # Governance is asserted (expected allow) on every case.
    assert all(c.expect.governance == "allow" for c in spec.cases)


# ---------------------------------------------------------------------------
# 4. The self-contained skill impl — tmp-SQLite record + read-back
# ---------------------------------------------------------------------------


def _load_impl() -> ModuleType:
    """Import the deployable copy's impl.py under a unique module name (three
    byte-identical copies exist; a plain ``sim-provision.impl`` import would
    be ambiguous in-repo)."""
    path = DEPLOYABLE / "skills" / "sim-provision" / "impl.py"
    spec = importlib.util.spec_from_file_location("itsm_sim_provision_impl", path)
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
    """Point the impl's ledger at a tmp SQLite file (never the shared PG)."""
    db = tmp_path / "sim.db"
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.delenv("MOVATE_PG_URL", raising=False)
    monkeypatch.setenv("MOVATE_DB", str(db))
    return db


@pytest.mark.unit
def test_skill_impl_records_provision_row_and_reads_back(sim_db: Path) -> None:
    impl = _load_impl()
    ctx = SimpleNamespace(run_id="wfr-itsm-1", mock=False)

    out = impl.run({"service": "vpn-access", "requester": "raj.patel"}, ctx)

    assert set(out) == {"provision_result"}
    assert "vpn-access" in out["provision_result"]
    assert "raj.patel" in out["provision_result"]
    assert "ITSM-PROV-" in out["provision_result"]

    assert _rows(sim_db) == [
        ("wfr-itsm-1", "itsm", "provision", {"service": "vpn-access", "requester": "raj.patel"})
    ]


@pytest.mark.unit
def test_skill_impl_run_id_input_overrides_ctx(sim_db: Path) -> None:
    """An explicit run_id in the input wins (the harness facades' convention);
    ctx.run_id is the fallback the tool node actually exercises."""
    impl = _load_impl()
    impl.run(
        {"service": "password-reset", "requester": "jane.doe", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


@pytest.mark.unit
def test_skill_impl_reference_is_stable_per_run_and_service(sim_db: Path) -> None:
    """A Temporal activity retry must confirm the SAME synthetic ticket id."""
    impl = _load_impl()
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    first = impl.run({"service": "email-group", "requester": "li.wei"}, ctx)
    second = impl.run({"service": "email-group", "requester": "li.wei"}, ctx)
    assert first == second


@pytest.mark.unit
def test_skill_impl_mock_short_circuits_without_db_write(sim_db: Path) -> None:
    impl = _load_impl()
    out = impl.run(
        {"service": "vpn-access", "requester": "raj.patel"},
        SimpleNamespace(run_id="wfr-mock", mock=True),
    )
    assert "provision_result" in out
    assert not sim_db.exists()  # the ledger was never touched


# ---------------------------------------------------------------------------
# 5. Anti-drift — the skill ships byte-identical in all three copies
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "rel",
    [
        "skills/sim-provision/skill.yaml",
        "skills/sim-provision/impl.py",
        "skills/sim-provision/schema/input.json",
        "skills/sim-provision/schema/output.json",
    ],
)
def test_skill_files_identical_across_copies(rel: str) -> None:
    canonical = (DEPLOYABLE / rel).read_bytes()
    assert (SCENARIO / rel).read_bytes() == canonical, f"scenario copy of {rel} drifted"
    assert (TEMPLATE / rel).read_bytes() == canonical, f"template copy of {rel} drifted"
