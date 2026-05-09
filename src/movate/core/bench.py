"""Multi-model benchmark engine.

Runs the same input through N models (with N runs per model for variance
defense) and reports cost / latency / quality per model. Quality scoring is
optional: if a rubric is provided (inline string or via the agent's
``evals/judge.yaml``), a freeform LLM-as-judge produces a score.

Cross-family enforcement: per-family judge ≠ tested model. If the configured
judge happens to share a family with one of the tested models, that row is
skipped with a warning rather than failing the whole bench — a partial
comparison is more useful than a hard stop.

Each row uses ``Executor.execute(model_override=...)`` so the configured
fallback chain is bypassed (the bench tests one model in isolation, never a
fallback's blended output).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from movate.core.eval import (
    EvalConfigError,
    _parse_judge_response,
    aggregate_scores,
    assert_cross_family,
)
from movate.core.models import (
    JudgeConfig,
    JudgeMethod,
    ModelConfig,
    RunRequest,
    RunResponse,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    Message,
)

if TYPE_CHECKING:
    from movate.core.executor import Executor
    from movate.core.loader import AgentBundle


_FREEFORM_JUDGE_PROMPT = """You are an expert evaluator. Score the answer against the rubric.

Input:
{input_json}

Answer:
{actual_json}

Rubric:
{rubric}

Return ONLY a JSON object on a single line, no prose, no code fences:
{{"score": <float between 0.0 and 1.0>, "rationale": "<brief explanation>"}}
"""


@dataclass
class BenchRun:
    response: RunResponse
    score: float | None
    rationale: str


@dataclass
class ModelBenchResult:
    provider: str
    runs: list[BenchRun]
    skipped_reason: str | None = None
    skipped_score: bool = False  # judge skipped (e.g. same family) but agent ran

    @property
    def successful_runs(self) -> list[BenchRun]:
        return [r for r in self.runs if r.response.status == "success"]

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.runs if r.response.status != "success")

    @property
    def cost_total_usd(self) -> float:
        return round(sum(r.response.metrics.cost_usd for r in self.runs), 6)

    @property
    def cost_mean_usd(self) -> float:
        ok = self.successful_runs
        if not ok:
            return 0.0
        return round(sum(r.response.metrics.cost_usd for r in ok) / len(ok), 6)

    @property
    def latency_p50_ms(self) -> int:
        ok = self.successful_runs
        if not ok:
            return 0
        return int(statistics.median(r.response.metrics.latency_ms for r in ok))

    @property
    def latency_p95_ms(self) -> int:
        ok = self.successful_runs
        if not ok:
            return 0
        if len(ok) == 1:
            return ok[0].response.metrics.latency_ms
        sorted_lat = sorted(r.response.metrics.latency_ms for r in ok)
        idx = max(0, int(len(sorted_lat) * 0.95) - 1)
        return sorted_lat[idx]

    @property
    def sample_output(self) -> dict[str, Any] | None:
        ok = self.successful_runs
        return ok[0].response.data if ok else None

    def aggregated_score(self, mode: str) -> float | None:
        scored = [r.score for r in self.successful_runs if r.score is not None]
        if not scored:
            return None
        return aggregate_scores(scored, mode)


@dataclass
class BenchSummary:
    agent: str
    agent_version: str
    input: dict[str, Any]
    judge_provider: str | None
    rubric: str | None
    runs_per_model: int
    gate_mode: str
    models: list[ModelBenchResult] = field(default_factory=list)


class BenchEngine:
    def __init__(
        self,
        *,
        executor: Executor,
        provider: BaseLLMProvider,
        runs_per_model: int = 1,
        gate_mode: str = "mean",
        judge: JudgeConfig | None = None,
        rubric: str | None = None,
    ) -> None:
        if runs_per_model < 1:
            raise EvalConfigError("runs_per_model must be >= 1")
        if judge is not None and judge.method == JudgeMethod.LLM_JUDGE:
            if judge.model is None:
                raise EvalConfigError("llm_judge requires 'model'")
            if rubric is None and judge.rubric is None:
                raise EvalConfigError("llm_judge requires a rubric (inline or in judge config)")
        self._executor = executor
        self._provider = provider
        self._runs_per_model = runs_per_model
        self._gate_mode = gate_mode
        self._judge = judge
        self._rubric = rubric or (judge.rubric if judge else None)

    async def run(
        self,
        bundle: AgentBundle,
        *,
        input_payload: dict[str, Any],
        providers: list[str],
    ) -> BenchSummary:
        if not providers:
            raise EvalConfigError("bench requires at least one --model")

        results: list[ModelBenchResult] = []
        for prov in providers:
            judge_skipped = False
            if self._judge and self._judge.method == JudgeMethod.LLM_JUDGE:
                try:
                    assert self._judge.model is not None
                    assert_cross_family(prov, self._judge.model.provider)
                except EvalConfigError:
                    judge_skipped = True

            override = ModelConfig(provider=prov, params=dict(bundle.spec.model.params))
            runs: list[BenchRun] = []
            for _ in range(self._runs_per_model):
                response = await self._executor.execute(
                    bundle,
                    RunRequest(agent=bundle.spec.name, input=input_payload),
                    model_override=override,
                )
                if response.status != "success":
                    runs.append(
                        BenchRun(
                            response=response,
                            score=None,
                            rationale=(
                                response.error.message if response.error else "agent failed"
                            ),
                        )
                    )
                    continue

                score: float | None = None
                rationale = ""
                if (
                    self._judge
                    and self._judge.method == JudgeMethod.LLM_JUDGE
                    and not judge_skipped
                ):
                    score, rationale = await self._score_freeform(input_payload, response.data)
                runs.append(BenchRun(response=response, score=score, rationale=rationale))

            results.append(
                ModelBenchResult(
                    provider=prov,
                    runs=runs,
                    skipped_score=judge_skipped,
                )
            )

        judge_provider = (
            self._judge.model.provider
            if self._judge and self._judge.method == JudgeMethod.LLM_JUDGE and self._judge.model
            else None
        )
        return BenchSummary(
            agent=bundle.spec.name,
            agent_version=bundle.spec.version,
            input=input_payload,
            judge_provider=judge_provider,
            rubric=self._rubric,
            runs_per_model=self._runs_per_model,
            gate_mode=self._gate_mode,
            models=results,
        )

    async def _score_freeform(
        self, input_payload: dict[str, Any], actual: dict[str, Any]
    ) -> tuple[float, str]:
        assert self._judge is not None and self._judge.model is not None
        assert self._rubric is not None
        prompt = _FREEFORM_JUDGE_PROMPT.format(
            input_json=json.dumps(input_payload),
            actual_json=json.dumps(actual),
            rubric=self._rubric,
        )
        req = CompletionRequest(
            provider=self._judge.model.provider,
            messages=[Message(role="user", content=prompt)],
            params=dict(self._judge.model.params),
        )
        response = await self._provider.complete(req)
        return _parse_judge_response(response.text)


__all__ = [
    "BenchEngine",
    "BenchRun",
    "BenchSummary",
    "ModelBenchResult",
]
