"""Governed agent-pattern templates (ADR 038) — template-level tests.

Covers the FIVE patterns scaffoldable via ``mdk init --pattern <name>``:
chatbot, task-oriented, goal-oriented, monitor, simulation.

For each pattern:
* the registry metadata is well-formed (dir exists, one-liner, topology);
* it scaffolds cleanly (single-agent → agent dir; workflow → bundle);
* the WORKFLOW patterns compose via ``load_workflow_spec`` + ``compile_workflow``
  AND pass the v0.3 phase gate ``validate_linear`` (so ``mdk run``/``eval`` on
  them work today);
* the single-agent chatbot loads via ``load_agent``;
* the GOVERNANCE bounds are present in the scaffolded config (budgets +
  fan-out cap / max-iterations / turn cap, as applicable).

These assert on the SHIPPED template files (the source of truth) plus a
scaffold-to-tempdir round trip for the name-substitution paths.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from movate.core.loader import load_agent
from movate.core.workflow.compiler import compile_workflow, validate_graph
from movate.core.workflow.spec import load_workflow_spec
from movate.templates import (
    PATTERN_TEMPLATES,
    get_pattern_path,
    list_patterns,
    pattern_is_workflow,
)

ALL_PATTERNS = [
    "chatbot",
    "task-oriented",
    "goal-oriented",
    "monitor",
    "simulation",
    "expense-approval",
    "itsm-request",
    "purchase-order",
    "approval-timeout",
    "human-escalation",
    "pii-detection",
    "data-privacy",
    "content-publishing",
    "multi-agent-investigation",
    "multi-agent-business-process",
    "external-api-failure",
    "partial-failure-recovery",
    "long-running-research",
    "agent-deploy-approval",
    "agent-benchmark",
    "continuous-eval",
    "promotion-pipeline",
    "ab-testing",
    "employee-onboarding",
    "incident-response",
    "cross-system-action",
    "executive-briefing",
    "ops-center",
]
WORKFLOW_PATTERNS = ["task-oriented", "goal-oriented", "monitor", "simulation"]


# ---------------------------------------------------------------------------
# Registry metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_patterns_registered() -> None:
    assert set(list_patterns()) == set(ALL_PATTERNS)
    assert len(PATTERN_TEMPLATES) == len(ALL_PATTERNS)


@pytest.mark.unit
@pytest.mark.parametrize("name", ALL_PATTERNS)
def test_pattern_metadata_well_formed(name: str) -> None:
    _rel, is_workflow, one_liner, topology = PATTERN_TEMPLATES[name]
    assert get_pattern_path(name).is_dir()
    assert isinstance(is_workflow, bool)
    assert one_liner and len(one_liner) > 10
    assert topology and ("→" in topology or "->" in topology)
    # chatbot is the only single-agent pattern.
    assert pattern_is_workflow(name) is (name != "chatbot")


@pytest.mark.unit
def test_get_pattern_path_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown pattern"):
        get_pattern_path("does-not-exist")


# ---------------------------------------------------------------------------
# chatbot — single agent loads + carries its budget bound
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chatbot_scaffolds_and_loads(tmp_path: Path) -> None:
    dest = tmp_path / "mychat"
    shutil.copytree(get_pattern_path("chatbot"), dest)
    yaml_path = dest / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", "mychat"))

    bundle = load_agent(dest)
    assert bundle.spec.name == "mychat"
    # GOVERNANCE: hard per-run budget cap baked in.
    assert bundle.spec.budget.max_cost_usd_per_run is not None
    assert bundle.spec.budget.max_cost_usd_per_run > 0
    # GOVERNANCE: token bound on the model params.
    assert bundle.spec.model.params.get("max_tokens")
    # GOVERNANCE: an objective (eval-gate threshold) is declared.
    assert bundle.spec.objectives
    assert all(0.0 < o.threshold <= 1.0 for o in bundle.spec.objectives)
    # Output contract enforced (schema present + reply field).
    assert "reply" in bundle.output_schema.get("properties", {})


# ---------------------------------------------------------------------------
# workflow patterns — compose + pass the v0.3 phase gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", WORKFLOW_PATTERNS)
def test_workflow_pattern_compiles_and_validates(name: str) -> None:
    """Each workflow pattern loads, compiles, and passes the phase gate —
    i.e. it composes via the governed primitives. ``validate_graph`` routes the
    linear patterns to ``validate_linear`` and the fan-out diamond
    (task-oriented, ADR 092) to ``validate_dag``."""
    spec, wf_dir = load_workflow_spec(get_pattern_path(name))
    graph = compile_workflow(spec, wf_dir)
    validate_graph(graph)  # must not raise
    assert graph.entrypoint in graph.nodes
    assert len(graph.nodes) >= 3  # multi-node bundle


@pytest.mark.unit
@pytest.mark.parametrize("name", WORKFLOW_PATTERNS)
def test_workflow_pattern_has_evals_stanza(name: str) -> None:
    """The workflow-level eval-gate is wired (dataset + gate)."""
    spec, wf_dir = load_workflow_spec(get_pattern_path(name))
    assert spec.evals is not None
    assert spec.evals.gate is not None
    dataset = (wf_dir / spec.evals.dataset).resolve()
    assert dataset.is_file()
    assert dataset.read_text().strip()  # non-empty dataset


@pytest.mark.unit
@pytest.mark.parametrize("name", WORKFLOW_PATTERNS)
def test_workflow_pattern_nodes_carry_budget_caps(name: str) -> None:
    """Every constituent agent in a workflow bundle carries a per-run budget
    cap — the per-branch bound whose sum is the workflow's ceiling."""
    spec, wf_dir = load_workflow_spec(get_pattern_path(name))
    agent_nodes = [n for n in spec.nodes if n.type == "agent"]
    assert agent_nodes, f"{name}: expected at least one agent node"
    seen_refs: set[Path] = set()
    for node in agent_nodes:
        ref = (wf_dir / node.ref).resolve()
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        bundle = load_agent(ref)
        cap = bundle.spec.budget.max_cost_usd_per_run
        assert cap is not None and cap > 0, f"{name}/{node.id}: missing budget cap"


# ---------------------------------------------------------------------------
# Pattern-specific governance bounds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_oriented_fan_out_is_capped() -> None:
    """Bounded fan-out: exactly TWO task branches (the fan-out cap is
    structural — wired in workflow.yaml, not a runtime suggestion)."""
    spec, _wf_dir = load_workflow_spec(get_pattern_path("task-oriented"))
    node_ids = {n.id for n in spec.nodes}
    assert "supervisor" in node_ids
    task_nodes = {nid for nid in node_ids if nid.startswith("task-")}
    assert task_nodes == {"task-a", "task-b"}, "fan-out must be capped at two tasks"
    assert "collector" in node_ids


@pytest.mark.unit
def test_goal_oriented_loop_is_bounded_and_acyclic() -> None:
    """max-iterations cap: the loop is unrolled to a FIXED two worker stages,
    gated by a JUDGE/GATE, and the compiled graph is acyclic (no runaway)."""
    spec, wf_dir = load_workflow_spec(get_pattern_path("goal-oriented"))
    graph = compile_workflow(spec, wf_dir)
    worker_nodes = {n.id for n in spec.nodes if n.id.startswith("worker-")}
    gate_nodes = {n.id for n in spec.nodes if n.type == "intent-router"}
    assert worker_nodes == {"worker-1", "worker-2"}, "max_iterations cap must be 2"
    assert gate_nodes == {"goal-gate-1", "goal-gate-2"}, "a JUDGE/GATE per iteration"
    assert not graph.has_cycle(), "the bounded loop must compile to an acyclic graph"


@pytest.mark.unit
def test_monitor_has_action_allowlist_and_gate() -> None:
    """The action node is reachable only via the VALIDATE/GATE, and ships an
    action allowlist (the governance boundary for the side-effecting step)."""
    pattern_dir = get_pattern_path("monitor")
    spec, _wf_dir = load_workflow_spec(pattern_dir)
    gate_nodes = [n for n in spec.nodes if n.type == "intent-router"]
    assert gate_nodes, "monitor must have a VALIDATE/GATE node"
    # The only edge into `action` is from the gate (a router route target).
    router = gate_nodes[0]
    assert "action" in {*router.routes.values(), router.fallback}
    # Allowlist file ships with the action node.
    assert (pattern_dir / "agents" / "action" / "ALLOWLIST.md").is_file()
    # A sample schedule/trigger config ships (schedule/trigger-friendly).
    assert (pattern_dir / "schedule.yaml.example").is_file()


@pytest.mark.unit
def test_simulation_is_bounded_not_a_swarm() -> None:
    """Simulation is bounded-ONLY: FIXED roster of two participants, a HARD
    turn cap (two unrolled turns), a terminating JUDGE, per-node budget caps,
    and an acyclic graph (no runaway). Documents the bound inline."""
    pattern_dir = get_pattern_path("simulation")
    spec, wf_dir = load_workflow_spec(pattern_dir)
    graph = compile_workflow(spec, wf_dir)

    # FIXED ROSTER: exactly two distinct participant agents.
    participant_refs = {
        (wf_dir / n.ref).resolve().name
        for n in spec.nodes
        if n.type == "agent" and "participant" in n.ref
    }
    assert participant_refs == {"participant-a", "participant-b"}, "fixed two-participant roster"

    # HARD TURN CAP: two turns' worth of participant nodes (a+b per turn).
    turn_a_nodes = {n.id for n in spec.nodes if n.id.startswith("turn-") and n.id.endswith("-a")}
    assert turn_a_nodes == {"turn-1-a", "turn-2-a"}, "turn cap must be 2"

    # TERMINATING JUDGE: an intent-router gate per turn.
    judge_nodes = {n.id for n in spec.nodes if n.type == "intent-router"}
    assert judge_nodes == {"turn-gate-1", "turn-gate-2"}

    # Acyclic — a runaway simulation is impossible by construction.
    assert not graph.has_cycle()

    # The bounded-vs-swarm rationale is documented inline.
    governance = (pattern_dir / "GOVERNANCE.md").read_text().lower()
    assert "swarm" in governance
    assert "fixed roster" in governance
    assert "turn cap" in governance


# ---------------------------------------------------------------------------
# Every pattern ships a GOVERNANCE.md
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", ALL_PATTERNS)
def test_pattern_ships_governance_doc(name: str) -> None:
    gov = get_pattern_path(name) / "GOVERNANCE.md"
    assert gov.is_file()
    text = gov.read_text().lower()
    assert "governance" in text
    assert "budget" in text


# ---------------------------------------------------------------------------
# Every pattern ships a judge (the eval-gate scorer)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", ALL_PATTERNS)
def test_pattern_ships_judge_example(name: str) -> None:
    judge = get_pattern_path(name) / "evals" / "judge.yaml.example"
    assert judge.is_file()
    data = yaml.safe_load(judge.read_text())
    assert data.get("method")
    assert data.get("threshold") is not None
