"""B1 approvals batch — scenarios #8, #14, #24 of the 30-use-case program.

Three certification scenarios, each shipping in the itsm-request THREE-copy
layout (deployable under ``workflows/``, suite mirror under
``certification/scenarios/``, ``mdk init --pattern`` template under
``src/movate/templates/``) that must stay coherent:

* ``purchase-order`` (#8) — DECISION tiering on the amount with a SEQUENTIAL
  APPROVAL CHAIN: manager gate, then a second DECISION chains >5000 orders
  into the director gate; PO creation is the ``sim-create-po`` TOOL node.
* ``approval-timeout`` (#14) — the first live exercise of ADR 062 D4 durable
  timeouts: a primary HUMAN gate whose 90s expiry escalates to a second HUMAN
  gate, whose own expiry fails safe to rejected; fulfilment is the
  ``sim-fulfill`` TOOL node.
* ``human-escalation`` (#24) — confidence-gated autonomy with
  resume-with-feedback: a triage agent self-scores, a DECISION node routes
  ``confidence gte 0.8`` past review, the review gate's ``feedback`` threads
  into the finalize prompt.

Like ``test_itsm_scenario``, native mock-runs are deliberately NOT tested
(the tails need real structured-output turns); this module asserts what IS
deterministic:

1. graph shape per scenario — node types, the chain / timeout / confidence
   routing metadata, the ADR 098 exclusive convergence, the ADR 062 D4
   synthetic ``human-timeout`` edges;
2. Temporal compilation — each scenario compiles; the timeout gates emit the
   durable ``timedelta(seconds=90.0)`` deadline; activity sets are exact;
3. ``cases.yaml`` — every scenario parses through the driver's own loader
   with the right pause / wait_timeout / side-effect expectations per path;
4. the driver's ADR 062 D4 extension — ``wait_timeout`` hitl steps +
   per-case ``timeout_s`` parse (back-compat preserved) and drive correctly
   against a scripted fake runtime (observe-don't-signal, then signal the
   SECOND gate / wait out the final expiry);
5. the self-contained skill impls — tmp-SQLite ledger record + read-back,
   ``ctx.mock`` short-circuit, run_id precedence, retry-stable references;
6. anti-drift — each skill ships byte-identical across its copies.
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

import httpx
import pytest
from certification.harness.driver import (
    CaseExpect,
    CaseSpec,
    CaseSpecError,
    HitlStep,
    RuntimeApiClient,
    ScenarioSpec,
    SuiteDriver,
    load_scenario_spec,
)

from movate.core.workflow.compiler import compile_workflow, validate_graph
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.spec import load_workflow_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / "workflows"
SCENARIOS = REPO_ROOT / "certification" / "scenarios"
TEMPLATES = REPO_ROOT / "src" / "movate" / "templates"

# scenario name → (deployable, scenario workflow dir, template). The scenario
# copies nest their workflow under workflows/<short>/ (relative refs to the
# scenario root — the itsm-request/expense-approval layout).
COPIES: dict[str, dict[str, Path]] = {
    "purchase-order": {
        "deployable": WORKFLOWS / "purchase-order",
        "scenario": SCENARIOS / "purchase-order" / "workflows" / "po",
        "template": TEMPLATES / "pattern_purchase_order",
    },
    "approval-timeout": {
        "deployable": WORKFLOWS / "approval-timeout",
        "scenario": SCENARIOS / "approval-timeout" / "workflows" / "timeout",
        "template": TEMPLATES / "pattern_approval_timeout",
    },
    "human-escalation": {
        "deployable": WORKFLOWS / "human-escalation",
        "scenario": SCENARIOS / "human-escalation" / "workflows" / "escalation",
        "template": TEMPLATES / "pattern_human_escalation",
    },
}
COPY_IDS = sorted(COPIES["purchase-order"])  # deployable / scenario / template


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# 1a. purchase-order — chained gates (ADR 099 x2 + ADR 094 x2), shared tail
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_purchase_order_graph_shape(which: str) -> None:
    graph = _graph(COPIES["purchase-order"][which])
    validate_graph(graph)

    assert graph.entrypoint == "classify"
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "classify": NodeType.DECISION,
        "manager-approval": NodeType.HUMAN,
        "escalate-check": NodeType.DECISION,
        "director-approval": NodeType.HUMAN,
        "create-po": NodeType.TOOL,
        "notify": NodeType.AGENT,
        "rejected": NodeType.AGENT,
    }
    # The TOOL node resolved sim-create-po at compile time (ADR 097 D2).
    create_po = graph.nodes["create-po"]
    assert create_po.metadata["skill"] == "sim-create-po"
    assert Path(create_po.ref).is_dir()
    assert (Path(create_po.ref) / "impl.py").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_purchase_order_chain_is_sequential(which: str) -> None:
    """The >5000 chain: manager approve does NOT reach the PO directly — it
    routes into the deterministic escalate-check, and only the DIRECTOR's
    approve (or the ≤5000 escalate-check default) reaches create-po."""
    graph = _graph(COPIES["purchase-order"][which])

    # Tier predicates (ADR 094) — pure values, no LLM.
    assert graph.nodes["classify"].metadata == {
        "cases": [{"when": {"field": "amount", "op": "lte", "value": 500}, "to": "create-po"}],
        "default": "manager-approval",
    }
    assert graph.nodes["escalate-check"].metadata == {
        "cases": [
            {"when": {"field": "amount", "op": "gt", "value": 5000}, "to": "director-approval"}
        ],
        "default": "create-po",
    }
    # Gate routing (ADR 099): the manager's approve lands on the CHAIN check,
    # never on the PO; the director's approve is the chain's only PO entry.
    manager = graph.nodes["manager-approval"]
    assert manager.metadata["routes"] == {"approve": "escalate-check", "reject": "rejected"}
    assert manager.metadata["fallback"] == "rejected"
    director = graph.nodes["director-approval"]
    assert director.metadata["routes"] == {"approve": "create-po", "reject": "rejected"}
    assert director.metadata["fallback"] == "rejected"
    # The director gate is reachable ONLY through escalate-check (i.e. only
    # after the manager approved) — the chain is sequential by construction.
    assert {e.from_id for e in graph.edges if e.to_id == "director-approval"} == {"escalate-check"}


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_purchase_order_converges_on_shared_tail(which: str) -> None:
    """ADR 098 exclusive convergence: auto tier + ≤5000 escalate-check default
    + director approve all land on ONE create-po→notify tail; both gates'
    rejects land on ONE rejected node."""
    graph = _graph(COPIES["purchase-order"][which])

    into_create_po = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "create-po"
    }
    assert into_create_po == {
        ("classify", "decision"),
        ("escalate-check", "decision"),
        ("director-approval", "human-route"),
    }
    assert {e.to_id for e in graph.edges if e.from_id == "create-po"} == {"notify"}
    into_rejected = {e.from_id for e in graph.edges if e.to_id == "rejected"}
    assert into_rejected == {"manager-approval", "director-approval"}


# ---------------------------------------------------------------------------
# 1b. approval-timeout — ADR 062 D4 durable deadlines + escalation routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_approval_timeout_graph_shape(which: str) -> None:
    graph = _graph(COPIES["approval-timeout"][which])
    validate_graph(graph)

    assert graph.entrypoint == "primary-approval"
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "primary-approval": NodeType.HUMAN,
        "escalation-approval": NodeType.HUMAN,
        "fulfill": NodeType.TOOL,
        "notify": NodeType.AGENT,
        "rejected": NodeType.AGENT,
    }
    fulfill = graph.nodes["fulfill"]
    assert fulfill.metadata["skill"] == "sim-fulfill"
    assert (Path(fulfill.ref) / "impl.py").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_approval_timeout_gates_carry_durable_deadlines(which: str) -> None:
    """ADR 062 D4 metadata: both gates carry timeout=90; the primary's expiry
    escalates, the escalation's expiry fails safe to rejected."""
    graph = _graph(COPIES["approval-timeout"][which])

    primary = graph.nodes["primary-approval"]
    assert primary.metadata["timeout"] == 90
    assert primary.metadata["on_timeout"] == "escalation-approval"
    assert primary.metadata["routes"] == {"approve": "fulfill", "reject": "rejected"}
    assert primary.metadata["fallback"] == "rejected"

    escalation = graph.nodes["escalation-approval"]
    assert escalation.metadata["timeout"] == 90
    assert escalation.metadata["on_timeout"] == "rejected"
    assert escalation.metadata["routes"] == {"approve": "fulfill", "reject": "rejected"}
    assert escalation.metadata["fallback"] == "rejected"


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_approval_timeout_synthetic_timeout_edges_and_convergence(which: str) -> None:
    """The compiler injects synthetic ``human-timeout`` edges for the ADR 062
    D4 legs (ADR 098 makes them convergence-eligible); both approves converge
    on ONE fulfill and every reject/expiry on ONE rejected."""
    graph = _graph(COPIES["approval-timeout"][which])

    timeout_edges = {
        (e.from_id, e.to_id) for e in graph.edges if e.metadata.get("source") == "human-timeout"
    }
    assert timeout_edges == {
        ("primary-approval", "escalation-approval"),
        ("escalation-approval", "rejected"),
    }
    assert all(
        e.metadata.get("synthetic")
        for e in graph.edges
        if e.metadata.get("source") == "human-timeout"
    )

    into_fulfill = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "fulfill"
    }
    assert into_fulfill == {
        ("primary-approval", "human-route"),
        ("escalation-approval", "human-route"),
    }
    into_rejected = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "rejected"
    }
    assert into_rejected == {
        ("primary-approval", "human-route"),
        ("escalation-approval", "human-route"),
        ("escalation-approval", "human-timeout"),
    }


# ---------------------------------------------------------------------------
# 1c. human-escalation — confidence decision + resume-with-feedback
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_human_escalation_graph_shape(which: str) -> None:
    graph = _graph(COPIES["human-escalation"][which])
    validate_graph(graph)

    assert graph.entrypoint == "triage"
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "triage": NodeType.AGENT,
        "confidence-check": NodeType.DECISION,
        "review": NodeType.HUMAN,
        "finalize": NodeType.AGENT,
        "rejected": NodeType.AGENT,
    }


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_human_escalation_confidence_routing_and_review_contract(which: str) -> None:
    """The autonomy boundary is a pure numeric predicate (ADR 094) and the
    review gate's contract carries BOTH decision and feedback (ADR 099)."""
    graph = _graph(COPIES["human-escalation"][which])

    assert graph.nodes["confidence-check"].metadata == {
        "cases": [{"when": {"field": "confidence", "op": "gte", "value": 0.8}, "to": "finalize"}],
        "default": "review",
    }
    review = graph.nodes["review"]
    assert review.metadata["output_contract"] == ["decision", "feedback"]
    assert review.metadata["routes"] == {"approve": "finalize", "reject": "rejected"}
    assert review.metadata["fallback"] == "rejected"

    into_finalize = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "finalize"
    }
    assert into_finalize == {("confidence-check", "decision"), ("review", "human-route")}
    assert {e.from_id for e in graph.edges if e.to_id == "rejected"} == {"review"}


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_human_escalation_finalize_prompt_threads_feedback(which: str) -> None:
    """Resume-with-feedback: the finalize prompt INCORPORATES input.feedback
    when present — guarded with `is defined` so the high-confidence auto path
    (where the review gate never merged a feedback key) still renders under
    StrictUndefined."""
    graph = _graph(COPIES["human-escalation"][which])
    prompt = (Path(graph.nodes["finalize"].ref) / "prompt.md").read_text()
    assert "input.feedback" in prompt
    assert "is defined" in prompt


# ---------------------------------------------------------------------------
# 2. Temporal compilation — durable timers, exact activity sets
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
@pytest.mark.parametrize(
    ("scenario", "activities"),
    [
        (
            "purchase-order",
            {
                "call_agent_activity",
                "call_human_activity",
                "call_skill_activity",
                "persist_workflow_result_activity",
            },
        ),
        (
            "approval-timeout",
            {
                "call_agent_activity",
                "call_human_activity",
                "call_skill_activity",
                "persist_workflow_result_activity",
            },
        ),
        (
            "human-escalation",
            {
                "call_agent_activity",
                "call_human_activity",
                "persist_workflow_result_activity",
            },
        ),
    ],
)
def test_temporal_compiles_with_exact_activity_set(
    scenario: str, activities: set[str], which: str
) -> None:
    result = TemporalCompiler().compile(_graph(COPIES[scenario][which]))
    src = result.module_source
    ast.parse(src)
    assert set(result.activity_names) == activities
    # Gate decisions route via the shared helper — never a classifier
    # activity (ADR 099); decision nodes route inline (ADR 094).
    assert "evaluate_human_route(" in src
    if scenario != "approval-timeout":
        assert "evaluate_decision(" in src


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_temporal_emits_durable_timeout_waits(which: str) -> None:
    """ADR 062 D4 live: both gates compile to a deadline-bound
    ``wait_condition`` whose TimeoutError arm takes the on_timeout route."""
    result = TemporalCompiler().compile(_graph(COPIES["approval-timeout"][which]))
    src = result.module_source
    assert src.count("timedelta(seconds=90.0)") == 2
    assert "except asyncio.TimeoutError:" in src
    assert "current = 'escalation-approval'" in src  # the primary's expiry route
    assert "current = 'rejected'" in src  # the escalation's expiry route


# ---------------------------------------------------------------------------
# 3. cases.yaml — every scenario parses through the DRIVER's loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_purchase_order_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "purchase-order" / "cases.yaml")
    assert spec.scenario == "purchase-order"
    assert spec.target == "purchase-order"
    assert [c.name for c in spec.cases] == [
        "auto-approve-small",
        "manager-approve",
        "manager-reject",
        "chain-both-approve",
        "chain-director-reject",
    ]
    by_name = {c.name: c for c in spec.cases}

    auto = by_name["auto-approve-small"]
    assert auto.input["amount"] <= 500
    assert auto.hitl == ()
    assert [(e.system, e.action, e.times) for e in auto.expect.side_effects] == [
        ("erp", "create_po", 1)
    ]

    manager_ok = by_name["manager-approve"]
    assert 500 < manager_ok.input["amount"] <= 5000
    assert [(s.node, s.decision) for s in manager_ok.hitl] == [
        ("manager-approval", {"decision": "approve"})
    ]

    manager_no = by_name["manager-reject"]
    assert [(s.node, s.decision) for s in manager_no.hitl] == [
        ("manager-approval", {"decision": "reject"})
    ]
    assert [(e.system, e.action) for e in manager_no.expect.no_side_effects] == [
        ("erp", "create_po")
    ]
    assert "po_result" in manager_no.expect.final_state_lacks

    # The chain cases pause TWICE, manager first — sequential approvals.
    chain_ok = by_name["chain-both-approve"]
    assert chain_ok.input["amount"] > 5000
    assert [(s.node, s.decision) for s in chain_ok.hitl] == [
        ("manager-approval", {"decision": "approve"}),
        ("director-approval", {"decision": "approve"}),
    ]
    assert [(e.system, e.action, e.times) for e in chain_ok.expect.side_effects] == [
        ("erp", "create_po", 1)
    ]

    # The chain's teeth: manager approved, director rejected ⇒ NO create_po.
    chain_no = by_name["chain-director-reject"]
    assert chain_no.input["amount"] > 5000
    assert [(s.node, s.decision) for s in chain_no.hitl] == [
        ("manager-approval", {"decision": "approve"}),
        ("director-approval", {"decision": "reject"}),
    ]
    assert [(e.system, e.action) for e in chain_no.expect.no_side_effects] == [("erp", "create_po")]
    assert "po_result" in chain_no.expect.final_state_lacks
    assert chain_no.expect.side_effects == ()

    assert all(c.expect.status == "success" for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.timeout_s is None for c in spec.cases)  # no durable waits here


@pytest.mark.unit
def test_approval_timeout_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "approval-timeout" / "cases.yaml")
    assert spec.scenario == "approval-timeout"
    assert [c.name for c in spec.cases] == [
        "primary-approves",
        "primary-times-out-then-escalation-approves",
        "both-time-out",
    ]
    signal_now, escalate, expire = spec.cases

    # Control: signal the first observed pause immediately; no wait_timeout.
    assert [(s.node, s.decision, s.wait_timeout) for s in signal_now.hitl] == [
        ("primary-approval", {"decision": "approve"}, False)
    ]
    assert signal_now.timeout_s is None
    assert [(e.system, e.action, e.times) for e in signal_now.expect.side_effects] == [
        ("fulfillment", "fulfill", 1)
    ]

    # ADR 062 D4: observe the FIRST pause without signalling, then signal the
    # SECOND gate. The case budgets real wall clock for the 90s deadline.
    assert [(s.node, s.decision, s.wait_timeout) for s in escalate.hitl] == [
        ("primary-approval", None, True),
        ("escalation-approval", {"decision": "approve"}, False),
    ]
    assert escalate.timeout_s is not None and escalate.timeout_s >= 90 * 2
    assert "fulfill_result" in escalate.expect.final_state_has

    # Total silence: both gates observed, neither signalled, ≥180s budgeted;
    # nobody decided and nothing was fulfilled.
    assert [(s.node, s.wait_timeout) for s in expire.hitl] == [
        ("primary-approval", True),
        ("escalation-approval", True),
    ]
    assert expire.timeout_s is not None and expire.timeout_s >= 90 * 4
    assert expire.expect.side_effects == ()
    assert [(e.system, e.action) for e in expire.expect.no_side_effects] == [
        ("fulfillment", "fulfill")
    ]
    assert set(expire.expect.final_state_lacks) == {"fulfill_result", "decision"}

    assert all(c.expect.status == "success" for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in spec.cases)


@pytest.mark.unit
def test_human_escalation_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "human-escalation" / "cases.yaml")
    assert spec.scenario == "human-escalation"
    assert [c.name for c in spec.cases] == [
        "high-confidence-auto",
        "low-confidence-review-approve",
        "low-confidence-reject",
    ]
    auto, approve, reject = spec.cases

    # Auto path: no gate, finalize ran, and feedback must NEVER appear.
    assert auto.hitl == ()
    assert "final_answer" in auto.expect.final_state_has
    assert "feedback" in auto.expect.final_state_lacks

    # The review gate's contract is [decision, feedback] and the signal
    # endpoint 422s on a missing key — BOTH signals must carry both.
    for case in (approve, reject):
        assert [s.node for s in case.hitl] == ["review"]
        decision = case.hitl[0].decision
        assert decision is not None
        assert set(decision) == {"decision", "feedback"}
        assert decision["feedback"].strip()

    assert approve.hitl[0].decision is not None
    assert approve.hitl[0].decision["decision"] == "approve"
    assert "final_answer" in approve.expect.final_state_has
    assert "feedback" in approve.expect.final_state_has  # the contract merge

    assert reject.hitl[0].decision is not None
    assert reject.hitl[0].decision["decision"] == "reject"
    assert "final_answer" in reject.expect.final_state_lacks
    assert "summary" in reject.expect.final_state_has

    # No tool node by design — no ledger expectations anywhere.
    assert all(c.expect.side_effects == () and c.expect.no_side_effects == () for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in spec.cases)


# ---------------------------------------------------------------------------
# 4a. Driver schema extension — wait_timeout / timeout_s parsing (back-compat)
# ---------------------------------------------------------------------------


def _write_cases(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cases.yaml"
    p.write_text(body, encoding="utf-8")
    return p


_CASE_HEADER = "scenario: s\ntarget: t\ncases:\n"


@pytest.mark.unit
def test_wait_timeout_step_parses_and_decision_stays_required_without_it(
    tmp_path: Path,
) -> None:
    spec = load_scenario_spec(
        _write_cases(
            tmp_path,
            _CASE_HEADER
            + "  - name: a\n    input: {}\n    timeout_s: 300\n"
            + "    hitl:\n      - {node: g1, wait_timeout: true}\n"
            + "      - {node: g2, decision: {decision: approve}}\n"
            + "    expect: {status: success}\n",
        )
    )
    (case,) = spec.cases
    assert case.timeout_s == 300.0
    assert [(s.node, s.decision, s.wait_timeout) for s in case.hitl] == [
        ("g1", None, True),
        ("g2", {"decision": "approve"}, False),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (  # back-compat: a plain step still requires a decision
            _CASE_HEADER + "  - name: a\n    input: {}\n    hitl: [{node: g}]\n"
            "    expect: {status: success}\n",
            "`decision` (non-empty mapping) is required",
        ),
        (  # wait_timeout + decision is a contradiction
            _CASE_HEADER + "  - name: a\n    input: {}\n"
            "    hitl: [{node: g, wait_timeout: true, decision: {decision: approve}}]\n"
            "    expect: {status: success}\n",
            "`decision` must be omitted when `wait_timeout` is true",
        ),
        (  # wait_timeout must be a bool
            _CASE_HEADER + "  - name: a\n    input: {}\n"
            "    hitl: [{node: g, wait_timeout: nope}]\n"
            "    expect: {status: success}\n",
            "`wait_timeout` must be a boolean",
        ),
        (  # timeout_s must be a positive number
            _CASE_HEADER + "  - name: a\n    input: {}\n    timeout_s: -5\n"
            "    expect: {status: success}\n",
            "timeout_s: must be a positive number",
        ),
        (
            _CASE_HEADER + "  - name: a\n    input: {}\n    timeout_s: true\n"
            "    expect: {status: success}\n",
            "timeout_s: must be a positive number",
        ),
    ],
)
def test_driver_extension_parse_failures(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(CaseSpecError, match=r".*") as exc_info:
        load_scenario_spec(_write_cases(tmp_path, body))
    assert fragment in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4b. Driver behavior — wait_timeout drives observe→wait→signal correctly
# ---------------------------------------------------------------------------


class ScriptedTimeoutRuntime:
    """A fake runtime where pauses advance on their own (the durable timer),
    NOT on signals. ``pause_script`` is consumed one entry per
    ``?status=paused`` GET (the last entry repeats); the terminal fact
    appears once the script is exhausted AND all ``signals_required``
    arrived."""

    def __init__(self, pause_script: list[str | None], signals_required: int) -> None:
        self.pause_script = list(pause_script)
        self.signals_required = signals_required
        self.signals: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/run":
            return httpx.Response(202, json={"job_id": "job-1", "status": "queued"})
        if request.method == "GET" and path == "/jobs/job-1":
            return httpx.Response(
                200, json={"job_id": "job-1", "status": "success", "result_run_id": "wfr-1"}
            )
        if request.method == "GET" and path == "/api/v1/workflow-runs":
            if request.url.params.get("status") == "paused":
                node = self.pause_script[0]
                if len(self.pause_script) > 1:
                    self.pause_script.pop(0)
                if node is None:
                    return httpx.Response(200, json={"workflow_runs": [], "count": 0})
                row = {
                    "workflow_run_id": "wfr-1",
                    "status": "paused",
                    "paused_node_id": node,
                }
                return httpx.Response(200, json={"workflow_runs": [row], "count": 1})
            row = {"workflow_run_id": "wfr-1", "status": "success", "final_state": {}}
            return httpx.Response(200, json={"workflow_runs": [row], "count": 1})
        if request.method == "POST" and path == "/api/v1/workflow-runs/wfr-1/signal":
            self.signals.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": "job-2", "status": "running"})
        if request.method == "GET" and path == "/api/v1/observability/facts":
            done = len(self.pause_script) == 1 and len(self.signals) >= self.signals_required
            fact = {
                "kind": "workflow_run",
                "source_id": "wfr-1",
                "workflow": "approval-timeout",
                "status": "success" if done else "paused",
                "route": None,
                "cost_usd": 0.0,
                "governance_effect": "allow",
                "error_type": None,
            }
            return httpx.Response(200, json={"facts": [fact], "count": 1})
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})


def _timeout_driver(fake: ScriptedTimeoutRuntime, **kwargs: Any) -> SuiteDriver:
    client = RuntimeApiClient("http://cert.test", "k", transport=httpx.MockTransport(fake.handler))
    defaults: dict[str, Any] = {
        "poll_interval_s": 0.0,
        "job_timeout_s": 5.0,
        "pause_timeout_s": 5.0,
        "fact_timeout_s": 5.0,
        "side_effects_enabled": False,
        "sleep": lambda _s: None,
    }
    defaults.update(kwargs)
    return SuiteDriver(client, **defaults)


def _timeout_case(*steps: HitlStep, timeout_s: float | None = None) -> CaseSpec:
    return CaseSpec(
        name="t",
        input={"request": "x"},
        hitl=steps,
        expect=CaseExpect(status="success", route=None),
        timeout_s=timeout_s,
    )


def _timeout_spec(case: CaseSpec) -> ScenarioSpec:
    return ScenarioSpec(scenario="approval-timeout", target="approval-timeout", cases=(case,))


@pytest.mark.unit
def test_driver_wait_timeout_observes_first_gate_then_signals_second() -> None:
    """The wait_timeout step must observe primary WITHOUT signalling; the
    next step's poll (excluding primary) waits out the 'timer' until the
    escalation pause shows, which IS signalled. Exactly one signal total."""
    fake = ScriptedTimeoutRuntime(
        # primary pause lingers (stale listings), then the timer 'fires' and
        # the escalation pause appears.
        pause_script=["primary-approval", "primary-approval", "escalation-approval"],
        signals_required=1,
    )
    case = _timeout_case(
        HitlStep(node="primary-approval", decision=None, wait_timeout=True),
        HitlStep(node="escalation-approval", decision={"decision": "approve"}),
    )
    result = _timeout_driver(fake).run_case(_timeout_spec(case), case)

    assert fake.signals == [{"decision": {"decision": "approve"}}]
    assert result.outcomes["hitl"].status == "pass"
    assert "no signal, awaiting the durable timeout" in result.outcomes["hitl"].note
    assert result.outcomes["durable-execution"].status == "pass"


@pytest.mark.unit
def test_driver_both_wait_timeouts_never_signal() -> None:
    """Total silence: both pauses observed, ZERO signals sent, and the
    terminal fact (the escalation expiry's rejected tail) still certifies."""
    fake = ScriptedTimeoutRuntime(
        pause_script=["primary-approval", "escalation-approval"],
        signals_required=0,
    )
    case = _timeout_case(
        HitlStep(node="primary-approval", decision=None, wait_timeout=True),
        HitlStep(node="escalation-approval", decision=None, wait_timeout=True),
    )
    result = _timeout_driver(fake).run_case(_timeout_spec(case), case)

    assert fake.signals == []
    assert result.outcomes["hitl"].status == "pass"
    assert result.outcomes["durable-execution"].status == "pass"


@pytest.mark.unit
def test_driver_per_case_timeout_overrides_suite_defaults() -> None:
    """A case's timeout_s must stretch the pause + fact polls past suite
    defaults that would otherwise expire (the durable waits are real time)."""
    # Suite defaults of ~0s would time out on the first empty listing; the
    # case override keeps polling until the pause/fact shows.
    fake = ScriptedTimeoutRuntime(
        pause_script=[None, None, "primary-approval"],
        signals_required=1,
    )
    case = _timeout_case(
        HitlStep(node="primary-approval", decision={"decision": "approve"}),
        timeout_s=30.0,
    )
    driver = _timeout_driver(fake, pause_timeout_s=0.0, fact_timeout_s=0.0)
    result = driver.run_case(_timeout_spec(case), case)
    assert result.outcomes["hitl"].status == "pass"
    assert result.outcomes["durable-execution"].status == "pass"


# ---------------------------------------------------------------------------
# 5. Skill impls — tmp-SQLite ledger record + read-back
# ---------------------------------------------------------------------------

SKILLS = {
    "sim-create-po": {
        "dirs": [
            WORKFLOWS / "purchase-order" / "skills" / "sim-create-po",
            SCENARIOS / "purchase-order" / "skills" / "sim-create-po",
            TEMPLATES / "pattern_purchase_order" / "skills" / "sim-create-po",
        ],
        "system": "erp",
        "action": "create_po",
    },
    "sim-fulfill": {
        "dirs": [
            WORKFLOWS / "approval-timeout" / "skills" / "sim-fulfill",
            SCENARIOS / "approval-timeout" / "skills" / "sim-fulfill",
            TEMPLATES / "pattern_approval_timeout" / "skills" / "sim-fulfill",
        ],
        "system": "fulfillment",
        "action": "fulfill",
    },
}


def _load_impl(skill: str) -> ModuleType:
    """Import the deployable copy's impl.py under a unique module name (three
    byte-identical copies exist; a plain ``<skill>.impl`` import would be
    ambiguous in-repo)."""
    path = SKILLS[skill]["dirs"][0] / "impl.py"  # type: ignore[index]
    module_name = f"b1_{skill.replace('-', '_')}_impl"
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
def test_create_po_impl_records_erp_row_and_reads_back(sim_db: Path) -> None:
    impl = _load_impl("sim-create-po")
    ctx = SimpleNamespace(run_id="wfr-po-1", mock=False)

    out = impl.run({"item": "gpu-workstation", "requester": "ana.silva", "amount": 12500}, ctx)

    assert set(out) == {"po_result"}
    assert "gpu-workstation" in out["po_result"]
    assert "ana.silva" in out["po_result"]
    assert "PO-" in out["po_result"]
    assert _rows(sim_db) == [
        (
            "wfr-po-1",
            "erp",
            "create_po",
            {"item": "gpu-workstation", "requester": "ana.silva", "amount": 12500},
        )
    ]


@pytest.mark.unit
def test_fulfill_impl_records_fulfillment_row_and_reads_back(sim_db: Path) -> None:
    impl = _load_impl("sim-fulfill")
    ctx = SimpleNamespace(run_id="wfr-ful-1", mock=False)

    out = impl.run({"request": "vpn-exception", "requester": "jane.doe"}, ctx)

    assert set(out) == {"fulfill_result"}
    assert "vpn-exception" in out["fulfill_result"]
    assert "FUL-" in out["fulfill_result"]
    assert _rows(sim_db) == [
        (
            "wfr-ful-1",
            "fulfillment",
            "fulfill",
            {"request": "vpn-exception", "requester": "jane.doe"},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_skill_impl_mock_short_circuits_without_db_write(sim_db: Path, skill: str) -> None:
    impl = _load_impl(skill)
    out = impl.run(
        {"item": "x", "request": "x", "requester": "y"},
        SimpleNamespace(run_id="wfr-mock", mock=True),
    )
    assert len(out) == 1  # the single confirmation key
    assert not sim_db.exists()  # the ledger was never touched


@pytest.mark.unit
@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_skill_impl_run_id_input_overrides_ctx(sim_db: Path, skill: str) -> None:
    """An explicit run_id in the input wins (the harness facades' convention);
    ctx.run_id is the fallback the tool node actually exercises."""
    impl = _load_impl(skill)
    impl.run(
        {"item": "x", "request": "x", "requester": "y", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


@pytest.mark.unit
@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_skill_impl_reference_is_stable_per_run(sim_db: Path, skill: str) -> None:
    """A Temporal activity retry must confirm the SAME synthetic reference."""
    impl = _load_impl(skill)
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    payload = {"item": "x", "request": "x", "requester": "y", "amount": 1}
    assert impl.run(dict(payload), ctx) == impl.run(dict(payload), ctx)


# ---------------------------------------------------------------------------
# 6. Anti-drift — each skill ships byte-identical across its three copies
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("skill", sorted(SKILLS))
@pytest.mark.parametrize(
    "rel", ["skill.yaml", "impl.py", "schema/input.json", "schema/output.json"]
)
def test_skill_files_identical_across_copies(skill: str, rel: str) -> None:
    deployable, scenario, template = SKILLS[skill]["dirs"]  # type: ignore[misc]
    canonical = (deployable / rel).read_bytes()
    assert (scenario / rel).read_bytes() == canonical, f"{skill}: scenario copy of {rel} drifted"
    assert (template / rel).read_bytes() == canonical, f"{skill}: template copy of {rel} drifted"
