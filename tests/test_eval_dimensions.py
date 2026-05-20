"""Four-dimension eval scoring (v0.6): accuracy / faithfulness / coverage / latency.

Covers:

  * Pure scoring helpers — coverage (deterministic) and latency
    (budget-relative curve) without an executor.
  * Per-dim ``DimensionScores`` + the ``DimensionalMeans`` rollup.
  * Dataset parsing of the new optional fields (``grounding``,
    ``expected_coverage``, ``latency_budget_ms``) with type validation.
  * Engine end-to-end through ``MockProvider`` / ``JudgeStubProvider``
    populating dimensions correctly.
  * Back-compat: ``CaseRun.score`` (the gate input) stays accuracy-only.
"""

from __future__ import annotations

import json as _json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.eval import (
    Dimension,
    DimensionalMeans,
    DimensionScore,
    DimensionScores,
    EvalConfigError,
    EvalEngine,
    _compute_dimensional_means,
    _score_coverage,
    _score_latency,
    load_dataset,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import (
    InMemoryStorage,
    NullTracer,
    scaffold_agent,
)

# ---------------------------------------------------------------------------
# Fixtures (mirror test_eval.py — small, no shared conftest)
# ---------------------------------------------------------------------------


def _scaffold(dst: Path, name: str = "demo") -> Path:
    return scaffold_agent(dst, name=name)


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


def _executor(provider: BaseLLMProvider, pricing: PricingTable, storage, tracer) -> Executor:
    return Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)


# ---------------------------------------------------------------------------
# Dimension enum + DimensionScore plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dimension_enum_members() -> None:
    """All four dims are defined and the values match the JSON keys."""
    assert Dimension.ACCURACY.value == "accuracy"
    assert Dimension.FAITHFULNESS.value == "faithfulness"
    assert Dimension.COVERAGE.value == "coverage"
    assert Dimension.LATENCY.value == "latency"


@pytest.mark.unit
def test_dimension_score_defaults_to_unscored() -> None:
    ds = DimensionScore()
    assert ds.value is None
    assert ds.rationale == ""


@pytest.mark.unit
def test_dimension_scores_scored_values_skips_none() -> None:
    """``scored_values`` returns only the dims with a value; unscored omitted."""
    dims = DimensionScores(
        accuracy=DimensionScore(0.8, "ok"),
        faithfulness=DimensionScore(),  # unscored
        coverage=DimensionScore(1.0, "all topics"),
        latency=DimensionScore(0.5, "over budget"),
    )
    vs = dims.scored_values()
    assert sorted(vs) == [0.5, 0.8, 1.0]


@pytest.mark.unit
def test_dimension_scores_aggregate_empty_is_zero() -> None:
    """No dim scored → aggregate is 0.0 (sentinel; gate uses accuracy alone)."""
    dims = DimensionScores()
    assert dims.aggregate() == 0.0


@pytest.mark.unit
def test_dimension_scores_aggregate_mean_of_scored() -> None:
    dims = DimensionScores(
        accuracy=DimensionScore(0.8, ""),
        coverage=DimensionScore(0.6, ""),
        latency=DimensionScore(1.0, ""),
    )
    # Mean of three scored dims = 0.8.
    assert dims.aggregate() == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# _score_coverage — deterministic substring match
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_score_coverage_full_match() -> None:
    ds = _score_coverage(
        ["price", "warranty"],
        {"answer": "Price is $50, warranty is 1 year"},
    )
    assert ds.value == pytest.approx(1.0)
    assert ds.rationale == "all topics covered"


@pytest.mark.unit
def test_score_coverage_partial_match() -> None:
    ds = _score_coverage(
        ["price", "warranty", "shipping"],
        {"answer": "Price is $50, warranty 1 year"},
    )
    assert ds.value == pytest.approx(2 / 3)
    assert "shipping" in ds.rationale


@pytest.mark.unit
def test_score_coverage_case_insensitive() -> None:
    ds = _score_coverage(["WARRANTY"], {"answer": "warranty included"})
    assert ds.value == pytest.approx(1.0)


@pytest.mark.unit
def test_score_coverage_no_match_is_zero() -> None:
    ds = _score_coverage(["zzz"], {"answer": "nothing relevant"})
    assert ds.value == pytest.approx(0.0)


@pytest.mark.unit
def test_score_coverage_empty_list_is_unscored() -> None:
    """Empty expected_coverage → unscored DimensionScore (None)."""
    ds = _score_coverage([], {"answer": "x"})
    assert ds.value is None


@pytest.mark.unit
def test_score_coverage_searches_nested_dict() -> None:
    """The JSON-stringified output catches keyword presence in any field."""
    ds = _score_coverage(["nested-value"], {"meta": {"detail": "nested-value here"}})
    assert ds.value == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _score_latency — budget-relative curve
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_score_latency_within_budget_is_one() -> None:
    ds = _score_latency(latency_ms=500, budget_ms=1000)
    assert ds.value == pytest.approx(1.0)
    assert "within budget" in ds.rationale


@pytest.mark.unit
def test_score_latency_exactly_at_budget_is_one() -> None:
    ds = _score_latency(latency_ms=1000, budget_ms=1000)
    assert ds.value == pytest.approx(1.0)


@pytest.mark.unit
def test_score_latency_one_point_five_budget_is_half() -> None:
    """1.5x budget -> linear decay puts the score at 0.5."""
    ds = _score_latency(latency_ms=1500, budget_ms=1000)
    assert ds.value == pytest.approx(0.5)
    assert "over budget" in ds.rationale


@pytest.mark.unit
def test_score_latency_two_x_budget_is_zero() -> None:
    ds = _score_latency(latency_ms=2000, budget_ms=1000)
    assert ds.value == pytest.approx(0.0)


@pytest.mark.unit
def test_score_latency_well_past_budget_is_zero() -> None:
    """Score clamps at 0; doesn't go negative."""
    ds = _score_latency(latency_ms=10_000, budget_ms=1000)
    assert ds.value == pytest.approx(0.0)


@pytest.mark.unit
def test_score_latency_invalid_budget_is_unscored() -> None:
    """0 / negative budget can't produce a meaningful score → unscored."""
    ds = _score_latency(latency_ms=500, budget_ms=0)
    assert ds.value is None
    assert "invalid budget" in ds.rationale


# ---------------------------------------------------------------------------
# _compute_dimensional_means rollup
# ---------------------------------------------------------------------------


def _case_summary_with(
    dims_per_run: list[DimensionScores],
) -> object:
    """Build a minimal CaseSummary-like with N runs each carrying given dims.

    Returns a small ad-hoc namespace because the only attribute
    ``_compute_dimensional_means`` walks is ``.runs[i].dimensions``.
    """
    runs = [SimpleNamespace(dimensions=d) for d in dims_per_run]
    return SimpleNamespace(runs=runs)


@pytest.mark.unit
def test_dimensional_means_no_cases_is_all_none() -> None:
    means = _compute_dimensional_means([])
    assert means == DimensionalMeans()  # every field None


@pytest.mark.unit
def test_dimensional_means_skips_none_in_denominator() -> None:
    """A dim with one scored + one None case → mean is the scored value alone."""
    cases = [
        _case_summary_with([DimensionScores(accuracy=DimensionScore(1.0, ""))]),
        _case_summary_with([DimensionScores(accuracy=DimensionScore(0.0, ""))]),
        _case_summary_with(
            [DimensionScores(accuracy=DimensionScore())]  # unscored
        ),
    ]
    means = _compute_dimensional_means(cases)  # type: ignore[arg-type]
    assert means.accuracy == pytest.approx(0.5)
    # No case scored these dims → None.
    assert means.faithfulness is None
    assert means.coverage is None
    assert means.latency is None


@pytest.mark.unit
def test_dimensional_means_averages_across_runs_too() -> None:
    """Multiple runs per case all contribute to the dim's denominator."""
    cases = [
        _case_summary_with(
            [
                DimensionScores(accuracy=DimensionScore(1.0, "")),
                DimensionScores(accuracy=DimensionScore(0.0, "")),
            ]
        ),
        _case_summary_with([DimensionScores(accuracy=DimensionScore(1.0, ""))]),
    ]
    means = _compute_dimensional_means(cases)  # type: ignore[arg-type]
    # (1.0 + 0.0 + 1.0) / 3 = 0.667
    assert means.accuracy == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# load_dataset parsing of new optional fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_dataset_parses_grounding(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}, '
        '"grounding": "Paris is in France"}\n'
    )
    bundle = load_agent(agent_dir)
    cases, _ = load_dataset(bundle)
    assert cases[0].grounding == "Paris is in France"


@pytest.mark.unit
def test_load_dataset_parses_expected_coverage(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}, '
        '"expected_coverage": ["price", "warranty"]}\n'
    )
    bundle = load_agent(agent_dir)
    cases, _ = load_dataset(bundle)
    assert cases[0].expected_coverage == ["price", "warranty"]


@pytest.mark.unit
def test_load_dataset_parses_latency_budget_ms(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}, "latency_budget_ms": 2500}\n'
    )
    bundle = load_agent(agent_dir)
    cases, _ = load_dataset(bundle)
    assert cases[0].latency_budget_ms == 2500


@pytest.mark.unit
def test_load_dataset_dims_default_to_none(tmp_path: Path) -> None:
    """Legacy rows without the new fields keep all three optionals at None."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)
    cases, _ = load_dataset(bundle)
    assert cases[0].grounding is None
    assert cases[0].expected_coverage is None
    assert cases[0].latency_budget_ms is None


@pytest.mark.unit
def test_load_dataset_rejects_non_string_grounding(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {}, "grounding": 123}\n'
    )
    bundle = load_agent(agent_dir)
    with pytest.raises(EvalConfigError, match="grounding must be a string"):
        load_dataset(bundle)


@pytest.mark.unit
def test_load_dataset_rejects_non_list_expected_coverage(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {}, "expected_coverage": "not-a-list"}\n'
    )
    bundle = load_agent(agent_dir)
    with pytest.raises(EvalConfigError, match="expected_coverage must be a list"):
        load_dataset(bundle)


@pytest.mark.unit
def test_load_dataset_rejects_non_int_latency_budget(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {}, "latency_budget_ms": "fast"}\n'
    )
    bundle = load_agent(agent_dir)
    with pytest.raises(EvalConfigError, match="latency_budget_ms must be an int"):
        load_dataset(bundle)


# ---------------------------------------------------------------------------
# Engine end-to-end: dimensions populated correctly
# ---------------------------------------------------------------------------


class _FaithfulnessStubProvider(BaseLLMProvider):
    """Provider that routes by prompt content:

    * Faithfulness judge prompt → fixed faithfulness_score
    * Accuracy LLM-judge prompt (``Rubric:``) → fixed accuracy_score
    * Agent prompt → fixed agent_response

    Lets tests exercise the engine's LLM-judge accuracy path AND its
    faithfulness path with separate, predictable scores so we can
    assert each dimension lands where it should.
    """

    name = "faithfulness_stub"
    version = "0.0.1"

    def __init__(
        self,
        *,
        agent_response: str,
        faithfulness_score: float,
        accuracy_score: float = 1.0,
    ) -> None:
        self._agent_response = agent_response
        self._faithfulness_score = faithfulness_score
        self._accuracy_score = accuracy_score
        self.calls: list[str] = []
        self.faithfulness_prompts: list[str] = []
        self.accuracy_prompts: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request.provider)
        body = request.messages[0].content if request.messages else ""
        if "FAITHFULNESS" in body:
            self.faithfulness_prompts.append(body)
            return CompletionResponse(
                text=f'{{"score": {self._faithfulness_score}, "rationale": "stub-faith"}}',
            )
        if "Rubric:" in body:
            self.accuracy_prompts.append(body)
            return CompletionResponse(
                text=f'{{"score": {self._accuracy_score}, "rationale": "stub-acc"}}',
            )
        if "specialist evaluator" in body:
            # v0.8 specialist scorers (safety, completeness, tool-usage, ux-tone)
            return CompletionResponse(
                text='{"score": 1.0, "rationale": "stub-specialist"}',
            )
        if "CONTEXT COMPLIANCE" in body:
            # context_compliance scorer — triggered when bundle.contexts is set.
            return CompletionResponse(
                text='{"score": 1.0, "rationale": "stub-ctx-compliance"}',
            )
        return CompletionResponse(text=self._agent_response)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        resp = await self.complete(request)
        yield StreamChunk(text=resp.text)
        yield StreamChunk(text="", tokens=resp.tokens)

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.unit
async def test_engine_legacy_dataset_only_scores_accuracy_and_latency(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Without grounding/coverage fields, faithfulness + coverage stay None."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)

    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    dims = summary.cases[0].runs[0].dimensions
    assert dims.accuracy.value == pytest.approx(1.0)
    assert dims.faithfulness.value is None
    assert dims.coverage.value is None
    assert dims.latency.value is not None  # always scored on success

    # DimensionalMeans rollup: faithfulness/coverage absent from the eval.
    assert summary.dimensional_means.accuracy == pytest.approx(1.0)
    assert summary.dimensional_means.faithfulness is None
    assert summary.dimensional_means.coverage is None
    assert summary.dimensional_means.latency is not None


@pytest.mark.unit
async def test_engine_scores_coverage_when_expected_coverage_set(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "price warranty"}, '
        '"expected_coverage": ["price", "warranty", "shipping"]}\n'
    )
    bundle = load_agent(agent_dir)

    # Agent answers two of three topics; coverage should be 2/3.
    provider = MockProvider(response='{"message": "price warranty"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    coverage = summary.cases[0].runs[0].dimensions.coverage
    assert coverage.value == pytest.approx(2 / 3)
    assert "shipping" in coverage.rationale
    assert summary.dimensional_means.coverage == pytest.approx(2 / 3)


@pytest.mark.unit
async def test_engine_scores_faithfulness_via_llm_judge(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """When grounding is set, the engine calls the judge with the FAITHFULNESS prompt."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}, '
        '"grounding": "Paris is in France"}\n'
    )
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n  provider: anthropic/claude-sonnet-4-6\n"
        "rubric: 'be strict'\n"
        "threshold: 0.7\n"
    )
    bundle = load_agent(agent_dir)

    provider = _FaithfulnessStubProvider(agent_response='{"message": "ok"}', faithfulness_score=0.9)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    dims = summary.cases[0].runs[0].dimensions
    assert dims.faithfulness.value == pytest.approx(0.9)
    # The stub recorded a faithfulness-flavored prompt.
    assert provider.faithfulness_prompts, "FAITHFULNESS judge prompt never sent"
    assert "Paris is in France" in provider.faithfulness_prompts[0]
    assert summary.dimensional_means.faithfulness == pytest.approx(0.9)


@pytest.mark.unit
async def test_engine_back_compat_gate_uses_accuracy_only(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """CaseRun.score must equal accuracy alone — even when other dims score lower.

    Critical back-compat with v0.5: ``--gate 0.7`` means "70% accuracy
    across cases". Other dims are reporting-only.
    """
    agent_dir = _scaffold(tmp_path / "demo")
    # Agent will exact-match the expected (accuracy=1.0) but cover only
    # 0/2 topics (coverage=0.0). The aggregate (mean) would be 0.5.
    # The gate input (CaseRun.score) must stay 1.0.
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "answer"}, '
        '"expected_coverage": ["totally-absent-topic-a", "totally-absent-b"]}\n'
    )
    bundle = load_agent(agent_dir)

    provider = MockProvider(response='{"message": "answer"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    run = summary.cases[0].runs[0]
    # Accuracy is perfect.
    assert run.dimensions.accuracy.value == pytest.approx(1.0)
    # Coverage is zero (neither topic present).
    assert run.dimensions.coverage.value == pytest.approx(0.0)
    # Gate input stays on accuracy — the multi-dim aggregate is NOT what
    # CaseRun.score reports.
    assert run.score == pytest.approx(1.0)
    # And the case passes under the default exact-match threshold (1.0).
    assert summary.cases[0].passed


@pytest.mark.unit
async def test_engine_failed_run_populates_zeros_with_reason(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """A provider failure → DimensionScores carries accuracy=0 + reason, others unscored."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)

    # MockProvider returning a valid JSON object that fails the agent's
    # output schema (missing the required ``message`` field) triggers
    # a validation failure; accuracy lands at 0 with a rationale.
    provider = MockProvider(response='{"wrong_field": "x"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    run = summary.cases[0].runs[0]
    assert run.dimensions.accuracy.value == pytest.approx(0.0)
    assert run.dimensions.accuracy.rationale  # non-empty reason
    # The other dims weren't computed (we don't score them on failure).
    assert run.dimensions.faithfulness.value is None
    assert run.dimensions.coverage.value is None
    assert run.dimensions.latency.value is None


# ---------------------------------------------------------------------------
# EvalSummary surfaces DimensionalMeans
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_summary_carries_dimensional_means(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """``EvalSummary.dimensional_means`` is populated on every run."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}, "expected_coverage": ["ok"]}\n'
    )
    bundle = load_agent(agent_dir)

    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    assert isinstance(summary.dimensional_means, DimensionalMeans)
    assert summary.dimensional_means.accuracy is not None
    assert summary.dimensional_means.coverage is not None
    # Latency is always scored on a successful run.
    assert summary.dimensional_means.latency is not None
    # No grounding in dataset → faithfulness stays None.
    assert summary.dimensional_means.faithfulness is None


# ---------------------------------------------------------------------------
# CLI JSON output — dimensional_means visible end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_eval_json_includes_dimensional_means(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``movate eval --output json`` exposes the dim rollup + per-run dims.

    End-to-end: scaffold an agent, opt-in to expected_coverage on a
    dataset case, run with --mock, and assert the JSON payload's
    ``dimensional_means`` block + per-run ``dimensions_per_run`` block.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "price warranty"}')
    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(app, ["init", "dim-agent", "-t", "default", "--target", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    agent_dir = tmp_path / "dim-agent"

    # Replace dataset with one case that opts in to coverage.
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "price warranty"}, '
        '"expected_coverage": ["price", "warranty"]}\n'
    )

    result = runner.invoke(app, ["eval", str(agent_dir), "--mock", "--gate", "0.0", "-o", "json"])
    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)

    # Headline rollup.
    assert "dimensional_means" in payload
    dm = payload["dimensional_means"]
    assert dm["accuracy"] == pytest.approx(1.0)
    assert dm["coverage"] == pytest.approx(1.0)
    assert dm["latency"] is not None
    assert dm["faithfulness"] is None  # not opted-in

    # Per-run dims attached to each case.
    case_0 = payload["cases"][0]
    assert "dimensions_per_run" in case_0
    run_0_dims = case_0["dimensions_per_run"][0]
    assert run_0_dims["accuracy"]["value"] == pytest.approx(1.0)
    assert run_0_dims["coverage"]["value"] == pytest.approx(1.0)
    assert run_0_dims["faithfulness"]["value"] is None


# ---------------------------------------------------------------------------
# --gate-context-compliance wired through _check_dimensional_gates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gate_context_compliance_passes_when_above_threshold() -> None:
    from movate.cli.eval import _check_dimensional_gates  # noqa: PLC0415

    means = DimensionalMeans(context_compliance=0.9)
    failed = _check_dimensional_gates(
        means,
        gate_faithfulness=None,
        gate_coverage=None,
        gate_latency=None,
        gate_context_compliance=0.8,
    )
    assert not failed


@pytest.mark.unit
def test_gate_context_compliance_fails_when_below_threshold() -> None:
    from movate.cli.eval import _check_dimensional_gates  # noqa: PLC0415

    means = DimensionalMeans(context_compliance=0.5)
    failed = _check_dimensional_gates(
        means,
        gate_faithfulness=None,
        gate_coverage=None,
        gate_latency=None,
        gate_context_compliance=0.8,
    )
    assert failed


@pytest.mark.unit
def test_gate_context_compliance_skipped_when_not_scored() -> None:
    from movate.cli.eval import _check_dimensional_gates  # noqa: PLC0415

    means = DimensionalMeans(context_compliance=None)
    failed = _check_dimensional_gates(
        means,
        gate_faithfulness=None,
        gate_coverage=None,
        gate_latency=None,
        gate_context_compliance=0.8,
    )
    assert not failed  # skipped, not failed


# ---------------------------------------------------------------------------
# PR-DD: contexts/ files as default faithfulness grounding
# ---------------------------------------------------------------------------


def _scaffold_with_contexts(dst: Path, ctx_name: str, ctx_body: str) -> Path:
    """Scaffold a minimal agent that declares one context file."""
    agent_dir = _scaffold(dst)
    project_root = dst.parent

    # Declare the context in agent.yaml (appended; loader merges with spec).
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text() + f"\ncontexts:\n  - {ctx_name}\n")

    # Write the context body at project_root/contexts/<name>.md
    ctx_dir = project_root / "contexts"
    ctx_dir.mkdir(exist_ok=True)
    (ctx_dir / f"{ctx_name}.md").write_text(ctx_body)

    return agent_dir


@pytest.mark.unit
async def test_faithfulness_uses_contexts_when_no_grounding_in_case(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """When dataset case has no grounding, bundle.contexts feeds faithfulness."""
    ctx_body = "Refunds are processed within 5 business days."
    agent_dir = _scaffold_with_contexts(
        tmp_path / "demo", ctx_name="refund-policy", ctx_body=ctx_body
    )

    (agent_dir / "evals" / "dataset.jsonl").write_text(
        # No "grounding" field — faithfulness should still be scored via contexts.
        '{"input": {"text": "how long for refund?"}, "expected": {"message": "5 days"}}\n'
    )
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n  provider: anthropic/claude-haiku-4-5-20251001\n"
        "rubric: 'be strict'\n"
        "threshold: 0.7\n"
    )
    bundle = load_agent(agent_dir)

    provider = _FaithfulnessStubProvider(
        agent_response='{"message": "5 days"}', faithfulness_score=0.95
    )
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    dims = summary.cases[0].runs[0].dimensions

    # Faithfulness must be scored — contexts/ injected as grounding.
    assert dims.faithfulness.value == pytest.approx(0.95)
    assert provider.faithfulness_prompts, "Faithfulness judge never called"
    # The contexts body should appear in the prompt sent to the judge.
    assert ctx_body in provider.faithfulness_prompts[0]


@pytest.mark.unit
async def test_explicit_grounding_takes_priority_over_contexts(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """case.grounding wins over bundle.contexts when both are present."""
    ctx_body = "Context file content — should NOT appear in the judge prompt."
    explicit_grounding = "Explicit grounding string from the dataset."
    agent_dir = _scaffold_with_contexts(tmp_path / "demo", ctx_name="policy", ctx_body=ctx_body)

    (agent_dir / "evals" / "dataset.jsonl").write_text(
        f'{{"input": {{"text": "q"}}, "expected": {{"message": "a"}}, '
        f'"grounding": "{explicit_grounding}"}}\n'
    )
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n  provider: anthropic/claude-haiku-4-5-20251001\n"
        "rubric: 'be strict'\n"
        "threshold: 0.7\n"
    )
    bundle = load_agent(agent_dir)

    provider = _FaithfulnessStubProvider(agent_response='{"message": "a"}', faithfulness_score=0.8)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    await engine.run(bundle)

    assert provider.faithfulness_prompts, "Faithfulness judge never called"
    prompt = provider.faithfulness_prompts[0]
    assert explicit_grounding in prompt
    assert ctx_body not in prompt  # contexts/ not injected when grounding is explicit


@pytest.mark.unit
async def test_faithfulness_not_scored_without_grounding_or_contexts(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Neither case.grounding nor bundle.contexts → faithfulness stays None (back-compat)."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
        # No grounding; agent has no contexts/ files.
    )
    bundle = load_agent(agent_dir)

    provider = _FaithfulnessStubProvider(agent_response='{"message": "ok"}', faithfulness_score=0.9)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    dims = summary.cases[0].runs[0].dimensions

    assert dims.faithfulness.value is None
    assert not provider.faithfulness_prompts, "Faithfulness judge should NOT have been called"
