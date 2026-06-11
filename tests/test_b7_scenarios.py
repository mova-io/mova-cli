"""B7 RAG/KB certification batch — structural + skill tests.

Covers the two scenarios of the B7 batch (program #11 / #23):

* ``rag-debug`` — TOOL retrieve → DECISION(top_score) → {answer | diagnose};
* ``kb-refresh`` — TOOL ingest → validate AGENT → DECISION(ok) →
  {TOOL publish | HUMAN(ack) } → notify.

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

1. graph shape per scenario x copy — node types, the decision-leg exclusivity
   of each route, the ADR 098 convergence on kb-refresh's notify tail, and
   the ADR 099 routes/fallback on the escalate gate (ack AND fallback both
   land forward on notify — no retry edge exists);
2. decision-routing parity — the SAME ``evaluate_decision`` /
   ``evaluate_human_route`` helpers both backends funnel through, evaluated
   against the compiled metadata at the routing boundaries (0.5 answers,
   0.4999 diagnoses; ok=true publishes, anything else escalates; any gate
   wording acknowledges);
3. Temporal compilation — the exact activity set per scenario and the inline
   (activity-free) ``evaluate_decision`` routing;
4. ``cases.yaml`` — parses through the driver's own loader
   (:func:`certification.harness.driver.load_scenario_spec`) with the right
   per-path ledger + state-marker expectations;
5. the ``sim-retrieve`` scoring — THE load-bearing piece of the rag-debug
   half: keyword scoring, stopword handling, tie-breaks, the top-k cap, and
   determinism, value by value;
6. the ``sim-ingest`` chunking rule — the kb-refresh half's load-bearing
   determinism: the 40-word boundary, empty-document accounting;
7. the sim ledger skills (``sim-retrieve`` / ``sim-ingest`` /
   ``sim-kb-publish``) — record + read-back on a tmp SQLite (``MOVATE_DB``),
   ``ctx.mock`` behavior, run_id precedence, stable references;
8. the StrictUndefined guard on the kb-refresh shared tail — the notify
   prompt MUST guard its path-exclusive keys (the B2 live failure mode);
9. anti-drift — skill AND agent files byte-identical across the three copies
   of each scenario;
10. pattern registration — both patterns appended to PATTERN_TEMPLATES as
    workflow bundles.
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
from movate.templates import PATTERN_TEMPLATES, get_pattern_path, list_patterns, pattern_is_workflow

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYABLES = REPO_ROOT / "workflows"
SCENARIOS = REPO_ROOT / "certification" / "scenarios"
TEMPLATES = REPO_ROOT / "src" / "movate" / "templates"

#: scenario → (scenario-mirror workflow subdir, template dir name).
B7 = {
    "rag-debug": ("rag", "pattern_rag_debug"),
    "kb-refresh": ("kb", "pattern_kb_refresh"),
}


def _workflow_dirs(scenario: str) -> dict[str, Path]:
    """The three compile sources for one scenario (id → workflow dir)."""
    subdir, template = B7[scenario]
    return {
        "deployable": DEPLOYABLES / scenario,
        "scenario": SCENARIOS / scenario / "workflows" / subdir,
        "template": TEMPLATES / template,
    }


#: (scenario, copy) pairs — the parametrization grid for the shape tests.
GRID = [(s, which) for s in sorted(B7) for which in ("deployable", "scenario", "template")]


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


def _edges_into(graph: WorkflowGraph, node_id: str) -> set[tuple[str, str | None]]:
    return {(e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == node_id}


# ---------------------------------------------------------------------------
# 1. Graph shape — node types, route exclusivity, convergence (ADR 098),
#    gate routes (ADR 099)
# ---------------------------------------------------------------------------

_EXPECTED_NODE_TYPES: dict[str, dict[str, NodeType]] = {
    "rag-debug": {
        "retrieve": NodeType.TOOL,
        "score-gate": NodeType.DECISION,
        "answer": NodeType.AGENT,
        "diagnose": NodeType.AGENT,
    },
    "kb-refresh": {
        "ingest": NodeType.TOOL,
        "validate": NodeType.AGENT,
        "quality-gate": NodeType.DECISION,
        "publish": NodeType.TOOL,
        "escalate": NodeType.HUMAN,
        "notify": NodeType.AGENT,
    },
}

_EXPECTED_ENTRYPOINTS = {
    "rag-debug": "retrieve",
    "kb-refresh": "ingest",
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
def test_rag_debug_routes_are_exclusive_and_terminal(which: str) -> None:
    """Each terminal agent is reachable ONLY via its decision leg — the
    hallucination path (a low-score retrieval reaching `answer`) is
    structurally unreachable, and there is no shared tail."""
    graph = _graph(_workflow_dirs("rag-debug")[which])
    assert _edges_into(graph, "score-gate") == {("retrieve", None)}
    assert _edges_into(graph, "answer") == {("score-gate", "decision")}
    assert _edges_into(graph, "diagnose") == {("score-gate", "decision")}
    # Both routes are terminal — nothing hangs off either agent.
    assert not [e for e in graph.edges if e.from_id in ("answer", "diagnose")]


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_kb_refresh_paths_converge_on_notify(which: str) -> None:
    """ADR 098: the published and acknowledged-failure paths converge on the
    ONE notify agent; publish is reachable ONLY via the decision's ok-leg
    (the graph, not a convention, keeps unvalidated refreshes unpublished)."""
    graph = _graph(_workflow_dirs("kb-refresh")[which])
    assert _edges_into(graph, "quality-gate") == {("validate", None)}
    assert _edges_into(graph, "publish") == {("quality-gate", "decision")}
    assert _edges_into(graph, "escalate") == {("quality-gate", "decision")}
    assert _edges_into(graph, "notify") == {("publish", None), ("escalate", "human-route")}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_kb_refresh_gate_routes_forward_only(which: str) -> None:
    """ADR 099: the escalate gate routes its own decision, ack AND the
    fallback both land FORWARD on notify (any wording acknowledges), and no
    route can re-enter ingest — the no-retry bound is structural."""
    graph = _graph(_workflow_dirs("kb-refresh")[which])
    gate = graph.nodes["escalate"]
    assert gate.metadata["routes"] == {"ack": "notify"}
    assert gate.metadata["fallback"] == "notify"
    assert gate.metadata["route_on"] == "decision"
    assert gate.metadata["output_contract"] == ["decision"]
    assert not graph.has_cycle()


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_kb_refresh_validate_judges_only_the_summary(which: str) -> None:
    """Data minimization is STRUCTURAL: the validate agent's input schema
    (the state projection the activity applies) lists ONLY ingest_result —
    the one LLM call before the gate sees counts, never the documents."""
    graph = _graph(_workflow_dirs("kb-refresh")[which])
    schema_path = Path(graph.nodes["validate"].ref) / "schema" / "input.json"
    props = json.loads(schema_path.read_text())["properties"]
    assert list(props) == ["ingest_result"]


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_kb_refresh_notify_prompt_guards_path_exclusive_keys(which: str) -> None:
    """The shared tail's prompt MUST guard the keys only one path merges
    (StrictUndefined renders an unguarded miss into a hard UndefinedError —
    the B2 live failure mode). `publish_result` exists only on the publish
    path, `decision` only on the acknowledged escalation."""
    graph = _graph(_workflow_dirs("kb-refresh")[which])
    prompt = (Path(graph.nodes["notify"].ref) / "prompt.md").read_text()
    for key in ("publish_result", "decision"):
        assert f"{{% if input.{key} is defined" in prompt, f"unguarded path-exclusive key {key!r}"


# ---------------------------------------------------------------------------
# 2. Decision-routing parity — the shared helpers against compiled metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"top_score": 1.0}, "answer"),
        ({"top_score": 0.75}, "answer"),
        ({"top_score": 0.5}, "answer"),  # gte: the threshold itself answers
        ({"top_score": 0.4999}, "diagnose"),
        ({"top_score": 0.0}, "diagnose"),
        ({}, "diagnose"),  # missing score can only fail safe to diagnosis
    ],
)
def test_rag_debug_score_gate_routing_parity(state: dict[str, Any], expected: str) -> None:
    """Native and Temporal funnel through the ONE evaluate_decision helper
    (ADR 094 D3) — evaluate the deployable's compiled metadata directly."""
    gate = _graph(_workflow_dirs("rag-debug")["deployable"]).nodes["score-gate"]
    assert evaluate_decision(gate.metadata["cases"], gate.metadata["default"], state) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"ok": True}, "publish"),
        ({"ok": False}, "escalate"),
        ({}, "escalate"),  # a missing verdict can only fail safe to escalation
    ],
)
def test_kb_refresh_quality_gate_routing_parity(state: dict[str, Any], expected: str) -> None:
    gate = _graph(_workflow_dirs("kb-refresh")["deployable"]).nodes["quality-gate"]
    assert evaluate_decision(gate.metadata["cases"], gate.metadata["default"], state) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["ack", " ACK ", "Ack", "acknowledged", "retry", "", None],
)
def test_kb_refresh_escalate_gate_every_wording_acknowledges(value: Any) -> None:
    """ADR 099: ack matches the route, EVERYTHING else (other wording, empty,
    missing) takes the fallback — and both land on notify. No input can
    publish, and no input can re-run the ingest."""
    gate = _graph(_workflow_dirs("kb-refresh")["deployable"]).nodes["escalate"]
    target = evaluate_human_route(gate.metadata["routes"], gate.metadata["fallback"], value)
    assert target == "notify"


# ---------------------------------------------------------------------------
# 3. Temporal compilation — exact activity sets, inline decision routing
# ---------------------------------------------------------------------------

_EXPECTED_ACTIVITIES = {
    # agents + TOOL dispatch + terminal persist; no gate-classifier activity
    # (ADR 094) and no human activity (no gate in rag-debug).
    "rag-debug": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    # + the durable HUMAN pause for escalate.
    "kb-refresh": {
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


# ---------------------------------------------------------------------------
# 4. cases.yaml — parses through the DRIVER's loader, per-path expectations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rag_debug_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "rag-debug" / "cases.yaml")
    assert spec.scenario == "rag-debug"
    assert spec.target == "rag-debug"
    assert [c.name for c in spec.cases] == [
        "password-reset-hit",
        "expense-report-hit",
        "nonsense-query-miss",
    ]
    hit_a, hit_b, miss = spec.cases

    # No human gate anywhere — no case declares hitl.
    assert all(c.hitl == () for c in spec.cases)
    # Governance asserted (expected allow) + terminal success on every case —
    # the low-score retrieval terminates at diagnose SUCCESSFULLY.
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)
    # EVERY case records exactly one retrieval row — the audited miss
    # included (the auditable zero-hit retrieval is the debugging story).
    for case in spec.cases:
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("vectorstore", "retrieve", 1)
        ]

    # Answer-route cases: answer keys present, diagnose keys ABSENT.
    for case in (hit_a, hit_b):
        assert {"answer", "sources"} <= set(case.expect.final_state_has)
        assert {"diagnosis", "suggested_query"} <= set(case.expect.final_state_lacks)
    # The miss: the inverse, and the retrieval evidence still in state.
    assert {"diagnosis", "suggested_query", "top_score"} <= set(miss.expect.final_state_has)
    assert {"answer", "sources"} <= set(miss.expect.final_state_lacks)


@pytest.mark.unit
def test_kb_refresh_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "kb-refresh" / "cases.yaml")
    assert spec.scenario == "kb-refresh"
    assert spec.target == "kb-refresh"
    assert [c.name for c in spec.cases] == [
        "two-docs-publish",
        "long-doc-publish",
        "empty-doc-escalate",
    ]
    pub_a, pub_b, escalated = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    # The failed refresh is a route, not an error — all three end success.
    assert all(c.expect.status == "success" for c in spec.cases)

    # Published cases: NO hitl (the gate must never be reached — a wrong
    # pause starves the fact poll), BOTH kb rows, and final_state lacks the
    # gate's decision key.
    for case in (pub_a, pub_b):
        assert case.hitl == ()
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("kb", "ingest", 1),
            ("kb", "publish", 1),
        ]
        assert {"ingest_result", "ok", "note", "publish_result", "summary"} <= set(
            case.expect.final_state_has
        )
        assert "decision" in case.expect.final_state_lacks

    # The failed refresh: pause + ack signal observed, the ingest is STILL
    # audited, and the publish row must NOT exist.
    assert [(s.node, s.decision) for s in escalated.hitl] == [("escalate", {"decision": "ack"})]
    assert [(e.system, e.action, e.times) for e in escalated.expect.side_effects] == [
        ("kb", "ingest", 1)
    ]
    assert [(e.system, e.action) for e in escalated.expect.no_side_effects] == [("kb", "publish")]
    assert {"ingest_result", "ok", "note", "decision", "summary"} <= set(
        escalated.expect.final_state_has
    )
    assert "publish_result" in escalated.expect.final_state_lacks


# ---------------------------------------------------------------------------
# 5. sim-retrieve — the load-bearing scoring, value by value
# ---------------------------------------------------------------------------


def _load_impl(scenario: str, skill: str) -> ModuleType:
    """Import a deployable copy's impl.py under a unique module name (the
    copies are byte-identical; a plain namespace import would be ambiguous
    in-repo)."""
    path = DEPLOYABLES / scenario / "skills" / skill / "impl.py"
    module_name = f"b7_{scenario}_{skill}_impl".replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def retriever() -> ModuleType:
    return _load_impl("rag-debug", "sim-retrieve")


@pytest.mark.unit
def test_retriever_full_keyword_match_scores_one(retriever: ModuleType) -> None:
    """Stopwords (how/do/i/my) drop out; reset+corporate+password all land in
    kb-001 → score 1.0, and that document ranks first."""
    docs, top = retriever.retrieve("How do I reset my corporate password?")
    assert top == 1.0
    assert docs[0]["id"] == "kb-001"
    assert docs[0]["score"] == 1.0
    assert set(docs[0]) == {"id", "title", "score", "text"}


@pytest.mark.unit
def test_retriever_partial_match_scores_fractionally(retriever: ModuleType) -> None:
    """deadline/submit/expense/report → 3 of 4 content tokens hit kb-003 →
    0.75 (above the 0.5 routing threshold: partial coverage still answers)."""
    docs, top = retriever.retrieve("What is the deadline to submit an expense report?")
    assert top == 0.75
    assert docs[0]["id"] == "kb-003"


@pytest.mark.unit
def test_retriever_tie_breaks_deterministically_by_id(retriever: ModuleType) -> None:
    """`vpn password` splits 0.5/0.5 across kb-001 and kb-002 — the tie
    breaks on the doc id, so replays rank identically."""
    docs, top = retriever.retrieve("vpn password")
    assert top == 0.5
    assert [d["id"] for d in docs] == ["kb-001", "kb-002"]


@pytest.mark.unit
def test_retriever_caps_hits_at_top_k(retriever: ModuleType) -> None:
    """`corporate portal` matches 4 documents; only the top 3 come back."""
    docs, top = retriever.retrieve("corporate portal")
    assert top == 1.0
    assert len(docs) == 3
    assert [d["id"] for d in docs] == ["kb-001", "kb-002", "kb-003"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    [
        "zebra cantaloupe spaceship maintenance",  # out-of-corpus vocabulary
        "how do you do",  # stopwords only → no content tokens
        "a I",  # single-char + stopword tokens only
        "",  # empty query
    ],
)
def test_retriever_miss_returns_empty_and_zero(retriever: ModuleType, query: str) -> None:
    assert retriever.retrieve(query) == ([], 0.0)


@pytest.mark.unit
def test_retriever_is_deterministic(retriever: ModuleType) -> None:
    """Two runs over the same query agree exactly — the Temporal replay/retry
    property the TOOL node relies on."""
    query = "How do I reset my corporate password?"
    assert retriever.retrieve(query) == retriever.retrieve(query)


@pytest.mark.unit
def test_retriever_run_contract(retriever: ModuleType, sim_db: Path) -> None:
    """run() returns exactly the documented keys and they agree with each
    other (top_score IS the best returned doc's score)."""
    out = retriever.run(
        {"query": "How do I reset my corporate password?"},
        SimpleNamespace(run_id="wfr-rag-contract", mock=False),
    )
    assert set(out) == {"retrieved_docs", "top_score"}
    assert out["top_score"] == out["retrieved_docs"][0]["score"]
    miss = retriever.run(
        {"query": "zebra cantaloupe spaceship maintenance"},
        SimpleNamespace(run_id="wfr-rag-contract", mock=False),
    )
    assert miss == {"retrieved_docs": [], "top_score": 0.0}


# ---------------------------------------------------------------------------
# 6. sim-ingest — the fixed chunking rule at its boundaries
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingestor() -> ModuleType:
    return _load_impl("kb-refresh", "sim-ingest")


def _doc(words: int) -> dict[str, str]:
    return {"id": f"doc-{words}w", "text": " ".join(["word"] * words)}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("documents", "expected"),
    [
        ([], (0, 0, 0)),  # nothing submitted
        ([{"id": "d", "text": ""}], (1, 0, 1)),  # empty text → empty_docs
        ([{"id": "d", "text": "   \n\t "}], (1, 0, 1)),  # whitespace-only too
        ([_doc(1)], (1, 1, 0)),  # one word → one chunk
        ([_doc(40)], (1, 1, 0)),  # exactly the boundary → one chunk
        ([_doc(41)], (1, 2, 0)),  # one past the boundary → two chunks
        ([_doc(80)], (1, 2, 0)),
        ([_doc(81)], (1, 3, 0)),
        ([_doc(40), {"id": "e", "text": ""}], (2, 1, 1)),  # mixed batch
        ([_doc(40), _doc(41)], (2, 3, 0)),
    ],
)
def test_ingest_chunking_rule(
    ingestor: ModuleType, documents: list[dict[str, str]], expected: tuple[int, int, int]
) -> None:
    assert ingestor.ingest(documents) == expected


@pytest.mark.unit
def test_ingest_run_contract(ingestor: ModuleType, sim_db: Path) -> None:
    """run() returns the documented nested summary with a stable reference."""
    out = ingestor.run(
        {"documents": [_doc(41), {"id": "e", "text": ""}]},
        SimpleNamespace(run_id="wfr-kb-contract", mock=False),
    )
    result = out["ingest_result"]
    assert set(out) == {"ingest_result"}
    assert result["doc_count"] == 2
    assert result["chunk_count"] == 2
    assert result["empty_docs"] == 1
    assert result["reference"].startswith("KB-ING-")


# ---------------------------------------------------------------------------
# 7. Sim ledger skills — tmp-SQLite record + read-back
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
def test_sim_retrieve_records_vectorstore_row(sim_db: Path, retriever: ModuleType) -> None:
    out = retriever.run(
        {"query": "How do I reset my corporate password?"},
        SimpleNamespace(run_id="wfr-rag-1", mock=False),
    )
    assert out["top_score"] == 1.0
    assert _rows(sim_db) == [
        (
            "wfr-rag-1",
            "vectorstore",
            "retrieve",
            {
                "query": "How do I reset my corporate password?",
                "top_score": 1.0,
                # kb-002/kb-004 match `corporate` (partial hits rank below
                # the full match, tie broken by id).
                "doc_ids": ["kb-001", "kb-002", "kb-004"],
            },
        )
    ]


@pytest.mark.unit
def test_sim_retrieve_audits_the_miss_too(sim_db: Path, retriever: ModuleType) -> None:
    """The zero-hit retrieval is STILL recorded — the audited miss is the
    debugging evidence the scenario certifies."""
    retriever.run(
        {"query": "zebra cantaloupe spaceship maintenance"},
        SimpleNamespace(run_id="wfr-rag-2", mock=False),
    )
    assert _rows(sim_db) == [
        (
            "wfr-rag-2",
            "vectorstore",
            "retrieve",
            {"query": "zebra cantaloupe spaceship maintenance", "top_score": 0.0, "doc_ids": []},
        )
    ]


@pytest.mark.unit
def test_sim_ingest_records_kb_row(sim_db: Path, ingestor: ModuleType) -> None:
    ingestor.run(
        {"documents": [_doc(41), {"id": "e", "text": ""}]},
        SimpleNamespace(run_id="wfr-kb-1", mock=False),
    )
    assert _rows(sim_db) == [
        ("wfr-kb-1", "kb", "ingest", {"doc_count": 2, "chunk_count": 2, "empty_docs": 1})
    ]


@pytest.mark.unit
def test_sim_kb_publish_records_kb_row(sim_db: Path) -> None:
    impl = _load_impl("kb-refresh", "sim-kb-publish")
    out = impl.run(
        {"ingest_result": {"doc_count": 2, "chunk_count": 5, "empty_docs": 0}},
        SimpleNamespace(run_id="wfr-kb-2", mock=False),
    )
    assert set(out) == {"publish_result"}
    assert "5 chunk(s)" in out["publish_result"]
    assert "KB-PUB-" in out["publish_result"]
    assert _rows(sim_db) == [("wfr-kb-2", "kb", "publish", {"chunk_count": 5})]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "skill", "payload"),
    [
        ("rag-debug", "sim-retrieve", {"query": "reset corporate password"}),
        ("kb-refresh", "sim-ingest", {"documents": [{"id": "d", "text": "one two"}]}),
        ("kb-refresh", "sim-kb-publish", {"ingest_result": {"chunk_count": 1}}),
    ],
)
def test_sim_skills_mock_skips_the_ledger(
    sim_db: Path, scenario: str, skill: str, payload: dict[str, Any]
) -> None:
    """ctx.mock skips the DB write (mdk run --mock stays hermetic) while the
    pure computation still satisfies the output contract."""
    impl = _load_impl(scenario, skill)
    out = impl.run(payload, SimpleNamespace(run_id="wfr-mock", mock=True))
    assert out
    assert not sim_db.exists()  # the ledger was never touched


@pytest.mark.unit
def test_sim_skill_run_id_input_overrides_ctx(sim_db: Path, retriever: ModuleType) -> None:
    """An explicit run_id in the input wins (the harness facades' convention);
    ctx.run_id is the fallback the tool node actually exercises."""
    retriever.run(
        {"query": "vpn password", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


@pytest.mark.unit
def test_sim_skill_reference_is_stable_per_run(sim_db: Path) -> None:
    """A Temporal activity retry must confirm the SAME synthetic reference."""
    impl = _load_impl("kb-refresh", "sim-kb-publish")
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    first = impl.run({"ingest_result": {"chunk_count": 3}}, ctx)
    second = impl.run({"ingest_result": {"chunk_count": 3}}, ctx)
    assert first == second


# ---------------------------------------------------------------------------
# 8. Anti-drift — skills AND agents ship byte-identical in all three copies
# ---------------------------------------------------------------------------

_SKILL_FILES = ("skill.yaml", "impl.py", "schema/input.json", "schema/output.json")
_AGENT_FILES = ("agent.yaml", "prompt.md", "schema/input.json", "schema/output.json")

_SCENARIO_SKILLS = {
    "rag-debug": ("sim-retrieve",),
    "kb-refresh": ("sim-ingest", "sim-kb-publish"),
}
_SCENARIO_AGENTS = {
    "rag-debug": ("answer", "diagnose"),
    "kb-refresh": ("validate", "notify"),
}


def _assert_identical_across_copies(scenario: str, rel: str) -> None:
    canonical = (DEPLOYABLES / scenario / rel).read_bytes()
    scenario_root = SCENARIOS / scenario
    template_root = TEMPLATES / B7[scenario][1]
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
@pytest.mark.parametrize("scenario", sorted(B7))
def test_state_schema_identical_across_copies(scenario: str) -> None:
    """All three copies share one state contract (the deployable's
    ./state.json, the scenario root's state.json, the template's)."""
    canonical = (DEPLOYABLES / scenario / "state.json").read_bytes()
    assert (SCENARIOS / scenario / "state.json").read_bytes() == canonical
    assert (TEMPLATES / B7[scenario][1] / "state.json").read_bytes() == canonical


# ---------------------------------------------------------------------------
# 9. Pattern registration — appended to the registry as workflow bundles
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(B7))
def test_pattern_registered_as_workflow_bundle(name: str) -> None:
    assert name in PATTERN_TEMPLATES
    assert name in list_patterns()
    assert pattern_is_workflow(name) is True
    rel, is_workflow, one_liner, topology = PATTERN_TEMPLATES[name]
    assert rel == B7[name][1]
    assert is_workflow is True
    assert "runtime: temporal" in one_liner
    assert "→" in topology
    pattern_dir = get_pattern_path(name)
    assert (pattern_dir / "workflow.yaml").is_file()
    assert (pattern_dir / "GOVERNANCE.md").is_file()
    assert (pattern_dir / "evals" / "judge.yaml.example").is_file()
    assert (pattern_dir / "evals" / "dataset.jsonl").is_file()
