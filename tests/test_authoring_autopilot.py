"""ADR 025 D7 — the "improve my agent" autopilot tests (D7b, #133).

Hermetic + offline: the whole autopilot is driven by a scripted
:class:`MockEvalRunner` (a stubbed eval result) + the deterministic
:class:`MockPlanner` (no API keys, no network) over tmp_path projects — exactly
how CI runs it. We assert the contract the brief calls out:

* a scripted "agent fails case X" snapshot → the autopilot asks the planner →
  the planner proposes the right catalog action → the driver applies it → the
  re-run eval reports improvement;
* proposals are confirm-gated (a gated action is NOT auto-applied), and the
  planner's output maps only to valid catalog actions (unknown → dropped);
* the per-pass action cap is respected (bounded), and the loop is finite;
* the ``mdk dev``/copilot surface (``_improve_action`` + the menu) invokes the
  autopilot path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import movate.cli.dev_cmd as dc
from movate.authoring import (
    AuthoringContext,
    AuthoringDriver,
    Autopilot,
    EvalSnapshot,
    FailingCase,
    MockEvalRunner,
    MockPlanner,
    PlannerOutcome,
    ProposedAction,
    build_improve_request,
    propose_improvements,
)
from movate.testing import scaffold_agent

# ---------------------------------------------------------------------------
# Fixtures — a tiny loadable project + scripted snapshots
# ---------------------------------------------------------------------------

_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: greeter
version: 0.1.0
description: A test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
  params:
    temperature: 0.0
prompt: ./prompt.md
schema:
  input:
    text: string
  output:
    message: string
evals:
  dataset: ./evals/dataset.jsonl
"""

_PROMPT = "You are a greeter. Reply with a greeting.\n"
_DATASET = '{"input": {"text": "hi"}, "expected": {"message": "hello"}}\n'


def _make_project(root: Path, *, agent: str = "greeter") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("agents_dir: ./agents\n")
    agent_dir = root / "agents" / agent
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML.replace("name: greeter", f"name: {agent}"))
    (agent_dir / "prompt.md").write_text(_PROMPT)
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text(_DATASET)
    return root


def _driver(root: Path) -> AuthoringDriver:
    return AuthoringDriver(AuthoringContext(project=root))


def _snapshot_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".mdk" not in p.parts and ".movate" not in p.parts:
            out[str(p.relative_to(root))] = p.read_text(encoding="utf-8")
    return out


def _failing() -> EvalSnapshot:
    return EvalSnapshot(
        total_cases=2,
        passed_cases=1,
        mean_score=0.5,
        failures=[
            FailingCase(
                input={"text": "what is the refund window?"},
                expected={"message": "30 days"},
                actual={"message": "I don't know"},
                score=0.0,
                rationale="mismatch — agent lacks the refund-policy fact",
                cost_usd=0.0003,
            )
        ],
    )


def _passing() -> EvalSnapshot:
    return EvalSnapshot(total_cases=2, passed_cases=2, mean_score=1.0)


# ---------------------------------------------------------------------------
# build_improve_request / propose_improvements — the planner-grounding seam
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_improve_request_surfaces_failures_and_bound() -> None:
    """The request grounds the planner in the failing cases + the action cap."""
    req = build_improve_request(_failing(), max_actions=3)
    assert "failing the cases below" in req.lower()
    assert "refund window" in req  # the failing input is surfaced
    assert "at most 3" in req  # the per-pass cap is communicated
    assert "1/2" in req  # pass rate


@pytest.mark.unit
def test_propose_improvements_maps_to_valid_catalog_action() -> None:
    """A failing snapshot → the mock planner proposes a valid catalog action."""
    proposals = propose_improvements(MockPlanner(), _failing(), agent="greeter")
    assert len(proposals) == 1
    assert proposals[0].name == "add-context"  # the scripted D7 fix
    # The proposal is a real catalog action (the driver could plan it).
    from movate.authoring import action_names  # noqa: PLC0415

    assert proposals[0].name in set(action_names())


@pytest.mark.unit
def test_propose_improvements_drops_unknown_actions() -> None:
    """An unknown action name in the planner's output is rejected (not proposed)."""
    planner = MockPlanner(
        script=[
            PlannerOutcome(
                actions=[
                    ProposedAction(name="add-context", args={"agent": "g", "name": "ok"}),
                    ProposedAction(name="rm-rf-everything", args={}),
                ]
            )
        ]
    )
    proposals = propose_improvements(planner, _failing(), agent="g")
    assert [p.name for p in proposals] == ["add-context"]  # unknown dropped


@pytest.mark.unit
def test_propose_improvements_respects_max_actions() -> None:
    """More proposals than the cap are truncated (bounded pass)."""
    planner = MockPlanner(
        script=[
            PlannerOutcome(
                actions=[
                    ProposedAction(name="add-context", args={"agent": "g", "name": f"c{i}"})
                    for i in range(5)
                ]
            )
        ]
    )
    proposals = propose_improvements(planner, _failing(), agent="g", max_actions=2)
    assert len(proposals) == 2


@pytest.mark.unit
def test_propose_nothing_when_all_passing() -> None:
    """No failures → no proposals (the planner is never even consulted)."""
    assert propose_improvements(MockPlanner(), _passing(), agent="greeter") == []


@pytest.mark.unit
def test_propose_nothing_on_clarification() -> None:
    """An ambiguous planner outcome yields no proposals — the pass mutates nothing."""
    planner = MockPlanner(script=[PlannerOutcome(needs_clarification="which agent?")])
    assert propose_improvements(planner, _failing(), agent="greeter") == []


# ---------------------------------------------------------------------------
# Brief scenario 1 — fails case X → propose → apply → re-verify improvement
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_autopilot_fixes_failing_case_and_reports_improvement(tmp_path: Path) -> None:
    """Scripted fail→fix→pass: autopilot applies a fix and the re-eval improves."""
    root = _make_project(tmp_path / "proj")
    runner = MockEvalRunner([_failing(), _passing()])
    autopilot = Autopilot(eval_runner=runner, planner=MockPlanner(), driver=_driver(root))

    # fast_mode auto-applies the additive+reversible+free fix (add-context).
    result = autopilot.run("greeter", fast_mode=True)

    # The fix was applied via the catalog driver (file + agent.yaml wired in).
    assert (root / "agents" / "greeter" / "contexts" / "eval-fixes.md").is_file()
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert "eval-fixes" in (data.get("contexts") or [])

    # The loop re-ran the eval after applying (proving the harvest→improve cycle).
    assert runner.calls == ["greeter", "greeter"]
    assert result.total_applied == 1
    assert result.improved
    assert result.final.pass_rate == 1.0
    # The applied proposal verified (validate + mock-run) and was not reverted.
    applied = result.passes[0].proposals[0]
    assert applied.applied
    assert applied.outcome is not None
    assert applied.outcome.verify is not None
    assert applied.outcome.verify.ok


@pytest.mark.unit
def test_autopilot_noop_when_already_passing(tmp_path: Path) -> None:
    """All cases pass at the start → no proposal, no mutation, planner untouched."""
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    runner = MockEvalRunner([_passing()])
    result = Autopilot(eval_runner=runner, planner=MockPlanner(), driver=_driver(root)).run(
        "greeter", fast_mode=True
    )

    assert result.total_applied == 0
    assert result.passes == []  # never entered a pass
    assert runner.calls == ["greeter"]  # ran the eval once, then stopped
    assert _snapshot_tree(root) == before


# ---------------------------------------------------------------------------
# Brief scenario 2 — proposals are confirm-gated (not auto-applied)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_proposals_not_auto_applied_without_confirmation(tmp_path: Path) -> None:
    """Without fast_mode and with no confirm callback, nothing is applied."""
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    runner = MockEvalRunner([_failing(), _failing()])
    result = Autopilot(eval_runner=runner, planner=MockPlanner(), driver=_driver(root)).run(
        "greeter"
    )  # no confirm, no fast_mode

    assert result.total_applied == 0
    assert result.passes[0].proposals[0].skipped
    assert _snapshot_tree(root) == before  # the driver never wrote anything


@pytest.mark.unit
def test_confirm_callback_gates_each_proposal(tmp_path: Path) -> None:
    """A confirm callback that says no skips the apply (project unchanged)."""
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    seen: list[str] = []

    def _decline(proposed, _plan) -> bool:
        seen.append(proposed.name)
        return False

    runner = MockEvalRunner([_failing(), _failing()])
    result = Autopilot(eval_runner=runner, planner=MockPlanner(), driver=_driver(root)).run(
        "greeter", confirm=_decline
    )

    assert seen == ["add-context"]  # the gate was consulted
    assert result.total_applied == 0
    assert _snapshot_tree(root) == before


@pytest.mark.unit
def test_gated_action_skipped_even_in_fast_mode(tmp_path: Path) -> None:
    """A cost/networked proposal is never auto-applied, even with fast_mode."""
    root = _make_project(tmp_path / "proj")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("knowledge " * 30)
    # Planner proposes a networked + cost ingest-kb (confirmation-gated).
    planner = MockPlanner(
        script=[
            PlannerOutcome(
                actions=[
                    ProposedAction(
                        name="ingest-kb", args={"agent": "greeter", "path": str(docs / "a.txt")}
                    )
                ]
            )
        ]
    )
    runner = MockEvalRunner([_failing(), _failing()])
    result = Autopilot(eval_runner=runner, planner=planner, driver=_driver(root)).run(
        "greeter", fast_mode=True
    )
    # fast_mode must not bypass the gate for a confirmation-required plan.
    assert result.total_applied == 0
    assert result.passes[0].proposals[0].skipped


# ---------------------------------------------------------------------------
# Brief scenario 3 — bounded (per-pass cap + finite iterations)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_per_pass_action_cap_is_respected(tmp_path: Path) -> None:
    """At most max_actions_per_pass proposals are driven in one pass."""
    root = _make_project(tmp_path / "proj")
    planner = MockPlanner(
        script=[
            PlannerOutcome(
                actions=[
                    ProposedAction(name="add-context", args={"agent": "greeter", "name": f"c{i}"})
                    for i in range(5)
                ]
            ),
            _passing_outcome(),
        ]
    )
    runner = MockEvalRunner([_failing(), _passing()])
    autopilot = Autopilot(
        eval_runner=runner,
        planner=planner,
        driver=_driver(root),
        max_actions_per_pass=2,
    )
    result = autopilot.run("greeter", fast_mode=True)
    assert len(result.passes[0].proposals) == 2  # capped


@pytest.mark.unit
def test_iteration_cap_bounds_the_loop(tmp_path: Path) -> None:
    """When the agent never improves, the loop stops at max_iterations."""
    root = _make_project(tmp_path / "proj")
    # Eval always fails; planner always proposes a fresh (uniquely-named) context.
    counter = {"n": 0}

    class _Planner:
        def plan(self, request: str, *, agent: str) -> PlannerOutcome:
            counter["n"] += 1
            return PlannerOutcome(
                actions=[
                    ProposedAction(
                        name="add-context", args={"agent": agent, "name": f"fix{counter['n']}"}
                    )
                ]
            )

    runner = MockEvalRunner([_failing()])  # repeats forever
    autopilot = Autopilot(
        eval_runner=runner, planner=_Planner(), driver=_driver(root), max_iterations=2
    )
    result = autopilot.run("greeter", fast_mode=True)
    assert len(result.passes) == 2  # capped at max_iterations
    # eval ran: initial + once after each of the 2 passes.
    assert len(runner.calls) == 3


@pytest.mark.unit
def test_loop_stops_when_a_pass_applies_nothing(tmp_path: Path) -> None:
    """If a pass applies nothing (e.g. clarification), the loop stops early."""
    root = _make_project(tmp_path / "proj")
    planner = MockPlanner(script=[PlannerOutcome(needs_clarification="huh?")])
    runner = MockEvalRunner([_failing(), _failing()])
    autopilot = Autopilot(
        eval_runner=runner, planner=planner, driver=_driver(root), max_iterations=5
    )
    result = autopilot.run("greeter", fast_mode=True)
    assert len(result.passes) == 1  # stopped after the first no-op pass
    assert result.total_applied == 0
    assert runner.calls == ["greeter"]  # never re-ran the eval


@pytest.mark.unit
def test_autopilot_rejects_bad_bounds() -> None:
    runner = MockEvalRunner([_passing()])
    with pytest.raises(ValueError, match="max_actions_per_pass"):
        Autopilot(eval_runner=runner, planner=MockPlanner(), driver=None, max_actions_per_pass=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_iterations"):
        Autopilot(eval_runner=runner, planner=MockPlanner(), driver=None, max_iterations=0)  # type: ignore[arg-type]


def _passing_outcome() -> PlannerOutcome:
    return PlannerOutcome(actions=[ProposedAction(name="add-eval-case", args={"agent": "greeter"})])


# ---------------------------------------------------------------------------
# MockEvalRunner — the hermetic stub
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mock_eval_runner_returns_snapshots_in_order() -> None:
    runner = MockEvalRunner([_failing(), _passing()])
    assert runner.run_eval("a").pass_rate == 0.5
    assert runner.run_eval("a").pass_rate == 1.0
    # exhausted → last repeats
    assert runner.run_eval("a").pass_rate == 1.0
    assert runner.calls == ["a", "a", "a"]


@pytest.mark.unit
def test_mock_eval_runner_requires_a_snapshot() -> None:
    with pytest.raises(ValueError, match="at least one snapshot"):
        MockEvalRunner([])


@pytest.mark.unit
def test_eval_snapshot_pass_rate_edge_cases() -> None:
    assert EvalSnapshot(total_cases=0, passed_cases=0).pass_rate == 0.0
    assert not EvalSnapshot(total_cases=0, passed_cases=0).all_passing
    assert _passing().all_passing


# ---------------------------------------------------------------------------
# The mdk dev / copilot surface invokes the autopilot path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_actions_menu_maps_m_to_improve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking 'm' in the menu resolves to the improve (autopilot) action."""
    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: "m"))
    assert dc._actions_menu() == "improve"


def _scaffold_in_project(tmp_path: Path, *, name: str = "demo") -> Path:
    project = tmp_path / "proj"
    (project / "agents").mkdir(parents=True)
    (project / "movate.yaml").write_text("agents_dir: ./agents\n")
    return scaffold_agent(project / "agents" / name, name=name)


@pytest.mark.unit
def test_improve_action_runs_hermetic_autopilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the CLI handler under --mock.

    Drives the real ``_improve_action`` glue with a stubbed eval runner so the
    whole loop (eval → MockPlanner propose → driver apply → re-eval) runs
    hermetically. The user confirms; the fix lands via the catalog driver.
    """
    agent_dir = _scaffold_in_project(tmp_path)
    name = agent_dir.name

    # Stub the eval runner so we don't depend on the scaffold's mock-eval scores:
    # fail first, pass after the fix. Patch the class the handler constructs.
    runner = MockEvalRunner([_failing(), _passing()])
    monkeypatch.setattr(dc, "_CliEvalRunner", lambda *a, **k: runner)
    # Confirm every proposed fix.
    monkeypatch.setattr(dc.typer, "confirm", lambda *a, **k: True)

    dc._improve_action(agent_dir, mock=True)

    # The autopilot applied the scripted add-context fix via the driver.
    assert (agent_dir / "contexts" / "eval-fixes.md").is_file()
    data = yaml.safe_load((agent_dir / "agent.yaml").read_text())
    assert "eval-fixes" in (data.get("contexts") or [])
    assert runner.calls == [name, name]  # eval re-ran after the fix


@pytest.mark.unit
def test_improve_action_noop_when_no_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No eval cases → the handler reports it and mutates nothing."""
    agent_dir = _scaffold_in_project(tmp_path)
    project = agent_dir.parent.parent
    before = _snapshot_tree(project)

    runner = MockEvalRunner([EvalSnapshot(total_cases=0, passed_cases=0)])
    monkeypatch.setattr(dc, "_CliEvalRunner", lambda *a, **k: runner)
    monkeypatch.setattr(
        dc.typer,
        "confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not confirm")),
    )

    dc._improve_action(agent_dir, mock=True)
    assert _snapshot_tree(project) == before


@pytest.mark.unit
def test_cli_eval_runner_reuses_eval_engine_hermetically(tmp_path: Path) -> None:
    """The real _CliEvalRunner runs the shipped eval engine offline (no keys).

    Proves the autopilot reuses the eval path rather than a new engine: a
    dataset whose expected output the mock returns scores 1.0 (all passing).
    """
    root = _make_project(tmp_path / "proj")
    snap = dc._CliEvalRunner(root, mock=True).run_eval("greeter")
    assert snap.total_cases == 1
    # The dataset-aware MockProvider returns the case's `expected`, so exact-match
    # accuracy is 1.0 → the case passes → no failures to improve.
    assert snap.all_passing
