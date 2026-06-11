"""B8 reporting certification batch — structural + skill tests.

Covers the two scenarios of the B8 batch (program #12 / #27):

* ``executive-briefing`` — TOOL gather-metrics → TOOL gather-incidents →
  digest AGENT → DECISION(risk_count) → {flag | archive}; CRON-BORN per
  ADR 100 (the schedule binding is documentation — nothing here creates one);
* ``ops-center`` — TOOL fetch-facts → summarize AGENT →
  DECISION(failure_count) → {HUMAN page (ack→report, fallback report) |
  report}.

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

1. graph shape per scenario x copy — node types, the sequential gather chain
   (entrypoint fan-out is not available for TOOL nodes), the ADR 098
   exclusive convergence on the ops-center report, the ADR 099 fail-open
   routes/fallback on the page gate;
2. Temporal compilation — the exact activity set per scenario and the inline
   (activity-free) ``evaluate_decision`` routing — plus native/Temporal
   routing PARITY via ``evaluate_decision`` itself on both decision nodes;
3. ``cases.yaml`` — parses through the driver's own loader
   (:func:`certification.harness.driver.load_scenario_spec`) with both
   routes covered per scenario and the right per-path ledger expectations;
4. the sim skills — canned-data determinism, the ``profile`` knob, the
   observability_facts row shape mirroring
   :class:`movate.core.models.ObservabilityFact` column-for-column, the
   no-network-IO endpoint echo, record + read-back on a tmp SQLite
   (``MOVATE_DB``), ``ctx.mock`` short-circuit, run_id precedence;
5. prompt guards — the ops-center report prompt MUST guard the
   path-exclusive ``decision`` key (the Jinja StrictUndefined rule);
6. pattern registration — both patterns appended to ``PATTERN_TEMPLATES``
   and resolvable;
7. anti-drift — skill AND agent files (and state.json) byte-identical across
   the three copies of each scenario.
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

from movate.core.models import ObservabilityFact
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
B8 = {
    "executive-briefing": ("briefing", "pattern_executive_briefing"),
    "ops-center": ("ops", "pattern_ops_center"),
}


def _workflow_dirs(scenario: str) -> dict[str, Path]:
    """The three compile sources for one scenario (id → workflow dir)."""
    subdir, template = B8[scenario]
    return {
        "deployable": DEPLOYABLES / scenario,
        "scenario": SCENARIOS / scenario / "workflows" / subdir,
        "template": TEMPLATES / template,
    }


#: (scenario, copy) pairs — the parametrization grid for the shape tests.
GRID = [(s, which) for s in sorted(B8) for which in ("deployable", "scenario", "template")]


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


def _edges_into(graph: WorkflowGraph, node_id: str) -> set[tuple[str, str | None]]:
    return {(e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == node_id}


# ---------------------------------------------------------------------------
# 1. Graph shape — node types, sequential chain, convergence, gate routes
# ---------------------------------------------------------------------------

_EXPECTED_NODE_TYPES: dict[str, dict[str, NodeType]] = {
    "executive-briefing": {
        "gather-metrics": NodeType.TOOL,
        "gather-incidents": NodeType.TOOL,
        "digest": NodeType.AGENT,
        "risk-gate": NodeType.DECISION,
        "flag": NodeType.AGENT,
        "archive": NodeType.AGENT,
    },
    "ops-center": {
        "fetch-facts": NodeType.TOOL,
        "summarize": NodeType.AGENT,
        "gate": NodeType.DECISION,
        "page": NodeType.HUMAN,
        "report": NodeType.AGENT,
    },
}

_EXPECTED_ENTRYPOINTS = {
    "executive-briefing": "gather-metrics",
    "ops-center": "fetch-facts",
}


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "which"), GRID)
def test_graph_shape_and_node_types(scenario: str, which: str) -> None:
    graph = _graph(_workflow_dirs(scenario)[which])
    validate_graph(graph)  # the phase gate must admit the routed shape

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
def test_briefing_sequential_gather_chain_and_exclusive_tails(which: str) -> None:
    """The gather chain is strictly SEQUENTIAL (entrypoint fan-out is not
    available for TOOL nodes), and each terminal tail is reachable only via
    its decision leg (exclusive routing, ADR 098)."""
    graph = _graph(_workflow_dirs("executive-briefing")[which])
    assert _edges_into(graph, "gather-incidents") == {("gather-metrics", None)}
    assert _edges_into(graph, "digest") == {("gather-incidents", None)}
    assert _edges_into(graph, "risk-gate") == {("digest", None)}
    assert _edges_into(graph, "flag") == {("risk-gate", "decision")}
    assert _edges_into(graph, "archive") == {("risk-gate", "decision")}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_ops_center_paths_converge_on_report(which: str) -> None:
    """ADR 098: the paged path (via the human gate, BOTH its ack route and
    its fallback) and the clean decision leg all land on the ONE report
    agent — and the page is reachable only via its decision leg."""
    graph = _graph(_workflow_dirs("ops-center")[which])
    assert _edges_into(graph, "summarize") == {("fetch-facts", None)}
    assert _edges_into(graph, "page") == {("gate", "decision")}
    assert _edges_into(graph, "report") == {("gate", "decision"), ("page", "human-route")}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_ops_center_page_gate_routes_fail_open(which: str) -> None:
    """ADR 099: the page gate routes its own decision and is FAIL-OPEN by
    design — ack and the fallback both target report (a page can delay the
    daily report, never kill it)."""
    graph = _graph(_workflow_dirs("ops-center")[which])
    gate = graph.nodes["page"]
    assert gate.metadata["routes"] == {"ack": "report"}
    assert gate.metadata["fallback"] == "report"
    assert gate.metadata["route_on"] == "decision"


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_briefing_digest_sees_only_the_gathered_results(which: str) -> None:
    """Grounding is STRUCTURAL: the digest's input schema (the state
    projection the activity applies) admits only the period + the two
    gathered results — the sim `profile` knob never reaches the LLM."""
    graph = _graph(_workflow_dirs("executive-briefing")[which])
    schema_path = Path(graph.nodes["digest"].ref) / "schema" / "input.json"
    props = json.loads(schema_path.read_text())["properties"]
    assert set(props) == {"period", "metrics", "incidents"}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_ops_center_summarize_sees_only_the_fetched_rows(which: str) -> None:
    graph = _graph(_workflow_dirs("ops-center")[which])
    schema_path = Path(graph.nodes["summarize"].ref) / "schema" / "input.json"
    props = json.loads(schema_path.read_text())["properties"]
    assert set(props) == {"window", "facts", "facts_source"}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_ops_center_report_prompt_guards_path_exclusive_decision(which: str) -> None:
    """`decision` exists ONLY on the paged path; the shared report prompt
    must read it through `| default(...)` (StrictUndefined would otherwise
    wedge every clean-window run — the B2 live failure mode)."""
    graph = _graph(_workflow_dirs("ops-center")[which])
    prompt = (Path(graph.nodes["report"].ref) / "prompt.md").read_text()
    assert "input.decision" in prompt
    assert 'input.decision | default("n/a")' in prompt


# ---------------------------------------------------------------------------
# 2. Temporal compilation + decision-routing parity (ADR 094 D3)
# ---------------------------------------------------------------------------

_EXPECTED_ACTIVITIES = {
    # agents + TOOL dispatch + terminal persist; no gate-classifier activity
    # (ADR 094) and no human activity (no gate in the briefing).
    "executive-briefing": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    # + the durable HUMAN pause for the page.
    "ops-center": {
        "call_agent_activity",
        "call_human_activity",
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
    # Decision nodes route inline via the shared helper — no activity, no LLM.
    assert "evaluate_decision(" in src
    assert set(result.activity_names) == _EXPECTED_ACTIVITIES[scenario]


_BRIEFING_CASES = [{"when": {"field": "risk_count", "op": "gt", "value": 0}, "to": "flag"}]
_OPS_CASES = [{"when": {"field": "failure_count", "op": "gt", "value": 0}, "to": "page"}]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"risk_count": 4}, "flag"),
        ({"risk_count": 1}, "flag"),
        ({"risk_count": "2"}, "flag"),  # numeric coercion (ADR 094 D3)
        ({"risk_count": 0}, "archive"),
        ({"risk_count": -1}, "archive"),
        ({}, "archive"),  # missing field is a non-match, never an error
    ],
)
def test_briefing_risk_gate_routing_parity(state: dict[str, Any], expected: str) -> None:
    """The exact rule both backends funnel through (``evaluate_decision``)
    routes the briefing's risk gate as the workflow.yaml declares."""
    assert evaluate_decision(_BRIEFING_CASES, "archive", state) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"failure_count": 2}, "page"),
        ({"failure_count": "1"}, "page"),
        ({"failure_count": 0}, "report"),
        ({}, "report"),
    ],
)
def test_ops_center_gate_routing_parity(state: dict[str, Any], expected: str) -> None:
    assert evaluate_decision(_OPS_CASES, "report", state) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ack", "report"),
        ("  ACK  ", "report"),  # trim + casefold (ADR 099 D2)
        ("acknowledged", "report"),  # out-of-vocabulary → fallback (fail-open)
        (None, "report"),
    ],
)
def test_ops_center_page_route_is_fail_open(value: Any, expected: str) -> None:
    """Every wording of the on-call's answer lands on report — by routes for
    `ack`, by fallback for everything else (the gate can delay the daily
    report, never kill it)."""
    assert evaluate_human_route({"ack": "report"}, "report", value) == expected


# ---------------------------------------------------------------------------
# 3. cases.yaml — parses through the DRIVER's loader, both routes covered
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_briefing_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "executive-briefing" / "cases.yaml")
    assert spec.scenario == "executive-briefing"
    assert spec.target == "executive-briefing"
    assert [c.name for c in spec.cases] == ["degraded-period-escalated", "steady-period-archived"]
    degraded, steady = spec.cases

    # No human gate anywhere — no case declares hitl.
    assert all(c.hitl == () for c in spec.cases)
    # Governance asserted (expected allow) + terminal success on both routes.
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)
    # The profile knob drives the route deterministically.
    assert degraded.input["profile"] == "degraded"
    assert steady.input["profile"] == "steady"

    # BOTH paths run the unconditional gather chain: one metrics + one
    # incidents ledger row each.
    for case in spec.cases:
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("metrics", "gather", 1),
            ("incidents", "gather", 1),
        ]
    # Route proof via exclusive state markers (the fact's route is None).
    assert "escalation" in degraded.expect.final_state_has
    assert "archive_note" in degraded.expect.final_state_lacks
    assert "archive_note" in steady.expect.final_state_has
    assert "escalation" in steady.expect.final_state_lacks


@pytest.mark.unit
def test_ops_center_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "ops-center" / "cases.yaml")
    assert spec.scenario == "ops-center"
    assert spec.target == "ops-center"
    assert [c.name for c in spec.cases] == ["degraded-facts-paged", "steady-facts-direct"]
    paged, direct = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)

    # The paged route: pause at `page`, signal ack, decision lands in state.
    assert [(s.node, s.decision) for s in paged.hitl] == [("page", {"decision": "ack"})]
    assert "decision" in paged.expect.final_state_has
    assert "daily_report" in paged.expect.final_state_has

    # The clean route: NO hitl declared (a pause would starve the fact poll),
    # no decision key, and the explicit facts_endpoint input must be echoed
    # verbatim in facts_source (the sim's no-network-IO contract).
    assert direct.hitl == ()
    assert "decision" in direct.expect.final_state_lacks
    assert "daily_report" in direct.expect.final_state_has
    endpoint = direct.input["facts_endpoint"]
    assert endpoint.endswith("/api/v1/observability/facts")
    assert dict(direct.expect.final_state_contains)["facts_source"] == (endpoint,)

    # BOTH paths run the unconditional facts pull: exactly one ledger row.
    for case in spec.cases:
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("observability", "fetch_facts", 1)
        ]


# ---------------------------------------------------------------------------
# 4. Sim skills — canned-data determinism, fact shape, tmp-SQLite ledger
# ---------------------------------------------------------------------------


def _load_impl(scenario: str, skill: str) -> ModuleType:
    """Import a deployable copy's impl.py under a unique module name (the
    copies are byte-identical; a plain namespace import would be ambiguous
    in-repo)."""
    path = DEPLOYABLES / scenario / "skills" / skill / "impl.py"
    module_name = f"b8_{scenario}_{skill}_impl".replace("-", "_")
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
def test_sim_gather_metrics_profiles_and_ledger(sim_db: Path) -> None:
    impl = _load_impl("executive-briefing", "sim-gather-metrics")
    degraded = impl.run(
        {"period": "daily", "profile": "degraded"},
        SimpleNamespace(run_id="wfr-brief-1", mock=False),
    )
    # The degraded snapshot must breach BOTH digest risk rules.
    assert degraded["metrics"]["success_rate"] < 0.95
    assert degraded["metrics"]["cost_usd"] > degraded["metrics"]["budget_usd"]

    steady = impl.run({"period": "daily"}, SimpleNamespace(run_id="wfr-brief-2", mock=False))
    # Missing profile → steady, which must trip NEITHER rule.
    assert steady["metrics"]["success_rate"] >= 0.95
    assert steady["metrics"]["cost_usd"] <= steady["metrics"]["budget_usd"]

    assert _rows(sim_db) == [
        ("wfr-brief-1", "metrics", "gather", {"period": "daily", "profile": "degraded"}),
        ("wfr-brief-2", "metrics", "gather", {"period": "daily", "profile": "steady"}),
    ]


@pytest.mark.unit
def test_sim_gather_incidents_profiles_and_ledger(sim_db: Path) -> None:
    impl = _load_impl("executive-briefing", "sim-gather-incidents")
    degraded = impl.run(
        {"period": "daily", "profile": "degraded"},
        SimpleNamespace(run_id="wfr-brief-3", mock=False),
    )
    assert [i["id"] for i in degraded["incidents"]] == ["INC-4102", "INC-4105"]
    assert all(i["status"] == "open" for i in degraded["incidents"])

    steady = impl.run({"period": "daily"}, SimpleNamespace(run_id="wfr-brief-4", mock=False))
    assert steady["incidents"] == []  # steady is EMPTY: zero digest risk flags

    assert _rows(sim_db) == [
        (
            "wfr-brief-3",
            "incidents",
            "gather",
            {"period": "daily", "profile": "degraded", "open_count": 2},
        ),
        (
            "wfr-brief-4",
            "incidents",
            "gather",
            {"period": "daily", "profile": "steady", "open_count": 0},
        ),
    ]


@pytest.mark.unit
def test_sim_fetch_facts_profiles_echo_and_ledger(sim_db: Path) -> None:
    impl = _load_impl("ops-center", "sim-fetch-facts")
    degraded = impl.run(
        {"window": "24h", "profile": "degraded"},
        SimpleNamespace(run_id="wfr-ops-1", mock=False),
    )
    # No explicit endpoint → the canonical reporting surface is echoed.
    assert degraded["facts_source"] == "/api/v1/observability/facts"
    # Exactly two non-success rows (what summarize must count) + a warn.
    failures = [r for r in degraded["facts"] if r["status"] != "success"]
    assert {(r["kind"], r["source_id"]) for r in failures} == {
        ("workflow_run", "wfr-cert-3003"),
        ("run", "run-cert-7103"),
    }
    assert [r["governance_effect"] for r in degraded["facts"]].count("warn") == 1

    endpoint = "https://ops.example.com/api/v1/observability/facts"
    steady = impl.run(
        {"window": "24h", "profile": "steady", "facts_endpoint": endpoint},
        SimpleNamespace(run_id="wfr-ops-2", mock=False),
    )
    # The explicit endpoint is echoed verbatim — the sim NEVER queries it.
    assert steady["facts_source"] == endpoint
    assert all(r["status"] == "success" for r in steady["facts"])
    # degraded = steady rows + the two failure rows (a superset, same order).
    assert degraded["facts"][: len(steady["facts"])] == steady["facts"]
    assert len(degraded["facts"]) == len(steady["facts"]) + 2

    assert _rows(sim_db) == [
        (
            "wfr-ops-1",
            "observability",
            "fetch_facts",
            {
                "window": "24h",
                "profile": "degraded",
                "endpoint": "/api/v1/observability/facts",
                "rows": 6,
            },
        ),
        (
            "wfr-ops-2",
            "observability",
            "fetch_facts",
            {"window": "24h", "profile": "steady", "endpoint": endpoint, "rows": 4},
        ),
    ]


@pytest.mark.unit
def test_sim_fetch_facts_rows_mirror_the_real_fact_shape(sim_db: Path) -> None:
    """Every canned row must parse as a real :class:`ObservabilityFact` (the
    ADR 096 surface GET /api/v1/observability/facts serves) — keys are a
    subset of the model's fields AND the values satisfy its validators, so
    the sim can never drift from what a live endpoint pull would return."""
    impl = _load_impl("ops-center", "sim-fetch-facts")
    out = impl.run({"profile": "degraded"}, SimpleNamespace(run_id="wfr-ops-3", mock=False))
    model_fields = set(ObservabilityFact.model_fields)
    for row in out["facts"]:
        assert set(row) <= model_fields, f"unknown fact column(s): {set(row) - model_fields}"
        fact = ObservabilityFact(**row)  # extra='forbid' + Literal kind enforce shape
        assert fact.fact_id == f"{fact.kind}:{fact.source_id}"  # the ADR 096 D4 key rule


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "skill", "payload"),
    [
        ("executive-briefing", "sim-gather-metrics", {"period": "daily", "profile": "degraded"}),
        ("executive-briefing", "sim-gather-incidents", {"period": "daily"}),
        ("ops-center", "sim-fetch-facts", {"window": "24h", "profile": "steady"}),
    ],
)
def test_sim_skills_mock_short_circuits_without_db_write(
    sim_db: Path, scenario: str, skill: str, payload: dict[str, Any]
) -> None:
    impl = _load_impl(scenario, skill)
    mocked = impl.run(payload, SimpleNamespace(run_id="wfr-mock", mock=True))
    assert mocked  # the canned result still satisfies the output contract
    assert not sim_db.exists()  # the ledger was never touched
    # The mock returns the SAME canned data as a live call (determinism).
    live = impl.run(payload, SimpleNamespace(run_id="wfr-live", mock=False))
    assert mocked == live


@pytest.mark.unit
def test_sim_skill_run_id_input_overrides_ctx(sim_db: Path) -> None:
    """An explicit run_id in the input wins (the harness facades' convention);
    ctx.run_id is the fallback the tool node actually exercises."""
    impl = _load_impl("ops-center", "sim-fetch-facts")
    impl.run(
        {"profile": "steady", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "skill"),
    [
        ("executive-briefing", "sim-gather-metrics"),
        ("executive-briefing", "sim-gather-incidents"),
        ("ops-center", "sim-fetch-facts"),
    ],
)
def test_sim_skills_unknown_profile_fails_loud(sim_db: Path, scenario: str, skill: str) -> None:
    """The enum lives in the input schema (dispatch_skill enforces it); the
    impl's KeyError is the fail-loud backstop — no silent default bucket."""
    impl = _load_impl(scenario, skill)
    with pytest.raises(KeyError):
        impl.run({"profile": "chaotic"}, SimpleNamespace(run_id="wfr-x", mock=False))
    assert not sim_db.exists()  # nothing was recorded


@pytest.mark.unit
def test_sim_skills_are_deterministic_across_calls(sim_db: Path) -> None:
    """A Temporal activity retry must return byte-identical data."""
    for scenario, skill, payload in (
        ("executive-briefing", "sim-gather-metrics", {"period": "daily", "profile": "degraded"}),
        ("executive-briefing", "sim-gather-incidents", {"period": "daily", "profile": "steady"}),
        ("ops-center", "sim-fetch-facts", {"window": "24h", "profile": "degraded"}),
    ):
        impl = _load_impl(scenario, skill)
        ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
        assert impl.run(dict(payload), ctx) == impl.run(dict(payload), ctx)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "skill"),
    [
        ("executive-briefing", "sim-gather-metrics"),
        ("executive-briefing", "sim-gather-incidents"),
        ("ops-center", "sim-fetch-facts"),
    ],
)
def test_sim_skill_impls_do_no_network_io(scenario: str, skill: str) -> None:
    """The documented contract: pure-stdlib, no network. Static check — no
    network-capable module is imported anywhere in the impl (asyncpg is the
    one allowed exception: the shared-ledger write path, deferred + only
    reached when MOVATE_DB_URL/MOVATE_PG_URL is configured)."""
    src = (DEPLOYABLES / scenario / "skills" / skill / "impl.py").read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"urllib", "http", "httpx", "requests", "aiohttp", "socket"}
    assert not (imported & forbidden), f"{scenario}/{skill} imports {imported & forbidden}"


# ---------------------------------------------------------------------------
# 5. Pattern registration — appended entries resolve like every other pattern
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(B8))
def test_b8_patterns_registered(name: str) -> None:
    rel, is_workflow, one_liner, topology = PATTERN_TEMPLATES[name]
    assert rel == B8[name][1]
    assert is_workflow is True
    assert pattern_is_workflow(name) is True
    assert one_liner and topology
    assert get_pattern_path(name) == TEMPLATES / B8[name][1]
    assert get_pattern_path(name).is_dir()


# ---------------------------------------------------------------------------
# 6. Anti-drift — skills AND agents ship byte-identical in all three copies
# ---------------------------------------------------------------------------

_SKILL_FILES = ("skill.yaml", "impl.py", "schema/input.json", "schema/output.json")
_AGENT_FILES = ("agent.yaml", "prompt.md", "schema/input.json", "schema/output.json")

_SCENARIO_SKILLS = {
    "executive-briefing": ("sim-gather-metrics", "sim-gather-incidents"),
    "ops-center": ("sim-fetch-facts",),
}
_SCENARIO_AGENTS = {
    "executive-briefing": ("digest", "flag", "archive"),
    "ops-center": ("summarize", "report"),
}


def _assert_identical_across_copies(scenario: str, rel: str) -> None:
    canonical = (DEPLOYABLES / scenario / rel).read_bytes()
    scenario_root = SCENARIOS / scenario
    template_root = TEMPLATES / B8[scenario][1]
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
@pytest.mark.parametrize("scenario", sorted(B8))
def test_state_schema_identical_across_copies(scenario: str) -> None:
    """All three copies share one state contract (the deployable's
    ./state.json, the scenario root's state.json, the template's)."""
    canonical = (DEPLOYABLES / scenario / "state.json").read_bytes()
    assert (SCENARIOS / scenario / "state.json").read_bytes() == canonical
    assert (TEMPLATES / B8[scenario][1] / "state.json").read_bytes() == canonical
