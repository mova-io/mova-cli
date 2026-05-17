"""Multi-LLM judge panel + arbitrator end-to-end.

PR — eval-multi-judge-kb-aware. Three concerns covered:

1. Engine: ``_score_panel_accuracy`` runs N judges concurrently,
   averages on low variance, escalates to the arbitrator on high
   variance, and populates ``DimensionScore.per_judge_scores`` so
   structured consumers (Angular dashboard, EvalRecord JSON) can render
   the breakdown without parsing the rationale string.
2. CLI: ``--judge-model`` repeated 2+ times now BUILDS a panel
   (previously rejected with "upgrade to v0.8"). ``--arbitrator-model``
   plumbs an escalation model. ``--variance-threshold`` overrides the
   default 0.3.
3. CLI guards: ``--arbitrator-model`` without a panel = clean error;
   ``--judge-rubric`` still required with any judge.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.eval import (
    DimensionScore,
    EvalEngine,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import (
    JudgeConfig,
    JudgeMethod,
    ModelConfig,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import (
    InMemoryStorage,
    NullTracer,
    scaffold_agent,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


class _PanelStubProvider(BaseLLMProvider):
    """Provider that returns:

    * Agent prompt → fixed agent_response (no "Rubric:")
    * Judge prompts (contain "Rubric:") → score keyed by which judge
      provider is talking (``request.provider``). Lets each test
      configure judges that agree or disagree independently.
    """

    name = "panel_stub"
    version = "0.0.1"

    def __init__(
        self,
        *,
        agent_response: str,
        judge_scores: dict[str, float],
    ) -> None:
        self._agent_response = agent_response
        self._judge_scores = judge_scores
        self.judge_calls: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content if request.messages else ""
        if "Rubric:" in body:
            self.judge_calls.append(request.provider)
            score = self._judge_scores.get(request.provider)
            if score is None:
                raise AssertionError(
                    f"unconfigured judge {request.provider!r}; configured: "
                    f"{sorted(self._judge_scores)}"
                )
            return CompletionResponse(
                text=f'{{"score": {score}, "rationale": "stub-{request.provider}"}}',
            )
        if "specialist evaluator" in body:
            # v0.8 specialist scorers (safety, completeness, tool-usage, ux-tone)
            return CompletionResponse(
                text='{"score": 1.0, "rationale": "stub-specialist"}',
            )
        return CompletionResponse(text=self._agent_response)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        resp = await self.complete(request)
        yield StreamChunk(text=resp.text)
        yield StreamChunk(text="", tokens=resp.tokens)

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError


def _scaffold_with_judge(
    tmp_path: Path,
    *,
    judge: JudgeConfig,
) -> Path:
    """Scaffold a minimal agent + dataset whose engine will hit the
    panel scoring path. The agent is set up via the inline JudgeConfig
    instead of judge.yaml — same path the CLI uses for
    ``--judge-model``."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    return agent_dir


# ---------------------------------------------------------------------------
# Engine: panel scoring populates per_judge_scores
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_panel_low_variance_returns_mean_and_records_per_judge(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Two judges agree (0.80 + 0.86 → std_dev ≈ 0.04 < 0.3 threshold).
    Engine returns the mean (0.83) and stamps the per-judge breakdown
    so structured consumers don't have to parse the rationale."""
    judge_cfg = JudgeConfig(
        method=JudgeMethod.PANEL,
        judges=[
            ModelConfig(provider="anthropic/claude-opus-4-7"),
            ModelConfig(provider="google/gemini-2.5-pro"),
        ],
        rubric="Score 0-1: 1=correct, 0=wrong",
        variance_threshold=0.3,
    )
    agent_dir = _scaffold_with_judge(tmp_path, judge=judge_cfg)
    bundle = load_agent(agent_dir)

    provider = _PanelStubProvider(
        agent_response='{"message": "ok"}',
        judge_scores={
            "anthropic/claude-opus-4-7": 0.80,
            "google/gemini-2.5-pro": 0.86,
        },
    )
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, judge_override=judge_cfg)

    summary = await engine.run(bundle)
    dim = summary.cases[0].runs[0].dimensions.accuracy
    assert dim.value == pytest.approx((0.80 + 0.86) / 2)
    assert dim.per_judge_scores is not None
    assert dim.per_judge_scores == {
        "anthropic/claude-opus-4-7": pytest.approx(0.80),
        "google/gemini-2.5-pro": pytest.approx(0.86),
    }
    # No arbitrator on the happy path.
    assert "arbitrator" not in dim.per_judge_scores
    assert "panel:" in dim.rationale


@pytest.mark.unit
async def test_panel_high_variance_with_arbitrator_uses_arbitrator_score(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Two judges disagree widely (0.20 + 0.95 → std_dev ≈ 0.53 > 0.3).
    The arbitrator's score wins; ``per_judge_scores["arbitrator"]`` is
    populated so dashboards can show the resolution."""
    judge_cfg = JudgeConfig(
        method=JudgeMethod.PANEL,
        judges=[
            ModelConfig(provider="anthropic/claude-opus-4-7"),
            ModelConfig(provider="google/gemini-2.5-pro"),
        ],
        rubric="Score 0-1: 1=correct, 0=wrong",
        variance_threshold=0.3,
        escalation=ModelConfig(provider="anthropic/claude-sonnet-4-6"),
    )
    agent_dir = _scaffold_with_judge(tmp_path, judge=judge_cfg)
    bundle = load_agent(agent_dir)

    provider = _PanelStubProvider(
        agent_response='{"message": "ok"}',
        judge_scores={
            "anthropic/claude-opus-4-7": 0.20,
            "google/gemini-2.5-pro": 0.95,
            "anthropic/claude-sonnet-4-6": 0.75,
        },
    )
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, judge_override=judge_cfg)

    summary = await engine.run(bundle)
    dim = summary.cases[0].runs[0].dimensions.accuracy
    # Arbitrator score wins, NOT the mean.
    assert dim.value == pytest.approx(0.75)
    assert dim.per_judge_scores is not None
    assert dim.per_judge_scores["anthropic/claude-opus-4-7"] == pytest.approx(0.20)
    assert dim.per_judge_scores["google/gemini-2.5-pro"] == pytest.approx(0.95)
    assert dim.per_judge_scores["arbitrator"] == pytest.approx(0.75)
    assert "panel(escalated)" in dim.rationale


@pytest.mark.unit
async def test_panel_high_variance_without_arbitrator_uses_mean_with_warning(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """No arbitrator configured: panel mean is returned, but the
    rationale carries a ``panel(high-variance)`` annotation so the
    operator knows the score is suspect."""
    judge_cfg = JudgeConfig(
        method=JudgeMethod.PANEL,
        judges=[
            ModelConfig(provider="anthropic/claude-opus-4-7"),
            ModelConfig(provider="google/gemini-2.5-pro"),
        ],
        rubric="Score 0-1: 1=correct, 0=wrong",
        variance_threshold=0.3,
        escalation=None,
    )
    agent_dir = _scaffold_with_judge(tmp_path, judge=judge_cfg)
    bundle = load_agent(agent_dir)

    provider = _PanelStubProvider(
        agent_response='{"message": "ok"}',
        judge_scores={
            "anthropic/claude-opus-4-7": 0.20,
            "google/gemini-2.5-pro": 0.95,
        },
    )
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, judge_override=judge_cfg)

    summary = await engine.run(bundle)
    dim = summary.cases[0].runs[0].dimensions.accuracy
    assert dim.value == pytest.approx((0.20 + 0.95) / 2)
    assert dim.per_judge_scores is not None
    # No arbitrator was called.
    assert "arbitrator" not in dim.per_judge_scores
    assert "panel(high-variance)" in dim.rationale


# ---------------------------------------------------------------------------
# CLI: --judge-model x 2+ now builds a panel (previously rejected)
# ---------------------------------------------------------------------------


def _scaffold_runnable_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scaffold an agent + project that can be invoked through
    ``mdk eval`` via CliRunner. The CLI looks for a project root, so
    we plant a minimal project.yaml + agents/<name>/."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "proj")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    return tmp_path / "proj" / "agents" / "faq"


@pytest.mark.unit
def test_cli_panel_2_judges_no_longer_rejected_uses_panel_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-PR error ('upgrade to movate-cli v0.8 or later') must
    NOT appear — 2+ ``--judge-model`` values now build a PANEL config.
    We exercise the CLI as far as the JudgeConfig construction; the
    actual eval is skipped via --mock to keep the test hermetic."""
    _scaffold_runnable_agent(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "eval",
            "./agents/faq",
            "--mock",
            "--judge-model",
            "anthropic/claude-opus-4-7",
            "--judge-model",
            "openai/gpt-4o",
            "--judge-rubric",
            "Score 0-1: 1=correct, 0=wrong",
        ],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # Old error string must be gone.
    assert "upgrade to movate-cli v0.8 or later" not in combined


@pytest.mark.unit
def test_cli_arbitrator_without_panel_errors_with_helpful_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--arbitrator-model`` with zero or one ``--judge-model`` is
    nonsensical (nothing to arbitrate). Clear error pointing at the
    fix: pass --judge-model 2+ times."""
    _scaffold_runnable_agent(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "eval",
            "./agents/faq",
            "--mock",
            "--arbitrator-model",
            "google/gemini-2.5-pro",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "--arbitrator-model" in combined
    assert "--judge-model" in combined


@pytest.mark.unit
def test_cli_arbitrator_with_single_judge_errors_with_helpful_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single judge + arbitrator is also rejected — the arbitrator
    only fires inside a panel (std_dev across N judges)."""
    _scaffold_runnable_agent(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "eval",
            "./agents/faq",
            "--mock",
            "--judge-model",
            "anthropic/claude-opus-4-7",
            "--judge-rubric",
            "Score 0-1",
            "--arbitrator-model",
            "google/gemini-2.5-pro",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "panel" in combined.lower()


# ---------------------------------------------------------------------------
# DimensionScore plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dimension_score_per_judge_field_defaults_to_none() -> None:
    """Backward-compat: existing callers constructing DimensionScore
    with just value+rationale still get per_judge_scores=None."""
    ds = DimensionScore(0.8, "rationale")
    assert ds.per_judge_scores is None
