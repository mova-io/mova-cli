"""Certification suite driver — case-spec parsing, capability mapping, matrix
rendering, and the submit→poll→signal→facts flow against a mocked HTTP layer.

Hermetic: every HTTP request goes through an ``httpx.MockTransport`` (the
repo's webhook-worker pattern) — the live dev API is never touched. Polling
seams (``sleep``/timeouts) are overridden so the tests are instant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from certification.harness import cert_metrics, sim_systems
from certification.harness.driver import (
    CAPABILITIES,
    CapabilityOutcome,
    CaseExpect,
    CaseResult,
    CaseSpec,
    CaseSpecError,
    HitlStep,
    RuntimeApiClient,
    ScenarioSpec,
    SideEffectExpect,
    SuiteDriver,
    aggregate_matrix,
    load_scenario_spec,
    render_matrix,
    side_effects_db_configured,
    summary_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPENSE_CASES = REPO_ROOT / "certification" / "scenarios" / "expense-approval" / "cases.yaml"


# ---------------------------------------------------------------------------
# Fake runtime — a stateful MockTransport handler for one workflow run.
# ---------------------------------------------------------------------------


class FakeRuntime:
    """Simulates the runtime API: one job → one workflow run → pauses → fact."""

    def __init__(
        self,
        *,
        pause_nodes: list[str] | None = None,
        final_status: str = "success",
        route: str | None = None,
        final_state: dict[str, Any] | None = None,
        cost_usd: float = 0.0,
        governance_effect: str | None = None,
        job_status: str = "success",
        job_polls_before_terminal: int = 1,
        never_terminal: bool = False,
    ) -> None:
        self.pending_pauses = list(pause_nodes or [])
        self.final_status = final_status
        self.route = route
        self.final_state = final_state if final_state is not None else {"summary": "done"}
        self.cost_usd = cost_usd
        self.governance_effect = governance_effect
        self.job_status = job_status
        self.job_polls_before_terminal = job_polls_before_terminal
        self.never_terminal = never_terminal
        self.signals: list[dict[str, Any]] = []
        self.requests: list[str] = []
        self.submitted: list[dict[str, Any]] = []

    # -- handler ------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if request.method == "POST" and path == "/run":
            self.submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": "job-1", "status": "queued"})
        if request.method == "GET" and path == "/jobs/job-1":
            return self._job_view()
        if request.method == "GET" and path == "/api/v1/workflow-runs":
            if request.url.params.get("status") == "paused":
                return httpx.Response(200, json=self._paused_listing())
            return httpx.Response(200, json=self._run_listing())
        if request.method == "POST" and path == "/api/v1/workflow-runs/wfr-1/signal":
            self.signals.append(json.loads(request.content))
            if self.pending_pauses:
                self.pending_pauses.pop(0)
            return httpx.Response(202, json={"job_id": "job-2", "status": "queued"})
        if request.method == "GET" and path == "/api/v1/observability/facts":
            return httpx.Response(200, json=self._facts_listing())
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})

    # -- pieces ---------------------------------------------------------------

    def _job_view(self) -> httpx.Response:
        if self.job_polls_before_terminal > 0:
            self.job_polls_before_terminal -= 1
            return httpx.Response(200, json={"job_id": "job-1", "status": "running"})
        body: dict[str, Any] = {"job_id": "job-1", "status": self.job_status}
        if self.job_status == "success":
            body["result_run_id"] = "wfr-1"
        else:
            body["error"] = {"type": "NodeError", "message": "boom"}
        return httpx.Response(200, json=body)

    def _paused_listing(self) -> dict[str, Any]:
        if not self.pending_pauses:
            return {"workflow_runs": [], "count": 0}
        row = {
            "workflow_run_id": "wfr-1",
            "workflow": "expense-approval",
            "status": "paused",
            "paused_node_id": self.pending_pauses[0],
            "human_task": {"prompt": "Approve?", "output_contract": ["decision"]},
        }
        return {"workflow_runs": [row], "count": 1}

    def _run_listing(self) -> dict[str, Any]:
        row = {
            "workflow_run_id": "wfr-1",
            "workflow": "expense-approval",
            "status": self.final_status if not self.pending_pauses else "paused",
            "final_state": self.final_state,
        }
        return {"workflow_runs": [row], "count": 1}

    def _facts_listing(self) -> dict[str, Any]:
        terminal_blocked = bool(self.pending_pauses) or self.never_terminal
        status = "paused" if terminal_blocked else self.final_status
        fact = {
            "fact_id": "workflow_run:wfr-1",
            "kind": "workflow_run",
            "source_id": "wfr-1",
            "workflow": "expense-approval",
            "status": status,
            "runtime": "temporal",
            "route": self.route,
            "cost_usd": self.cost_usd,
            "governance_effect": self.governance_effect,
            "error_type": None,
        }
        return {"facts": [fact], "count": 1}


def _driver(fake: FakeRuntime, **kwargs: Any) -> SuiteDriver:
    client = RuntimeApiClient(
        "http://cert.test", "test-key", transport=httpx.MockTransport(fake.handler)
    )
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


def _spec(*cases: CaseSpec) -> ScenarioSpec:
    return ScenarioSpec(scenario="expense-approval", target="expense-approval", cases=cases)


def _case(
    name: str = "auto-tier",
    *,
    hitl: tuple[HitlStep, ...] = (),
    expect: CaseExpect | None = None,
) -> CaseSpec:
    return CaseSpec(
        name=name,
        input={"expense_text": "x", "amount": 50},
        hitl=hitl,
        expect=expect or CaseExpect(status="success", route=None),
    )


# ---------------------------------------------------------------------------
# Case-spec parsing — the shipped cases.yaml + validation failures
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_shipped_expense_cases_parse() -> None:
    spec = load_scenario_spec(EXPENSE_CASES)
    assert spec.scenario == "expense-approval"
    assert spec.target == "expense-approval"
    assert [c.name for c in spec.cases] == [
        "auto-tier",
        "manager-approve",
        "director-approve",
        "director-reject",
    ]
    auto, manager, director, reject = spec.cases
    assert auto.hitl == ()
    assert manager.hitl[0].node == "manager-approval"
    assert manager.hitl[0].decision == {"decision": "approve"}
    assert director.hitl[0].node == "director-approval"
    assert reject.hitl[0].decision == {"decision": "reject"}
    # All cases terminate SUCCESS (the rejected branch is a route, not an
    # error) and honestly expect route=None (nothing writes tier/route).
    assert all(c.expect.status == "success" for c in spec.cases)
    assert all(c.expect.route is None for c in spec.cases)
    # Cost is an honest skip everywhere (workflow_run facts carry 0 by design).
    assert all(c.expect.cost is False for c in spec.cases)
    # Governance: every case asserts the bundled policy's gates evaluated and
    # allowed (non-null effect == 'allow' on the terminal fact).
    assert all(c.expect.governance == "allow" for c in spec.cases)
    # The reject case is the only one with ledger expectations: no ERP post.
    assert reject.expect.no_side_effects == (SideEffectExpect(system="erp", action="submit"),)
    assert "erp_result" in reject.expect.final_state_lacks
    assert all("erp_result" in c.expect.final_state_has for c in (auto, manager, director))


def _write_cases(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cases.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("[]", "top level must be a mapping"),
        ("scenario: s\ntarget: t\ncases: []\n", "`cases` (non-empty list) required"),
        (
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    expect: {status: exploded}\n",
            "expect.status",
        ),
        (
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    expect: {status: success, routee: x}\n",
            "unknown key(s) ['routee']",
        ),
        (
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    hitl: [{node: gate}]\n    expect: {status: success}\n",
            "`decision` (non-empty mapping) is required",
        ),
        (
            "scenario: s\ntarget: t\ncases:\n"
            "  - {name: a, input: {}, expect: {status: success}}\n"
            "  - {name: a, input: {}, expect: {status: success}}\n",
            "duplicate case name",
        ),
        (
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    expect: {status: success, side_effects: [{system: erp}]}\n",
            "`action` (string) is required",
        ),
        (
            # `deny` is deliberately not an expectable effect: an enforced
            # deny never yields the terminal-success fact this case shape
            # asserts against.
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    expect: {status: success, governance: deny}\n",
            "expect.governance",
        ),
    ],
)
def test_case_spec_validation_errors(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(CaseSpecError) as exc_info:
        load_scenario_spec(_write_cases(tmp_path, body))
    message = str(exc_info.value)
    assert "cases.yaml" in message  # the error names the offending file
    assert fragment in message


# ---------------------------------------------------------------------------
# Matrix aggregation + rendering
# ---------------------------------------------------------------------------


def _result(name: str, **statuses: str) -> CaseResult:
    outcomes = {
        cap: CapabilityOutcome(statuses.get(cap.replace("-", "_"), "skip")) for cap in CAPABILITIES
    }
    return CaseResult(name=name, workflow_run_id="wfr-1", outcomes=outcomes)


@pytest.mark.unit
def test_aggregate_matrix_fail_dominates_then_pass_then_skip() -> None:
    results = [
        _result("a", durable_execution="pass", hitl="skip", cost="skip"),
        _result("b", durable_execution="pass", hitl="fail", cost="skip"),
    ]
    matrix = aggregate_matrix(results)
    assert matrix["durable-execution"] == "pass"
    assert matrix["hitl"] == "fail"
    assert matrix["cost"] == "skip"


@pytest.mark.unit
def test_render_matrix_contains_capabilities_and_verdicts() -> None:
    text = render_matrix(
        [("expense-approval", {cap: "pass" for cap in CAPABILITIES} | {"cost": "skip"})]
    )
    for cap in CAPABILITIES:
        assert cap in text
    assert "expense-approval" in text
    assert "PASS" in text
    assert "SKIP" in text


@pytest.mark.unit
def test_summary_json_shape_and_ok_flag() -> None:
    spec = _spec(_case())
    good = summary_json("dev", [(spec, [_result("auto-tier", durable_execution="pass")])])
    assert good["ok"] is True
    assert good["scenarios"][0]["matrix"]["durable-execution"] == "pass"
    assert good["scenarios"][0]["cases"][0]["name"] == "auto-tier"
    bad = summary_json("dev", [(spec, [_result("auto-tier", hitl="fail")])])
    assert bad["ok"] is False


# ---------------------------------------------------------------------------
# Driver flow — mocked HTTP
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_auto_tier_passes_durable_and_routing_skips_the_rest() -> None:
    fake = FakeRuntime(final_state={"erp_result": "posted", "summary": "ok"})
    case = _case(
        expect=CaseExpect(status="success", route=None, final_state_has=("erp_result", "summary"))
    )
    result = _driver(fake).run_case(_spec(case), case)
    assert result.workflow_run_id == "wfr-1"
    assert result.outcomes["durable-execution"].status == "pass"
    assert result.outcomes["decision-routing"].status == "pass"
    assert result.outcomes["hitl"].status == "skip"
    assert result.outcomes["cost"].status == "skip"
    assert result.outcomes["governance"].status == "skip"  # not declared on this case
    assert result.outcomes["side-effects"].status == "skip"
    assert not result.failed
    assert fake.signals == []


@pytest.mark.unit
def test_submit_stamps_certification_provenance_into_input() -> None:
    # The driver merges a `certification: {case, scenario}` marker into the
    # input at SUBMIT time (cases.yaml stays clean) so test traffic is
    # identifiable in the Temporal UI / workflow_runs.initial_state / Langfuse.
    fake = FakeRuntime(final_state={"erp_result": "posted", "summary": "ok"})
    case = _case()
    _driver(fake).run_case(_spec(case), case)
    assert len(fake.submitted) == 1
    body = fake.submitted[0]
    assert body["kind"] == "workflow"
    assert body["target"] == "expense-approval"
    # Original case input is intact, the marker is additive.
    assert body["input"]["expense_text"] == "x"
    assert body["input"]["amount"] == 50
    assert body["input"]["certification"] == {
        "case": "auto-tier",
        "scenario": "expense-approval",
    }
    # The case spec itself is NOT mutated by stamping.
    assert "certification" not in case.input


@pytest.mark.unit
def test_hitl_case_observes_pause_signals_and_completes() -> None:
    fake = FakeRuntime(
        pause_nodes=["manager-approval"],
        final_state={"erp_result": "posted", "summary": "ok", "decision": "approve"},
    )
    case = _case(
        "manager-approve",
        hitl=(HitlStep(node="manager-approval", decision={"decision": "approve"}),),
        expect=CaseExpect(status="success", route=None, final_state_has=("erp_result",)),
    )
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["hitl"].status == "pass"
    assert "manager-approval" in result.outcomes["hitl"].note
    assert result.outcomes["durable-execution"].status == "pass"
    assert fake.signals == [{"decision": {"decision": "approve"}}]


@pytest.mark.unit
def test_pause_at_wrong_node_fails_hitl_and_short_circuits() -> None:
    fake = FakeRuntime(pause_nodes=["director-approval"])
    case = _case(
        "manager-approve",
        hitl=(HitlStep(node="manager-approval", decision={"decision": "approve"}),),
    )
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["hitl"].status == "fail"
    assert "director-approval" in result.outcomes["hitl"].note
    assert result.outcomes["durable-execution"].status == "fail"
    assert "HITL phase failed" in result.outcomes["durable-execution"].note
    assert result.outcomes["decision-routing"].status == "skip"
    assert fake.signals == []  # never signalled the wrong gate


@pytest.mark.unit
def test_job_error_fails_durable_execution_and_skips_the_rest() -> None:
    fake = FakeRuntime(job_status="error")
    case = _case()
    result = _driver(fake).run_case(_spec(case), case)
    assert result.workflow_run_id is None
    assert result.outcomes["durable-execution"].status == "fail"
    assert "boom" in result.outcomes["durable-execution"].note
    for cap in ("decision-routing", "hitl", "cost", "governance", "side-effects"):
        assert result.outcomes[cap].status == "skip"


@pytest.mark.unit
def test_fact_never_terminal_times_out_as_durable_failure() -> None:
    fake = FakeRuntime(never_terminal=True)
    case = _case()
    result = _driver(fake, fact_timeout_s=0.0).run_case(_spec(case), case)
    assert result.outcomes["durable-execution"].status == "fail"
    assert "timed out" in result.outcomes["durable-execution"].note


@pytest.mark.unit
def test_unexpected_terminal_status_fails_durable_execution() -> None:
    fake = FakeRuntime(final_status="error")
    case = _case()  # expects success
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["durable-execution"].status == "fail"
    assert "status='error'" in result.outcomes["durable-execution"].note


@pytest.mark.unit
def test_route_mismatch_fails_decision_routing() -> None:
    fake = FakeRuntime(route="manager")
    case = _case()  # expects route=None
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["decision-routing"].status == "fail"
    assert "route='manager'" in result.outcomes["decision-routing"].note


@pytest.mark.unit
def test_final_state_lacks_marker_fails_routing_when_branch_leaked() -> None:
    # Reject case, but the fake run's final_state shows the ERP DID post.
    fake = FakeRuntime(
        pause_nodes=["director-approval"],
        final_state={"erp_result": "posted", "summary": "ok"},
    )
    case = _case(
        "director-reject",
        hitl=(HitlStep(node="director-approval", decision={"decision": "reject"}),),
        expect=CaseExpect(status="success", route=None, final_state_lacks=("erp_result",)),
    )
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["hitl"].status == "pass"
    assert result.outcomes["decision-routing"].status == "fail"
    assert "erp_result" in result.outcomes["decision-routing"].note


@pytest.mark.unit
def test_cost_expectation_passes_and_fails_on_fact_cost_usd() -> None:
    case = _case(expect=CaseExpect(status="success", route=None, cost=True))
    zero = _driver(FakeRuntime(cost_usd=0.0)).run_case(_spec(case), case)
    assert zero.outcomes["cost"].status == "fail"
    spent = _driver(FakeRuntime(cost_usd=0.0042)).run_case(_spec(case), case)
    assert spent.outcomes["cost"].status == "pass"


# ---------------------------------------------------------------------------
# Governance capability — fact.governance_effect (ADR 096)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_governance_passes_when_effect_matches_expectation() -> None:
    case = _case(expect=CaseExpect(status="success", route=None, governance="allow"))
    result = _driver(FakeRuntime(governance_effect="allow")).run_case(_spec(case), case)
    assert result.outcomes["governance"].status == "pass"
    assert "governance_effect=allow" in result.outcomes["governance"].note

    warn_case = _case(expect=CaseExpect(status="success", route=None, governance="warn"))
    warned = _driver(FakeRuntime(governance_effect="warn")).run_case(_spec(warn_case), warn_case)
    assert warned.outcomes["governance"].status == "pass"


@pytest.mark.unit
def test_governance_fails_on_null_effect_with_wiring_hint() -> None:
    # NULL means NO gate evaluated — the deployed worker never loaded the
    # bundled policy. That is a distinct failure from a wrong effect, and the
    # note must point at the wiring.
    case = _case(expect=CaseExpect(status="success", route=None, governance="allow"))
    result = _driver(FakeRuntime(governance_effect=None)).run_case(_spec(case), case)
    assert result.outcomes["governance"].status == "fail"
    assert "null" in result.outcomes["governance"].note
    assert "policy" in result.outcomes["governance"].note


@pytest.mark.unit
def test_governance_fails_on_effect_mismatch() -> None:
    case = _case(expect=CaseExpect(status="success", route=None, governance="allow"))
    result = _driver(FakeRuntime(governance_effect="warn")).run_case(_spec(case), case)
    assert result.outcomes["governance"].status == "fail"
    assert "expected 'allow'" in result.outcomes["governance"].note


@pytest.mark.unit
def test_governance_skips_when_case_does_not_declare_it() -> None:
    case = _case()  # expect.governance is None
    result = _driver(FakeRuntime(governance_effect="allow")).run_case(_spec(case), case)
    assert result.outcomes["governance"].status == "skip"
    assert "does not assert governance" in result.outcomes["governance"].note


# ---------------------------------------------------------------------------
# Side-effects gating + ledger assertions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_side_effects_skip_when_shared_db_not_configured() -> None:
    case = _case(
        expect=CaseExpect(
            status="success",
            route=None,
            no_side_effects=(SideEffectExpect(system="erp", action="submit"),),
        )
    )
    result = _driver(FakeRuntime(), side_effects_enabled=False).run_case(_spec(case), case)
    assert result.outcomes["side-effects"].status == "skip"
    assert "MOVATE_PG_URL" in result.outcomes["side-effects"].note


@pytest.mark.unit
def test_side_effects_db_configured_reads_env() -> None:
    assert side_effects_db_configured({}) is False
    assert side_effects_db_configured({"MOVATE_PG_URL": "  "}) is False
    assert side_effects_db_configured({"MOVATE_PG_URL": "postgres://x"}) is True
    assert side_effects_db_configured({"MOVATE_DB_URL": "postgres://y"}) is True


@pytest.mark.unit
def test_side_effects_assert_against_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sim_systems, "read", lambda run_id=None: [e for e in ledger if e["run_id"] == run_id]
    )
    case = _case(
        expect=CaseExpect(
            status="success",
            route=None,
            no_side_effects=(SideEffectExpect(system="erp", action="submit"),),
        )
    )
    clean = _driver(FakeRuntime(), side_effects_enabled=True).run_case(_spec(case), case)
    assert clean.outcomes["side-effects"].status == "pass"

    ledger.append({"run_id": "wfr-1", "system": "erp", "action": "submit", "payload": {}})
    dirty = _driver(FakeRuntime(), side_effects_enabled=True).run_case(_spec(case), case)
    assert dirty.outcomes["side-effects"].status == "fail"
    assert "erp.submit" in dirty.outcomes["side-effects"].note


# ---------------------------------------------------------------------------
# Assert-to-capability metric mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_capability_outcomes_emit_certification_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, str, str]] = []

    def _capture(*, scenario: str, capability: str, status: str) -> None:
        emitted.append((scenario, capability, status))

    monkeypatch.setattr(cert_metrics, "record_certification_result", _capture)
    fake = FakeRuntime(
        pause_nodes=["manager-approval"],
        final_state={"erp_result": "posted", "summary": "ok"},
    )
    case = _case(
        "manager-approve",
        hitl=(HitlStep(node="manager-approval", decision={"decision": "approve"}),),
        expect=CaseExpect(status="success", route=None, final_state_has=("erp_result",)),
    )
    _driver(fake).run_case(_spec(case), case)
    assert ("expense-approval", "hitl", "pass") in emitted
    assert ("expense-approval", "durable-execution", "pass") in emitted
    assert ("expense-approval", "decision-routing", "pass") in emitted
    # Skipped capabilities (cost / governance / side-effects here) emit
    # NOTHING — skip is a local-matrix verdict, never a green or red
    # datapoint on the dashboard.
    assert not any(cap in ("cost", "governance", "side-effects") for _, cap, _ in emitted)


@pytest.mark.unit
def test_launch_failure_emits_durable_execution_fail_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, str, str]] = []

    def _capture(*, scenario: str, capability: str, status: str) -> None:
        emitted.append((scenario, capability, status))

    monkeypatch.setattr(cert_metrics, "record_certification_result", _capture)
    case = _case()
    _driver(FakeRuntime(job_status="error")).run_case(_spec(case), case)
    assert emitted == [("expense-approval", "durable-execution", "fail")]


# ---------------------------------------------------------------------------
# final_state_contains / final_state_omits — the B2 value-level markers
# (additive driver extension; key-presence behavior above is untouched)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_final_state_contains_and_omits_parse_through_loader(tmp_path: Path) -> None:
    spec = load_scenario_spec(
        _write_cases(
            tmp_path,
            "scenario: pii\ntarget: pii\ncases:\n"
            "  - name: a\n    input: {document: x}\n"
            "    expect:\n      status: success\n"
            "      final_state_contains: {redacted_text: ['[EMAIL]', '[SSN]']}\n"
            "      final_state_omits: {redacted_text: ['jane@example.com']}\n",
        )
    )
    expect = spec.cases[0].expect
    assert expect.final_state_contains == (("redacted_text", ("[EMAIL]", "[SSN]")),)
    assert expect.final_state_omits == (("redacted_text", ("jane@example.com",)),)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    expect: {status: success, final_state_contains: [x]}\n",
            "must be a mapping of state key",
        ),
        (
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    expect: {status: success, final_state_contains: {k: []}}\n",
            "NON-EMPTY list",
        ),
        (
            "scenario: s\ntarget: t\ncases:\n  - name: a\n    input: {}\n"
            "    expect: {status: success, final_state_omits: {k: [3]}}\n",
            "every substring must be a non-empty string",
        ),
    ],
)
def test_final_state_substring_spec_validation_errors(
    tmp_path: Path, body: str, fragment: str
) -> None:
    with pytest.raises(CaseSpecError, match=fragment):
        load_scenario_spec(_write_cases(tmp_path, body))


def _contains_case(
    contains: tuple[tuple[str, tuple[str, ...]], ...] = (),
    omits: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> CaseSpec:
    return _case(
        "pii-document",
        expect=CaseExpect(
            status="success",
            route=None,
            final_state_contains=contains,
            final_state_omits=omits,
        ),
    )


@pytest.mark.unit
def test_final_state_contains_passes_on_masked_tokens() -> None:
    fake = FakeRuntime(final_state={"redacted_text": "Reach me at [EMAIL]; SSN [SSN]."})
    case = _contains_case(
        contains=(("redacted_text", ("[EMAIL]", "[SSN]")),),
        omits=(("redacted_text", ("jane.doe@example.com", "123-45-6789")),),
    )
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["decision-routing"].status == "pass"
    assert "4 value-level marker(s) verified" in result.outcomes["decision-routing"].note


@pytest.mark.unit
def test_final_state_contains_fails_when_token_missing() -> None:
    fake = FakeRuntime(final_state={"redacted_text": "Reach me at [EMAIL] only."})
    case = _contains_case(contains=(("redacted_text", ("[EMAIL]", "[SSN]")),))
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["decision-routing"].status == "fail"
    assert "[SSN]" in result.outcomes["decision-routing"].note


@pytest.mark.unit
def test_final_state_contains_fails_when_key_absent() -> None:
    fake = FakeRuntime(final_state={"summary": "done"})
    case = _contains_case(contains=(("redacted_text", ("[EMAIL]",)),))
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["decision-routing"].status == "fail"
    assert "lacks 'redacted_text'" in result.outcomes["decision-routing"].note


@pytest.mark.unit
def test_final_state_omits_fails_when_raw_value_survives() -> None:
    fake = FakeRuntime(final_state={"redacted_text": "Reach me at jane.doe@example.com."})
    case = _contains_case(omits=(("redacted_text", ("jane.doe@example.com",)),))
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["decision-routing"].status == "fail"
    assert "jane.doe@example.com" in result.outcomes["decision-routing"].note


@pytest.mark.unit
def test_final_state_omits_trivially_passes_when_key_absent() -> None:
    # Absence is final_state_lacks's job; omits must not double-fail it.
    fake = FakeRuntime(final_state={"summary": "done"})
    case = _contains_case(omits=(("redacted_text", ("123-45-6789",)),))
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["decision-routing"].status == "pass"


@pytest.mark.unit
def test_final_state_contains_stringifies_non_string_values() -> None:
    # A non-string state value is JSON-stringified before the substring check
    # (deterministic, sorted keys — no repr quoting surprises).
    fake = FakeRuntime(final_state={"provision": {"ref": "ITSM-1", "ok": True}})
    case = _contains_case(contains=(("provision", ('"ref": "ITSM-1"',)),))
    result = _driver(fake).run_case(_spec(case), case)
    assert result.outcomes["decision-routing"].status == "pass"
