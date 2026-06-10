"""Certification suite driver — run scenario cases against a deployed runtime.

The driver turns each scenario's declarative ``cases.yaml`` into end-to-end
runs of the **deployed** workflow over the public runtime API, then asserts
per *platform capability* and emits each outcome through
:func:`certification.harness.cert_metrics.certify` (the
``mdk.certification.scenario`` metric behind the Grafana matrix).

Flow per case (the ``dev`` target):

1. ``POST /run {kind: workflow, target, input}`` → poll ``GET /jobs/{id}``
   until terminal. A workflow that pauses at a HUMAN gate completes its job
   segment as ``success`` with ``result_run_id`` = the workflow_run_id (the
   durable handle for everything below).
2. For each declared ``hitl`` step: poll ``GET /api/v1/workflow-runs
   ?status=paused`` until the run shows up (the single-run GET is not used —
   it 404s on the deployed runtime), assert it paused at the *expected* node,
   then ``POST /api/v1/workflow-runs/{id}/signal``.
3. Poll ``GET /api/v1/observability/facts?kind=workflow_run`` (ADR 096 — the
   one integration surface, deliberately dogfooded as the primary read-back)
   until a terminal fact for the run appears, then assert status / route /
   final-state markers / cost / sim-ledger side-effects per the case spec.

Capabilities (one ``certify`` block each, per case):

* ``durable-execution`` — a terminal fact with the expected status appeared.
* ``decision-routing`` — the fact's ``route`` matches the expectation
  (honestly ``null`` for workflows whose state never carries ``tier``/
  ``route``) plus optional ``final_state_has``/``final_state_lacks`` markers
  read from the workflow-runs list (e.g. ``erp_result`` present only on
  approve paths).
* ``hitl`` — every expected pause was observed *at the expected node* and the
  signal resumed it.
* ``cost`` — ``fact.cost_usd > 0``; only when the case opts in (workflow_run
  facts carry 0 by design — ADR 096 — and the Temporal path emits no
  per-node run facts yet, so scenario specs mark this honest skip).
* ``side-effects`` — sim-ledger expectations via
  :mod:`certification.harness.asserts`; evaluated ONLY when the shared DB is
  reachable (``MOVATE_PG_URL``/``MOVATE_DB_URL``), SKIP otherwise.

The printed matrix + exit code are the local source of truth; the metric only
leaves the process when an OTLP sink is configured (in-env runs).
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
import yaml

from certification.harness import asserts, cert_metrics

CAPABILITIES: tuple[str, ...] = (
    "durable-execution",
    "decision-routing",
    "hitl",
    "cost",
    "side-effects",
)

_TERMINAL_JOB_STATUSES = frozenset({"success", "error", "safety_blocked", "dead_letter"})
_EXPECTED_STATUSES = frozenset({"success", "error", "paused"})

_T = TypeVar("_T")


class CaseSpecError(ValueError):
    """A scenario's ``cases.yaml`` is malformed — message says where and why."""


# ---------------------------------------------------------------------------
# Case spec — the parsed shape of a scenario's cases.yaml.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SideEffectExpect:
    """One sim-ledger expectation: ``{system, action, times}``."""

    system: str
    action: str | None = None
    times: int | None = None


@dataclass(frozen=True)
class HitlStep:
    """One expected HUMAN pause: the node id + the decision to signal."""

    node: str
    decision: dict[str, Any]


@dataclass(frozen=True)
class CaseExpect:
    """What the terminal fact (+ ledger) must show for one case."""

    status: str
    route: str | None = None
    cost: bool = False
    final_state_has: tuple[str, ...] = ()
    final_state_lacks: tuple[str, ...] = ()
    side_effects: tuple[SideEffectExpect, ...] = ()
    no_side_effects: tuple[SideEffectExpect, ...] = ()


@dataclass(frozen=True)
class CaseSpec:
    """One certification case: input → (optional pauses) → expectations."""

    name: str
    input: dict[str, Any]
    expect: CaseExpect
    hitl: tuple[HitlStep, ...] = ()


@dataclass(frozen=True)
class ScenarioSpec:
    """A scenario's full case file: the deploy target + its cases."""

    scenario: str
    target: str
    cases: tuple[CaseSpec, ...]


def _require(condition: bool, where: str, message: str) -> None:
    if not condition:
        raise CaseSpecError(f"{where}: {message}")


def _check_keys(raw: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    _require(not unknown, where, f"unknown key(s) {unknown}; allowed: {sorted(allowed)}")


_CASE_KEYS = frozenset({"name", "input", "hitl", "expect"})
_EXPECT_KEYS = frozenset(
    {
        "status",
        "route",
        "cost",
        "final_state_has",
        "final_state_lacks",
        "side_effects",
        "no_side_effects",
    }
)
_HITL_KEYS = frozenset({"node", "decision"})
_EFFECT_KEYS = frozenset({"system", "action", "times"})


def _parse_effects(raw: Any, where: str, *, action_required: bool) -> tuple[SideEffectExpect, ...]:
    _require(isinstance(raw, list), where, "must be a list")
    out: list[SideEffectExpect] = []
    for i, item in enumerate(raw):
        w = f"{where}[{i}]"
        _require(isinstance(item, dict), w, "must be a mapping")
        _check_keys(item, _EFFECT_KEYS, w)
        system = item.get("system")
        _require(isinstance(system, str) and bool(system), w, "`system` (string) is required")
        action = item.get("action")
        if action_required:
            _require(isinstance(action, str) and bool(action), w, "`action` (string) is required")
        else:
            _require(action is None or isinstance(action, str), w, "`action` must be a string")
        times = item.get("times")
        _require(
            times is None or (isinstance(times, int) and not isinstance(times, bool)),
            w,
            "`times` must be an integer",
        )
        out.append(SideEffectExpect(system=str(system), action=action, times=times))
    return tuple(out)


def _parse_str_list(raw: Any, where: str) -> tuple[str, ...]:
    _require(
        isinstance(raw, list) and all(isinstance(s, str) for s in raw),
        where,
        "must be a list of strings",
    )
    return tuple(raw)


def _parse_expect(raw: Any, where: str) -> CaseExpect:
    _require(isinstance(raw, dict), where, "`expect` must be a mapping")
    _check_keys(raw, _EXPECT_KEYS, where)
    status = raw.get("status")
    _require(
        isinstance(status, str) and status in _EXPECTED_STATUSES,
        f"{where}.status",
        f"required; one of {sorted(_EXPECTED_STATUSES)}",
    )
    route = raw.get("route")
    _require(route is None or isinstance(route, str), f"{where}.route", "must be a string or null")
    cost = raw.get("cost", False)
    _require(isinstance(cost, bool), f"{where}.cost", "must be a boolean")
    return CaseExpect(
        status=str(status),
        route=route,
        cost=cost,
        final_state_has=_parse_str_list(raw.get("final_state_has", []), f"{where}.final_state_has"),
        final_state_lacks=_parse_str_list(
            raw.get("final_state_lacks", []), f"{where}.final_state_lacks"
        ),
        side_effects=_parse_effects(
            raw.get("side_effects", []), f"{where}.side_effects", action_required=True
        ),
        no_side_effects=_parse_effects(
            raw.get("no_side_effects", []), f"{where}.no_side_effects", action_required=False
        ),
    )


def _parse_hitl(raw: Any, where: str) -> tuple[HitlStep, ...]:
    _require(isinstance(raw, list), where, "`hitl` must be a list")
    steps: list[HitlStep] = []
    for i, item in enumerate(raw):
        w = f"{where}[{i}]"
        _require(isinstance(item, dict), w, "must be a mapping")
        _check_keys(item, _HITL_KEYS, w)
        node = item.get("node")
        _require(isinstance(node, str) and bool(node), w, "`node` (string) is required")
        decision = item.get("decision")
        _require(
            isinstance(decision, dict) and bool(decision),
            w,
            "`decision` (non-empty mapping) is required",
        )
        steps.append(HitlStep(node=str(node), decision=dict(decision)))
    return tuple(steps)


def load_scenario_spec(path: Path) -> ScenarioSpec:
    """Parse + validate one scenario's ``cases.yaml`` (raises :class:`CaseSpecError`)."""
    where = str(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough message
        raise CaseSpecError(f"{where}: invalid YAML: {exc}") from exc
    _require(isinstance(raw, dict), where, "top level must be a mapping")
    scenario = raw.get("scenario")
    _require(isinstance(scenario, str) and bool(scenario), where, "`scenario` (string) is required")
    target = raw.get("target")
    _require(isinstance(target, str) and bool(target), where, "`target` (string) is required")
    _check_keys(raw, frozenset({"scenario", "target", "cases"}), where)
    raw_cases = raw.get("cases")
    _require(
        isinstance(raw_cases, list) and bool(raw_cases), where, "`cases` (non-empty list) required"
    )
    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_cases):
        w = f"{where}: cases[{i}]"
        _require(isinstance(item, dict), w, "must be a mapping")
        _check_keys(item, _CASE_KEYS, w)
        name = item.get("name")
        _require(isinstance(name, str) and bool(name), w, "`name` (string) is required")
        _require(name not in seen, w, f"duplicate case name {name!r}")
        seen.add(str(name))
        case_input = item.get("input")
        _require(isinstance(case_input, dict), w, "`input` (mapping) is required")
        cases.append(
            CaseSpec(
                name=str(name),
                input=dict(case_input),
                hitl=_parse_hitl(item.get("hitl", []), f"{w}.hitl"),
                expect=_parse_expect(item.get("expect"), f"{w}.expect"),
            )
        )
    return ScenarioSpec(scenario=str(scenario), target=str(target), cases=tuple(cases))


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityOutcome:
    """One capability's verdict for one case: pass / fail / skip + a note."""

    status: str  # "pass" | "fail" | "skip"
    note: str = ""


@dataclass
class CaseResult:
    """All capability verdicts for one executed case."""

    name: str
    workflow_run_id: str | None
    outcomes: dict[str, CapabilityOutcome] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return any(o.status == "fail" for o in self.outcomes.values())


def side_effects_db_configured(env: Mapping[str, str] | None = None) -> bool:
    """Whether the *shared* certification DB is reachable from this process.

    The sim ledger (:mod:`certification.harness.sim_systems`) lives in the
    SAME Postgres the deployed runtime writes — only the ``MOVATE_PG_URL`` /
    ``MOVATE_DB_URL`` DSNs reach it. A local SQLite fallback would be a
    *different, empty* ledger, so it never counts.
    """
    e = os.environ if env is None else env
    return any(str(e.get(var, "")).strip() for var in ("MOVATE_PG_URL", "MOVATE_DB_URL"))


# ---------------------------------------------------------------------------
# HTTP client — thin wrapper over the public runtime API.
# ---------------------------------------------------------------------------


class RuntimeApiClient:
    """Bearer-authenticated client for the deployed runtime's public API.

    ``transport`` is the test seam (``httpx.MockTransport``) — mirrors the
    repo's webhook-worker test pattern, so unit tests never touch the network.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RuntimeApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def submit_workflow(self, target: str, input_state: dict[str, Any]) -> str:
        """``POST /run`` → the queued job id."""
        resp = self._http.post(
            "/run", json={"kind": "workflow", "target": target, "input": input_state}
        )
        resp.raise_for_status()
        return str(resp.json()["job_id"])

    def get_job(self, job_id: str) -> dict[str, Any]:
        """``GET /jobs/{id}`` → the job view."""
        resp = self._http.get(f"/jobs/{job_id}")
        resp.raise_for_status()
        return dict(resp.json())

    def list_paused_runs(self) -> list[dict[str, Any]]:
        """``GET /api/v1/workflow-runs?status=paused`` — the HITL queue."""
        resp = self._http.get("/api/v1/workflow-runs", params={"status": "paused", "limit": 100})
        resp.raise_for_status()
        return list(resp.json().get("workflow_runs", []))

    def find_workflow_run(self, workflow_run_id: str) -> dict[str, Any] | None:
        """Locate one run via the LIST endpoint (the single-run GET 404s on
        the deployed runtime, so it is deliberately not used)."""
        resp = self._http.get("/api/v1/workflow-runs", params={"limit": 100})
        resp.raise_for_status()
        for row in resp.json().get("workflow_runs", []):
            if row.get("workflow_run_id") == workflow_run_id:
                return dict(row)
        return None

    def signal(self, workflow_run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/v1/workflow-runs/{id}/signal`` — resume a paused run."""
        resp = self._http.post(
            f"/api/v1/workflow-runs/{workflow_run_id}/signal", json={"decision": decision}
        )
        resp.raise_for_status()
        return dict(resp.json())

    def list_workflow_run_facts(self, *, workflow: str | None = None) -> list[dict[str, Any]]:
        """``GET /api/v1/observability/facts?kind=workflow_run`` (ADR 096)."""
        params: dict[str, Any] = {"kind": "workflow_run", "limit": 200}
        if workflow:
            params["workflow"] = workflow
        resp = self._http.get("/api/v1/observability/facts", params=params)
        resp.raise_for_status()
        return list(resp.json().get("facts", []))


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


class SuiteDriver:
    """Run a scenario's cases against the runtime API and certify capabilities.

    ``sleep``/``clock`` are injection seams so unit tests poll instantly;
    ``side_effects_enabled=None`` defers to :func:`side_effects_db_configured`.
    """

    def __init__(
        self,
        client: RuntimeApiClient,
        *,
        poll_interval_s: float = 2.0,
        job_timeout_s: float = 120.0,
        pause_timeout_s: float = 120.0,
        fact_timeout_s: float = 180.0,
        side_effects_enabled: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._poll_interval_s = poll_interval_s
        self._job_timeout_s = job_timeout_s
        self._pause_timeout_s = pause_timeout_s
        self._fact_timeout_s = fact_timeout_s
        self._side_effects_enabled = (
            side_effects_db_configured() if side_effects_enabled is None else side_effects_enabled
        )
        self._sleep = sleep
        self._clock = clock

    # ----------------------------------------------------------------- polling

    def _poll(self, timeout_s: float, attempt: Callable[[], _T | None], what: str) -> _T:
        """Re-run ``attempt`` until it returns a value; AssertionError on timeout."""
        deadline = self._clock() + timeout_s
        while True:
            found = attempt()
            if found is not None:
                return found
            if self._clock() >= deadline:
                raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what}")
            self._sleep(self._poll_interval_s)

    def _terminal_job(self, job_id: str) -> dict[str, Any] | None:
        job = self._client.get_job(job_id)
        return job if str(job.get("status")) in _TERMINAL_JOB_STATUSES else None

    def _paused_row(
        self, workflow_run_id: str, *, exclude_node: str | None
    ) -> dict[str, Any] | None:
        for row in self._client.list_paused_runs():
            if row.get("workflow_run_id") != workflow_run_id:
                continue
            if exclude_node is not None and row.get("paused_node_id") == exclude_node:
                continue  # stale listing of the gate we already signalled
            return row
        return None

    def _terminal_fact(self, target: str, workflow_run_id: str) -> dict[str, Any] | None:
        for fact in self._client.list_workflow_run_facts(workflow=target):
            if fact.get("source_id") == workflow_run_id and fact.get("status") != "paused":
                return fact
        return None

    # ------------------------------------------------------------- certifying

    @staticmethod
    def _certified(
        scenario: str, capability: str, fn: Callable[[], str | None]
    ) -> CapabilityOutcome:
        """Run one capability assertion under ``cert_metrics.certify``."""
        try:
            with cert_metrics.certify(scenario, capability):
                note = fn()
        except AssertionError as exc:
            return CapabilityOutcome("fail", str(exc))
        except httpx.HTTPError as exc:
            return CapabilityOutcome("fail", f"HTTP error: {exc}")
        return CapabilityOutcome("pass", note or "")

    # ------------------------------------------------------------- case phases

    def _launch(self, spec: ScenarioSpec, case: CaseSpec) -> str:
        """Submit the case; poll the job to terminal; return the workflow_run_id."""
        job_id = self._client.submit_workflow(spec.target, case.input)
        job = self._poll(
            self._job_timeout_s,
            lambda: self._terminal_job(job_id),
            f"job {job_id} to reach a terminal status",
        )
        status = str(job.get("status"))
        assert status == "success", (
            f"job {job_id} ended {status!r} (error={job.get('error')}) — "
            "the workflow never started or its first segment failed"
        )
        run_id = job.get("result_run_id")
        assert run_id, f"job {job_id} succeeded but carried no result_run_id"
        return str(run_id)

    def _drive_hitl(self, spec: ScenarioSpec, case: CaseSpec, workflow_run_id: str) -> str:
        """Observe each expected pause at its node, signal the decision."""
        notes: list[str] = []
        previous_node: str | None = None
        for step in case.hitl:
            row = self._poll(
                self._pause_timeout_s,
                functools.partial(self._paused_row, workflow_run_id, exclude_node=previous_node),
                f"run {workflow_run_id} to pause at {step.node!r}",
            )
            paused_node = row.get("paused_node_id")
            assert paused_node == step.node, (
                f"run {workflow_run_id} paused at {paused_node!r}, expected {step.node!r}"
            )
            self._client.signal(workflow_run_id, step.decision)
            notes.append(f"paused@{step.node} → signalled {step.decision}")
            previous_node = step.node
        return "; ".join(notes)

    def _assert_routing(
        self, case: CaseSpec, fact: dict[str, Any], workflow_run_id: str
    ) -> str | None:
        expected, actual = case.expect.route, fact.get("route")
        assert actual == expected, f"fact.route={actual!r}, expected {expected!r}"
        notes: list[str] = [f"route={actual!r}"]
        if case.expect.final_state_has or case.expect.final_state_lacks:
            row = self._client.find_workflow_run(workflow_run_id)
            assert row is not None, (
                f"run {workflow_run_id} not found in the workflow-runs list "
                "(needed for final_state markers)"
            )
            final_state = row.get("final_state") or {}
            for key in case.expect.final_state_has:
                assert key in final_state, (
                    f"final_state lacks {key!r} — the expected branch did not run "
                    f"(keys: {sorted(final_state)})"
                )
            for key in case.expect.final_state_lacks:
                assert key not in final_state, (
                    f"final_state unexpectedly contains {key!r} — a branch that "
                    "must not run did (e.g. ERP posted on a rejected expense)"
                )
            notes.append(
                f"final_state has {list(case.expect.final_state_has)}, "
                f"lacks {list(case.expect.final_state_lacks)}"
            )
        return "; ".join(notes)

    def _assert_side_effects(self, case: CaseSpec, workflow_run_id: str) -> str | None:
        for eff in case.expect.side_effects:
            asserts.assert_side_effect(
                workflow_run_id, eff.system, eff.action or "", times=eff.times
            )
        for eff in case.expect.no_side_effects:
            asserts.assert_no_side_effect(workflow_run_id, eff.system, eff.action)
        return (
            f"{len(case.expect.side_effects)} expected / "
            f"{len(case.expect.no_side_effects)} forbidden ledger entries verified"
        )

    # ------------------------------------------------------------------ public

    def run_case(self, spec: ScenarioSpec, case: CaseSpec) -> CaseResult:
        """Execute one case end-to-end; never raises — verdicts land in outcomes."""
        outcomes: dict[str, CapabilityOutcome] = {}

        # Phase 1 — submit + job poll. A failure here means nothing else is
        # observable: durable-execution fails (metric included), the rest skip.
        try:
            workflow_run_id = self._launch(spec, case)
        except (AssertionError, httpx.HTTPError) as exc:
            cert_metrics.record(spec.scenario, "durable-execution", passed=False)
            outcomes["durable-execution"] = CapabilityOutcome("fail", f"launch: {exc}")
            for cap in CAPABILITIES:
                outcomes.setdefault(
                    cap, CapabilityOutcome("skip", "run never reached the platform")
                )
            return CaseResult(case.name, None, outcomes)

        # Phase 2 — HITL. No gates declared → honest skip for this case.
        if case.hitl:
            outcomes["hitl"] = self._certified(
                spec.scenario, "hitl", lambda: self._drive_hitl(spec, case, workflow_run_id)
            )
        else:
            outcomes["hitl"] = CapabilityOutcome("skip", "no human gate on this path")

        if outcomes["hitl"].status == "fail":
            # The run is stuck at a gate — a terminal fact will never appear;
            # don't burn the fact timeout pretending otherwise.
            cert_metrics.record(spec.scenario, "durable-execution", passed=False)
            outcomes["durable-execution"] = CapabilityOutcome(
                "fail", f"run never reached terminal — HITL phase failed: {outcomes['hitl'].note}"
            )
            for cap in CAPABILITIES:
                outcomes.setdefault(cap, CapabilityOutcome("skip", "run did not reach terminal"))
            return CaseResult(case.name, workflow_run_id, outcomes)

        # Phase 3 — terminal fact (the ADR 096 surface) + per-capability asserts.
        fact_holder: dict[str, Any] = {}

        def _terminal() -> str | None:
            fact = self._poll(
                self._fact_timeout_s,
                lambda: self._terminal_fact(spec.target, workflow_run_id),
                f"a terminal workflow_run fact for {workflow_run_id}",
            )
            fact_holder.update(fact)
            actual = fact.get("status")
            assert actual == case.expect.status, (
                f"terminal fact status={actual!r}, expected {case.expect.status!r} "
                f"(error_type={fact.get('error_type')})"
            )
            return f"status={actual}"

        outcomes["durable-execution"] = self._certified(
            spec.scenario, "durable-execution", _terminal
        )

        if fact_holder:
            outcomes["decision-routing"] = self._certified(
                spec.scenario,
                "decision-routing",
                lambda: self._assert_routing(case, fact_holder, workflow_run_id),
            )
        else:
            outcomes["decision-routing"] = CapabilityOutcome("skip", "no terminal fact to assert")

        outcomes["cost"] = self._cost_outcome(spec, case, fact_holder)
        outcomes["side-effects"] = self._side_effects_outcome(spec, case, workflow_run_id)
        return CaseResult(case.name, workflow_run_id, outcomes)

    def _cost_outcome(
        self, spec: ScenarioSpec, case: CaseSpec, fact: dict[str, Any]
    ) -> CapabilityOutcome:
        if not case.expect.cost:
            return CapabilityOutcome(
                "skip",
                "case does not assert cost (workflow_run facts carry cost_usd=0 by "
                "design — ADR 096; no per-node run facts on the Temporal path yet)",
            )
        if not fact:
            return CapabilityOutcome("skip", "no terminal fact to assert")

        def _cost() -> str | None:
            cost = float(fact.get("cost_usd") or 0.0)
            assert cost > 0, f"fact.cost_usd={cost} — LLM spend not visible on the fact"
            return f"cost_usd={cost}"

        return self._certified(spec.scenario, "cost", _cost)

    def _side_effects_outcome(
        self, spec: ScenarioSpec, case: CaseSpec, workflow_run_id: str
    ) -> CapabilityOutcome:
        if not (case.expect.side_effects or case.expect.no_side_effects):
            return CapabilityOutcome("skip", "no ledger expectations declared for this case")
        if not self._side_effects_enabled:
            return CapabilityOutcome(
                "skip",
                "shared certification DB unreachable — set MOVATE_PG_URL/MOVATE_DB_URL "
                "to the deployed Postgres to evaluate sim-ledger expectations",
            )
        return self._certified(
            spec.scenario,
            "side-effects",
            lambda: self._assert_side_effects(case, workflow_run_id),
        )

    def run_scenario(
        self, spec: ScenarioSpec, *, log: Callable[[str], None] = print
    ) -> list[CaseResult]:
        """Run every case in a scenario, logging progress per case."""
        results: list[CaseResult] = []
        for case in spec.cases:
            log(f"[{spec.scenario}] case {case.name!r} …")
            result = self.run_case(spec, case)
            for cap in CAPABILITIES:
                outcome = result.outcomes[cap]
                suffix = f" — {outcome.note}" if outcome.note else ""
                log(f"  {cap}: {outcome.status.upper()}{suffix}")
            results.append(result)
        return results


# ---------------------------------------------------------------------------
# Matrix aggregation + rendering
# ---------------------------------------------------------------------------


def aggregate_matrix(results: list[CaseResult]) -> dict[str, str]:
    """Fold case outcomes into one scenario verdict per capability.

    Any fail → ``fail``; else any pass → ``pass``; else ``skip``.
    """
    matrix: dict[str, str] = {}
    for cap in CAPABILITIES:
        statuses = {r.outcomes.get(cap, CapabilityOutcome("skip")).status for r in results}
        if "fail" in statuses:
            matrix[cap] = "fail"
        elif "pass" in statuses:
            matrix[cap] = "pass"
        else:
            matrix[cap] = "skip"
    return matrix


def render_matrix(rows: list[tuple[str, dict[str, str]]]) -> str:
    """Plain-text scenario x capability matrix (the local source of truth)."""
    name_w = max([len("scenario"), *(len(name) for name, _ in rows)])
    col_ws = [max(len(cap), 4) for cap in CAPABILITIES]
    header = (
        "scenario".ljust(name_w)
        + "  "
        + "  ".join(cap.ljust(w) for cap, w in zip(CAPABILITIES, col_ws, strict=True))
    )
    lines = [header, "-" * len(header)]
    for name, matrix in rows:
        cells = [
            matrix.get(cap, "skip").upper().ljust(w)
            for cap, w in zip(CAPABILITIES, col_ws, strict=True)
        ]
        lines.append(name.ljust(name_w) + "  " + "  ".join(cells))
    return "\n".join(lines)


def summary_json(
    target: str, scenario_results: list[tuple[ScenarioSpec, list[CaseResult]]]
) -> dict[str, Any]:
    """The ``--json`` summary shape (machine-readable mirror of the matrix)."""
    scenarios: list[dict[str, Any]] = []
    ok = True
    for spec, results in scenario_results:
        matrix = aggregate_matrix(results)
        ok = ok and "fail" not in matrix.values()
        scenarios.append(
            {
                "scenario": spec.scenario,
                "matrix": matrix,
                "cases": [
                    {
                        "name": r.name,
                        "workflow_run_id": r.workflow_run_id,
                        "capabilities": {
                            cap: {"status": o.status, "note": o.note}
                            for cap, o in r.outcomes.items()
                        },
                    }
                    for r in results
                ],
            }
        )
    return {"target": target, "ok": ok, "scenarios": scenarios}
