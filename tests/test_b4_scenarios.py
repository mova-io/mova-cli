"""B4 resiliency batch — scenarios #13, #15, #5 of the 30-use-case program.

Three certification scenarios, each shipping in the itsm-request THREE-copy
layout (deployable under ``workflows/``, suite mirror under
``certification/scenarios/``, ``mdk init --pattern`` template under
``src/movate/templates/``) that must stay coherent:

* ``external-api-failure`` (#13) — retries + fallback provider with
  OBSERVABLE activity retries: the ``flaky-call`` TOOL entrypoint appends one
  ledger ``attempt`` row per invocation and RAISES while its per-run attempt
  count is <= ``fail_times`` — so ledger rows = Temporal retry attempts
  (``_RETRY_POLICY``, maximum_attempts=3); ``fail_times >= 3`` exhausts the
  budget into a terminal ERROR.
* ``partial-failure-recovery`` (#15) — completed steps are NOT re-executed on
  retry: a 3-step TOOL pipeline whose flaky middle step records attempt rows
  while step-one's single row proves it never re-ran.
* ``long-running-research`` (#5) — scheduled incremental research (the
  ADR 100 D1 cron-schedule dogfood): each fire runs one increment; a decision
  routes ``increment gte 3`` into the final report.

Like ``test_b1_scenarios``, native mock-runs are deliberately NOT tested;
this module asserts what IS deterministic:

1. graph shape per scenario — node types, the decision predicates, the
   ADR 097 D1 input-map literals + output_key namespacing, the tool nodes'
   compile-time skill resolution;
2. Temporal compilation — each scenario compiles; the retry policy that
   makes the flaky skills' raises observable is emitted and applied to the
   skill activities; activity sets are exact;
3. ``cases.yaml`` — every scenario parses through the driver's own loader,
   including the expected-error retry-exhaustion case and the
   attempt-row-count expectations;
4. the driver's expected-error extension — a case with ``expect.status:
   error`` accepts a job segment that terminates ``error`` (the workflow
   started and failed durably) while every other case keeps the strict
   success-only launch contract;
5. the attempt-counting skill impls — tmp-SQLite ledger: raise-then-recover
   sequencing, rows = attempts, success rows recorded exactly once,
   ``ctx.mock`` short-circuit (never raises, never touches the DB), run_id
   precedence, retry-stable references;
6. anti-drift — each skill ships byte-identical across its three copies.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
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
    "external-api-failure": {
        "deployable": WORKFLOWS / "external-api-failure",
        "scenario": SCENARIOS / "external-api-failure" / "workflows" / "flaky",
        "template": TEMPLATES / "pattern_external_api_failure",
    },
    "partial-failure-recovery": {
        "deployable": WORKFLOWS / "partial-failure-recovery",
        "scenario": SCENARIOS / "partial-failure-recovery" / "workflows" / "pipeline",
        "template": TEMPLATES / "pattern_partial_failure_recovery",
    },
    "long-running-research": {
        "deployable": WORKFLOWS / "long-running-research",
        "scenario": SCENARIOS / "long-running-research" / "workflows" / "research",
        "template": TEMPLATES / "pattern_long_running_research",
    },
}
COPY_IDS = sorted(COPIES["external-api-failure"])  # deployable / scenario / template


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# 1a. external-api-failure — flaky TOOL entrypoint + provider decision
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_external_api_failure_graph_shape(which: str) -> None:
    graph = _graph(COPIES["external-api-failure"][which])
    validate_graph(graph)

    assert graph.entrypoint == "call"
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "call": NodeType.TOOL,
        "provider-check": NodeType.DECISION,
        "record": NodeType.TOOL,
        "notify": NodeType.AGENT,
        "failed": NodeType.AGENT,
    }
    # Both TOOL nodes resolved their skills at compile time (ADR 097 D2).
    call = graph.nodes["call"]
    assert call.metadata["skill"] == "flaky-call"
    assert (Path(call.ref) / "impl.py").is_file()
    record = graph.nodes["record"]
    assert record.metadata["skill"] == "sim-record"
    assert (Path(record.ref) / "impl.py").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_external_api_failure_provider_routing(which: str) -> None:
    """The provider decision is a pure truthy predicate (ADR 094); the
    success tail is the ONLY way to the record side effect, and the `failed`
    default is the structural fail-safe."""
    graph = _graph(COPIES["external-api-failure"][which])

    assert graph.nodes["provider-check"].metadata == {
        # `truthy` ignores `value`; the spec layer normalizes it to None.
        "cases": [
            {"when": {"field": "provider_ok", "op": "truthy", "value": None}, "to": "record"}
        ],
        "default": "failed",
    }
    into_record = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "record"
    }
    assert into_record == {("provider-check", "decision")}
    assert {e.to_id for e in graph.edges if e.from_id == "record"} == {"notify"}
    into_failed = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "failed"
    }
    assert into_failed == {("provider-check", "decision")}


# ---------------------------------------------------------------------------
# 1b. partial-failure-recovery — sequential TOOL pipeline, parameterized step
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_partial_failure_recovery_graph_shape(which: str) -> None:
    graph = _graph(COPIES["partial-failure-recovery"][which])
    validate_graph(graph)

    assert graph.entrypoint == "step-one"
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "step-one": NodeType.TOOL,
        "step-two": NodeType.TOOL,
        "step-three": NodeType.TOOL,
        "notify": NodeType.AGENT,
    }
    # The pipeline order is structural: explicit edges chain the three steps.
    assert {(e.from_id, e.to_id) for e in graph.edges} == {
        ("step-one", "step-two"),
        ("step-two", "step-three"),
        ("step-three", "notify"),
    }


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_partial_failure_recovery_parameterized_steps(which: str) -> None:
    """ONE sim-step skill serves step1 and step3 via the ADR 097 D1 input
    literal map; every step namespaces its delta with output_key so three
    results from two skills can never collide."""
    graph = _graph(COPIES["partial-failure-recovery"][which])

    one, two, three = (graph.nodes[n] for n in ("step-one", "step-two", "step-three"))
    assert one.metadata["skill"] == "sim-step"
    assert three.metadata["skill"] == "sim-step"
    assert one.ref == three.ref  # the SAME resolved skill dir — one impl, two stages
    assert one.metadata["input_map"] == {"step": {"literal": "step1"}, "request": "request"}
    assert three.metadata["input_map"] == {"step": {"literal": "step3"}, "request": "request"}
    assert two.metadata["skill"] == "sim-step-flaky"
    assert two.metadata["input_map"] is None  # schema projection carries fail_times
    assert [n.metadata["output_key"] for n in (one, two, three)] == ["step1", "step2", "step3"]
    for node in (one, two, three):
        assert (Path(node.ref) / "impl.py").is_file()


# ---------------------------------------------------------------------------
# 1c. long-running-research — increment body + deterministic finality
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_long_running_research_graph_shape(which: str) -> None:
    graph = _graph(COPIES["long-running-research"][which])
    validate_graph(graph)

    assert graph.entrypoint == "research"
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "research": NodeType.AGENT,
        "append": NodeType.TOOL,
        "finality-check": NodeType.DECISION,
        "final-report": NodeType.AGENT,
        "ack": NodeType.AGENT,
    }
    append = graph.nodes["append"]
    assert append.metadata["skill"] == "sim-append-findings"
    assert (Path(append.ref) / "impl.py").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_long_running_research_finality_routing(which: str) -> None:
    """Finality is a pure numeric predicate (ADR 094): the closing increment
    (gte 3) is the ONLY way to the final report; earlier increments ack."""
    graph = _graph(COPIES["long-running-research"][which])

    assert graph.nodes["finality-check"].metadata == {
        "cases": [{"when": {"field": "increment", "op": "gte", "value": 3}, "to": "final-report"}],
        "default": "ack",
    }
    into_final = {
        (e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "final-report"
    }
    assert into_final == {("finality-check", "decision")}
    into_ack = {(e.from_id, e.metadata.get("source")) for e in graph.edges if e.to_id == "ack"}
    assert into_ack == {("finality-check", "decision")}
    # Every increment appends BEFORE finality is decided — the log gets the
    # final increment too.
    assert {(e.from_id, e.to_id) for e in graph.edges if e.metadata.get("source") is None} == {
        ("research", "append"),
        ("append", "finality-check"),
    }


# ---------------------------------------------------------------------------
# 2. Temporal compilation — the retry policy that powers the batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
@pytest.mark.parametrize(
    "scenario",
    ["external-api-failure", "partial-failure-recovery", "long-running-research"],
)
def test_temporal_compiles_with_exact_activity_set(scenario: str, which: str) -> None:
    """No HUMAN nodes anywhere in this batch: agent + skill + persist is the
    exact activity set; decision nodes route inline (ADR 094)."""
    result = TemporalCompiler().compile(_graph(COPIES[scenario][which]))
    src = result.module_source
    ast.parse(src)
    assert set(result.activity_names) == {
        "call_agent_activity",
        "call_skill_activity",
        "persist_workflow_result_activity",
    }
    if scenario == "partial-failure-recovery":
        # Purely sequential — no decision helper in the emitted module.
        assert "evaluate_decision(" not in src
    else:
        assert "evaluate_decision(" in src


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
@pytest.mark.parametrize(
    ("scenario", "skill_calls"),
    [
        ("external-api-failure", 2),  # flaky-call + sim-record
        ("partial-failure-recovery", 3),  # step-one + step-two + step-three
        ("long-running-research", 1),  # sim-append-findings
    ],
)
def test_temporal_emits_retry_policy_on_every_skill_activity(
    scenario: str, skill_calls: int, which: str
) -> None:
    """THE RETRY-OBSERVABILITY CONTRACT: the compiled module pins
    maximum_attempts=3 and applies it to every skill activity — that policy
    is what turns the flaky impls' raises into 1/2/3 ledger attempt rows."""
    result = TemporalCompiler().compile(_graph(COPIES[scenario][which]))
    src = result.module_source
    assert "_RETRY_POLICY = RetryPolicy(maximum_attempts=3)" in src
    assert src.count("call_skill_activity,") >= skill_calls
    # Every activity invocation in the module carries the retry policy.
    assert src.count("await workflow.execute_activity(") == src.count("retry_policy=_RETRY_POLICY,")


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_temporal_emits_step_namespacing(which: str) -> None:
    """The ADR 097 D1 output_key / input-literal args ride the compiled skill
    activities — the namespacing the recovery assertions read back."""
    result = TemporalCompiler().compile(_graph(COPIES["partial-failure-recovery"][which]))
    src = result.module_source
    for key in ("step1", "step2", "step3"):
        assert f"'{key}'" in src
    assert "{'step': {'literal': 'step1'}, 'request': 'request'}" in src
    assert "{'step': {'literal': 'step3'}, 'request': 'request'}" in src


# ---------------------------------------------------------------------------
# 3. cases.yaml — every scenario parses through the DRIVER's loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_external_api_failure_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "external-api-failure" / "cases.yaml")
    assert spec.scenario == "external-api-failure"
    assert spec.target == "external-api-failure"
    assert [c.name for c in spec.cases] == [
        "no-failure",
        "transient-failure",
        "retry-exhaustion",
    ]
    clean, transient, exhausted = spec.cases

    # Control: one attempt, one record — no retry consumed.
    assert clean.input["fail_times"] == 0
    assert clean.expect.status == "success"
    assert [(e.system, e.action, e.times) for e in clean.expect.side_effects] == [
        ("external-api", "attempt", 1),
        ("external-api", "record", 1),
    ]

    # THE retry proof: 2 attempt rows (the durable retry), still ONE record.
    assert transient.input["fail_times"] == 1
    assert transient.expect.status == "success"
    assert [(e.system, e.action, e.times) for e in transient.expect.side_effects] == [
        ("external-api", "attempt", 2),
        ("external-api", "record", 1),
    ]
    assert "record_result" in transient.expect.final_state_has

    # Exhaustion: fail_times=5 > the 3-attempt budget ⇒ terminal ERROR with
    # exactly 3 attempt rows and provably NO record row.
    assert exhausted.input["fail_times"] >= 3
    assert exhausted.expect.status == "error"
    assert [(e.system, e.action, e.times) for e in exhausted.expect.side_effects] == [
        ("external-api", "attempt", 3),
    ]
    assert [(e.system, e.action) for e in exhausted.expect.no_side_effects] == [
        ("external-api", "record")
    ]
    assert set(exhausted.expect.final_state_lacks) >= {"api_result", "record_result"}
    assert exhausted.expect.governance is None  # honest skip — no LLM ran

    # No HUMAN gates anywhere in this batch.
    assert all(c.hitl == () for c in spec.cases)
    assert all(c.expect.governance == "allow" for c in (clean, transient))


@pytest.mark.unit
def test_partial_failure_recovery_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "partial-failure-recovery" / "cases.yaml")
    assert spec.scenario == "partial-failure-recovery"
    assert [c.name for c in spec.cases] == ["clean-run", "mid-pipeline-transient-failure"]
    clean, transient = spec.cases

    assert clean.input["fail_times"] == 0
    assert [(e.system, e.action, e.times) for e in clean.expect.side_effects] == [
        ("pipeline", "step1", 1),
        ("pipeline", "step2_attempt", 1),
        ("pipeline", "step2", 1),
        ("pipeline", "step3", 1),
    ]

    # THE assertion: step1 EXACTLY ONCE while step2's attempts show 2 tries —
    # Temporal replayed only the failed activity, never the completed one.
    assert transient.input["fail_times"] == 1
    assert [(e.system, e.action, e.times) for e in transient.expect.side_effects] == [
        ("pipeline", "step1", 1),
        ("pipeline", "step2_attempt", 2),
        ("pipeline", "step2", 1),
        ("pipeline", "step3", 1),
    ]

    for case in spec.cases:
        assert case.expect.status == "success"
        assert case.expect.governance == "allow"
        assert case.hitl == ()
        assert set(case.expect.final_state_has) >= {"step1", "step2", "step3", "summary"}


@pytest.mark.unit
def test_long_running_research_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "long-running-research" / "cases.yaml")
    assert spec.scenario == "long-running-research"
    assert [c.name for c in spec.cases] == ["mid-increment", "final-increment"]
    mid, final = spec.cases

    # Mid-series: append row, ack tail, NO final report yet.
    assert mid.input["increment"] < 3
    assert "ack_note" in mid.expect.final_state_has
    assert "final_report" in mid.expect.final_state_lacks

    # Closing increment: the log still gets its append row + the report.
    assert final.input["increment"] >= 3
    assert "final_report" in final.expect.final_state_has
    assert "ack_note" in final.expect.final_state_lacks

    for case in spec.cases:
        assert case.expect.status == "success"
        assert case.expect.governance == "allow"
        assert case.hitl == ()
        assert [(e.system, e.action, e.times) for e in case.expect.side_effects] == [
            ("research", "append", 1)
        ]


# ---------------------------------------------------------------------------
# 4. Driver extension — expected-error launches (append-style, back-compat)
# ---------------------------------------------------------------------------


class ScriptedErrorRuntime:
    """A fake runtime whose job terminates with ``job_status`` and whose
    terminal fact reads ``fact_status`` — the retry-exhaustion shape."""

    def __init__(self, *, job_status: str, fact_status: str) -> None:
        self.job_status = job_status
        self.fact_status = fact_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/run":
            return httpx.Response(202, json={"job_id": "job-1", "status": "queued"})
        if request.method == "GET" and path == "/jobs/job-1":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-1",
                    "status": self.job_status,
                    "result_run_id": "wfr-1",
                    "error": {"type": "internal", "message": "boom"}
                    if self.job_status == "error"
                    else None,
                },
            )
        if request.method == "GET" and path == "/api/v1/workflow-runs":
            row = {
                "workflow_run_id": "wfr-1",
                "status": self.fact_status,
                "final_state": {"request": "fraud-screening", "fail_times": 5},
            }
            return httpx.Response(200, json={"workflow_runs": [row], "count": 1})
        if request.method == "GET" and path == "/api/v1/observability/facts":
            fact = {
                "kind": "workflow_run",
                "source_id": "wfr-1",
                "workflow": "external-api-failure",
                "status": self.fact_status,
                "route": None,
                "cost_usd": 0.0,
                "governance_effect": None,
                "error_type": "temporal_workflow_failed" if self.fact_status == "error" else None,
            }
            return httpx.Response(200, json={"facts": [fact], "count": 1})
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})


def _error_driver(fake: ScriptedErrorRuntime) -> SuiteDriver:
    client = RuntimeApiClient("http://cert.test", "k", transport=httpx.MockTransport(fake.handler))
    return SuiteDriver(
        client,
        poll_interval_s=0.0,
        job_timeout_s=5.0,
        pause_timeout_s=5.0,
        fact_timeout_s=5.0,
        side_effects_enabled=False,
        sleep=lambda _s: None,
    )


def _case(expect_status: str, **expect_kwargs: Any) -> CaseSpec:
    return CaseSpec(
        name="t",
        input={"request": "fraud-screening", "fail_times": 5},
        expect=CaseExpect(status=expect_status, route=None, **expect_kwargs),
    )


def _spec(case: CaseSpec) -> ScenarioSpec:
    return ScenarioSpec(
        scenario="external-api-failure", target="external-api-failure", cases=(case,)
    )


@pytest.mark.unit
def test_driver_expected_error_case_accepts_error_job_and_certifies() -> None:
    """expect.status=error: the job segment may end `error` (the workflow
    started and failed durably); the terminal fact's `error` status is the
    durable-execution PASS, and final_state_lacks markers still evaluate."""
    fake = ScriptedErrorRuntime(job_status="error", fact_status="error")
    case = _case("error", final_state_lacks=("api_result", "record_result"))
    result = _error_driver(fake).run_case(_spec(case), case)

    assert result.workflow_run_id == "wfr-1"
    assert result.outcomes["durable-execution"].status == "pass"
    assert "status=error" in result.outcomes["durable-execution"].note
    assert result.outcomes["decision-routing"].status == "pass"
    assert result.outcomes["hitl"].status == "skip"


@pytest.mark.unit
def test_driver_expected_error_case_still_accepts_success_job() -> None:
    """A detached segment can end `success` while the FACT reads error — the
    expected-error case must accept that shape too."""
    fake = ScriptedErrorRuntime(job_status="success", fact_status="error")
    case = _case("error")
    result = _error_driver(fake).run_case(_spec(case), case)
    assert result.outcomes["durable-execution"].status == "pass"


@pytest.mark.unit
def test_driver_success_case_still_fails_on_error_job() -> None:
    """BACK-COMPAT: a case expecting success keeps the strict launch
    contract — an `error` job is a launch failure, everything else skips."""
    fake = ScriptedErrorRuntime(job_status="error", fact_status="error")
    case = _case("success")
    result = _error_driver(fake).run_case(_spec(case), case)

    assert result.workflow_run_id is None
    assert result.outcomes["durable-execution"].status == "fail"
    assert "launch" in result.outcomes["durable-execution"].note
    assert all(
        result.outcomes[cap].status == "skip"
        for cap in result.outcomes
        if cap != "durable-execution"
    )


@pytest.mark.unit
def test_driver_expected_error_case_fails_on_success_fact() -> None:
    """A run that unexpectedly SUCCEEDS must fail an expected-error case —
    no green-washing the exhaustion proof."""
    fake = ScriptedErrorRuntime(job_status="success", fact_status="success")
    case = _case("error")
    result = _error_driver(fake).run_case(_spec(case), case)
    assert result.outcomes["durable-execution"].status == "fail"


# ---------------------------------------------------------------------------
# 5. Skill impls — tmp-SQLite ledger: attempts, raises, recovery
# ---------------------------------------------------------------------------

SKILLS: dict[str, list[Path]] = {
    "flaky-call": [
        WORKFLOWS / "external-api-failure" / "skills" / "flaky-call",
        SCENARIOS / "external-api-failure" / "skills" / "flaky-call",
        TEMPLATES / "pattern_external_api_failure" / "skills" / "flaky-call",
    ],
    "sim-record": [
        WORKFLOWS / "external-api-failure" / "skills" / "sim-record",
        SCENARIOS / "external-api-failure" / "skills" / "sim-record",
        TEMPLATES / "pattern_external_api_failure" / "skills" / "sim-record",
    ],
    "sim-step": [
        WORKFLOWS / "partial-failure-recovery" / "skills" / "sim-step",
        SCENARIOS / "partial-failure-recovery" / "skills" / "sim-step",
        TEMPLATES / "pattern_partial_failure_recovery" / "skills" / "sim-step",
    ],
    "sim-step-flaky": [
        WORKFLOWS / "partial-failure-recovery" / "skills" / "sim-step-flaky",
        SCENARIOS / "partial-failure-recovery" / "skills" / "sim-step-flaky",
        TEMPLATES / "pattern_partial_failure_recovery" / "skills" / "sim-step-flaky",
    ],
    "sim-append-findings": [
        WORKFLOWS / "long-running-research" / "skills" / "sim-append-findings",
        SCENARIOS / "long-running-research" / "skills" / "sim-append-findings",
        TEMPLATES / "pattern_long_running_research" / "skills" / "sim-append-findings",
    ],
}


def _load_impl(skill: str) -> ModuleType:
    """Import the deployable copy's impl.py under a unique module name (three
    byte-identical copies exist; a plain ``<skill>.impl`` import would be
    ambiguous in-repo)."""
    path = SKILLS[skill][0] / "impl.py"
    module_name = f"b4_{skill.replace('-', '_')}_impl"
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
def test_flaky_call_succeeds_first_attempt_without_failures(sim_db: Path) -> None:
    impl = _load_impl("flaky-call")
    ctx = SimpleNamespace(run_id="wfr-api-0", mock=False)

    out = impl.run({"request": "currency-rates", "fail_times": 0}, ctx)

    assert out["provider_ok"] is True
    assert out["provider"] == "primary"
    assert "attempt 1" in out["api_result"]
    assert "API-" in out["api_result"]
    assert [(r[0], r[2]) for r in _rows(sim_db)] == [("wfr-api-0", "attempt")]


@pytest.mark.unit
def test_flaky_call_raises_then_recovers_with_two_attempt_rows(sim_db: Path) -> None:
    """The transient shape: invocation 1 records a row AND raises; invocation
    2 (Temporal's retry) records the second row and succeeds via the
    fallback provider. Rows = attempts."""
    impl = _load_impl("flaky-call")
    ctx = SimpleNamespace(run_id="wfr-api-1", mock=False)
    payload = {"request": "shipment-status", "fail_times": 1}

    with pytest.raises(RuntimeError, match="attempt 1 of fail_times=1"):
        impl.run(dict(payload), ctx)
    out = impl.run(dict(payload), ctx)

    assert out["provider_ok"] is True
    assert out["provider"] == "fallback"
    assert "attempt 2" in out["api_result"]
    rows = _rows(sim_db)
    assert [(r[0], r[1], r[2]) for r in rows] == [("wfr-api-1", "external-api", "attempt")] * 2
    assert all(r[3] == {"request": "shipment-status", "fail_times": 1} for r in rows)


@pytest.mark.unit
def test_flaky_call_exhaustion_burns_exactly_the_retry_budget(sim_db: Path) -> None:
    """fail_times=5 can never be satisfied in 3 attempts: every invocation up
    to the policy cap raises, leaving exactly 3 rows — the ledger shows the
    budget was spent."""
    impl = _load_impl("flaky-call")
    ctx = SimpleNamespace(run_id="wfr-api-5", mock=False)
    payload = {"request": "fraud-screening", "fail_times": 5}

    for attempt in (1, 2, 3):  # what Temporal's maximum_attempts=3 drives
        with pytest.raises(RuntimeError, match=f"attempt {attempt} of fail_times=5"):
            impl.run(dict(payload), ctx)

    assert [(r[0], r[2]) for r in _rows(sim_db)] == [("wfr-api-5", "attempt")] * 3


@pytest.mark.unit
def test_flaky_call_attempt_counts_are_scoped_per_run(sim_db: Path) -> None:
    """A fresh run starts at attempt 1 even with another run's rows present —
    the count is keyed by run_id, never global."""
    impl = _load_impl("flaky-call")
    payload = {"request": "currency-rates", "fail_times": 1}

    with pytest.raises(RuntimeError):
        impl.run(dict(payload), SimpleNamespace(run_id="wfr-a", mock=False))
    # A different run's first attempt must ALSO raise (count restarts at 1).
    with pytest.raises(RuntimeError, match="attempt 1 of fail_times=1"):
        impl.run(dict(payload), SimpleNamespace(run_id="wfr-b", mock=False))


@pytest.mark.unit
def test_sim_record_records_one_row(sim_db: Path) -> None:
    impl = _load_impl("sim-record")
    out = impl.run(
        {"request": "shipment-status", "provider": "fallback"},
        SimpleNamespace(run_id="wfr-rec-1", mock=False),
    )
    assert set(out) == {"record_result"}
    assert "fallback" in out["record_result"]
    assert "REC-" in out["record_result"]
    assert _rows(sim_db) == [
        (
            "wfr-rec-1",
            "external-api",
            "record",
            {"request": "shipment-status", "provider": "fallback"},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize("step", ["step1", "step3"])
def test_sim_step_records_the_parameterized_step(sim_db: Path, step: str) -> None:
    """ONE skill, two stages: the recorded action IS the input literal the
    workflow's ADR 097 D1 map injects."""
    impl = _load_impl("sim-step")
    out = impl.run(
        {"step": step, "request": "nightly-batch"},
        SimpleNamespace(run_id="wfr-pipe-1", mock=False),
    )
    assert set(out) == {"step_result"}
    assert f"Completed {step}" in out["step_result"]
    assert _rows(sim_db) == [("wfr-pipe-1", "pipeline", step, {"request": "nightly-batch"})]


@pytest.mark.unit
def test_sim_step_flaky_attempt_rows_and_single_success_row(sim_db: Path) -> None:
    """The recovery proof's second half: attempt rows accumulate per
    invocation, the step2 success row lands exactly once — on the
    invocation that completes."""
    impl = _load_impl("sim-step-flaky")
    ctx = SimpleNamespace(run_id="wfr-pipe-2", mock=False)
    payload = {"request": "invoice-export", "fail_times": 1}

    with pytest.raises(RuntimeError, match="attempt 1 of fail_times=1"):
        impl.run(dict(payload), ctx)
    out = impl.run(dict(payload), ctx)

    assert "attempt 2" in out["step_result"]
    assert [(r[2], r[3]) for r in _rows(sim_db)] == [
        ("step2_attempt", {"request": "invoice-export", "fail_times": 1}),
        ("step2_attempt", {"request": "invoice-export", "fail_times": 1}),
        ("step2", {"request": "invoice-export", "attempts": 2}),
    ]


@pytest.mark.unit
def test_sim_append_findings_payload_carries_the_increment(sim_db: Path) -> None:
    impl = _load_impl("sim-append-findings")
    out = impl.run(
        {
            "topic": "vector databases",
            "increment": 2,
            "findings": "f" * 300,  # excerpted, never the whole document
        },
        SimpleNamespace(run_id="wfr-res-2", mock=False),
    )
    assert set(out) == {"append_result"}
    assert "increment 2" in out["append_result"]
    assert "LOG-" in out["append_result"]
    ((run_id, system, action, payload),) = _rows(sim_db)
    assert (run_id, system, action) == ("wfr-res-2", "research", "append")
    assert payload["increment"] == 2
    assert payload["topic"] == "vector databases"
    assert len(payload["findings"]) == 200  # the audit-trail excerpt cap


@pytest.mark.unit
@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_skill_impl_mock_short_circuits_without_db_write(sim_db: Path, skill: str) -> None:
    """ctx.mock never touches the ledger — and for the flaky impls it never
    raises either (mdk run --mock stays hermetic AND green)."""
    impl = _load_impl(skill)
    out = impl.run(
        {
            "request": "x",
            "step": "step1",
            "topic": "x",
            "increment": 1,
            "fail_times": 5,  # would raise for the flaky impls if not mocked
        },
        SimpleNamespace(run_id="wfr-mock", mock=True),
    )
    assert out  # a schema-shaped stub came back
    assert not sim_db.exists()  # the ledger was never touched


@pytest.mark.unit
@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_skill_impl_run_id_input_overrides_ctx(sim_db: Path, skill: str) -> None:
    """An explicit run_id in the input wins (the harness facades' convention);
    ctx.run_id is the fallback the tool node actually exercises."""
    impl = _load_impl(skill)
    impl.run(
        {
            "request": "x",
            "step": "step1",
            "topic": "x",
            "increment": 1,
            "run_id": "wfr-explicit",
        },
        SimpleNamespace(run_id="wfr-ctx", mock=False),
    )
    assert {r[0] for r in _rows(sim_db)} == {"wfr-explicit"}


@pytest.mark.unit
@pytest.mark.parametrize("skill", ["sim-record", "sim-step", "sim-append-findings"])
def test_skill_impl_reference_is_stable_per_run(sim_db: Path, skill: str) -> None:
    """A Temporal activity retry must confirm the SAME synthetic reference."""
    impl = _load_impl(skill)
    ctx = SimpleNamespace(run_id="wfr-retry", mock=False)
    payload = {"request": "x", "step": "step1", "topic": "x", "increment": 1}
    assert impl.run(dict(payload), ctx) == impl.run(dict(payload), ctx)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("skill", "out_key", "prefix"),
    [("flaky-call", "api_result", "API-"), ("sim-step-flaky", "step_result", "STEP-")],
)
def test_flaky_impl_references_stay_stable_across_attempts(
    sim_db: Path, skill: str, out_key: str, prefix: str
) -> None:
    """The reference embeds only (run, request) — a later attempt in the same
    run confirms the SAME id, never a freshly-minted one."""
    impl = _load_impl(skill)
    ctx = SimpleNamespace(run_id="wfr-stable", mock=False)
    payload = {"request": "x", "fail_times": 0}

    first = re.search(rf"{prefix}[0-9A-F]{{6}}", impl.run(dict(payload), ctx)[out_key])
    second = re.search(rf"{prefix}[0-9A-F]{{6}}", impl.run(dict(payload), ctx)[out_key])
    assert first is not None and second is not None
    assert first.group() == second.group()


# ---------------------------------------------------------------------------
# 6. Anti-drift — each skill ships byte-identical across its three copies
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("skill", sorted(SKILLS))
@pytest.mark.parametrize(
    "rel", ["skill.yaml", "impl.py", "schema/input.json", "schema/output.json"]
)
def test_skill_files_identical_across_copies(skill: str, rel: str) -> None:
    deployable, scenario, template = SKILLS[skill]
    canonical = (deployable / rel).read_bytes()
    assert (scenario / rel).read_bytes() == canonical, f"{skill}: scenario copy of {rel} drifted"
    assert (template / rel).read_bytes() == canonical, f"{skill}: template copy of {rel} drifted"
