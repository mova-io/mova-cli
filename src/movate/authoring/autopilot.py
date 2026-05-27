"""The "improve my agent" autopilot (ADR 025 D7) — close the harvest→improve loop.

This wires ADR 016's harvest→eval signal into the authoring catalog: it runs the
agent's evals, reads the **failing cases** (+ the ADR-024 per-step cost where it
helps), asks the **existing** :class:`~movate.authoring.planner.Planner` to
propose **targeted catalog actions** that fix them, and drives each proposal
through the **existing** :class:`~movate.authoring.driver.AuthoringDriver`
plan → preview → confirm → apply → verify spine. The LLM proposes; the driver
gates / applies / verifies / reverts. The autopilot never edits files itself.

What it reuses (no new engine work — pure orchestration over shipped seams):

* **eval** — the `eval` path (:class:`movate.core.eval.EvalEngine`) produces the
  pass/fail + failing-case detail. The :class:`EvalRunner` Protocol abstracts
  "run the evals → return a compact :class:`EvalSnapshot`" so a real run and a
  stubbed/mock run share one shape. :class:`MockEvalRunner` returns a scripted
  snapshot so the whole autopilot is hermetic (no keys, no network) — the
  `--mock` test path the brief requires.
* **planner** — the same provider-pluggable :class:`Planner` the conversational
  copilot uses. :func:`propose_improvements` turns the failure summary into one
  grounded request and calls ``planner.plan``; ambiguous → clarification → the
  pass stops (it never guesses).
* **driver** — every proposal is applied via :class:`AuthoringDriver`, so the D2
  confirmation gate (cost / networked / destructive stay confirm-gated), D3
  verify (validate → run --mock, revert-on-failure), and D4 checkpoint/undo all
  hold transitively. An unknown action name is rejected by the driver's catalog
  lookup; only valid catalog actions can be applied.

Bounds (D8 / brief): a per-pass cap on proposed actions
(:data:`DEFAULT_MAX_ACTIONS_PER_PASS`) and a cap on improvement iterations
(:data:`DEFAULT_MAX_ITERATIONS`) — there is no infinite improve loop.

Boundary: this is a control-plane authoring tool (``cli`` ⊥ ``runtime``); it
composes the eval engine + planner + catalog driver and ships nothing into the
execution plane.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from movate.authoring.base import AuthoringActionError
from movate.authoring.budget import BudgetExceededError
from movate.authoring.catalog import UnknownActionError, action_names
from movate.authoring.driver import ApplyOutcome, AuthoringDriver, ConfirmationRequiredError

if TYPE_CHECKING:
    from movate.authoring.models import ActionPlan
    from movate.authoring.planner import Planner, ProposedAction

# Bounds — keep one improve pass small and the loop finite (D8 / brief).
DEFAULT_MAX_ACTIONS_PER_PASS = 3
"""Cap on catalog actions proposed (and applied) per improve pass. A planner
that returns more is truncated to this many — one pass makes a *small*, easy to
review set of edits, not a sweeping rewrite."""

DEFAULT_MAX_ITERATIONS = 3
"""Cap on improve iterations (eval → propose → apply → re-eval) per autopilot
run. Bounds total cost / time so the loop can never run away."""

# How many failing cases to surface to the planner. The planner only needs a
# representative sample of failure modes — feeding every failure bloats the
# prompt without improving the proposal.
_MAX_FAILURES_IN_SUMMARY = 5


# ---------------------------------------------------------------------------
# Compact failure summary — the eval signal the planner is grounded in (D7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailingCase:
    """One failing eval case, compacted for the planner (D7).

    Carries just what the planner needs to reason about the failure mode: the
    case input, what was expected, what the agent actually produced, the
    aggregated score, the judge/scorer rationale, and the ADR-024 run cost
    (so a cost-aware planner can prefer cheap fixes). JSON-serializable.
    """

    input: dict[str, object]
    expected: dict[str, object]
    actual: dict[str, object]
    score: float
    rationale: str = ""
    cost_usd: float = 0.0


@dataclass(frozen=True)
class EvalSnapshot:
    """A compact pass/fail snapshot of one eval run (D7).

    The :class:`EvalRunner` Protocol returns this; the autopilot reads
    :attr:`failures` to drive a pass and :attr:`pass_rate` to measure whether an
    iteration actually improved the agent. Deliberately small + JSON-friendly so
    a real eval, a stub, and the mock all produce the same shape.
    """

    total_cases: int
    passed_cases: int
    failures: list[FailingCase] = field(default_factory=list)
    mean_score: float = 0.0
    total_cost_usd: float = 0.0

    @property
    def pass_rate(self) -> float:
        """Fraction of cases that passed (``0.0`` when there are no cases)."""
        if self.total_cases <= 0:
            return 0.0
        return self.passed_cases / self.total_cases

    @property
    def all_passing(self) -> bool:
        """True when there is at least one case and none failed."""
        return self.total_cases > 0 and not self.failures


@runtime_checkable
class EvalRunner(Protocol):
    """The "run the agent's evals → compact snapshot" seam (D7).

    Abstracts the eval path so the autopilot is testable without a model:
    :class:`MockEvalRunner` returns scripted snapshots; the real runner (the
    thin CLI wires it) reuses :class:`movate.core.eval.EvalEngine`. The
    autopilot never constructs an executor / eval engine itself — it only
    consumes this Protocol, keeping ``cli`` ⊥ ``runtime`` intact.
    """

    def run_eval(self, agent: str) -> EvalSnapshot:
        """Run ``agent``'s eval suite once and return a compact snapshot."""
        ...


class MockEvalRunner:
    """A deterministic, scripted :class:`EvalRunner` — the hermetic test path (D7).

    Returns a pre-baked sequence of :class:`EvalSnapshot`s with NO executor, NO
    model, NO network — so the whole autopilot (eval → propose → apply → verify
    → re-eval) is CI-runnable offline. It backs ``mdk dev --mock``'s improve
    action and every autopilot test.

    Pass ``snapshots`` (an ordered list): each :meth:`run_eval` call returns the
    next one, and the last repeats once exhausted. A test scripts "fails case X,
    then passes after the fix" as ``[failing_snapshot, passing_snapshot]``.
    """

    def __init__(self, snapshots: list[EvalSnapshot]) -> None:
        if not snapshots:
            raise ValueError("MockEvalRunner needs at least one snapshot")
        self._snapshots = list(snapshots)
        self._idx = 0
        self.calls: list[str] = []
        """Agents passed to :meth:`run_eval`, in order — lets a test assert the
        loop actually re-ran the eval after applying a fix."""

    def run_eval(self, agent: str) -> EvalSnapshot:
        self.calls.append(agent)
        snap = self._snapshots[min(self._idx, len(self._snapshots) - 1)]
        self._idx += 1
        return snap


# ---------------------------------------------------------------------------
# Failure summary → grounded planner request (reuses the existing planner, D7)
# ---------------------------------------------------------------------------


def build_improve_request(snapshot: EvalSnapshot, *, max_actions: int) -> str:
    """Render the failing cases into ONE natural-language request for the planner.

    The autopilot does not invent a second planner: it feeds this failure-
    grounded request to the *existing* :class:`Planner` (the same one the
    conversational copilot uses), so the planner maps it to typed catalog
    actions exactly as it would a hand-typed request. Bounded: at most
    :data:`_MAX_FAILURES_IN_SUMMARY` failures are surfaced and the request asks
    for at most ``max_actions`` actions.
    """
    lines: list[str] = [
        "The agent is failing the cases below in its eval suite. Propose targeted "
        "authoring actions that would FIX these failures — for example tighten the "
        "instructions for a recurring failure mode (edit-instructions), add a missing "
        "fact as a context (add-context), or lock in a corrected case (add-eval-case). "
        f"Propose at most {max_actions} action(s); prefer the smallest fix that "
        "addresses the most failures.",
        "",
        f"Pass rate: {snapshot.passed_cases}/{snapshot.total_cases} "
        f"(mean score {snapshot.mean_score:.2f}).",
        "",
        "Failing cases:",
    ]
    for i, fc in enumerate(snapshot.failures[:_MAX_FAILURES_IN_SUMMARY], start=1):
        lines.append(
            f"{i}. input={fc.input!r} expected={fc.expected!r} "
            f"actual={fc.actual!r} score={fc.score:.2f}"
            + (f" — {fc.rationale}" if fc.rationale else "")
            + (f" (run cost ~${fc.cost_usd:.4f})" if fc.cost_usd else "")
        )
    return "\n".join(lines)


def propose_improvements(
    planner: Planner,
    snapshot: EvalSnapshot,
    *,
    agent: str,
    max_actions: int = DEFAULT_MAX_ACTIONS_PER_PASS,
) -> list[ProposedAction]:
    """Ask the planner for bounded, valid catalog actions that fix the failures.

    Reuses the existing :class:`Planner` seam: builds a failure-grounded request
    (:func:`build_improve_request`) and calls ``planner.plan``. The result is:

    * **bounded** — truncated to ``max_actions`` (the per-pass cap, D8);
    * **valid** — every proposed action name is checked against the catalog;
      unknown names are dropped (the driver would reject them anyway, but we
      filter early so the pass doesn't waste a confirm prompt on a no-op);
    * **safe** — a clarification outcome (ambiguous) yields an empty list, so
      the autopilot mutates nothing rather than guessing (D6).
    """
    if not snapshot.failures:
        return []
    request = build_improve_request(snapshot, max_actions=max_actions)
    outcome = planner.plan(request, agent=agent)
    if outcome.is_clarification:
        return []
    known = set(action_names())
    valid = [a for a in outcome.actions if a.name in known]
    return valid[:max_actions]


# ---------------------------------------------------------------------------
# Pass / run results
# ---------------------------------------------------------------------------


@dataclass
class AppliedProposal:
    """One proposed action carried through the driver in an improve pass."""

    proposed: ProposedAction
    outcome: ApplyOutcome | None = None
    skipped: bool = False
    """True when the confirm gate said no (declined / not confirmed)."""
    error: str | None = None
    """Set when planning/applying the proposal failed (recorded, not raised)."""

    @property
    def applied(self) -> bool:
        """True when the action was applied and not reverted by verify."""
        out = self.outcome
        return (
            out is not None
            and out.result is not None
            and not (out.verify is not None and out.verify.reverted)
        )


@dataclass
class ImprovePass:
    """The result of one eval → propose → apply pass."""

    iteration: int
    before: EvalSnapshot
    proposals: list[AppliedProposal] = field(default_factory=list)

    @property
    def applied_count(self) -> int:
        return sum(1 for p in self.proposals if p.applied)


@dataclass
class AutopilotResult:
    """The full result of an autopilot run (one or more bounded passes)."""

    initial: EvalSnapshot
    final: EvalSnapshot
    passes: list[ImprovePass] = field(default_factory=list)
    budget_exceeded: bool = False
    """True when the run stopped early because the session LLM budget was hit
    (D7e). The planner refuses the next proposal call; the autopilot ends the
    loop cleanly with whatever it had already applied — no half-applied action."""

    @property
    def improved(self) -> bool:
        """True when the final pass rate beats the initial one."""
        return self.final.pass_rate > self.initial.pass_rate

    @property
    def total_applied(self) -> int:
        return sum(p.applied_count for p in self.passes)


# ---------------------------------------------------------------------------
# The autopilot — orchestrates eval → propose → drive → re-eval (bounded)
# ---------------------------------------------------------------------------


# A confirm callback: given a proposal + its dry-run plan, return True to apply.
ConfirmFn = Callable[["ProposedAction", "ActionPlan"], bool]


class Autopilot:
    """Closes the eval → improve loop over the catalog (ADR 025 D7).

    Construct with an :class:`EvalRunner` (the eval signal), a :class:`Planner`
    (the proposer), and an :class:`AuthoringDriver` (the safe apply path). Then
    call :meth:`run`. Everything composes shipped seams — no new eval engine, no
    raw filesystem writes (the driver owns those), no LLM hardcoded in.

    Safety + bounds:

    * proposals are previewed (the driver's plan) and confirmed via ``confirm``
      before apply — gated actions are never auto-applied;
    * verify (validate → run --mock) runs after apply and reverts on failure;
    * at most ``max_actions_per_pass`` actions per pass and ``max_iterations``
      passes — the loop is finite.
    """

    def __init__(
        self,
        *,
        eval_runner: EvalRunner,
        planner: Planner,
        driver: AuthoringDriver,
        max_actions_per_pass: int = DEFAULT_MAX_ACTIONS_PER_PASS,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        if max_actions_per_pass < 1:
            raise ValueError("max_actions_per_pass must be >= 1")
        if max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        self._eval_runner = eval_runner
        self._planner = planner
        self._driver = driver
        self._max_actions = max_actions_per_pass
        self._max_iterations = max_iterations

    def run(
        self,
        agent: str,
        *,
        confirm: ConfirmFn | None = None,
        fast_mode: bool = False,
    ) -> AutopilotResult:
        """Run the bounded improve loop for ``agent``.

        Parameters
        ----------
        confirm:
            Called per proposal with ``(proposed, plan)`` before apply; return
            True to apply. When ``None``, only additive+reversible+free plans
            auto-apply (and only if ``fast_mode``); any confirmation-gated plan
            is skipped. This is the D2 gate — the autopilot never silently
            applies a cost/networked/destructive action.
        fast_mode:
            Auto-apply additive+reversible+free proposals without a confirm
            callback (opt-in fast path). Has no effect on gated plans.

        The loop stops early once every case passes, or once a pass applies no
        actions (the planner had nothing useful / everything was declined) —
        whichever comes first, and at most ``max_iterations`` passes.
        """
        initial = self._eval_runner.run_eval(agent)
        current = initial
        passes: list[ImprovePass] = []
        budget_exceeded = False

        for iteration in range(1, self._max_iterations + 1):
            if current.all_passing:
                break
            try:
                improve_pass = self._run_pass(
                    agent, iteration, current, confirm=confirm, fast_mode=fast_mode
                )
            except BudgetExceededError:
                # The planner refused the next proposal call (D7e): stop the loop
                # cleanly with whatever already applied. Enforcement happens
                # BEFORE the call, so nothing is half-applied.
                budget_exceeded = True
                break
            passes.append(improve_pass)
            if improve_pass.applied_count == 0:
                # Nothing changed this pass (no proposals, all declined, or all
                # reverted) → re-running the eval would just repeat → stop.
                break
            current = self._eval_runner.run_eval(agent)

        return AutopilotResult(
            initial=initial, final=current, passes=passes, budget_exceeded=budget_exceeded
        )

    def _run_pass(
        self,
        agent: str,
        iteration: int,
        snapshot: EvalSnapshot,
        *,
        confirm: ConfirmFn | None,
        fast_mode: bool,
    ) -> ImprovePass:
        """Propose (bounded) + drive each proposal through the confirm-gated spine."""
        proposals = propose_improvements(
            self._planner, snapshot, agent=agent, max_actions=self._max_actions
        )
        result = ImprovePass(iteration=iteration, before=snapshot)
        for proposed in proposals:
            result.proposals.append(self._drive_one(proposed, confirm=confirm, fast_mode=fast_mode))
        return result

    def _drive_one(
        self,
        proposed: ProposedAction,
        *,
        confirm: ConfirmFn | None,
        fast_mode: bool,
    ) -> AppliedProposal:
        """Plan → confirm → apply → verify one proposal through the driver.

        Failures are recorded on the returned :class:`AppliedProposal`, never
        raised — one bad proposal must not abort the whole pass.
        """
        applied = AppliedProposal(proposed=proposed)
        try:
            plan = self._driver.plan(proposed.name, proposed.args)
        except (UnknownActionError, AuthoringActionError, ValueError) as exc:
            applied.error = f"plan failed: {exc}"
            return applied

        # D2 gate. A confirm callback decides; absent one, only auto-apply an
        # additive+reversible+free plan in fast_mode — never a gated plan.
        if confirm is not None:
            go = confirm(proposed, plan)
        else:
            go = fast_mode and not plan.requires_confirmation
        if not go:
            applied.skipped = True
            return applied

        try:
            # Networked actions (ingest-kb) have no meaningful mock run; the
            # rest run the D3 verify loop. Mirrors the copilot's choice.
            verify = "network" not in [s.value for s in plan.side_effects]
            applied.outcome = self._driver.apply(
                proposed.name,
                proposed.args,
                confirmed=True,
                fast_mode=fast_mode,
                verify=verify,
            )
        except ConfirmationRequiredError as exc:
            # The plan needed confirmation the gate didn't grant — treat as a
            # skip, not an error (the user/policy declined).
            applied.skipped = True
            applied.error = str(exc)
        except (AuthoringActionError, ValueError) as exc:
            applied.error = f"apply failed: {exc}"
        return applied


__all__ = [
    "DEFAULT_MAX_ACTIONS_PER_PASS",
    "DEFAULT_MAX_ITERATIONS",
    "AppliedProposal",
    "Autopilot",
    "AutopilotResult",
    "ConfirmFn",
    "EvalRunner",
    "EvalSnapshot",
    "FailingCase",
    "ImprovePass",
    "MockEvalRunner",
    "build_improve_request",
    "propose_improvements",
]
