"""B2 governance/redaction certification batch — structural + skill tests.

Covers the three scenarios of the B2 batch (program #3 / #30 / #2):

* ``pii-detection`` — TOOL redact → DECISION(pii_found) → {TOOL quarantine |
  TOOL store-clean} → notify;
* ``data-privacy`` — classify AGENT → DECISION(classification) → {TOOL redact
  → TOOL audit-store | TOOL audit-store} → summary;
* ``content-publishing`` — compliance AGENT → DECISION → brand AGENT →
  DECISION → HUMAN(routes) → TOOL publish → notify | rejected.

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
   convergences, the ADR 099 routes/fallback on the publishing gate;
2. Temporal compilation — the exact activity set per scenario and the inline
   (activity-free) ``evaluate_decision`` routing;
3. ``cases.yaml`` — parses through the driver's own loader
   (:func:`certification.harness.driver.load_scenario_spec`) with the right
   per-path ledger + redaction-marker expectations;
4. the ``redact-pii`` regexes — THE load-bearing piece of the batch: masking
   per identifier kind, boundary behavior, and the no-false-positive
   posture, character by character;
5. the sim ledger skills (``sim-dlp`` / ``sim-store`` / ``sim-audit-store`` /
   ``sim-publish``) — record + read-back on a tmp SQLite (``MOVATE_DB``),
   ``ctx.mock`` short-circuit, run_id precedence, stable references;
6. anti-drift — skill AND agent files byte-identical across the three copies
   of each scenario, and ``redact-pii`` additionally byte-identical ACROSS
   the two scenarios that ship it (6 copies, one truth).
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

from movate.core.loader import load_agent
from movate.core.workflow.compiler import compile_workflow, validate_graph
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.spec import load_workflow_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYABLES = REPO_ROOT / "workflows"
SCENARIOS = REPO_ROOT / "certification" / "scenarios"
TEMPLATES = REPO_ROOT / "src" / "movate" / "templates"

#: scenario → (scenario-mirror workflow subdir, template dir name).
B2 = {
    "pii-detection": ("pii", "pattern_pii_detection"),
    "data-privacy": ("privacy", "pattern_data_privacy"),
    "content-publishing": ("publishing", "pattern_content_publishing"),
}


def _workflow_dirs(scenario: str) -> dict[str, Path]:
    """The three compile sources for one scenario (id → workflow dir)."""
    subdir, template = B2[scenario]
    return {
        "deployable": DEPLOYABLES / scenario,
        "scenario": SCENARIOS / scenario / "workflows" / subdir,
        "template": TEMPLATES / template,
    }


#: (scenario, copy) pairs — the parametrization grid for the shape tests.
GRID = [(s, which) for s in sorted(B2) for which in ("deployable", "scenario", "template")]


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


def _edges_into(graph: WorkflowGraph, node_id: str) -> set[tuple[str, str | None]]:
    return {(e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == node_id}


# ---------------------------------------------------------------------------
# 1. Graph shape — node types, convergence (ADR 098), gate routes (ADR 099)
# ---------------------------------------------------------------------------

_EXPECTED_NODE_TYPES: dict[str, dict[str, NodeType]] = {
    "pii-detection": {
        "redact": NodeType.TOOL,
        "gate": NodeType.DECISION,
        "quarantine": NodeType.TOOL,
        "store-clean": NodeType.TOOL,
        "notify": NodeType.AGENT,
    },
    "data-privacy": {
        "classify": NodeType.AGENT,
        "route": NodeType.DECISION,
        "redact": NodeType.TOOL,
        "audit-store": NodeType.TOOL,
        "summary": NodeType.AGENT,
    },
    "content-publishing": {
        "compliance-review": NodeType.AGENT,
        "compliance-gate": NodeType.DECISION,
        "brand-review": NodeType.AGENT,
        "brand-gate": NodeType.DECISION,
        "final-approval": NodeType.HUMAN,
        "publish": NodeType.TOOL,
        "notify": NodeType.AGENT,
        "rejected": NodeType.AGENT,
    },
}

_EXPECTED_ENTRYPOINTS = {
    "pii-detection": "redact",
    "data-privacy": "classify",
    "content-publishing": "compliance-review",
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
def test_pii_detection_dispositions_converge_on_notify(which: str) -> None:
    """ADR 098: quarantine + clean-store both land on the ONE notify agent,
    and each disposition is reachable only via its decision leg."""
    graph = _graph(_workflow_dirs("pii-detection")[which])
    assert _edges_into(graph, "quarantine") == {("gate", "decision")}
    assert _edges_into(graph, "store-clean") == {("gate", "decision")}
    assert _edges_into(graph, "notify") == {("quarantine", None), ("store-clean", None)}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_data_privacy_all_paths_converge_on_audit_store(which: str) -> None:
    """ADR 098: the regulated detour (via redact) and the internal/public
    decision legs all converge on the ONE audit-store TOOL — the audit row no
    path can skip — and the redact detour is reachable only from the route."""
    graph = _graph(_workflow_dirs("data-privacy")[which])
    assert _edges_into(graph, "redact") == {("route", "decision")}
    assert _edges_into(graph, "audit-store") == {("route", "decision"), ("redact", None)}
    assert _edges_into(graph, "summary") == {("audit-store", None)}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_content_publishing_chain_gates_and_convergence(which: str) -> None:
    """Both review gates fail safe to the shared rejected agent; publish is
    reachable ONLY via the human gate's approve route (the graph, not a
    convention, guarantees the veto)."""
    graph = _graph(_workflow_dirs("content-publishing")[which])
    assert _edges_into(graph, "rejected") == {
        ("compliance-gate", "decision"),
        ("brand-gate", "decision"),
        ("final-approval", "human-route"),
    }
    assert _edges_into(graph, "publish") == {("final-approval", "human-route")}
    assert _edges_into(graph, "notify") == {("publish", None)}
    assert _edges_into(graph, "final-approval") == {("brand-gate", "decision")}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_content_publishing_gate_routes_and_fail_safe_fallback(which: str) -> None:
    """ADR 099: the human gate routes its own decision; prose fails safe."""
    graph = _graph(_workflow_dirs("content-publishing")[which])
    gate = graph.nodes["final-approval"]
    assert gate.metadata["routes"] == {"approve": "publish", "reject": "rejected"}
    assert gate.metadata["fallback"] == "rejected"
    assert gate.metadata["route_on"] == "decision"


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_pii_detection_notify_never_sees_the_document(which: str) -> None:
    """Data minimization is STRUCTURAL: the notify agent's input schema (the
    state projection the activity applies) must not list `document`."""
    graph = _graph(_workflow_dirs("pii-detection")[which])
    schema_path = Path(graph.nodes["notify"].ref) / "schema" / "input.json"
    props = json.loads(schema_path.read_text())["properties"]
    assert "document" not in props
    assert "redacted_text" in props


@pytest.mark.unit
@pytest.mark.parametrize("which", ["deployable", "scenario", "template"])
def test_data_privacy_summary_never_sees_the_document(which: str) -> None:
    graph = _graph(_workflow_dirs("data-privacy")[which])
    schema_path = Path(graph.nodes["summary"].ref) / "schema" / "input.json"
    props = json.loads(schema_path.read_text())["properties"]
    assert "document" not in props


# ---------------------------------------------------------------------------
# 2. Temporal compilation — exact activity sets, inline decision routing
# ---------------------------------------------------------------------------

_EXPECTED_ACTIVITIES = {
    # agents + TOOL dispatch + terminal persist; no gate-classifier activity
    # (ADR 094) and no human activity (no gate in these two).
    "pii-detection": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    "data-privacy": {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    },
    # + the durable HUMAN pause for final-approval.
    "content-publishing": {
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
# 3. cases.yaml — parses through the DRIVER's loader, per-path expectations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pii_detection_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "pii-detection" / "cases.yaml")
    assert spec.scenario == "pii-detection"
    assert spec.target == "pii-detection"
    assert [c.name for c in spec.cases] == ["clean-document", "pii-document", "mixed-document"]
    clean, pii, mixed = spec.cases

    # No human gate anywhere — no case declares hitl.
    assert all(c.hitl == () for c in spec.cases)
    # Governance asserted (expected allow) + terminal success on every case.
    assert all(c.expect.governance == "allow" for c in spec.cases)
    assert all(c.expect.status == "success" for c in spec.cases)

    # Clean path: store_clean row, NO quarantine, store_result marker only.
    assert [(e.system, e.action, e.times) for e in clean.expect.side_effects] == [
        ("dlp", "store_clean", 1)
    ]
    assert [(e.system, e.action) for e in clean.expect.no_side_effects] == [("dlp", "quarantine")]
    assert "store_result" in clean.expect.final_state_has
    assert "dlp_result" in clean.expect.final_state_lacks

    # PII paths: quarantine row, NO clean store, masked tokens present and
    # raw values absent — scoped to redacted_text.
    for case in (pii, mixed):
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("dlp", "quarantine", 1)
        ]
        assert [(e.system, e.action) for e in case.expect.no_side_effects] == [
            ("dlp", "store_clean")
        ]
        assert "dlp_result" in case.expect.final_state_has
        assert "store_result" in case.expect.final_state_lacks
        contains = dict(case.expect.final_state_contains)
        omits = dict(case.expect.final_state_omits)
        assert "[EMAIL]" in contains["redacted_text"]
        assert omits["redacted_text"]  # raw values pinned
    # The all-three-kinds case pins all three tokens + all three raw values.
    pii_contains = dict(pii.expect.final_state_contains)["redacted_text"]
    assert set(pii_contains) == {"[EMAIL]", "[PHONE]", "[SSN]"}
    assert len(dict(pii.expect.final_state_omits)["redacted_text"]) == 3
    # The mixed case also proves the clean prose SURVIVED masking.
    assert (
        "the all-hands moves to Thursday"
        in dict(mixed.expect.final_state_contains)["redacted_text"]
    )


@pytest.mark.unit
def test_data_privacy_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "data-privacy" / "cases.yaml")
    assert spec.scenario == "data-privacy"
    assert spec.target == "data-privacy"
    assert [c.name for c in spec.cases] == [
        "public-document",
        "internal-document",
        "regulated-document",
    ]
    assert all(c.hitl == () for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in spec.cases)

    # The compliance trail: each case asserts EXACTLY its classification's
    # audit row and the absence of BOTH other classifications' rows.
    by_name = {c.name: c for c in spec.cases}
    all_actions = {"store_public", "store_internal", "store_regulated"}
    for name, action in (
        ("public-document", "store_public"),
        ("internal-document", "store_internal"),
        ("regulated-document", "store_regulated"),
    ):
        case = by_name[name]
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("dlp", action, 1)
        ]
        assert {e.action for e in case.expect.no_side_effects} == all_actions - {action}
        assert "audit_result" in case.expect.final_state_has

    # Only the regulated path redacts — and pins tokens + raw values.
    for name in ("public-document", "internal-document"):
        assert "redacted_text" in by_name[name].expect.final_state_lacks
        assert by_name[name].expect.final_state_contains == ()
    regulated = by_name["regulated-document"]
    assert "redacted_text" in regulated.expect.final_state_has
    assert set(dict(regulated.expect.final_state_contains)["redacted_text"]) == {
        "[SSN]",
        "[EMAIL]",
        "[PHONE]",
    }
    omitted = dict(regulated.expect.final_state_omits)["redacted_text"]
    assert "078-05-1120" in omitted  # the raw SSN must not survive


@pytest.mark.unit
def test_content_publishing_cases_yaml_validates_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "content-publishing" / "cases.yaml")
    assert spec.scenario == "content-publishing"
    assert spec.target == "content-publishing"
    assert [c.name for c in spec.cases] == [
        "clean-content-approved",
        "compliance-flagged",
        "brand-flagged",
        "human-rejected",
    ]
    approved, compliance, brand, vetoed = spec.cases
    assert all(c.expect.governance == "allow" for c in spec.cases)
    # Rejection is a route, not an error — all four terminate success.
    assert all(c.expect.status == "success" for c in spec.cases)

    # Approved: the ONLY case with a publish row; pause + approve signal.
    assert [(s.node, s.decision) for s in approved.hitl] == [
        ("final-approval", {"decision": "approve"})
    ]
    assert [(e.system, e.action, e.times) for e in approved.expect.side_effects] == [
        ("cms", "publish", 1)
    ]
    assert "publish_result" in approved.expect.final_state_has

    # Flagged cases: NO hitl declared (the gate must never be reached — a
    # wrong pause starves the fact poll and fails durable-execution), no
    # publish row, and final_state lacks BOTH publish_result and the human
    # gate's decision key.
    for case in (compliance, brand):
        assert case.hitl == ()
        assert case.expect.side_effects == ()
        assert [(e.system, e.action) for e in case.expect.no_side_effects] == [("cms", "publish")]
        assert "publish_result" in case.expect.final_state_lacks
        assert "decision" in case.expect.final_state_lacks

    # The human veto: BOTH reviews pass, the approver rejects — pause +
    # reject signal observed, and the publish row must NOT exist.
    assert [(s.node, s.decision) for s in vetoed.hitl] == [
        ("final-approval", {"decision": "reject"})
    ]
    assert vetoed.expect.side_effects == ()
    assert [(e.system, e.action) for e in vetoed.expect.no_side_effects] == [("cms", "publish")]
    assert "publish_result" in vetoed.expect.final_state_lacks


# ---------------------------------------------------------------------------
# 4. redact-pii — the load-bearing regexes, character by character
# ---------------------------------------------------------------------------


def _load_impl(scenario: str, skill: str) -> ModuleType:
    """Import a deployable copy's impl.py under a unique module name (the
    copies are byte-identical; a plain namespace import would be ambiguous
    in-repo)."""
    path = DEPLOYABLES / scenario / "skills" / skill / "impl.py"
    module_name = f"b2_{scenario}_{skill}_impl".replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def redactor() -> ModuleType:
    return _load_impl("pii-detection", "redact-pii")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "masked", "count"),
    [
        # --- emails ---------------------------------------------------------
        ("mail jane.doe@example.com now", "mail [EMAIL] now", 1),
        ("mail first.last+tag@sub.domain.co.uk now", "mail [EMAIL] now", 1),
        ("(jane.doe@example.com)", "([EMAIL])", 1),
        ("JANE.DOE@EXAMPLE.COM", "[EMAIL]", 1),
        ("a@b.io and c_d%e@f-g.org", "[EMAIL] and [EMAIL]", 2),
        # --- SSNs (hyphenated form ONLY) -------------------------------------
        ("SSN 123-45-6789.", "SSN [SSN].", 1),
        ("SSN: 078-05-1120", "SSN: [SSN]", 1),
        # --- phones -----------------------------------------------------------
        ("call 555-123-4567 today", "call [PHONE] today", 1),
        ("call (555) 123-4567 today", "call [PHONE] today", 1),
        ("call (555)123-4567 today", "call [PHONE] today", 1),
        ("call 555.123.4567 today", "call [PHONE] today", 1),
        ("call 555 123 4567 today", "call [PHONE] today", 1),
        ("call +1 555-123-4567 today", "call [PHONE] today", 1),
        ("call +1-555-123-4567 today", "call [PHONE] today", 1),
        ("call +1 (555) 123-4567 today", "call [PHONE] today", 1),
        # --- combined / ordering ---------------------------------------------
        (
            "Reach jane.doe@example.com or (555) 123-4567. SSN 123-45-6789.",
            "Reach [EMAIL] or [PHONE]. SSN [SSN].",
            3,
        ),
        # --- empty / clean -----------------------------------------------------
        ("", "", 0),
        ("nothing sensitive here", "nothing sensitive here", 0),
    ],
)
def test_redactor_masks(redactor: ModuleType, text: str, masked: str, count: int) -> None:
    assert redactor.redact(text) == (masked, count)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        # Dates: the SSN shape must not eat ISO dates or date-ish runs.
        "released 2026-06-10",
        "window 2026-06-10 to 2026-07-01",
        # Longer digit/hyphen runs around an SSN-shaped core: not an SSN.
        "order 1123-45-6789",
        "ref 123-45-67890",
        "sku 123-45-6789-A",
        # Bare digit runs: deliberately NOT masked (false-positive posture) —
        # a 9-digit invoice and a 16-digit card-shaped number stay intact.
        "invoice 123456789",
        "card 4111111111111111",
        "call 5551234567",  # bare 10-digit run is not a maskable phone shape
        "local 123-4567",  # 7-digit local number: no area code, no mask
        # Version / IP / money / timestamp shapes that look phone-ish.
        "version 10.20.3000",
        "ip 192.168.1.100",
        "total 1,234.5678",
        "at 2026-06-10 12:30:4567",  # digit-adjacent: lookarounds reject
        # Not-quite-emails.
        "user@localhost",  # no TLD dot
        "ratio 3@4.5x is fine @home",
    ],
)
def test_redactor_no_false_positives(redactor: ModuleType, text: str) -> None:
    assert redactor.redact(text) == (text, 0)


@pytest.mark.unit
def test_redactor_is_idempotent_and_deterministic(redactor: ModuleType) -> None:
    """Masked output contains no PII shapes, so a second pass is a no-op —
    and two runs over the same input agree byte-for-byte (the Temporal
    replay/retry property the TOOL node relies on)."""
    text = "Reach jane.doe@example.com or (555) 123-4567. SSN 123-45-6789."
    once = redactor.redact(text)
    assert redactor.redact(text) == once
    assert redactor.redact(once[0]) == (once[0], 0)


@pytest.mark.unit
def test_redactor_run_contract(redactor: ModuleType) -> None:
    """run() returns exactly the documented keys; pii_found mirrors count."""
    hit = redactor.run({"document": "SSN 123-45-6789"}, SimpleNamespace(run_id="r", mock=False))
    assert hit == {"redacted_text": "SSN [SSN]", "pii_found": True, "pii_count": 1}
    miss = redactor.run({"document": "all clean"}, SimpleNamespace(run_id="r", mock=False))
    assert miss == {"redacted_text": "all clean", "pii_found": False, "pii_count": 0}
    assert isinstance(hit["pii_found"], bool)
    assert isinstance(hit["pii_count"], int)


# ---------------------------------------------------------------------------
# 5. Sim ledger skills — tmp-SQLite record + read-back
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
def test_sim_dlp_records_quarantine_row(sim_db: Path) -> None:
    impl = _load_impl("pii-detection", "sim-dlp")
    out = impl.run(
        {"source": "hr/escalation-4821", "pii_count": 3},
        SimpleNamespace(run_id="wfr-pii-1", mock=False),
    )
    assert set(out) == {"dlp_result"}
    assert "3 PII value(s)" in out["dlp_result"]
    assert "DLP-QTN-" in out["dlp_result"]
    assert _rows(sim_db) == [
        ("wfr-pii-1", "dlp", "quarantine", {"source": "hr/escalation-4821", "pii_count": 3})
    ]


@pytest.mark.unit
def test_sim_store_records_clean_row(sim_db: Path) -> None:
    impl = _load_impl("pii-detection", "sim-store")
    out = impl.run(
        {"source": "wiki/onboarding-guide"}, SimpleNamespace(run_id="wfr-pii-2", mock=False)
    )
    assert set(out) == {"store_result"}
    assert "DLP-STORE-" in out["store_result"]
    assert _rows(sim_db) == [
        ("wfr-pii-2", "dlp", "store_clean", {"source": "wiki/onboarding-guide"})
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("classification", "action"),
    [("public", "store_public"), ("internal", "store_internal"), ("regulated", "store_regulated")],
)
def test_sim_audit_store_action_keyed_by_classification(
    sim_db: Path, classification: str, action: str
) -> None:
    impl = _load_impl("data-privacy", "sim-audit-store")
    out = impl.run(
        {"classification": classification, "requester": "hr.ops"},
        SimpleNamespace(run_id="wfr-priv-1", mock=False),
    )
    assert set(out) == {"audit_result"}
    assert action in out["audit_result"]
    assert "DLP-AUD-" in out["audit_result"]
    assert _rows(sim_db) == [
        ("wfr-priv-1", "dlp", action, {"classification": classification, "requester": "hr.ops"})
    ]


@pytest.mark.unit
def test_sim_audit_store_rejects_unknown_classification(sim_db: Path) -> None:
    """The enum lives in the input schema (dispatch_skill enforces it); the
    impl's KeyError is the fail-loud backstop — no silent default bucket."""
    impl = _load_impl("data-privacy", "sim-audit-store")
    with pytest.raises(KeyError):
        impl.run({"classification": "secret"}, SimpleNamespace(run_id="wfr-x", mock=False))
    assert not sim_db.exists()  # nothing was recorded


@pytest.mark.unit
def test_sim_publish_records_cms_row(sim_db: Path) -> None:
    impl = _load_impl("content-publishing", "sim-publish")
    out = impl.run({"channel": "blog"}, SimpleNamespace(run_id="wfr-pub-1", mock=False))
    assert set(out) == {"publish_result"}
    assert "CMS-PUB-" in out["publish_result"]
    assert _rows(sim_db) == [("wfr-pub-1", "cms", "publish", {"channel": "blog"})]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "skill", "payload"),
    [
        ("pii-detection", "sim-dlp", {"source": "s", "pii_count": 1}),
        ("pii-detection", "sim-store", {"source": "s"}),
        ("data-privacy", "sim-audit-store", {"classification": "public"}),
        ("content-publishing", "sim-publish", {"channel": "blog"}),
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
    impl = _load_impl("content-publishing", "sim-publish")
    impl.run(
        {"channel": "blog", "run_id": "wfr-explicit"},
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert [r[0] for r in _rows(sim_db)] == ["wfr-explicit"]


@pytest.mark.unit
def test_sim_skill_reference_is_stable_per_run(sim_db: Path) -> None:
    """A Temporal activity retry must confirm the SAME synthetic reference."""
    impl = _load_impl("pii-detection", "sim-dlp")
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    first = impl.run({"source": "hr/escalation-4821", "pii_count": 2}, ctx)
    second = impl.run({"source": "hr/escalation-4821", "pii_count": 2}, ctx)
    assert first == second


# ---------------------------------------------------------------------------
# 6. Anti-drift — skills AND agents ship byte-identical in all three copies
# ---------------------------------------------------------------------------

_SKILL_FILES = ("skill.yaml", "impl.py", "schema/input.json", "schema/output.json")
_AGENT_FILES = ("agent.yaml", "prompt.md", "schema/input.json", "schema/output.json")

_SCENARIO_SKILLS = {
    "pii-detection": ("redact-pii", "sim-dlp", "sim-store"),
    "data-privacy": ("redact-pii", "sim-audit-store"),
    "content-publishing": ("sim-publish",),
}
_SCENARIO_AGENTS = {
    "pii-detection": ("notify",),
    "data-privacy": ("classify", "summary"),
    "content-publishing": ("compliance-review", "brand-review", "notify", "rejected"),
}


def _assert_identical_across_copies(scenario: str, rel: str) -> None:
    dirs = _workflow_dirs(scenario)
    canonical = (dirs["deployable"].parent / scenario / rel).read_bytes()
    scenario_root = SCENARIOS / scenario
    template_root = TEMPLATES / B2[scenario][1]
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
@pytest.mark.parametrize("rel", [f"skills/redact-pii/{f}" for f in _SKILL_FILES])
def test_redact_pii_identical_across_scenarios(rel: str) -> None:
    """redact-pii ships in BOTH pii-detection and data-privacy (x3 copies
    each): one redactor, one truth — all six files byte-identical."""
    canonical = (DEPLOYABLES / "pii-detection" / rel).read_bytes()
    assert (DEPLOYABLES / "data-privacy" / rel).read_bytes() == canonical, (
        f"data-privacy deployable copy of {rel} drifted from pii-detection"
    )


@pytest.mark.unit
@pytest.mark.parametrize("scenario", sorted(B2))
def test_state_schema_identical_across_copies(scenario: str) -> None:
    """All three copies share one state contract (the deployable's
    ./state.json, the scenario root's state.json, the template's)."""
    canonical = (DEPLOYABLES / scenario / "state.json").read_bytes()
    assert (SCENARIOS / scenario / "state.json").read_bytes() == canonical
    assert (TEMPLATES / B2[scenario][1] / "state.json").read_bytes() == canonical


# ---------------------------------------------------------------------------
# 7. Prompt rendering — every path's state projection renders under
#    StrictUndefined (the #841 regression)
# ---------------------------------------------------------------------------
#
# Each scenario's terminal agent is a CONVERGENCE point: every branch reaches
# it, but each branch leaves a different subset of state keys behind, and the
# runner's `_project_state` DROPS missing keys rather than passing None. The
# prompt renders with StrictUndefined, so any path-exclusive `{{ input.X }}`
# without an `is defined` guard fails the node at run time on every path that
# lacks X — which is exactly how the first live B2 certification died (#841).
# These tests render each converged agent's prompt with the minimal input
# every path actually produces (the projection of the path's final state onto
# the agent's input schema; key sets mirror `final_state_has`/`_lacks` in
# each cases.yaml). Only the deployable copy is rendered —
# `test_agent_files_identical_across_copies` extends the proof to the other
# two copies.

#: (scenario, agent) → path name → that path's projected agent input.
_CONVERGED_AGENT_PATH_INPUTS: dict[tuple[str, str], dict[str, dict[str, Any]]] = {
    ("pii-detection", "notify"): {
        # clean path: store-clean ran, quarantine didn't → no dlp_result.
        "clean": {
            "source": "wiki/onboarding-guide",
            "pii_found": False,
            "pii_count": 0,
            "redacted_text": "Welcome to the team wiki.",
            "store_result": "DLP-STORE-3B8XA1",
        },
        # quarantine path: the mirror image → no store_result.
        "quarantined": {
            "source": "hr/complaint-4821",
            "pii_found": True,
            "pii_count": 2,
            "redacted_text": "Reach me at [EMAIL]; my SSN is [SSN].",
            "dlp_result": "DLP-QTN-7K2F9Q",
        },
    },
    ("data-privacy", "summary"): {
        # public / internal skip the redact node → no pii_count.
        "public": {
            "classification": "public",
            "rationale": "Marketing copy with no personal data.",
            "requester": "comms-team",
            "audit_result": "DLP-AUD-5F8B1A",
        },
        "internal": {
            "classification": "internal",
            "rationale": "Roadmap discussion; company-confidential only.",
            "requester": "eng-leads",
            "audit_result": "DLP-AUD-2D7C4B",
        },
        "regulated": {
            "classification": "regulated",
            "rationale": "Contains employee identifiers.",
            "requester": "hr-ops",
            "audit_result": "DLP-AUD-9C4D2E",
            "pii_count": 2,
        },
    },
    ("content-publishing", "rejected"): {
        # flagged at either review → never reached the human gate → no
        # `decision` (the cases.yaml final_state_lacks assertion).
        "compliance-flagged": {
            "verdict": "flag",
            "notes": "Medical cure claim: guaranteed to cure arthritis.",
        },
        "brand-flagged": {
            "verdict": "flag",
            "notes": "Tone is off-brand: aggressive competitor bashing.",
        },
        # both reviews passed, the human approver declined.
        "human-rejected": {
            "verdict": "pass",
            "notes": "Brand voice ok.",
            "decision": "reject",
        },
    },
}

_RENDER_CASES = [
    pytest.param(scenario, agent, input_data, id=f"{scenario}-{agent}-{path}")
    for (scenario, agent), paths in sorted(_CONVERGED_AGENT_PATH_INPUTS.items())
    for path, input_data in paths.items()
]


@pytest.mark.unit
@pytest.mark.parametrize(("scenario", "agent", "input_data"), _RENDER_CASES)
def test_converged_agent_prompt_renders_per_path(
    scenario: str, agent: str, input_data: dict[str, Any]
) -> None:
    bundle = load_agent(DEPLOYABLES / scenario / "agents" / agent)
    bundle.input_validator.validate(input_data)
    # StrictUndefined: an unguarded path-exclusive key raises UndefinedError.
    rendered = bundle.render_prompt(input_data)
    # Guards must SHOW a key when the path provides it, not just not-crash.
    for value in input_data.values():
        assert str(value) in rendered
