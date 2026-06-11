"""B3 multi-agent batch — scenarios #9 and #17 of the 30-use-case program.

Two certification scenarios, each shipping in the itsm-request THREE-copy
layout (deployable under ``workflows/``, suite mirror under
``certification/scenarios/``, ``mdk init --pattern`` template under
``src/movate/templates/``) that must stay coherent:

* ``multi-agent-investigation`` (#9) — the PARALLEL FAN-OUT/FAN-IN diamond
  (ADR 092 Phases 1+2): ``plan`` fans out to THREE single-node specialist
  branches (web-researcher / kb-researcher / data-analyst — calibrated sims
  with disjoint findings keys) that fan in on ``synthesize``, which merges
  the findings into {conclusion, confidence} and must acknowledge
  disagreement between sources.
* ``multi-agent-business-process`` (#17) — the bounded SUPERVISOR primitive
  (ADR 092 D4 / Phase 3): a ``process-manager`` manager delegating across
  the FIXED allowlist research / pricing / compliance (max_delegations: 4),
  then a ``proposal`` composer and ``notify``.

Unlike the B1 module, NATIVE scripted-provider runs ARE tested here: both
scenarios are gate-free (no HUMAN pause to drive), so a deterministic
provider keyed on each agent's rendered prompt exercises the full diamond /
delegation loop end-to-end — including the manager prompt's ``is defined``
guards under StrictUndefined and the barrier-join merge. What the module
asserts:

1. graph shape per scenario x copy — the diamond's fan-out/fan-in edges +
   join strategy metadata and the agent-only node set; the supervisor's
   manager/allowlist/cap metadata and the linear tail;
2. Temporal compilation — the diamond lowers to ``asyncio.gather`` with the
   branch nodes emitted INSIDE the gather (no standalone dispatch arms); the
   supervisor lowers to the bounded ``for _ in range(4)`` delegation loop;
   activity sets are exact;
3. native execution — the diamond runs its three branches CONCURRENTLY
   (barrier-proof) and the join delivers all three findings to synthesize;
   the supervisor delegates research → pricing → compliance exactly once
   each, then ``done``, then the proposal/notify tail;
4. ``cases.yaml`` — both scenarios parse through the driver's own loader
   with the right no-hitl / final-state / governance expectations;
5. anti-drift — every agent file + state.json ships byte-identical across
   the three copies.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from certification.harness.driver import load_scenario_spec

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import WorkflowRunner, declares_parallel
from movate.core.workflow.compiler import compile_workflow, validate_graph
from movate.core.workflow.compilers.temporal import TemporalCompiler
from movate.core.workflow.ir import EdgeKind, NodeType, WorkflowGraph
from movate.core.workflow.spec import load_workflow_spec
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / "workflows"
SCENARIOS = REPO_ROOT / "certification" / "scenarios"
TEMPLATES = REPO_ROOT / "src" / "movate" / "templates"

# scenario name → (deployable, scenario workflow dir, template). The scenario
# copies nest their workflow under workflows/<short>/ (relative refs to the
# scenario root — the itsm-request/purchase-order layout).
COPIES: dict[str, dict[str, Path]] = {
    "multi-agent-investigation": {
        "deployable": WORKFLOWS / "multi-agent-investigation",
        "scenario": SCENARIOS / "multi-agent-investigation" / "workflows" / "investigation",
        "template": TEMPLATES / "pattern_multi_agent_investigation",
    },
    "multi-agent-business-process": {
        "deployable": WORKFLOWS / "multi-agent-business-process",
        "scenario": SCENARIOS / "multi-agent-business-process" / "workflows" / "process",
        "template": TEMPLATES / "pattern_multi_agent_business_process",
    },
}
COPY_IDS = sorted(COPIES["multi-agent-investigation"])  # deployable / scenario / template

SPECIALIST_BRANCHES = ("data-analyst", "kb-researcher", "web-researcher")


def _graph(workflow_dir: Path) -> WorkflowGraph:
    spec, parent = load_workflow_spec(workflow_dir)
    return compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# 1a. multi-agent-investigation — the canonical diamond (ADR 092)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_investigation_graph_shape(which: str) -> None:
    graph = _graph(COPIES["multi-agent-investigation"][which])
    validate_graph(graph)  # declares_parallel routes this to validate_dag

    assert graph.entrypoint == "plan"
    assert declares_parallel(graph) is True
    # Phase 1/2 diamonds are agent-only — and that's the whole node set.
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "plan": NodeType.AGENT,
        "web-researcher": NodeType.AGENT,
        "kb-researcher": NodeType.AGENT,
        "data-analyst": NodeType.AGENT,
        "synthesize": NodeType.AGENT,
    }
    # Every agent ref resolved to a real bundled dir at compile time.
    for node in graph.nodes.values():
        assert Path(node.ref).is_dir()
        assert (Path(node.ref) / "agent.yaml").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_investigation_diamond_edges_and_join_strategy(which: str) -> None:
    """The FIXED three-branch roster (ADR 092 D5 — the branch count is a
    structural bound) and the clobber-free default join: three fan_out edges
    out of plan, three fan_in edges into synthesize stamped last_wins."""
    graph = _graph(COPIES["multi-agent-investigation"][which])

    fan_out = {(e.from_id, e.to_id) for e in graph.edges if e.kind is EdgeKind.PARALLEL_FAN_OUT}
    assert fan_out == {("plan", b) for b in SPECIALIST_BRANCHES}
    fan_in = [e for e in graph.edges if e.kind is EdgeKind.PARALLEL_FAN_IN]
    assert {(e.from_id, e.to_id) for e in fan_in} == {
        (b, "synthesize") for b in SPECIALIST_BRANCHES
    }
    # The compiler stamps the merge strategy onto every fan-in edge; the three
    # findings keys are disjoint, so last_wins (the default) is clobber-free.
    assert all(e.metadata.get("join") == "last_wins" for e in fan_in)
    assert all("join_key" not in e.metadata for e in fan_in)
    # No other edges exist — the diamond IS the workflow.
    assert len(graph.edges) == 6
    # Single source (the entrypoint) and single sink (the join node).
    assert graph.sources() == ["plan"]
    assert graph.sinks() == ["synthesize"]


# ---------------------------------------------------------------------------
# 1b. multi-agent-business-process — the bounded supervisor (ADR 092 D4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_business_process_graph_shape(which: str) -> None:
    graph = _graph(COPIES["multi-agent-business-process"][which])
    validate_graph(graph)  # no parallel edge → the unchanged linear gate

    assert graph.entrypoint == "supervisor"
    assert declares_parallel(graph) is False
    assert {nid: node.type for nid, node in graph.nodes.items()} == {
        "supervisor": NodeType.SUPERVISOR,
        "proposal": NodeType.AGENT,
        "notify": NodeType.AGENT,
    }
    # The delegation loop is INTERNAL to the node — the graph is a plain
    # linear chain (no synthetic edges, no branches).
    assert [(e.from_id, e.to_id, e.kind) for e in graph.edges] == [
        ("supervisor", "proposal", EdgeKind.SEQUENTIAL),
        ("proposal", "notify", EdgeKind.SEQUENTIAL),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_business_process_supervisor_bounds(which: str) -> None:
    """The ADR 092 D4 bounds: manager + FIXED allowlist resolved to real agent
    dirs at compile time, the hard delegation cap, the default decision_field,
    and NO aggregate budget key (only stamped when set)."""
    graph = _graph(COPIES["multi-agent-business-process"][which])
    meta = graph.nodes["supervisor"].metadata

    assert Path(meta["manager"]).name == "process-manager"
    assert (Path(meta["manager"]) / "agent.yaml").is_file()
    assert set(meta["specialists"]) == {"research", "pricing", "compliance"}
    for sid, sref in meta["specialists"].items():
        assert Path(sref).name == sid
        assert (Path(sref) / "agent.yaml").is_file()
    assert meta["max_delegations"] == 4  # 3 consultations + the closing done
    assert meta["decision_field"] == "next"
    assert "budget" not in meta  # unset → absent (ADR 092 D5 stamp rule)
    # The supervisor's ref IS the manager (the delegator).
    assert graph.nodes["supervisor"].ref == meta["manager"]


# ---------------------------------------------------------------------------
# 2. Temporal compilation — durable gather, bounded delegation loop
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_temporal_lowers_diamond_to_gather(which: str) -> None:
    """ADR 092 Phase 2 / D3: the single-node-branch diamond compiles to
    Temporal-native ``asyncio.gather`` parallelism — the branch agents are
    emitted INSIDE the fan-out node's gather, never as standalone dispatch
    arms — and control advances to the join node."""
    result = TemporalCompiler().compile(_graph(COPIES["multi-agent-investigation"][which]))
    src = result.module_source
    ast.parse(src)
    # Agents only — no gate/judge/skill/human activity anywhere.
    assert set(result.activity_names) == {
        "call_agent_activity",
        "persist_workflow_result_activity",
    }
    assert "plan_branches = await asyncio.gather(" in src
    for branch in SPECIALIST_BRANCHES:
        assert f"args=['{branch}'," in src  # inside the gather
        assert f"elif current == '{branch}':" not in src  # not a dispatch arm
    # last_wins join + the advance to the fan-in node.
    assert "for _b in plan_branches:" in src
    assert "state.update(_b)" in src
    assert "current = 'synthesize'" in src
    assert "elif current == 'synthesize':" in src


@pytest.mark.unit
@pytest.mark.parametrize("which", COPY_IDS)
def test_temporal_lowers_supervisor_to_bounded_loop(which: str) -> None:
    """ADR 092 Phase 3b (#805): the supervisor compiles to a deterministic
    ``for _ in range(max_delegations)`` loop — manager activity, allowlisted
    specialist activity — with done/out-of-roster breaking the loop."""
    result = TemporalCompiler().compile(_graph(COPIES["multi-agent-business-process"][which]))
    src = result.module_source
    ast.parse(src)
    assert set(result.activity_names) == {
        "call_agent_activity",
        "persist_workflow_result_activity",
    }
    assert "for _ in range(4):  # max_delegations — anti-runaway cap" in src
    # The FIXED roster is an emitted literal — the manager can only select
    # from it at runtime.
    assert "supervisor_specialists = {" in src
    for sid in ("research", "pricing", "compliance"):
        assert f"'{sid}': " in src
    assert (
        "if supervisor_choice == 'done' or supervisor_choice not in supervisor_specialists:"
    ) in src
    # Post-loop tail: supervisor advances to proposal, proposal to notify.
    assert "current = 'proposal'" in src
    assert "elif current == 'notify':" in src


# ---------------------------------------------------------------------------
# 3. Native execution — scripted providers against the DEPLOYABLE copies
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _runner(
    provider: BaseLLMProvider, storage: InMemoryStorage, pricing: PricingTable
) -> WorkflowRunner:
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=NullTracer())
    return WorkflowRunner(executor=executor, storage=storage)


class _InvestigationProvider(BaseLLMProvider):
    """Deterministic sim keyed on each agent's rendered prompt. The three
    specialists rendezvous on a 3-party barrier, so the test deadlocks (and
    times out) unless the branches really run concurrently."""

    name = "b3-investigation"
    version = "0.0.1"

    def __init__(self) -> None:
        self._barrier = asyncio.Barrier(3)
        self.synthesize_prompt: str | None = None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "You are the planning agent" in body:
            return CompletionResponse(text=json.dumps({"scope": "SCOPE"}))
        if "You are the WEB RESEARCHER" in body:
            await self._barrier.wait()
            return CompletionResponse(text=json.dumps({"web_findings": "[web] W-50"}))
        if "You are the KB RESEARCHER" in body:
            await self._barrier.wait()
            return CompletionResponse(text=json.dumps({"kb_findings": "[kb] K-200"}))
        if "You are the DATA ANALYST" in body:
            await self._barrier.wait()
            return CompletionResponse(text=json.dumps({"data_findings": "[data] D-178"}))
        if "You are the synthesis agent" in body:
            self.synthesize_prompt = body
            return CompletionResponse(
                text=json.dumps({"conclusion": "CONFLICT-ACKNOWLEDGED", "confidence": 0.55})
            )
        raise AssertionError(f"unexpected prompt: {body[:80]!r}")  # pragma: no cover

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.unit
async def test_investigation_runs_concurrently_and_joins_all_findings(
    pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """The diamond end-to-end on native: plan → the three specialists IN
    PARALLEL (barrier-proof) → the join hands synthesize ALL THREE findings
    (its rendered prompt carries each labeled finding — the case assert's
    final_state keys, proven at the source)."""
    graph = _graph(COPIES["multi-agent-investigation"]["deployable"])
    provider = _InvestigationProvider()
    runner = _runner(provider, storage, pricing)

    result = await asyncio.wait_for(
        runner.run(
            graph,
            initial_state={"question": "What is the concurrent-user ceiling?"},
        ),
        timeout=10.0,
    )

    assert result.status is WorkflowStatus.SUCCESS
    for key, value in {
        "scope": "SCOPE",
        "web_findings": "[web] W-50",
        "kb_findings": "[kb] K-200",
        "data_findings": "[data] D-178",
        "conclusion": "CONFLICT-ACKNOWLEDGED",
        "confidence": 0.55,
    }.items():
        assert result.final_state[key] == value
    assert {r.node_id for r in result.runs} == set(graph.nodes)
    # The join really fed synthesize all three branch outputs.
    assert provider.synthesize_prompt is not None
    for marker in ("[web] W-50", "[kb] K-200", "[data] D-178"):
        assert marker in provider.synthesize_prompt


class _BusinessProcessProvider(BaseLLMProvider):
    """Deterministic sim of the delegation loop. The manager's choice mirrors
    its prompt rules: the first not-yet-gathered findings key (rendered as a
    '- <key>:' line by the prompt's ``is defined`` guards), else done."""

    name = "b3-business-process"
    version = "0.0.1"

    def __init__(self) -> None:
        self.manager_prompts: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "You are the PROCESS MANAGER" in body:
            self.manager_prompts.append(body)
            if "- research_findings:" not in body:
                choice = "research"
            elif "- pricing_quote:" not in body:
                choice = "pricing"
            elif "- compliance_assessment:" not in body:
                choice = "compliance"
            else:
                choice = "done"
            return CompletionResponse(text=json.dumps({"next": choice}))
        if "You are the RESEARCH specialist" in body:
            return CompletionResponse(text=json.dumps({"research_findings": "[research] R"}))
        if "You are the PRICING specialist" in body:
            return CompletionResponse(text=json.dumps({"pricing_quote": "[pricing] P"}))
        if "You are the COMPLIANCE specialist" in body:
            return CompletionResponse(text=json.dumps({"compliance_assessment": "[compliance] C"}))
        if "You are the proposal writer" in body:
            return CompletionResponse(text=json.dumps({"proposal": "PROPOSAL"}))
        if "You are the notification agent" in body:
            return CompletionResponse(text=json.dumps({"summary": "SUMMARY"}))
        raise AssertionError(f"unexpected prompt: {body[:80]!r}")  # pragma: no cover

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.unit
async def test_business_process_delegates_each_specialist_once_then_composes(
    pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """The supervisor end-to-end on native: the manager (whose prompt renders
    under StrictUndefined via ``is defined`` guards) delegates research →
    pricing → compliance exactly once each, says done on round 4 (inside the
    max_delegations cap), and the proposal/notify tail runs."""
    graph = _graph(COPIES["multi-agent-business-process"]["deployable"])
    provider = _BusinessProcessProvider()
    runner = _runner(provider, storage, pricing)

    result = await runner.run(
        graph, initial_state={"request": "80 Enterprise seats, patient data, EU residency."}
    )

    assert result.status is WorkflowStatus.SUCCESS
    for key, value in {
        "research_findings": "[research] R",
        "pricing_quote": "[pricing] P",
        "compliance_assessment": "[compliance] C",
        "next": "done",  # the manager's final sentinel
        "proposal": "PROPOSAL",
        "summary": "SUMMARY",
    }.items():
        assert result.final_state[key] == value
    # 4 manager turns interleaved with the three specialists, then the tail.
    assert [r.node_id for r in result.runs] == [
        "supervisor",
        "supervisor/research",
        "supervisor",
        "supervisor/pricing",
        "supervisor",
        "supervisor/compliance",
        "supervisor",
        "proposal",
        "notify",
    ]
    # The state-driven prompts: round 1 saw no findings; round 4 saw all three.
    assert len(provider.manager_prompts) == 4
    assert "- research_findings:" not in provider.manager_prompts[0]
    for marker in ("- research_findings:", "- pricing_quote:", "- compliance_assessment:"):
        assert marker in provider.manager_prompts[3]


# ---------------------------------------------------------------------------
# 4. cases.yaml — both scenarios parse through the DRIVER's loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_investigation_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "multi-agent-investigation" / "cases.yaml")
    assert spec.scenario == "multi-agent-investigation"
    assert spec.target == "multi-agent-investigation"
    assert [c.name for c in spec.cases] == [
        "straightforward-question",
        "synthesis-with-conflicting-findings",
    ]
    straightforward, conflicting = spec.cases

    # The consensus question hits the corpora where they AGREE (3 regions);
    # the conflict question where they DISAGREE (50 vs 200 vs observed 178).
    assert "region" in straightforward.input["question"]
    assert "concurrent" in conflicting.input["question"]

    for case in spec.cases:
        # No HUMAN gate on any path — the hitl capability is an honest skip.
        assert case.hitl == ()
        assert case.expect.status == "success"
        assert case.expect.route is None
        assert case.expect.governance == "allow"
        assert case.expect.cost is False
        # The diamond proof: every specialist's findings key AND the join's
        # output must be in final_state — present only if all three branches
        # ran and the barrier merge delivered them to synthesize.
        assert set(case.expect.final_state_has) == {
            "scope",
            "web_findings",
            "kb_findings",
            "data_findings",
            "conclusion",
            "confidence",
        }
        assert case.expect.final_state_lacks == ()
        # No tool node by design — no ledger expectations anywhere.
        assert case.expect.side_effects == () and case.expect.no_side_effects == ()
        assert case.timeout_s is None  # no durable waits here


@pytest.mark.unit
def test_business_process_cases_validate_against_driver_loader() -> None:
    spec = load_scenario_spec(SCENARIOS / "multi-agent-business-process" / "cases.yaml")
    assert spec.scenario == "multi-agent-business-process"
    assert spec.target == "multi-agent-business-process"
    assert [c.name for c in spec.cases] == ["standard-request", "compliance-heavy-request"]
    standard, heavy = spec.cases

    # The compliance-heavy request names the obligations that hit the
    # compliance sim's checklist where it bites; the standard one does not.
    assert "HIPAA" in heavy.input["request"]
    assert "data residency" in heavy.input["request"]
    assert "HIPAA" not in standard.input["request"]

    for case in spec.cases:
        assert case.hitl == ()
        assert case.expect.status == "success"
        assert case.expect.route is None
        assert case.expect.governance == "allow"
        assert case.expect.cost is False
        # The delegation proof: each specialist's findings key exists ONLY if
        # the manager delegated to it; proposal/summary prove the post-loop
        # tail ran after the supervisor exited.
        assert set(case.expect.final_state_has) == {
            "research_findings",
            "pricing_quote",
            "compliance_assessment",
            "proposal",
            "summary",
        }
        assert case.expect.final_state_lacks == ()
        assert case.expect.side_effects == () and case.expect.no_side_effects == ()
        assert case.timeout_s is None


# ---------------------------------------------------------------------------
# 5. Anti-drift — agents + state.json byte-identical across the three copies
# ---------------------------------------------------------------------------

# scenario name → its agent roster (every dir ships the same 5 files).
AGENT_ROSTERS: dict[str, tuple[str, ...]] = {
    "multi-agent-investigation": (
        "plan",
        "web-researcher",
        "kb-researcher",
        "data-analyst",
        "synthesize",
    ),
    "multi-agent-business-process": (
        "process-manager",
        "research",
        "pricing",
        "compliance",
        "proposal",
        "notify",
    ),
}
AGENT_FILES = (
    "agent.yaml",
    "prompt.md",
    "schema/input.json",
    "schema/output.json",
    "evals/dataset.jsonl",
)


def _copy_roots(scenario: str) -> tuple[Path, Path, Path]:
    """The three roots agents/ + state.json live under (the scenario copy
    keeps them at the scenario ROOT — its workflow refs ../../agents)."""
    return (
        WORKFLOWS / scenario,
        SCENARIOS / scenario,
        TEMPLATES / f"pattern_{scenario.replace('-', '_')}",
    )


@pytest.mark.unit
@pytest.mark.parametrize("scenario", sorted(AGENT_ROSTERS))
def test_agent_files_identical_across_copies(scenario: str) -> None:
    deployable, scenario_root, template = _copy_roots(scenario)
    for agent in AGENT_ROSTERS[scenario]:
        for rel in AGENT_FILES:
            canonical = (deployable / "agents" / agent / rel).read_bytes()
            assert (scenario_root / "agents" / agent / rel).read_bytes() == canonical, (
                f"{scenario}: scenario copy of agents/{agent}/{rel} drifted"
            )
            assert (template / "agents" / agent / rel).read_bytes() == canonical, (
                f"{scenario}: template copy of agents/{agent}/{rel} drifted"
            )


@pytest.mark.unit
@pytest.mark.parametrize("scenario", sorted(AGENT_ROSTERS))
def test_agent_rosters_are_exhaustive(scenario: str) -> None:
    """No copy carries an extra (or missing) agent dir the roster — and the
    byte-identity sweep above — would silently skip."""
    for root in _copy_roots(scenario):
        assert {p.name for p in (root / "agents").iterdir() if p.is_dir()} == set(
            AGENT_ROSTERS[scenario]
        ), f"{scenario}: unexpected agent roster under {root}"


@pytest.mark.unit
@pytest.mark.parametrize("scenario", sorted(AGENT_ROSTERS))
def test_state_schema_identical_across_copies(scenario: str) -> None:
    deployable, scenario_root, template = _copy_roots(scenario)
    canonical = (deployable / "state.json").read_bytes()
    assert (scenario_root / "state.json").read_bytes() == canonical
    assert (template / "state.json").read_bytes() == canonical
