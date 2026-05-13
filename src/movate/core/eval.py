"""Eval engine: dataset loader, scorers (exact + LLM-as-judge), runner.

Locked rules:

- Judge MUST be a different *family* than the agent (e.g. agent=openai/* → judge
  cannot be openai/* or azure/*). Same-family judging risks self-preference
  contamination. The check is enforced at config time, not run time.
- Each eval case runs through the same Executor as production runs, so cost,
  tracing, retries, and fallback behavior are identical.
- N runs per case + ``--gate-mode mean|min|p10`` mitigates LLM-judge variance.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml
from pydantic import ValidationError

from movate.core.models import (
    EvalRecord,
    JudgeConfig,
    JudgeMethod,
    Metrics,
    RunRequest,
    RunResponse,
)
from movate.providers import provider_family
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    Message,
)

if TYPE_CHECKING:
    from movate.core.executor import Executor
    from movate.core.loader import AgentBundle


class EvalConfigError(Exception):
    """Raised when judge.yaml or dataset is invalid."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    input: dict[str, Any]
    expected: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    objective: str | None = None
    """Which agent objective this case tests, if any.

    Cases declare ``"objective": "<id>"`` in dataset.jsonl rows. The id
    must match an entry in the agent's ``objectives:`` list (validated
    by ``mdk eval`` at engine entry; an unknown id is an EvalConfigError).
    Cases without an ``objective`` field land in the implicit "default"
    bucket — backwards-compat for every existing dataset.
    """


@dataclass
class CaseRun:
    """One execution of one case."""

    response: RunResponse
    score: float
    rationale: str


@dataclass
class CaseSummary:
    case: EvalCase
    runs: list[CaseRun]
    aggregated_score: float
    passed: bool

    @property
    def cost_usd(self) -> float:
        return round(sum(r.response.metrics.cost_usd for r in self.runs), 6)


@dataclass
class ObjectiveSummary:
    """Per-objective rollup for one EvalSummary.

    Aggregates the subset of cases tagged with this objective id, plus
    the objective's declared threshold from agent.yaml. The eval gate
    can target this directly: each objective passes / fails on its OWN
    threshold, independent of the overall pass rate.

    ``objective_id == "default"`` is the implicit bucket for cases that
    didn't declare an ``objective`` field — those are scored against
    the eval's --gate value (or no gate if absent).
    """

    objective_id: str
    description: str
    threshold: float
    judge_method: str
    cases: list[CaseSummary]

    @property
    def sample_count(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.passed) / len(self.cases)

    @property
    def mean_score(self) -> float:
        if not self.cases:
            return 0.0
        return statistics.fmean(c.aggregated_score for c in self.cases)

    @property
    def passed(self) -> bool:
        """True iff the objective's mean score meets its threshold."""
        return self.sample_count > 0 and self.mean_score >= self.threshold


@dataclass
class EvalSummary:
    agent: str
    agent_version: str
    dataset_hash: str
    judge: JudgeConfig
    judge_provider: str | None
    runs_per_case: int
    gate_mode: str
    threshold: float
    cases: list[CaseSummary]
    objective_summaries: list[ObjectiveSummary] = field(default_factory=list)
    """Per-objective rollup. Built by EvalEngine when the agent has
    ``objectives:`` declared in agent.yaml. Empty for legacy agents
    (no objectives → no per-objective view), in which case ``cases`` +
    the top-level threshold are the only assertions.
    """

    @property
    def sample_count(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.passed) / len(self.cases)

    @property
    def mean_score(self) -> float:
        if not self.cases:
            return 0.0
        return statistics.fmean(c.aggregated_score for c in self.cases)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.cases), 6)

    @property
    def overall_pass(self) -> bool:
        # Default rule: every case must pass at the per-case threshold.
        return self.sample_count > 0 and all(c.passed for c in self.cases)

    def to_record(self, *, tenant_id: str = "local") -> EvalRecord:
        return EvalRecord(
            eval_id=str(uuid4()),
            tenant_id=tenant_id,
            agent=self.agent,
            agent_version=self.agent_version,
            dataset_hash=self.dataset_hash,
            judge_method=self.judge.method,
            judge_provider=self.judge_provider,
            runs_per_case=self.runs_per_case,
            gate_mode=self.gate_mode,
            threshold=self.threshold,
            mean_score=round(self.mean_score, 6),
            pass_rate=round(self.pass_rate, 6),
            sample_count=self.sample_count,
            total_cost_usd=self.total_cost_usd,
        )


# ---------------------------------------------------------------------------
# Loader: dataset (.jsonl) and judge config (.yaml)
# ---------------------------------------------------------------------------


def load_dataset(bundle: AgentBundle) -> tuple[list[EvalCase], str]:
    """Read the agent's dataset; returns (cases, sha256-hex)."""
    if not bundle.spec.evals.dataset:
        raise EvalConfigError(f"agent {bundle.spec.name} has no evals.dataset configured")
    path = (bundle.agent_dir / bundle.spec.evals.dataset).resolve()
    if not path.exists():
        raise EvalConfigError(f"dataset not found: {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    cases: list[EvalCase] = []
    for line_no, line in enumerate(raw.decode().splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except json.JSONDecodeError as exc:
            raise EvalConfigError(f"{path}:{line_no} invalid JSON: {exc}") from exc
        if not isinstance(d, dict):
            raise EvalConfigError(f"{path}:{line_no} each row must be a JSON object")
        cases.append(
            EvalCase(
                input=d.get("input", {}),
                expected=d.get("expected", {}),
                tags=list(d.get("tags", []) or []),
                objective=d.get("objective"),
            )
        )
    return cases, digest


def load_judge_config(bundle: AgentBundle) -> JudgeConfig:
    """Resolve the judge config.

    Resolution order:
      1. Explicit path in ``agent.yaml: evals.judge``
      2. Convention: ``<agent>/evals/judge.yaml``
      3. Default: exact-match scoring
    """
    if bundle.spec.evals.judge:
        path = (bundle.agent_dir / bundle.spec.evals.judge).resolve()
    else:
        path = (bundle.agent_dir / "evals" / "judge.yaml").resolve()

    if not path.exists():
        return JudgeConfig(method=JudgeMethod.EXACT)

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise EvalConfigError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return JudgeConfig.model_validate(data)
    except ValidationError as exc:
        raise EvalConfigError(f"invalid judge config in {path}:\n{exc}") from exc


# ---------------------------------------------------------------------------
# Cross-family enforcement
# ---------------------------------------------------------------------------


def assert_cross_family(agent_provider: str, judge_provider: str) -> None:
    """Raise EvalConfigError if agent and judge share a model family."""
    a = provider_family(agent_provider)
    j = provider_family(judge_provider)
    if a == j:
        raise EvalConfigError(
            f"judge family {j!r} matches agent family — same-family judging risks "
            f"self-preference contamination. Pick a judge from a different family "
            f"(agent={agent_provider!r}, judge={judge_provider!r})."
        )


# ---------------------------------------------------------------------------
# Aggregation modes for N runs per case
# ---------------------------------------------------------------------------


_GATE_MODES = ("mean", "min", "p10")


def _build_objective_summaries(
    bundle: AgentBundle,
    case_summaries: list[CaseSummary],
    judge: JudgeConfig,
) -> list[ObjectiveSummary]:
    """Group case summaries by the case's ``objective`` field, producing
    one :class:`ObjectiveSummary` per declared agent objective.

    Cases without an ``objective`` field are skipped — the legacy
    top-level ``EvalSummary.cases`` already covers them. Agents with
    no objectives get an empty list (no per-objective view).

    The threshold on each ObjectiveSummary comes from the agent's
    objective declaration, not from the eval's --gate value. This is
    the key design point: an objective's pass criteria is part of the
    agent's contract (lives in agent.yaml), not a per-run flag.
    """
    if not bundle.spec.objectives:
        return []

    by_objective: dict[str, list[CaseSummary]] = {obj.id: [] for obj in bundle.spec.objectives}
    for cs in case_summaries:
        oid = cs.case.objective
        if oid is not None and oid in by_objective:
            by_objective[oid].append(cs)

    out: list[ObjectiveSummary] = []
    for obj in bundle.spec.objectives:
        out.append(
            ObjectiveSummary(
                objective_id=obj.id,
                description=obj.description,
                threshold=obj.threshold,
                judge_method=obj.judge,
                cases=by_objective[obj.id],
            )
        )
    return out


def aggregate_scores(scores: list[float], mode: str) -> float:
    """Reduce N per-run scores to one per-case score.

    ``mean``: defends against single-run noise (default for LLM-as-judge).
    ``min``:  conservative; catches any failure mode in the N runs.
    ``p10``:  near-worst-case but tolerates one bad outlier — good middle ground.
    """
    if not scores:
        return 0.0
    if mode == "mean":
        return statistics.fmean(scores)
    if mode == "min":
        return min(scores)
    if mode == "p10":
        if len(scores) == 1:
            return scores[0]
        sorted_s = sorted(scores)
        idx = max(0, int(len(sorted_s) * 0.10))
        return sorted_s[idx]
    raise EvalConfigError(f"unknown gate_mode {mode!r}; use one of {_GATE_MODES}")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


_JUDGE_PROMPT = """You are an expert evaluator. Score the actual output against the expected output.

Input:
{input_json}

Expected:
{expected_json}

Actual:
{actual_json}

Rubric:
{rubric}

Return ONLY a JSON object on a single line, no prose, no code fences:
{{"score": <float between 0.0 and 1.0>, "rationale": "<brief explanation>"}}
"""


class EvalEngine:
    def __init__(
        self,
        *,
        executor: Executor,
        provider: BaseLLMProvider,
        runs_per_case: int = 1,
        gate_mode: str = "mean",
        objective_filter: str | None = None,
        on_case_complete: Callable[[int, int, CaseSummary], None] | None = None,
    ) -> None:
        if runs_per_case < 1:
            raise EvalConfigError("runs_per_case must be >= 1")
        if gate_mode not in _GATE_MODES:
            raise EvalConfigError(f"gate_mode {gate_mode!r} must be one of {_GATE_MODES}")
        self._executor = executor
        self._provider = provider
        self._runs_per_case = runs_per_case
        self._gate_mode = gate_mode
        self._objective_filter = objective_filter
        """If set, only run cases whose ``objective`` field matches this id.
        Used by ``mdk eval --objective <id>`` to score / gate a single
        objective without running the whole dataset. Unknown id is an
        EvalConfigError at run() entry."""
        self._on_case_complete = on_case_complete
        """Optional progress hook: ``(done, total, summary)``. Fires
        after each case finishes; CLI uses it to drive a Rich progress
        bar without coupling the engine to UI."""

    async def run(self, bundle: AgentBundle) -> EvalSummary:
        judge = load_judge_config(bundle)
        self._validate_judge(bundle, judge)
        cases, dataset_hash = load_dataset(bundle)

        # Validate that every case's `objective` references a real
        # objective id on the agent. Cases without an objective land
        # in the "default" bucket — allowed for legacy datasets.
        declared_objective_ids = {obj.id for obj in bundle.spec.objectives}
        for i, case in enumerate(cases, start=1):
            if case.objective is not None and case.objective not in declared_objective_ids:
                raise EvalConfigError(
                    f"dataset case #{i} declares objective={case.objective!r} which "
                    f"isn't defined in agent.yaml. Known objectives: "
                    f"{sorted(declared_objective_ids) or '(none)'}"
                )

        # --objective <id> filter
        if self._objective_filter is not None:
            if self._objective_filter not in declared_objective_ids:
                raise EvalConfigError(
                    f"--objective {self._objective_filter!r} doesn't match any objective "
                    f"in agent.yaml. Known: {sorted(declared_objective_ids) or '(none)'}"
                )
            cases = [c for c in cases if c.objective == self._objective_filter]
            if not cases:
                raise EvalConfigError(
                    f"--objective {self._objective_filter!r} matched zero cases in the "
                    f"dataset. Tag dataset rows with this objective id to score them."
                )

        case_summaries: list[CaseSummary] = []
        total = len(cases)
        for case in cases:
            runs: list[CaseRun] = []
            for _ in range(self._runs_per_case):
                response = await self._executor.execute(
                    bundle, RunRequest(agent=bundle.spec.name, input=case.input)
                )
                if response.status != "success":
                    runs.append(
                        CaseRun(
                            response=response,
                            score=0.0,
                            rationale=(
                                response.error.message if response.error else "agent failed"
                            ),
                        )
                    )
                    continue
                score, rationale = await self._score(case, response.data, judge)
                runs.append(CaseRun(response=response, score=score, rationale=rationale))

            agg = aggregate_scores([r.score for r in runs], self._gate_mode)
            summary = CaseSummary(
                case=case,
                runs=runs,
                aggregated_score=agg,
                passed=agg >= judge.threshold,
            )
            case_summaries.append(summary)
            if self._on_case_complete is not None:
                # Decorative; can't kill the run. UI is best-effort,
                # the eval result is the source of truth.
                with contextlib.suppress(Exception):
                    self._on_case_complete(len(case_summaries), total, summary)

        judge_provider = (
            judge.model.provider if judge.method == JudgeMethod.LLM_JUDGE and judge.model else None
        )

        # Per-objective rollup. Build one summary per declared objective,
        # plus an implicit "default" bucket for cases that didn't declare
        # an objective field. Empty when the agent has no objectives:
        # this is the legacy code path and the existing top-level
        # cases/threshold are the only assertions.
        objective_summaries = _build_objective_summaries(bundle, case_summaries, judge)

        return EvalSummary(
            agent=bundle.spec.name,
            agent_version=bundle.spec.version,
            dataset_hash=dataset_hash,
            judge=judge,
            judge_provider=judge_provider,
            runs_per_case=self._runs_per_case,
            gate_mode=self._gate_mode,
            threshold=judge.threshold,
            cases=case_summaries,
            objective_summaries=objective_summaries,
        )

    # ---------------------------------------------------------- private

    def _validate_judge(self, bundle: AgentBundle, judge: JudgeConfig) -> None:
        if judge.method != JudgeMethod.LLM_JUDGE:
            return
        if judge.model is None or judge.rubric is None:
            raise EvalConfigError("llm_judge requires both 'model' and 'rubric'")
        assert_cross_family(bundle.spec.model.provider, judge.model.provider)

    async def _score(
        self,
        case: EvalCase,
        actual: dict[str, Any],
        judge: JudgeConfig,
    ) -> tuple[float, str]:
        if judge.method == JudgeMethod.EXACT:
            return (1.0, "exact match") if actual == case.expected else (0.0, "mismatch")

        assert judge.model is not None and judge.rubric is not None  # validated upstream
        prompt = _JUDGE_PROMPT.format(
            input_json=json.dumps(case.input),
            expected_json=json.dumps(case.expected),
            actual_json=json.dumps(actual),
            rubric=judge.rubric,
        )
        req = CompletionRequest(
            provider=judge.model.provider,
            messages=[Message(role="user", content=prompt)],
            params=dict(judge.model.params),
        )
        response = await self._provider.complete(req)
        return _parse_judge_response(response.text)


def _parse_judge_response(text: str) -> tuple[float, str]:
    """Tolerant parser: strips fences, falls back to last `{...}` substring."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.rfind("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise EvalConfigError(f"judge returned non-JSON: {text!r}") from None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise EvalConfigError(f"judge returned non-JSON: {text!r}") from exc
    if not isinstance(parsed, dict) or "score" not in parsed:
        raise EvalConfigError(f"judge response missing 'score': {parsed!r}")
    score = max(0.0, min(1.0, float(parsed["score"])))
    rationale = str(parsed.get("rationale", ""))
    return score, rationale


# Re-export Metrics so callers don't need a second import.
__all__ = [
    "CaseRun",
    "CaseSummary",
    "EvalCase",
    "EvalConfigError",
    "EvalEngine",
    "EvalSummary",
    "Metrics",
    "aggregate_scores",
    "assert_cross_family",
    "load_dataset",
    "load_judge_config",
]
